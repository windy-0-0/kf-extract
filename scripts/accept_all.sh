#!/usr/bin/env bash
# 全量验收：一条命令跑完所有闸门。
#
# 为什么要有这个：现在有 152 条单元测试 + 33 条 e2e + 两道闸门，
# 分散跑容易漏（我就漏过 check_no_artifacts，结果把 84 个 png 提交进仓库）。
#
# 用法：
#   bash scripts/accept_all.sh          # 全部
#   bash scripts/accept_all.sh --fast   # 跳过 e2e（e2e 要装浏览器，几十秒到几分钟）
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
FAST=0
[ "${1:-}" = "--fast" ] && FAST=1
FAIL=0

echo "=================== 1. 单元测试 ==================="
for f in tests/kf/test_*.py; do
  printf '%-22s ' "$(basename "$f")"
  OUT=$(PYTHONPATH="$ROOT/src" python3 "$f" 2>&1)
  R=$(echo "$OUT" | grep -oE '[0-9]+/[0-9]+ (passed|项通过)' | tail -1)
  if echo "$OUT" | grep -qE 'FAIL|^[^/]*\b[0-9]+/[0-9]+ [^p]'; then
    if echo "$OUT" | tail -1 | grep -qE '[0-9]+/[0-9]+ passed|项通过'; then :; fi
  fi
  # 判据：最后一行必须是 "N/N passed" 且 N==N
  LAST=$(echo "$OUT" | tail -1)
  if echo "$LAST" | grep -qE '([0-9]+)/([0-9]+) (passed|项通过)'; then
    A=$(echo "$LAST" | sed -E 's/.*[^0-9]([0-9]+)\/([0-9]+).*/\1/')
    B=$(echo "$LAST" | sed -E 's/.*[^0-9]([0-9]+)\/([0-9]+).*/\2/')
    if [ "$A" = "$B" ]; then echo "$R"; else echo "$R  ❌"; FAIL=1; fi
  else
    echo "$R  ❌"; FAIL=1
  fi
done

echo
echo "=================== 2. 基线冻结闸门 ==================="
python3 scripts/freeze_baseline.py --check || FAIL=1

echo
echo "=================== 3. 产物入库闸门 ==================="
python3 scripts/check_no_artifacts.py || FAIL=1

echo
echo "=================== 4. 探针（三模块交付体检）==================="
PYTHONPATH="$ROOT/src" python3 scripts/probe_accept.py >/tmp/probe.out 2>&1
if [ $? -eq 0 ]; then tail -2 /tmp/probe.out; else tail -6 /tmp/probe.out; FAIL=1; fi

if [ "$FAST" = "0" ]; then
  echo
  echo "=================== 5. GUI 端到端（真浏览器）==================="
  # 先清掉 e2e 专用的队列（它由 KF_QUEUE 指向，与生产隔离）
  rm -f eval/.e2e-queue.db
  if [ -d tests/e2e/node_modules ]; then
    # 失败时把完整输出留到文件：偶发失败最怕"重跑就绿、下次又红"，
    # 必须能翻到当次是哪一条断掉的（否则等于没有这个闸门）。
    node tests/e2e/run.mjs 2>&1 | tee /tmp/kf-e2e.log
    if [ "${PIPESTATUS[0]}" != "0" ]; then
      FAIL=1
      echo "  失败的完整日志: /tmp/kf-e2e.log"
    fi
  else
    echo "  跳过：tests/e2e/node_modules 不存在。先跑 cd tests/e2e && npm install"
  fi
else
  echo
  echo "=================== 5. GUI 端到端 ==================="
  echo "  跳过（--fast）"
fi

echo
if [ "$FAIL" = "0" ]; then
  echo "✅ 全部验收通过"
else
  echo "❌ 有验收项失败"
fi
exit "$FAIL"
