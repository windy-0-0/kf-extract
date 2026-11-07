#!/usr/bin/env python3
"""闸门：仓库里不许出现评测产物（图片 / zip / 缓存）。

**为什么需要这条**（实测踩到，2026-11-07）：GUI 默认把产物写到素材旁边，
落在 `eval/shots*/` 下。我跑 `git add -A` 时**把 86 个产物文件提交进了仓库**
（16 张 png + 10 张 jpg + 缓存 + 报告）。它们不报错、不冲突，
只是让仓库悄悄变大，而且以后每次 clone 都要拖这些没用的图。

R124 的教训（对外发布前要普查）在这一层是失效的：`git add -A` 不看内容。
所以需要一个**内容级判据**：按扩展名和大小，在 `eval/` 等素材目录里找产物。

用法：
  python3 scripts/check_no_artifacts.py          # 检查暂存区 + 索引
  python3 scripts/check_no_artifacts.py --all    # 检查索引里全部文件
"""
from __future__ import annotations

import os.path
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 不许进仓库的产物类型（素材目录内）。**只针对素材/输出目录**，
#: 因为 `sheets/` 里的示例图是文档的一部分，另算。
ARTIFACT_DIRS = ('eval/',)
ARTIFACT_EXT = {'.png', '.jpg', '.jpeg', '.npz', '.zip'}
#: 素材本身（mp4 等）不进仓库，但那是 Release 附件，已单独 ignore
ALLOW = {
    'eval/README.md', 'eval/corpus.json',
}


def tracked_files(rev: str) -> list[str]:
    r = subprocess.run(['git', 'ls-files', '--', *[f'{d}*' for d in ARTIFACT_DIRS]],
                       cwd=ROOT, capture_output=True, text=True)
    return [x for x in r.stdout.splitlines() if x.strip()]


def main() -> int:
    if not (ROOT / '.git').exists():
        print('（不是 git 仓库，跳过）')
        return 0
    files = tracked_files('HEAD')
    bad = []
    for f in files:
        if f in ALLOW or not f.startswith(ARTIFACT_DIRS):
            continue
        ext = os.path.splitext(f)[1].lower()
        if ext in ARTIFACT_EXT:
            size = 0
            p = ROOT / f
            if p.exists():
                size = p.stat().st_size
            bad.append(f'{f}  ({size/1e6:.1f}MB)')
    if bad:
        print('❌ 素材目录里有产物进了版本库：')
        for b in bad:
            print('   ', b)
        print('\n修法：')
        print('   git rm -r --cached eval/shots*')
        print('   并把对应目录加进 .gitignore（不要只靠 git add -A 的直觉）')
        return 1
    print(f'✅ 无产物入库（检查了 {len(files)} 个 eval/ 下的文件）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
