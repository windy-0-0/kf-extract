#!/usr/bin/env bash
# M2 验收门（任务书 docs/prompts/M2-v1.txt §验收）。
#
#   bash scripts/accept_m2.sh                  A4-1 + A4-3：fanren184@6fps 端到端 ≤180s、RSS≤500MB
#   bash scripts/accept_m2.sh --baseline-check A4-2：trailer 168 帧 8 个冻结数字逐位断言
#   bash scripts/accept_m2.sh --selftest       R99 自证：闸门必须能红（7 点阶梯 → 必须 FAIL）
#
# 任何一步失败即非零退出。输出一律 PASS:/FAIL: 开头，可复跑。
set -uo pipefail
cd "$(dirname "$0")/.."
# Python 解析：环境变量 → 仓库 venv → PATH 上的 python3
if [ -n "${KF_PY:-}" ]; then PY="$KF_PY"
elif [ -x "$PWD/venv/bin/python" ]; then PY="$PWD/venv/bin/python"
elif [ -x "$(dirname "$0")/../venv/bin/python" ]; then PY="$(cd "$(dirname "$0")/../venv/bin/python" && pwd)"
else PY=python3; fi

echo "=== M2 验收 ==="

# 0) 基线冻结（必须最先跑；eval.py/metrics.py 被改 = 一切作废）
if ! $PY scripts/freeze_baseline.py --check; then
  echo "FAIL: 基线被动过（见上面清单）"
  exit 1
fi
echo "PASS: 基线未动（eval.py 哈希 + 冻结常量）"

# 1) 交付物存在
for f in src/kf/fast.py tests/kf/test_fast.py scripts/bench_m2.py; do
  if [ ! -f "$f" ]; then
    echo "FAIL: $f 不存在（M2 未交付）"
    exit 1
  fi
done
echo "PASS: 交付物齐全（fast.py / test_fast.py / bench_m2.py）"

MODE="${1:-}"

# 2) A4-2 基线逐位检查（trailer 168 帧）
baseline_check() {
  $PY - <<'EOF'
import json, sys
sys.path.insert(0, '.')
import importlib.util
spec = importlib.util.spec_from_file_location('ev', 'scripts/eval.py')
ev = importlib.util.module_from_spec(spec); spec.loader.exec_module(ev)
from src.kf.fast import evaluate_fast

FROZEN = {10: (14, 100), 15: (7, 14), 20: (6, 12), 30: (4, 5)}
got = evaluate_fast('eval/trailer.mp4', sample_fps=12)
ref = ev.evaluate('eval/trailer.mp4', sample_fps=12)

bad = []
for b, (kc_f, un_f) in FROZEN.items():
    kc = got['coverage'][b]['kcenter']; un = got['coverage'][b]['uniform']
    status = 'OK' if (kc, un) == (kc_f, un_f) else 'MISMATCH'
    print(f'  budget={b}: kcenter={kc} (冻结 {kc_f}) uniform={un} (冻结 {un_f}) {status}')
    if (kc, un) != (kc_f, un_f):
        bad.append(b)
# 附加：与 eval.py 全字段 A/B（比 8 个数更强的等价断言）
for k in ('redundancy', 'scene_cuts', 'frames_processed', 'meta_assert_ok'):
    if ref.get(k) != got.get(k):
        bad.append(k)
        print(f'  字段 {k}: fast={got.get(k)} != eval={ref.get(k)}')
cov_ref = {str(k): v for k, v in ref['coverage'].items()}
cov_got = {str(k): v for k, v in got['coverage'].items()}
if cov_ref != cov_got:
    bad.append('coverage(全量)')
if bad:
    print(f'BASELINE_CHECK_FAIL: {bad}')
    sys.exit(1)
print('BASELINE_CHECK_OK')
EOF
}

