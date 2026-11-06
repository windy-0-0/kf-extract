"""M4 测试：边界 / 约束冲突 / token 账 / 反向样本。

外部锚点（不是自己跟自己对）：
  * token 公式锚点   —— see-now `src/tokens.js`：800×450=233 / 1066×600=407 / 1280×720=578
  * A3-3 绝对锚点    —— `docs/status/M0-fanren.json`：fanren184@6fps pack4=0.509 / pack8=0.489
  * 现状几何锚点     —— `sheets/4帧.png` 实测：800×462 画布，分隔线在 x=400 / y=225 → 每格 400×225

跑法（两种都行）：
    python3 -m pytest tests/kf/test_layout.py -q
    python3 tests/kf/test_layout.py
"""
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import numpy as np                                                    # noqa: E402
from src.kf.layout import (plan, accept_line, grid_for, image_tokens,  # noqa: E402
                           optimal_canvas, make_candidate, Constraints,
                           MIN_SHORT_SIDE, BASELINE_CANVAS, BASELINE_PACK, Candidate)

ROOT = pathlib.Path(__file__).resolve().parents[2]
FFMPEG = '/opt/homebrew/bin/ffmpeg'

DISTINCT = {4: 0.509, 8: 0.489}


# ---------------------------------------------------------------- 反向样本（缺一不算过）

def test_baseline_fails_short_side():
    """反向样本 1a：现状 800×450 放 4 帧 = 400×225 → 必须 FAIL。"""
    p = plan(80, distinct_table=DISTINCT)
    line = accept_line(p.baseline)
    assert line.startswith('FAIL'), f'基线 225px 竟判 PASS: {line}'
    assert '225' in line


def test_reverse_sample_8_frames_800x450():
    """反向样本 1b（任务书原文）：8 帧 / 800×450 → 必须报 FAIL（短边 < 300）。"""
    c = make_candidate((800, 450), 8, Constraints(), distinct_table=DISTINCT)
    assert c.short_side < MIN_SHORT_SIDE, c.short_side
    assert not c.ok, c.as_dict()
    assert any('A3-1 FAIL' in r for r in c.reasons), c.reasons
    assert accept_line(c).startswith('FAIL')


def test_reverse_sample_1_frame_passes_but_wastes_tokens():
    """反向样本 2：1 帧 / 800×450 → PASS，但必须能报出 token 浪费。

    这个测试**不许只断言 pack>=1**（那是永远为真的假断言）——
    必须真的量出「同样的帧数，单帧方案每帧贵 2.29 倍」。
    """
    p1 = plan(40, distinct_table=DISTINCT)
    c1 = p1.recommended
    assert c1.ok_short_side                       # 单帧 800×450 短边 450 → 几何达标
    solo = make_candidate((800, 450), 1, Constraints(), distinct_table=DISTINCT)
    assert solo.ok
    ref = c1.tokens_per_frame()
    waste = solo.tokens_per_frame() / ref
    assert waste > 2.0, f'单帧每帧 {solo.tokens_per_frame():.1f} vs 推荐 {ref:.1f} = {waste:.2f}x'
    # 张数也暴涨 4 倍（同一批 40 帧：10 张 → 40 张）
    assert solo.sheets(40) == 4 * c1.sheets(40) == 40


# ---------------------------------------------------------------- A3-1 边界 / 变形闸门

def test_a3_1_exact_boundary_300():
    """A3-1 边界值：1066×600 放 4 帧恰好 533×300，短边正好 300 → PASS。"""
    c = make_candidate((1066, 600), 4, Constraints(), distinct_table=DISTINCT)
    assert c.cell == (533, 300), c.cell
    assert c.short_side == 300.0
    assert c.ok_short_side and c.ok_aspect and c.ok_distinct and c.ok
    # 再小一点都不行：800×450 那张就是 225
    assert not make_candidate((800, 450), 4, Constraints()).ok_short_side


