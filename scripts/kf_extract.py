#!/usr/bin/env python3
"""兼容垫片：老文档/老脚本里写的 `scripts/kf_extract.py` 继续能用。

真正的入口是 `src/kf/cli.py`（安装后是命令 `kf-extract`）。
这里只做一件事：把 `src/` 加进 sys.path 然后转调 —— **不再维护第二份逻辑**。

为什么保留它（而不是直接删）：
  - 仓库内 `docs/status/*.md` 和 README 的历史章节引用过这个路径；
  - 已有用户/脚本可能已经把它写进流水线。
删除会让「以前能跑的命令」突然报错，这是我们不想制造的破坏。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'src'))

from kf.cli import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
