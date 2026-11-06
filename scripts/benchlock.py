"""bench 单例锁 —— 防止多个 bench 脚本并行产生的孤儿 ffmpeg 污染测量。

## 为什么需要它

实测（2026-11-07）：三个 agent 各自跑 bench 时，会留下**孤儿 ffmpeg 进程**
（父 Python 被杀、ffmpeg 继续按 395% CPU 抓帧 5 分钟以上），导致：
  - load average 冲到 20+
  - 别人的 bench 耗时从 133s 变成 317s（**2.4 倍**）
  - decode 路径时好时坏（videotoolbox / software 随机）

这是**典型的虚假测量**：数字变了，但代码没变。

## 用法

```python
from benchlock import bench_lock
with bench_lock('m2'):
    ...跑测量...
```

第二个进程会立即拿到 `RuntimeError: 另一个 bench 正在跑`，而不是静默排队。
"""
from __future__ import annotations

import contextlib
import os
import pathlib
import sys
import time

LOCK = pathlib.Path('/tmp/kf-bench.lock')
STALE_SEC = 900          # 15 分钟没更新就认为持有者死了


def _holder() -> dict | None:
    try:
        txt = LOCK.read_text()
        parts = dict(kv.split('=', 1) for kv in txt.split() if '=' in kv)
        parts['age'] = time.time() - float(parts.get('at', 0))
        return parts
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def bench_lock(name: str, force: bool = False):
    h = _holder()
    if h and h['age'] < STALE_SEC and not force:
        print(f'FAIL: 另一个 bench 正在跑（{h.get("name")}，{h["age"]:.0f}s 前）'
              f'\n  等它结束，或确认它是孤儿后 rm {LOCK}', file=sys.stderr)
        raise RuntimeError('bench 已在运行')
    if h and h['age'] >= STALE_SEC:
        print(f'⚠ 清掉过期锁（{h.get("name")}，{h["age"]:.0f}s）', file=sys.stderr)
    LOCK.write_text(f'name={name} pid={os.getpid()} at={time.time()}\n')
    try:
        yield
    finally:
        try:
            if LOCK.exists() and f'pid={os.getpid()}' in LOCK.read_text():
                LOCK.unlink()
        except OSError:
            pass


def kill_orphans(dry=True) -> list[str]:
    """扫出「父进程已死」的 ffmpeg（孤儿），返回命令行列表。"""
    import subprocess
    out = []
    r = subprocess.run(['ps', '-eo', 'pid,ppid,command'], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if 'ffmpeg' not in line or 'kf-lab' not in line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid = parts[0], parts[1]
        # 父进程是否还活着？
        try:
            os.kill(int(ppid), 0)
            continue        # 父活着 = 不是孤儿
        except OSError:
            out.append(f'{pid} (ppid={ppid}) {" ".join(parts[2].split()[:5])}')
            if not dry:
                try:
                    os.kill(int(pid), 9)
                except OSError:
                    pass
    return out


if __name__ == '__main__':
    dry = '--kill' not in sys.argv
    found = kill_orphans(dry=dry)
    print(f'孤儿 ffmpeg: {len(found)} 个')
    for f in found:
        print('  ', f)
    if found and dry:
        print('\n用 --kill 清理')
