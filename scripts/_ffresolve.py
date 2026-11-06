"""给 scripts/ 和 tests/ 用的 ffmpeg 定位（复用 src/kf/procs.py）。

**为什么需要这个垫片**：`scripts/` 和 `tests/` 里的 benchmark/闸门脚本在
`sys.path.insert` 之前就要用到 FFMPEG 常量。直接 `from kf.procs import ...`
会因为还没插 path 而失败，于是早期版本都写了本机硬编码路径
`/opt/homebrew/bin/ffmpeg` —— 那在 Linux/Windows 上直接崩。

这个文件只做一件事：先插 path，再转手。任何脚本都可以：
    from _ffresolve import FFMPEG, FPROBE
"""
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT / 'src'), str(_ROOT / 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kf.procs import find_ffmpeg, find_ffprobe  # noqa: E402

FFMPEG = find_ffmpeg()
FPROBE = find_ffprobe()