def test_aspect_distortion_is_not_a_pass():
    """「短边够了但画面被拉扁」不许放行。

    800×450 放 2 帧 → 格子 400×450，短边 400 看起来达标，但格子比例 0.89 vs 源 1.78。
    16:9 的帧塞进这个格子只有两种结局：拉扁一倍，或上下留黑边后**画面只剩 400×225**
    （短边 225，比现状还差）。所以 A3-1 必须在「画出来的画面」上量，比例是硬约束。
    """
    c = make_candidate((800, 450), 2, Constraints(), distinct_table=DISTINCT)
    assert c.short_side >= MIN_SHORT_SIDE        # 几何上确实 >= 300
    assert not c.ok_aspect                       # 但比例不对 → 不许放行
    assert not c.ok
    assert any('保比例 FAIL' in r for r in c.reasons), c.reasons


def test_a3_1_and_a3_3_conflict_on_pack8():
    """A3-1 与 A3-3 的正面冲突：1600×900 放 8 帧几何达标（533×300），但 A3-3 红。"""
    c = make_candidate((1600, 900), 8, Constraints(), distinct_table=DISTINCT)
    assert c.short_side == 300.0 and c.ok_short_side and c.ok_aspect
    assert not c.ok_distinct                     # pack=8 > 现状 pack=4
    assert not c.ok, 'pack=8 不许可行 —— 否则就是牺牲 A3-3 换成本'


def test_distinct_gate_can_go_red_when_measured_lower():
    """R99：实测 distinct_frac 低于 pack4 时，闸门必须真的红（不许靠推理放行）。"""
    table = {4: 0.600, 2: 0.550}                 # 2 帧的实测值反而更低（人为构造的反例）
    c = make_candidate((800, 450), 2, Constraints(), distinct_table=table)
    assert not c.ok_distinct, c.as_dict()


# ---------------------------------------------------------------- 约束冲突 / 无解

def test_constraint_conflict_no_feasible_plan():
    """约束冲突：只给 800×450 且只准 8 帧/张 → 无可行解，必须明确报 FAIL。"""
    c = Constraints(canvases=((800, 450),), frame_counts=(8,))
    p = plan(80, c, distinct_table=DISTINCT)
    assert p.recommended is None
    assert p.ok is False
    assert any('FAIL' in n for n in p.notes), p.notes


def test_unmeasured_distinct_is_not_verified():
    """只有帧数、没有帧矩阵 → distinct 未实测，distinct_verified 必须是 False。"""
    p = plan(80)
    assert p.ok and p.recommended is not None
    assert p.distinct_verified is False
    assert any('UNVER' in n for n in p.notes), p.notes


def test_measured_distinct_marks_verified():
    """给了帧矩阵 → distinct 实测，verified 必须翻真。"""
    rng = np.random.default_rng(7)
    X = rng.random((48, 32 * 18), dtype='float32') * 255
    p = plan(X, distinct_table=None)
    assert p.distinct_verified is True
    assert p.recommended.distinct_frac is not None


# ---------------------------------------------------------------- token 账 / 几何

def test_baseline_tokens_233():
    """800×450 = 233 token 是外部锚点（see-now `src/tokens.js`）。"""
    assert image_tokens(*BASELINE_CANVAS) == 233, image_tokens(*BASELINE_CANVAS)


def test_token_formula_other_anchors():
    """另外两个锚点 + 阶梯性质：1036×583 比 1066×600 便宜一整档。"""
    assert image_tokens(1066, 600) == 407
    assert image_tokens(1280, 720) == 578
    assert image_tokens(1036, 583) == 366
    assert image_tokens(1066, 600) - image_tokens(1036, 583) == 41


def test_token_billing_floor_544():
    """计费地板：小于 544×544 的图按 544² 收费（小画布不省钱）。"""
    assert image_tokens(534, 300) >= image_tokens(544, 544) - 1


def test_tokens_monotone_in_canvas():
    """画布越大 token 越多（同口径）。"""
    t = [image_tokens(w, h) for (w, h) in ((800, 450), (1066, 600), (1280, 720), (1600, 900))]
    assert t == sorted(t), f'token 应随画布单调增：{t}'


def test_sheet_and_token_math():
    """张数与总 token 的算术：K=9/10 帧、4 帧一张 → 3 张。"""
    p = plan(9, distinct_table=DISTINCT)
    r = p.recommended
    assert r.tokens_per_sheet == 407
    assert r.sheets(9) == 3 and r.total_tokens(9) == 3 * 407
    assert plan(10, distinct_table=DISTINCT).sheets == 3
    assert plan(12, distinct_table=DISTINCT).sheets == 3


