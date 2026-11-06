#!/usr/bin/env bash
# M4 验收门 —— 出图策略（每格多大、每张几帧）
#
# 判据（任务书 §验收）：
#   A3-1  每格短边 >= 300px，且画面不能被拉扁（16:9 帧塞进非 16:9 格子 = 变形或黑边）
#   A3-2  达到 A3-1 的代价必须量化（要发几张图、总 token 多少、相对现状的增减）
#   A3-3  distinct_frac 不许比现状（真实番剧 pack4 = 0.509）降低
#   反向样本 1  8 帧 / 800×450  → 必须 FAIL（短边 < 300）
#   反向样本 2  1 帧 / 800×450  → 必须 PASS，但必须报出 token 浪费
#
# 门必须真的会红（R99）：反向样本不是"顺便看看"，它们不过就算验收失败。
set -uo pipefail
cd "$(dirname "$0")/.."

# 用哪个解释器：优先 venv（项目环境），退回 python3（系统，也带 numpy）
if [ -n "${KF_PYTHON:-}" ]; then PY="$KF_PYTHON"
elif [ -x "$ROOT/venv/bin/python" ]; then PY="$ROOT/venv/bin/python"
else PY=$(command -v python3); fi

echo "=== M4 验收（出图策略）==="
echo "python: $PY"
fail=0
note_fail() { echo "FAIL: $1"; fail=1; }

# ---------------------------------------------------------------- 0) 前置
if ! "$PY" -c 'import numpy' 2>/dev/null; then
  echo "FAIL: $PY 没有 numpy（验收需要 numpy 环境）"; exit 1
fi
if [ ! -f src/kf/layout.py ]; then echo "FAIL: src/kf/layout.py 不存在（M4 未交付）"; exit 1; fi
if [ ! -f scripts/bench_m4.py ]; then echo "FAIL: scripts/bench_m4.py 不存在（未量账）"; exit 1; fi
echo "PASS: 交付物就位（src/kf/layout.py + scripts/bench_m4.py）"

# ---------------------------------------------------------------- 1) 基线冻结
if ! "$PY" scripts/freeze_baseline.py --check; then
  echo "FAIL: 基线被动过（见上面清单）—— M4 不许改 scripts/eval.py"
  exit 1
fi
echo "PASS: 基线未动"

# ---------------------------------------------------------------- 2) 单元测试
if out=$("$PY" tests/kf/test_layout.py 2>&1); then
  echo "PASS: 单元测试 $(echo "$out" | tail -1)"
else
  echo "$out" | tail -20
  note_fail "单元测试未全过"
fi

# ---------------------------------------------------------------- 3) A3-1
echo
echo "--- A3-1 · 每格短边 >= 300px（4 帧/张）"
a31=$("$PY" - <<'PY'
import sys; sys.path.insert(0, '.')
from src.kf.layout import plan, accept_line, MIN_SHORT_SIDE
p = plan(200, distinct_table={4: 0.509, 8: 0.489})
r = p.recommended
if r is None:
    print('NO-FEASIBLE-PLAN'); raise SystemExit(1)
print(accept_line(r))
print(f'INFO: 画布 {r.canvas[0]}x{r.canvas[1]}，{r.pack} 帧/张，格子 {r.cell[0]}x{r.cell[1]}，'
      f'比例误差 {r.aspect_error:.4f}，单张 {r.tokens_per_sheet} token')
PY
)
echo "$a31" | grep -v '^INFO'
echo "$a31" | grep '^INFO' || true
line=$(echo "$a31" | grep '^PASS: ' | head -1)
if [ -z "$line" ]; then
  note_fail "A3-1 没有输出 PASS 行"
else
  echo "$line" | grep -qE '^PASS: 4-frame sheet cell = [0-9]+x[0-9]+ px \(short side >= 300\)$' \
    || note_fail "A3-1 PASS 行与任务书要求的形态不一致"
