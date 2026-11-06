#!/usr/bin/env bash
# M2 验收门（M2 agent 负责把它做成真的会红）。
# 现在先占位并给出明确 FAIL 原因 —— 验收脚本先有、再实现，防止"先写代码再补验收"。
set -uo pipefail
cd "$(dirname "$0")/.."

echo "=== M2 验收 ==="
fail=0

# 1) 基线冻结（这条必须最先跑）
if ! /Users/mac/kf-lab/venv/bin/python scripts/freeze_baseline.py --check; then
  echo "FAIL: 基线被动过（见上面清单）"
  exit 1
fi
echo "PASS: 基线未动"

# 2) 实现是否存在
if [ ! -f src/kf/fast.py ]; then
  echo "FAIL: src/kf/fast.py 不存在（M2 未交付）"
  exit 1
fi

# 3) 基准脚本
if [ ! -f scripts/bench_m2.py ]; then
  echo "FAIL: scripts/bench_m2.py 不存在（未量账）"
  exit 1
fi
echo "(指令) 运行  bash scripts/bench_m2.py --corpus eval/fanren184.mp4 --fps 6"
echo "(要求)  输出末行必须是  PASS: elapsed=XXXs (<=180s)"
exit $fail