def test_grid_aspect():
    """1 帧 1×1；4 帧 2×2；8 帧优先 3×3（保比例，宁留 1 个空格子）。"""
    assert grid_for(1) == (1, 1)
    assert grid_for(4) == (2, 2)
    assert grid_for(8) == (3, 3)
    assert grid_for(16) == (4, 4)


def test_ladder_canvas_is_optimal_within_ladder():
    """阶梯内 1066×600/4（407）最省；**全局最优在阶梯外**是 392（−3.7%）。

    独立复核抓到旧版 `optimal_canvas` 把画布比例写死成源比例，于是漏掉了
    「画布比例 ≠ 16:9、但每格恰好接近 16:9」的一族解（1050×600 等）。
    本测试两个事实都钉住 —— 阶梯内最优与全局最优**不是同一个数**，不许混说。
    """
    cc = Constraints()
    compliant = [x for x in
                 (make_candidate(cv, 4, cc, distinct_table=DISTINCT)
                  for cv in cc.canvases) if x.ok]
    assert min(x.tokens_per_sheet for x in compliant) == 407        # 阶梯内
    best = optimal_canvas(4, cc)
    assert best is not None and best[2] == 392, best                # 全局（阶梯外）
    g = make_candidate((best[0], best[1]), 4, cc, distinct_table=DISTINCT)
    assert g.ok and g.short_side >= MIN_SHORT_SIDE
    assert g.aspect_error <= cc.aspect_tolerance                    # 1.94% —— 在容差内
    assert best[2] < 407                                            # 承认阶梯不是全局最优


def test_token_cap_shrink_is_not_a_pass():
    """1024 token 上限会把大画布缩小 —— 可读性必须按**送进模型的实际尺寸**量。

    2136×1202 的 16 格拼图名义每格 534×300（看着达标），实际被缩到 1708×952，
    每格只剩 427×238。不查这一步就会把「缩完不达标」报成 PASS（假绿）。
    数字与 see-now `src/tokens.js` 用 node 实跑的一致（1708×952，968 token）。

    **并且 `accept_line` 也必须判 FAIL** —— 只看名义格子的旧版会输出
    `PASS: 16-frame sheet cell = 534x300.5 px`，那是闸门在说谎（独立复核抓到）。
    """
    c = make_candidate((2136, 1202), 16, Constraints(), distinct_table=DISTINCT)
    assert c.nominal_short_side >= MIN_SHORT_SIDE      # 名义上达标
    assert c.shrunk and c.effective_size == (1708, 952)
    assert c.short_side < MIN_SHORT_SIDE               # 实际不达标
    assert not c.ok_short_side and not c.ok
    assert any('1024 token 上限' in r for r in c.reasons), c.reasons
    line = accept_line(c)
    assert line.startswith('FAIL') and '238' in line, line


def test_accept_line_rejects_aspect_distortion_too():
    """`accept_line` 必须与 `plan()` 同源判据：比例不对也要 FAIL。

    800×450 / 2 帧的格子是 400×450（短边 400 ≥300），只看短边的旧版会假 PASS。
    """
    c = make_candidate((800, 450), 2, Constraints(), distinct_table=DISTINCT)
    assert c.short_side >= MIN_SHORT_SIDE and not c.ok_aspect
    line = accept_line(c)
    assert line.startswith('FAIL') and 'aspect' in line, line


def test_big_grid_has_no_compliant_canvas_at_all():
    """A3-1 下每张帧数的上限是 **10**（不是 9）—— 5×2 的 2800×630 每格恰好 16:9。

    `pack ≥ 11` 才无解：画布还没大到让每格 ≥300px，就先越过 1024 token 上限。
    """
    cc = Constraints()
    assert optimal_canvas(11, cc) is None
    assert optimal_canvas(12, cc) is None
    assert optimal_canvas(16, cc) is None
    ten = optimal_canvas(10, cc)
    assert ten is not None and ten[2] == 938, ten                  # 1046×1500 的 2×5
    t = make_candidate((ten[0], ten[1]), 10, cc, distinct_table=DISTINCT)
    assert t.ok_short_side and t.ok_aspect and t.short_side >= MIN_SHORT_SIDE
    assert not t.ok                                                # 只有 A3-3 红
    # 每格**恰好** 16:9 的那一族解也存在（2800×630 的 5×2 = 每格 560×315），
    # 但全局最省不是它（1022 > 938）—— 说明「恰好 16:9」不是最省的必要条件。
    exact = make_candidate((2800, 630), 10, cc, distinct_table=DISTINCT)
    assert exact.aspect_error < 1e-9 and exact.ok_short_side and exact.ok_aspect
    assert exact.tokens_per_sheet == 1022 > ten[2]


