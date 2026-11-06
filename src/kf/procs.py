"""外部程序定位 —— ffmpeg / ffprobe 跨平台查找。

**为什么单独成一个模块**（而不是每个文件里写一个常量）：

原型的每个模块都写着 `FFMPEG = '/opt/homebrew/bin/ffmpeg'`。那是「我这台机器上能跑」，
不是「别人装完能跑」。产品化的第一件事就是把「我这台机器」从代码里赶出去。

解析顺序（找到第一个**可执行且 --version 能跑通**的）：
  1. 环境变量 `KF_FFMPEG` / `KF_FFPROBE`（显式覆盖，永远最高优先）
  2. `PATH` 里的 `ffmpeg` / `ffprobe`
  3. 平台常见安装位（Homebrew ARM/Intel、MacPorts、/usr/local、Windows 常见路径）
  4. `imageio-ffmpeg` 自带的二进制（若装了该包）

**失败必须说人话**：找不到时抛 `FFmpegNotFound`，消息里给出三个平台的确切安装命令，
而不是让用户拿到 `FileNotFoundError: 'ffmpeg'` 去猜。

缓存：解析结果进程内缓存（`_CACHE`），避免一个 30 分钟任务里起 200 次 ffmpeg 就查 200 次 PATH。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading

__all__ = ['FFmpegNotFound', 'find_ffmpeg', 'find_ffprobe', 'require', 'describe']

_CACHE: dict[str, str | None] = {}
_LOCK = threading.Lock()

# 各平台常见安装位。**只放「真的会有人装在那里」的路径**，不做全盘搜索（那会让首次调用卡住）。
_FALLBACKS = {
    'darwin': [
        '/opt/homebrew/bin/{name}',        # Apple Silicon Homebrew
        '/usr/local/bin/{name}',           # Intel Homebrew / 官方静态包
        '/opt/local/bin/{name}',           # MacPorts
        '/Applications/ffmpeg.app/Contents/MacOS/{name}',
    ],
    'linux': [
        '/usr/bin/{name}',
        '/usr/local/bin/{name}',
        '/snap/bin/{name}',
        '/var/lib/flatpak/exports/bin/{name}',
    ],
    'win32': [
        r'C:\ffmpeg\bin\{name}.exe',
        r'C:\Program Files\ffmpeg\bin\{name}.exe',
        r'C:\ProgramData\chocolatey\bin\{name}.exe',
    ],
}

_INSTALL_HINT = """\
找不到 {name}。请先安装 ffmpeg（本项目只需要命令行版，不需要 GUI）：

  macOS      brew install ffmpeg
  Ubuntu/Debian   sudo apt update && sudo apt install -y ffmpeg
  Fedora     sudo dnf install -y ffmpeg
  Windows    winget install --id Gyan.FFmpeg    （或 scoop install ffmpeg）

装完确认：{name} -version

如果它已装但不在 PATH（例如自定义路径），直接指定：

  export KF_{name_upper}={example}

关于 `pip install imageio-ffmpeg`（**只能救一半**，实测确认）：
它自带一个静态 **ffmpeg**，但**不含 ffprobe**。所以：
  - 只缺 ffmpeg  → 装它有救，本项目会自动找到
  - 缺 ffprobe   → 装它没救，仍需系统安装（或把 ffprobe 放到同一目录）