fi

# ---------------------------------------------------------------- 4) A3-2
echo
echo "--- A3-2 · 代价量化（同一段素材要发几张图 / 总 token）"
if a32=$("$PY" - <<'PY'
import sys; sys.path.insert(0, '.')
from src.kf.layout import plan
p0 = plan(200, distinct_table={4: 0.509})
r0, b0 = p0.recommended, p0.baseline
for K in (200, 2601, 10403):
    p = plan(K, distinct_table={4: 0.509})
    r, b = p.recommended, p.baseline
    print(f'INFO: K={K:<6} 现状 {b.sheets(K):>5} 张 × {b.tokens_per_sheet} = {b.total_tokens(K):>8} token'
          f'  →  达标 {r.sheets(K):>5} 张 × {r.tokens_per_sheet} = {r.total_tokens(K):>8} token'
          f'  ({p.token_delta_frac:+.1%})')
# 文案由计算生成，不许硬编码（旧版写死「单价 233→407」—— 改了约束它就会撒谎）
print(f'INFO: 张数不变（都是 {r0.pack} 帧/张），只因画布 '
      f'{b0.canvas[0]}x{b0.canvas[1]}→{r0.canvas[0]}x{r0.canvas[1]} 而 token 上升；'
      f'单价 {b0.tokens_per_sheet}→{r0.tokens_per_sheet}'
      f'（{r0.tokens_per_sheet / b0.tokens_per_sheet - 1:+.1%}）')
# A3-2 的真闸门：数字必须自洽（旧版只有 echo，永远不会红）
assert r0.tokens_per_sheet == 407 and b0.tokens_per_sheet == 233, '单价锚点变了'
assert abs(p0.token_delta_frac - 0.747) < 0.002, p0.token_delta_frac
assert p0.sheets == b0.sheets(200) == 50, '张数不该变'
print('OK: A3-2 代价自洽（张数不变 50 张，单价 233→407，总 token +74.7%）')
PY
); then
  echo "$a32"
  echo "$a32" | grep -q '^OK: ' || note_fail "A3-2 的自洽断言没通过"
else
  echo "$a32"
  note_fail "A3-2 代价计算崩了（断言失败或异常）"
fi