# ---------------------------------------------------------------- 验收行 / plan 契约

def test_accept_line_literal_format():
    """A3-1 的 PASS 行必须与任务书要求的形态逐字一致。"""
    p = plan(80, distinct_table=DISTINCT)
    assert accept_line(p.recommended) == 'PASS: 4-frame sheet cell = 533x300 px (short side >= 300)'


def test_accept_line_never_blank_pass():
    """R99：闸门不许对不达标候选输出 PASS。"""
    bad = Candidate(canvas=(800, 450), pack=4, cols=2, rows=2,
                    cell_w=400.0, cell_h=225.0, short_side=225.0,
                    aspect_error=0.0, tokens_per_sheet=233,
                    ok_short_side=False, ok_aspect=True, ok_distinct=True,
                    distinct_frac=0.509, distinct_ref=0.509, distinct_verified=True,
                    reasons=())
    assert accept_line(bad).startswith('FAIL')


def test_plan_ok_flag():
    p = plan(80, distinct_table=DISTINCT)
    assert p.ok is True
    assert p.recommended is not None
    assert p.recommended.pack == BASELINE_PACK          # 帧数不变，只换画布
    assert p.baseline_tokens == 20 * 233
    assert p.total_tokens == 20 * 407


def test_frozen_distinct_anchor_matches_baseline_config():
    """A3-3 的绝对值锚点：现状 pack4 的 distinct_frac 就是 0.509。"""
    c = make_candidate((800, 450), 4, Constraints(), distinct_table=DISTINCT)
    assert c.distinct_frac == 0.509
    assert c.distinct_ref == 0.509
    assert c.ok_distinct                                  # 不变 = 不降


# ---------------------------------------------------------------- 外部锚点：实测拼图几何

def test_measured_4frame_sheet_geometry():
    """现状几何必须与**实测产物**对齐（不是抄文档）：sheets/4帧.png 的分隔线。"""
    png = ROOT / 'sheets' / '4帧.png'
    if not png.exists() or not pathlib.Path(FFMPEG).exists():
        print('  (skip: 缺 sheets/4帧.png 或 ffmpeg)')
        return
    r = subprocess.run([FFMPEG, '-v', 'error', '-i', str(png),
                        '-f', 'rawvideo', '-pix_fmt', 'gray', '-'], capture_output=True)
    w, h = 800, 462
    assert len(r.stdout) >= w * h, f'解码失败: {r.stderr[:120]}'
    a = np.frombuffer(r.stdout[:w * h], dtype=np.uint8).reshape(h, w).astype(float)
    col = np.abs(np.diff(a[:h // 2], axis=1)).mean(axis=0)
    row = np.abs(np.diff(a[:, :w // 2], axis=0)).mean(axis=1)
    sep_x = 350 + int(np.argmax(col[350:450]))
    sep_y = 180 + int(np.argmax(row[180:300]))
    assert abs(sep_x - 400) <= 4, f'竖分隔线在 x={sep_x}，实测期望 ≈400'
    assert abs(sep_y - 225) <= 6, f'横分隔线在 y={sep_y}，实测期望 ≈225'
    # 与 layout 的现状候选一致
    c = make_candidate((800, 450), 4, Constraints(), distinct_table=DISTINCT)
    assert c.cell == (400, 225), c.cell


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    ok = 0
    for fn in fns:
        try:
            fn()
            print(f'PASS {fn.__name__}')
            ok += 1
        except Exception as e:                                        # noqa: BLE001
            print(f'FAIL {fn.__name__}: {type(e).__name__}: {e}')
    print(f'\n{ok}/{len(fns)} passed')
    sys.exit(0 if ok == len(fns) else 1)
