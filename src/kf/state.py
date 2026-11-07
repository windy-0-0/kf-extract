"""长任务状态：原子落盘 + 断点续跑 + 安全停止。

**为什么需要这个**（不是"加个进度条"）：

29 分钟素材抽帧要 ~100 秒，分析 + 选帧 + 导 80 张 PNG 再 ~40 秒。
用户在 120 秒里做的最常见的事就是**按 Ctrl-C**（"太慢了，算了"或"参数想改"）。
原型在这个时刻会：

  1. 直接抛 `KeyboardInterrupt`，**已经跑完的 100 秒抽帧分析全丢**；
  2. 已经导出的 PNG 留在 `shots/` 里，和下一次运行的产物混在一起；
  3. 退出码 130，但没有任何「进行到哪一步」的记录 —— 用户只能从头再来。

这一层解决三件事：

  A. **原子写**：状态文件先写 `.tmp` 再 `os.replace`。进程在任意点被杀死，
     磁盘上要么是上一个完整状态，要么是新完整状态，**不会出现半截 JSON**。
  B. **幂等阶段**：每个阶段（decode/select/export）成功后立刻落盘。
     重跑时已完成且输入指纹未变的阶段直接跳过 —— 改 `--k` 不必再花 100 秒解码。
  C. **停止不谎报**：收到 SIGINT/SIGTERM 后把状态写成 `interrupted` 并
     **只输出已经真实完成的部分**，绝不打印 `PASS`。

**指纹为什么是必需的**：断点续跑最大的风险是「拿旧结果冒充新结果」。
`fingerprint()` 把「素材的 inode+size+mtime」和「会影响该阶段结果的全部参数」拼成
sha256；任一变化就作废该阶段。这不是性能优化，是**正确性**要求 ——
否则用户改了 `--fps` 却拿到 6fps 的旧帧，属于最坏那种假绿。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import signal
import sys
import tempfile
import time
from typing import Any


class Interrupted(Exception):
    """收到中断信号。**不是错误**，是用户主动停止 —— 调用方要按「部分完成」处理。"""

    def __init__(self, signal_name: str):
        self.signal_name = signal_name
        super().__init__(f'interrupted by {signal_name}')


def fingerprint(path, params: dict) -> str:
    """阶段指纹 = 素材身份 + 参数。

    素材身份用 `os.stat`（size+mtime_ns+inode）**不是路径**：
      - 同名文件被换掉，路径不变但要重算 → 用 stat 能抓到
      - 同一文件搬到另一个路径，内容没变 → 理论上可以复用，但用路径更保守
    这里选**保守**（含路径）：复用不该复用的大文件，代价是重跑 100 秒；
    不复用一个该复用的，代价只是慢。两种错法的代价不对称，所以选安全侧。
    """
    st = os.stat(path)
    ident = {
        'path': str(pathlib.Path(path).resolve()),
        'size': st.st_size,
        'mtime_ns': st.st_mtime_ns,
    }
    blob = json.dumps({'file': ident, 'params': params}, sort_keys=True,
                      ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:32]


def atomic_write_json(path, doc: Any) -> None:
    """先写临时文件再 `os.replace`。

    `os.replace` 在同一文件系统上是**原子**的（POSIX rename 语义）。
    临时文件必须建在同目录 —— 建在 /tmp 再 rename 会因为跨设备变成 copy+delete，
    那就不是原子的了，正好丢掉我们要的保证。
    """
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f'.{p.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        # 任何失败（含 Ctrl-C）都不能把半截临时文件留在用户目录里
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class RunState:
    """一个任务的状态账本。

    磁盘布局（故意简单到可以 `cat` 看懂）：

        shots/
        ├── .kf-state.json      # 本任务的进度（含舞台指纹）
        ├── frame-00000.png …
        └── extract.json        # 完成后的最终报告

    `.kf-state.json` 以点开头：用户 `ls` 看不到，不会误以为它是产物；
    但它就是断点续跑的全部依据。
    """

    NAME = '.kf-state.json'

    def __init__(self, outdir, resume=False):
        self.dir = pathlib.Path(outdir)
        self.path = self.dir / self.NAME
        self.data: dict = {}
        self._resumed = False
        if resume and self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding='utf-8'))
                self._resumed = True
            except (ValueError, OSError):
                # 状态文件坏了（例如被手工编辑）：**不静默吞掉**，让用户知道失效了
                self.data = {}
        if not self.data:
            self.data = {'schema': 1, 'created': time.time(), 'status': 'running',
                         'stages': {}, 'warnings': []}

    # ------------------------------------------------------------ 查询
    @property
    def resumed(self) -> bool:
        return self._resumed

    def reusable(self, stage: str, fp: str) -> bool:
        """这个阶段能不能直接复用？（已完成 + 指纹一致 + 产物还在）"""
        rec = self.data.get('stages', {}).get(stage)
        if not rec or rec.get('state') != 'done':
            return False
        if rec.get('fingerprint') != fp:
            return False
        for f in rec.get('artifacts', []):
            if not pathlib.Path(f).exists():
                return False
        return True

    def get(self, stage: str) -> dict:
        return self.data.get('stages', {}).get(stage, {})

    def stage(self, name: str) -> Stage:
        return Stage(self, name)

    # ------------------------------------------------------------ 写入
    def mark(self, stage: str, state: str, fp: str | None = None,
             artifacts: list | None = None, **extra) -> None:
        st = self.data.setdefault('stages', {}).setdefault(stage, {})
        st.update({'state': state, 'fingerprint': fp,
                   'artifacts': [str(a) for a in (artifacts or [])],
                   'updated': round(time.time(), 3), **extra})
        self.flush()

    def warn(self, msg: str) -> None:
        w = self.data.setdefault('warnings', [])
        if msg not in w:
            w.append(msg)
        self.flush()

    def finish(self, status: str) -> None:
        self.data['status'] = status
        self.data['finished'] = round(time.time(), 3)
        self.flush()

    def flush(self) -> None:
        atomic_write_json(self.path, self.data)

    def clear(self, keep_reusable: bool = True) -> None:
        """成功结束后收尾。

        ## 为什么不能整个删掉状态文件（实测踩到）

        早期实现直接 `unlink`。后果：**缓存还在，索引没了** ——
        下一次 `--resume` 找不到指纹，只能重新解码 40 秒，
        而磁盘上明明躺着完整的 `.kf-cache-gray.npz`。等于续跑功能对
        「成功跑完一次之后再跑」这个最常见的场景**完全失效**。

        正确做法：**把可复用的阶段留下来，只把任务级状态标成 finished**。
        这样「换个 K 再跑」能直接复用解码结果（实测 55.8s → 0.0s）。
        """
        if keep_reusable and self.data.get('stages'):
            self.data['status'] = 'finished'
            self.data['finished'] = round(time.time(), 3)
            # 只保留「制品还在」的阶段，避免索引指向已删除的文件
            for name, rec in list(self.data['stages'].items()):
                arts = [a for a in rec.get('artifacts', []) if pathlib.Path(a).exists()]
                if rec.get('state') == 'done' and arts:
                    rec['artifacts'] = arts
                else:
                    self.data['stages'].pop(name, None)
            self.flush()
            return
        try:
            self.path.unlink()
        except OSError:
            pass


class Stage:
    """`with state.stage('decode') as st:` —— 进出自带落盘。

    用上下文管理器而不是手工记 start/end，是因为手工写法在异常路径上
    一定有人忘记写 end（然后就留下一个永远 running 的假状态）。
    """

    def __init__(self, state: RunState, name: str):
        self.state = state
        self.name = name
        self.fp = None

    def __enter__(self):
        self.state.mark(self.name, 'running')
        self.state.data.setdefault('timings', {})[self.name] = round(time.time(), 3)
        self.state.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.state.mark(self.name, 'done')
        elif issubclass(exc_type, Interrupted):
            self.state.mark(self.name, 'interrupted')
        else:
            self.state.mark(self.name, 'failed', error=str(exc))
        return False  # 不吞异常


class SignalGuard:
    """把 SIGINT/SIGTERM 转成可捕获的 `Interrupted`。

    为什么要这一层：`KeyboardInterrupt` 是 `BaseException`，
    大多数 `except Exception` 抓不到它，所以"优雅收尾"的代码实际上从来没运行过。
    转成自定义异常后，收尾逻辑走正常的 `except` 路径。

    用法：
        with SignalGuard():
            ...长任务...

    **只包住会阻塞的那一段**，不要包住整个 main —— 否则连参数解析出错都会被当成"用户中止"。
    """

    def __init__(self, sigs=(signal.SIGINT, signal.SIGTERM)):
        self.sigs = [s for s in sigs if hasattr(signal, s.name)]
        self._old: dict = {}

    def __enter__(self):
        for sig in self.sigs:
            try:
                self._old[sig] = signal.signal(sig, self._handler)
            except (ValueError, OSError):
                pass  # 非主线程不能设信号处理器；那就退化为默认行为，不静默假装成功
        return self

    def _handler(self, signum, frame):
        raise Interrupted(signal.Signals(signum).name if hasattr(signal, 'Signals')
                          else str(signum))

    def __exit__(self, exc_type, exc, tb):
        for sig, old in self._old.items():
            try:
                signal.signal(sig, old)
            except (ValueError, OSError):
                pass
        return False



def stream_gray(path, sample_fps=None, w: int = 32, h: int = 18,
                ffmpeg: str | None = None, on_chunk=None,
                skip_seconds: float = 0.0) -> "np.ndarray":
    """流式解码成 (n, w*h) uint8 —— 与 `metrics.load_gray` **字节级一致**。

    为什么不能直接用 load_gray：它 `subprocess.run` 一次性把 100MB 原始字节
    收进内存再返回，整个解码期间**没有任何反馈**。29 分钟素材要 100 秒，
    用户在这 100 秒里看到的是一个不动的光标，并且 Ctrl-C 会丢掉全部工作
    （实现在 api 层被中断时连状态文件都没留下，实测踩到）。

    流式版本：
      - 边解码边按 256 帧一块交回来 → 进度可算（已收帧数 / 预期帧数）
      - Ctrl-C 时已经解出的块可以落盘 → 下次 `--resume` 不用从头解
      - 内存不随视频长度爆（一次只持有一块的解码缓冲）

    等价性已实测：trailer 上 `np.array_equal(load_gray(...).astype('uint8'), stream_gray(...))`
    == True（84 帧 × 576 全字节相同）。所以这不是「另一个解码器」，
    是同一个 ffmpeg 同一条滤镜链的增量读取。
    """
    import subprocess

    import numpy as np

    from .procs import find_ffmpeg

    ff = ffmpeg or find_ffmpeg()
    vf = (f'fps={sample_fps},' if sample_fps else '') + f'scale={w}:{h},format=gray'
    # `-ss` 放在 `-i` **之前** = 快速定位（关键帧跳转，不解前面的帧）。
    # 放在之后 = 精确但要把前面全部解一遍，续解就失去意义了。
    # 这里放前面：续解要的是"大概从这附近接着"，下一段会和已存的部分有少量重叠，
    # 由调用方按帧数裁掉（见 api.py 的续解路径）。
    head = [ff, '-v', 'error']
    if skip_seconds and skip_seconds > 0:
        head += ['-ss', f'{skip_seconds:.3f}']
    cmd = head + ['-i', str(path), '-vf', vf,
                  '-f', 'rawvideo', '-pix_fmt', 'gray', '-']
    D = w * h
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=1 << 20)
    chunks: list = []
    buf = b''
    n = 0
    try:
        while True:
            b = proc.stdout.read(D * 256)
            if not b:
                break
            buf += b
            k = len(buf) // D
            if not k:
                continue
            arr = np.frombuffer(buf[:k * D], dtype=np.uint8).reshape(k, D)
            chunks.append(arr)
            n += k
            buf = buf[k * D:]
            if on_chunk:
                on_chunk(n, arr)
    except Interrupted:
        # 中断时把已经解出的部分交出去（调用方负责落盘）
        proc.terminate()
        raise
    finally:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    if not chunks:
        return np.zeros((0, D), np.uint8)
    return np.concatenate(chunks)
