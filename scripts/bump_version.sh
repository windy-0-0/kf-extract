#!/usr/bin/env bash
# 版本号唯一来源是 src/kf/__init__.py 的 __version__（pyproject 用 dynamic attr 读它）。
# 这个脚本只做一件事：改完之后**验证安装出来的版本真的对上了**。
#
# 为什么需要这一步：实测踩到 —— 发了 v0.2.2 的 Release，但从 GitHub 安装出来
# `kf-extract --version` 仍是 0.2.0。用户会以为装到的是旧版。
set -euo pipefail
V="${1:?用法: bump_version.sh <version>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

python3 - "$V" <<'PY'
import pathlib, re, sys
v = sys.argv[1]
p = pathlib.Path('src/kf/__init__.py')
s = p.read_text()
s2, n = re.subn(r"__version__ = '[^']*'", f"__version__ = '{v}'", s)
if n != 1:
    sys.exit(f'期望恰好一处 __version__，实际 {n}')
p.write_text(s2)
print(f'__version__ -> {v}')
PY

echo '--- 验证安装后的版本来真的对上（不只看文件）---'
TMP="$(mktemp -d)"
python3 -m venv "$TMP/v" >/dev/null
"$TMP/v/bin/pip" install -q . >/dev/null 2>&1
"$TMP/v/bin/kf-extract" --version
"$TMP/v/bin/pip" show kf-extract 2>/dev/null | grep -E '^(Name|Version)'
rm -rf "$TMP"