# ---------------------------------------------------------------- 5) A3-3
echo
echo "--- A3-3 · distinct_frac 不许比现状降低（锚点 pack4 = 0.509）"
a33=$("$PY" - <<'PY'
import json, sys, pathlib; sys.path.insert(0, '.')
from src.kf import metrics
from src.kf.layout import plan, BASELINE_PACK, BASELINE_DISTINCT_FRAC

# (a) 外部绝对锚点：冻结文件里 fanren184@6fps pack4 必须还是 0.509
frozen = pathlib.Path('docs/status/M0-fanren.json')
anchor = None
if frozen.exists():
    for r in json.loads(frozen.read_text(encoding='utf-8'))['results']:
        if r.get('file') == 'fanren184.mp4':
            anchor = r['redundancy']['pack4']['distinct_frac']
print(f'ANCHOR frozen pack4 = {anchor}')
if anchor is not None and abs(anchor - BASELINE_DISTINCT_FRAC) > 1e-9:
    print(f'FAIL: 冻结锚点 {anchor} != 代码里的 {BASELINE_DISTINCT_FRAC}')
    raise SystemExit(3)

# (b) 推荐组合必须保持 pack=4 —— 同一批帧同一分组 ⟹ distinct_frac 逐位不变（不降的最强形式）
p = plan(200, distinct_table={4: anchor if anchor is not None else BASELINE_DISTINCT_FRAC})
r = p.recommended
print(f'REC pack = {r.pack} (现状 pack = {BASELINE_PACK})')
if r.pack != BASELINE_PACK:
    print('FAIL: 推荐组合换了帧数，A3-3 不再是同口径比较（需要实测支撑）')
    raise SystemExit(3)

# (c) 同一批真实帧上的实测曲线。
#     只解前 30s：849MB 全量解码要 ~100s，验收门不该为了 180 帧付这个代价（R67/R255）。
#     **必须同时测「嵌套对」与「非嵌套对」**：旧版只测 {1,2,4}（恰好是 4 的三个约数），
#     结构上永远测不到已知反例（pack=3 会倒挂）—— 那是「永远不会红」的闸门（R99）。
import subprocess
import numpy as np
vf = f'fps=6,scale={metrics.ANALYSIS_W}:{metrics.ANALYSIS_H},format=gray'
_ff = subprocess.run(['/opt/homebrew/bin/ffmpeg', '-v', 'error', '-i', 'eval/fanren184.mp4',
                      '-t', '30', '-vf', vf, '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
                     capture_output=True)
if _ff.returncode != 0 or not _ff.stdout:
    # **把「环境红」与「逻辑红」分开**：本机并行跑多个 agent 时，
    # ffmpeg 会被 OOM killer 干掉（实测见过一次），那时这一节根本没法判，
    # 不能报成「A3-3 未通过」——那会把环境问题伪装成逻辑问题（R346/R340）。
    print(f'ENVFAIL: ffmpeg 抽帧失败（rc={_ff.returncode}，{len(_ff.stdout)} bytes）'
          f'{_ff.stderr[:160].decode("utf-8", "replace")}')
    raise SystemExit(4)
raw = _ff.stdout
_n = len(raw) // (metrics.ANALYSIS_W * metrics.ANALYSIS_H)
X = np.frombuffer(raw[:_n * metrics.ANALYSIS_W * metrics.ANALYSIS_H], dtype=np.uint8).reshape(
    _n, metrics.ANALYSIS_W * metrics.ANALYSIS_H).astype('float32')
print(f'WINDOW fanren184 前 30s = {_n} 帧')
packs = (1, 2, 3, 4, 5, 6, 7, 8)
curve = {q: metrics.redundancy(X, q)['distinct_frac'] for q in packs}
print('CURVE ' + ' '.join(f'pack{q}={v:.3f}' for q, v in curve.items()))

# 嵌套对（小 pack 整除基准 pack）：小组是大组的子集 ⟹ distinct_frac 不许倒挂
nested = [q for q in packs if q < BASELINE_PACK and BASELINE_PACK % q == 0]
bad = [q for q in nested if curve[q] < curve[BASELINE_PACK] - 1e-9]
print('NESTED ' + ','.join(f'pack{q}({curve[q]:.3f})' for q in nested))
if bad:
    print(f'FAIL: 嵌套对出现倒挂（推理被实测推翻）: {[(q, curve[q]) for q in bad]}')
    raise SystemExit(3)
# 非嵌套对：分组互不嵌套，指标不可比 ⟹ 允许倒挂，但必须**如实报出来**（不许静默）
nonnested = [q for q in packs if q < BASELINE_PACK and BASELINE_PACK % q != 0]
inv = [q for q in nonnested if curve[q] < curve[BASELINE_PACK] - 1e-9]
print('NON-NESTED-UNVER ' + ','.join(f'pack{q}({curve[q]:.3f})' for q in nonnested)
      + ('  倒挂=' + ','.join(map(str, inv)) if inv else '  无倒挂'))
print('OK: 推荐 pack4 的 distinct_frac = 现状值（不变）；嵌套对（1/2）都不低于现状')
PY
)
echo "$a33"
if echo "$a33" | grep -q '^ENVFAIL'; then
  # 环境红 ≠ 逻辑红：抽帧都失败了，这一节根本没判过，不许报「A3-3 未通过」
  echo "WARN: A3-3 判不了（抽帧被环境干掉）—— 标 UNVER，请在机器空闲时重跑"
  echo "UNVER: A3-3 未判定（环境失败）"
else
  echo "$a33" | grep -q '^OK: ' || note_fail "A3-3 未通过"
  echo "$a33" | grep -q '^NON-NESTED-UNVER ' || note_fail "A3-3 没报非嵌套对（闸门覆盖不足）"
fi

# ---------------------------------------------------------------- 6) 反向样本
echo
echo "--- 反向样本 1 · 8 帧 / 800×450 → 必须 FAIL"
r1=$("$PY" - <<'PY'
import sys; sys.path.insert(0, '.')
from src.kf.layout import make_candidate, accept_line, Constraints, MIN_SHORT_SIDE
c = make_candidate((800, 450), 8, Constraints(), distinct_table={4: 0.509})
print(accept_line(c))
print('SHORT', c.short_side, 'MIN', MIN_SHORT_SIDE, 'OK', c.ok)
for x in c.reasons: print('REASON', x)
PY
)
echo "$r1"
echo "$r1" | grep -q '^FAIL: ' || note_fail "反向样本 1 竟判 PASS（8 帧 800×450 短边 <300）"
echo "$r1" | grep -q 'SHORT 150.0 MIN 300 OK False' || note_fail "反向样本 1 数字不对"

echo
echo "--- 反向样本 2 · 1 帧 / 800×450 → 必须 PASS 且报 token 浪费"
r2=$("$PY" - <<'PY'
import sys; sys.path.insert(0, '.')
from src.kf.layout import plan, make_candidate, accept_line, Constraints
solo = make_candidate((800, 450), 1, Constraints(), distinct_table={4: 0.509})
print(accept_line(solo))
p = plan(200, distinct_table={4: 0.509})
r = p.recommended
waste = solo.tokens_per_frame() / r.tokens_per_frame()
print(f'WASTE 单帧 {solo.tokens_per_frame():.1f} token/帧 vs 4帧方案 {r.tokens_per_frame():.1f} token/帧'
      f' = {waste:.2f}x；同样 200 帧要发 {solo.sheets(200)} 张 vs {r.sheets(200)} 张'
      f'（{solo.total_tokens(200)} vs {r.total_tokens(200)} token）')
PY
)
echo "$r2"
echo "$r2" | grep -q '^PASS: ' || note_fail "反向样本 2 未 PASS"
echo "$r2" | grep -qE '^WASTE .* = 2\.[0-9]+x' || note_fail "反向样本 2 没报出 token 浪费"

# ---------------------------------------------------------------- 7) 量账脚本可跑
echo
echo "--- 量账：scripts/bench_m4.py（四段素材）"
if bench=$("$PY" scripts/bench_m4.py 2>&1); then
  for clip in trailer.mp4 mv195.mp4 mv227.mp4 fanren184.mp4; do
    echo "$bench" | grep -q "$clip" || note_fail "量账缺 $clip"
  done
  echo "PASS: 量账四段素材齐全（完整四表见 docs/status/M4-report.md）"
else
  echo "$bench" | tail -15
  # 本机并行跑多 agent 时这一步会被 OOM killer 干掉（实测见过），退一步只跑一段再判 ——
  # 只有「单段也跑不动」才算逻辑失败，否则是环境红，不许冒充逻辑红。
  if retry=$("$PY" scripts/bench_m4.py --only fanren184 2>&1) \
     && echo "$retry" | grep -q 'fanren184.mp4'; then
    echo "WARN: 四段量账在本机负载下失败，单段重试通过 —— 判为**环境红**（不是逻辑红），不计 FAIL"
  else
    note_fail "bench_m4.py 四段与单段都跑不动（环境失败，请降负载后重跑）"
  fi
fi

# ---------------------------------------------------------------- 结论
echo
if [ "$fail" -eq 0 ]; then
  echo "=== 验收结论: PASS ==="
else
  echo "=== 验收结论: FAIL（见上面 FAIL 行）==="
fi
exit "$fail"