# 3) R99 自证：把 uniform 换成 7 点阶梯（D-010 的事故形态），闸门必须红
selftest_red() {
  $PY - <<'EOF'
import sys
sys.path.insert(0, '.')
import importlib.util
spec = importlib.util.spec_from_file_location('ev', 'scripts/eval.py')
ev = importlib.util.module_from_spec(spec); spec.loader.exec_module(ev)
import numpy as np

X = ev.load_gray_bytes('eval/trailer.mp4', fps=12)
FROZEN_UN = {10: 100, 15: 14, 20: 12, 30: 5}

def uniform_ladder(X, budget):
    """D-010 的事故形态：7 点阶梯代替精确二分。"""
    n = len(X)
    for K in (5, 10, 21, 42, 84, 126, 168):
        if ev.maxdist(X, ev.uniform_idx(n, K)) <= budget:
            return K
    return None

bad = []
for b, frozen in FROZEN_UN.items():
    ladder = uniform_ladder(X, b)
    print(f'  budget={b}: 7点阶梯={ladder} 冻结={frozen}', flush=True)
    if ladder != frozen:
        bad.append((b, ladder, frozen))
if not bad:
    print('SELFTEST_BROKEN: 阶梯近似居然全对——闸门无法变红（R99 违反）')
    sys.exit(1)
print(f'GATE_RED_CONFIRMED: 阶梯近似把 {len(bad)}/4 个数字改错 {bad} → baseline-check 必 FAIL')
sys.exit(0)
EOF
}

case "$MODE" in
  --selftest)
    echo "--- R99 自证：7 点阶梯必须让基线检查变红 ---"
    if selftest_red; then
      echo "PASS: 闸门能红（阶梯近似被抓获）"
      exit 0
    else
      echo "FAIL: 闸门在阶梯近似下没有红（R99 违反）"
      exit 1
    fi
    ;;
  --baseline-check)
    echo "--- A4-2: trailer 168 帧 8 个冻结数字逐位断言 ---"
    if baseline_check; then
      echo "PASS: 基线数字逐个不变（8/8 逐位相同 + eval.py 全字段一致）"
      exit 0
    else
      echo "FAIL: 基线数字变了（见上面 MISMATCH）"
      exit 1
    fi
    ;;
esac

# 4) A4-1 + A4-3：fanren184@6fps 端到端 ≤180s、RSS≤500MB，
#    且 coverage/redundancy/scene_cuts 与 M0-fanren.json 一致
echo "--- A4-1/A4-3: fanren184@6fps 端到端（约 2-3 分钟）---"
OUT=/tmp/accept_m2_last.json
if ! $PY scripts/bench_m2.py --mode optimized --corpus eval/fanren184.mp4 --fps 6 \
     --watch-rss --json > "$OUT" 2>/tmp/accept_m2_last.err; then
  echo "FAIL: bench_m2.py 退出非零："
  tail -5 /tmp/accept_m2_last.err
  exit 1
fi
cat "$OUT"

$PY - "$OUT" <<'EOF'
import json, sys
bench = json.load(open(sys.argv[1]))
m0 = json.load(open('docs/status/M0-fanren.json'))['results'][0]

el = bench['elapsed_s']
rss = bench.get('peak_rss_mb')
bad = []

# A4-1 耗时
if not (el <= 180.0):
    bad.append(f'耗时 {el}s > 180s')

# A4-3 内存
if rss is None or not (rss <= 500.0):
    bad.append(f'RSS {rss}MB 超 500MB（或缺失 --watch-rss）')

# 与 M0 冻结的 fanren 口径一致（外部锚点：docs/status/M0-fanren.json）
cov_b = {str(k): v for k, v in bench['coverage'].items()}
cov_m = m0['coverage']
if cov_b != cov_m:
    bad.append(f'coverage 与 M0 不一致: {cov_b} vs {cov_m}')
if bench['redundancy'] != m0['redundancy']:
    bad.append('redundancy 与 M0 不一致')
if bench['scene_cuts'] != m0['scene_cuts']:
    bad.append('scene_cuts 与 M0 不一致')
if not bench.get('meta_assert_ok'):
    bad.append('A5 元断言失败')
if bench.get('frames_processed') != m0['frames_processed']:
    bad.append(f"frames {bench.get('frames_processed')} != {m0['frames_processed']}")

if bad:
    for b in bad:
        print('FAIL:', b)
    sys.exit(1)
print(f"PASS: fanren184@6fps 端到端 1050s -> {el}s (要求 <= 180s)")
print(f"PASS: RSS 峰值 {rss}MB <= 500MB")
print(f"PASS: coverage/redundancy/scene_cuts 与 M0-fanren.json 一致")
EOF
rc=$?
if [ $rc -ne 0 ]; then
  echo "FAIL: A4 验收未过"
  exit 1
fi

# 5) 单元测试
echo "--- 单元测试 ---"
if ! $PY tests/kf/test_fast.py; then
  echo "FAIL: 单元测试未全过"
  exit 1
fi

echo "=== M2 验收全部通过 ==="