本项目在只有 ffmpeg 的情况下会**降级运行**：时长/帧率改用 ffmpeg 的 stderr
解析，报告里把相关字段标为 `unverified`，绝不假装拿到精确值。
"""


class FFmpegNotFound(RuntimeError):
    """找不到可用的 ffmpeg/ffprobe。

    单独一个异常类型，是为了让调用方（CLI / 库用户）能区分「环境没装」
    与「视频本身有问题」——前者该退出码 3，后者该退出码 4。
    """

    def __init__(self, name: str, tried: list[str], hint: str):
        self.name = name
        self.tried = tried
        self.hint = hint
        super().__init__(f'找不到 {name}（已尝试 {len(tried)} 个位置）')


def _runs(path: str) -> bool:
    """这个二进制真的能执行吗？——不只看存在，还要跑一次 --version。

    只看 `os.path.exists` 是经典的假绿：一个 0 字节的占位文件、一个架构不匹配的
    二进制（x86 装在 ARM 机器上）都存在，但一跑就报错。首次调用多花 ~50ms 换掉这类故障。
    """
    try:
        r = subprocess.run([path, '-version'], capture_output=True, timeout=10,
                           stdin=subprocess.DEVNULL)
        return r.returncode == 0 and (r.stdout or r.stderr)
    except (OSError, subprocess.SubprocessError):
        return False


def _imageio_ffmpeg() -> str | None:
    """imageio-ffmpeg 自带一个静态 ffmpeg（pip 装即可用，无需系统安装）。"""
    try:
        import imageio_ffmpeg

        p = imageio_ffmpeg.get_ffmpeg_exe()
        return p if p and os.path.exists(p) else None
    except Exception:
        return None


def _resolve(name: str) -> str | None:
    env_key = f'KF_{name.upper()}'
    env_val = os.environ.get(env_key)
    if env_val:
        # 显式指定的路径不通过 PATH 解析；但允许用户写裸命令名
        cand = shutil.which(env_val) or env_val
        if os.path.exists(cand):
            if _runs(cand):
                return cand
            # 显式指定却跑不起来：**不静默回退**，否则用户以为自己的配置生效了
            raise FFmpegNotFound(
                name,
                tried=[f'{env_key}={env_val}（存在但无法执行：-version 失败）'],
                hint=_INSTALL_HINT.format(name=name, name_upper=name.upper(),
                                          example='/opt/homebrew/bin/ffmpeg'),
            )
        raise FFmpegNotFound(
            name,
            tried=[f'{env_key}={env_val}（该路径不存在）'],
            hint=_INSTALL_HINT.format(name=name, name_upper=name.upper(),
                                      example='/opt/homebrew/bin/ffmpeg'),
        )

    tried: list[str] = []
    p = shutil.which(name)
    tried.append(f'PATH:{name}')
    if p and _runs(p):
        return p

    plat = sys.platform
    key = 'darwin' if plat == 'darwin' else ('win32' if plat.startswith('win') else 'linux')
    for tmpl in _FALLBACKS.get(key, []):
        cand = tmpl.format(name=name)
        tried.append(cand)
        if os.path.exists(cand) and _runs(cand):
            return cand

    if name == 'ffmpeg':
        cand = _imageio_ffmpeg()
        if cand:
            return cand

    return None


def _cached(name: str) -> str | None:
    """带锁的缓存查询。**锁只包住字典读写**，不包住解析过程本身
    （解析过程可能起子进程；把它包进锁里会让多线程调用串行化，
    也会让「解析函数互相调用」直接自锁死）。"""
    with _LOCK:
        return _CACHE.get(name)


def _store(name: str, value: str | None) -> str | None:
    with _LOCK:
        _CACHE[name] = value
    return value


def find_ffmpeg() -> str:
    v = _cached('ffmpeg')
    if v is None:
        v = _store('ffmpeg', _resolve('ffmpeg'))
    if v is None:
        raise FFmpegNotFound(
            'ffmpeg',
            tried=['PATH:ffmpeg', *(t.format(name='ffmpeg')
                                  for t in _FALLBACKS.get(_plat_key(), []))],
            hint=_INSTALL_HINT.format(name='ffmpeg', name_upper='FFMPEG',
                                      example='/opt/homebrew/bin/ffmpeg'))
    return v


def find_ffprobe() -> str:
    """ffprobe 与 ffmpeg 通常同目录。先按 ffmpeg 的目录找，再走通用解析。

    这条「同目录优先」很实际：用户把静态包解压到自定义目录并只设了 KF_FFMPEG 时，
    ffprobe 就在旁边，不该因为没设 KF_FFPROBE 就报错。
    """
    v = _cached('ffprobe')
    if v is None:
        try:
            sibling = os.path.join(
                os.path.dirname(find_ffmpeg()),
                'ffprobe' + ('.exe' if sys.platform.startswith('win') else ''))
            if os.path.exists(sibling) and _runs(sibling):
                v = sibling
        except FFmpegNotFound:
            pass  # ffmpeg 都没有，下一步给出 ffprobe 自己的报错（信息更准）
        if v is None:
            v = _resolve('ffprobe')
        _store('ffprobe', v)
    if v is None:
        raise FFmpegNotFound('ffprobe', tried=['PATH:ffprobe'],
                             hint=_INSTALL_HINT.format(name='ffprobe', name_upper='FFPROBE',
                                                      example='/opt/homebrew/bin/ffprobe'))
    return v


def _plat_key() -> str:
    p = sys.platform
    return 'darwin' if p == 'darwin' else ('win32' if p.startswith('win') else 'linux')


def require() -> tuple[str, str]:
    """一次性拿 (ffmpeg, ffprobe)，失败时抛出带安装说明的异常。"""
    return find_ffmpeg(), find_ffprobe()


def _source(name: str, path: str) -> str:
    """这个二进制是从哪条路径找到的 —— 报告里必须能看出「用了谁的 ffmpeg」。"""
    if os.environ.get(f'KF_{name.upper()}'):
        return 'env'
    if 'imageio' in path:
        return 'imageio-ffmpeg'
    if shutil.which(name) == path:
        return 'PATH'
    return 'fallback-path'


def describe() -> dict:
    """给报告用：这一轮实际用的是哪个二进制、来自哪里。**不许只说「OK」**。"""
    out = {}
    for name, fn in (('ffmpeg', find_ffmpeg), ('ffprobe', find_ffprobe)):
        try:
            path = fn()
            out[name] = {'path': path, 'source': _source(name, path)}
        except FFmpegNotFound as e:
            out[name] = {'error': str(e), 'hint': e.hint}
    return out


def _reset_cache() -> None:
    """仅供测试：清掉解析缓存，让下一次调用重新走完整查找链。"""
    with _LOCK:
        _CACHE.clear()
