"""M4 · 出图策略：给定 K 个关键帧，决定「每张拼图放几帧、每个格子多大」。

入口：``plan(frames, constraints) -> Plan``

三条口径（全部来自实测，不是拍脑袋）：

1. **可读性代理 = 每格短边像素**（A3-1 硬门槛 300px）。
   注意是「画出来的画面」的短边 —— 所以格子比例必须与源画面一致（见 `aspect_error`）：
   把 16:9 的帧塞进 400×300 的格子，要么拉扁 25%，要么上下留黑边（内容只剩 400×225）。
   留黑边时短边仍是 225，A3-1 照样不达标。**保比例是 A3-1 的一部分，不是可选项。**
2. **信息量代理 = `metrics.redundancy()` 的 `distinct_frac`**（同一批帧，pack = 每张几帧）。
3. **成本 = 张数 × 每张 token**，token 用宿主真实口径（见下）。

token 公式（**逐行移植自 see-now `src/tokens.js`**，Apache-2.0，NOTICE 见文件尾）：

    先按 14px patch 向上对齐 → 每轴 3:1 降采样 → 一个 token 格 = 42×42 px
    tokens = rows × (cols + 1) + 2        （每行多 1 个分隔 token，整张 +2 框架 token）
    小于 544×544 的图**计费时**会被放大到该面积；超过 1024 token 会按长宽比缩到能塞进预算的最大网格

移植锚点（外部锚点，见 see-now `src/tokens.js` 注释与 README）：
    800×450  → 11×21+2 = **233**
    1066×600 → 15×27+2 = **407**
    1280×720 → 18×32+2 = **578**

**关键性质：token 是 42px 网格的阶梯函数，不是像素面积的线性函数。**
所以「画布大 1.5% 可能便宜一整行 token」，反过来「每格大一点点可能贵一整档」。
本模块因此不做「像素÷常数」的估算，一律走 `image_tokens()`。

**几何结论（本模块要证的）**：
在 16:9 画布上要塞进「比例正确的 16:9 格子」，格子数必须是完全平方数（1/4/9/16）。
每格短边 ≥300 ⟹ 每格 ≥ 533.33×300 ⟹ 4 帧需要 ≥1066.67×600 的画布。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

# ---------------------------------------------------------------- 常量

#: 源画面比例（16:9）。cell 比例必须落在它附近，否则要么变形要么留黑边。
SRC_ASPECT = 16.0 / 9.0

#: 画布尺寸阶梯（任务书 §允许的技术路线 1 写死的那四档）
CANVAS_LADDER: tuple[tuple[int, int], ...] = (
    (800, 450), (1066, 600), (1280, 720), (1600, 900),
)

#: 每张图的帧数候选（任务书 §允许的技术路线 1）
FRAME_COUNTS: tuple[int, ...] = (1, 2, 4, 8, 16)

#: 现状（见-now 拼图器当前口径）：800×450 一张 4 帧
BASELINE_CANVAS: tuple[int, int] = (800, 450)
BASELINE_PACK: int = 4
#: 现状的 A3-3 锚点：真实番剧 fanren184 @6fps pack4 = 50.9%（docs/status/M0-baseline.md）
BASELINE_DISTINCT_FRAC: float = 0.509

# A3-1 硬门槛
MIN_SHORT_SIDE: int = 300

# ---------------------------------------------------------------- token（移植自 see-now/src/tokens.js）

IMAGE_PATCH = 14
IMAGE_DOWNSAMPLE = 3
IMAGE_CELL = IMAGE_PATCH * IMAGE_DOWNSAMPLE          # 42
IMAGE_TOKEN_CAP = 1024                               # 宿主单图 token 上限
IMAGE_UPSCALE_AREA = 544 * 544                       # 计费地板：小于该面积的图按该面积收费


def _int_div(v: int, d: int) -> int:
    return v // d


def _ceil_div(v: int, d: int) -> int:
    return -(-v // d)


def _pad_to_patch(px: int) -> int:
    return _ceil_div(px, IMAGE_PATCH) * IMAGE_PATCH


def _grid_cells(padded: int) -> int:
    return _ceil_div(_int_div(padded, IMAGE_PATCH), IMAGE_DOWNSAMPLE)


def _grid_tokens(rows: int, cols: int) -> int:
    return rows * (cols + 1) + 2


def _project_size(w: int, h: int, cap: int) -> tuple[int, int, int]:
    """一次对齐 → 投影；塞得下就原样，塞不下就解出最大网格。"""
    tw = _grid_tokens(_grid_cells(_pad_to_patch(h)), _grid_cells(_pad_to_patch(w)))
    if tw <= cap:
        return w, h, tw
    aspect = h / w
    ideal_w = math.sqrt((cap - 2) / aspect + 0.25) - 0.5
    solved_w = math.trunc(ideal_w)
    solved_h = math.trunc(ideal_w * aspect)
    if solved_w < 1:
        solved_w = 1
        solved_h = _int_div(cap - 2, 2)
    elif solved_h < 1:
        solved_h = 1
        solved_w = _int_div(cap - 2, solved_h) - 1
    scale = min((solved_w * IMAGE_CELL) / w, (solved_h * IMAGE_CELL) / h)
    bw = math.trunc((w * scale) / IMAGE_PATCH) * IMAGE_PATCH
    bh = math.trunc((h * scale) / IMAGE_PATCH) * IMAGE_PATCH
    return bw, bh, _grid_tokens(_grid_cells(bh), _grid_cells(bw))


def image_tokens(w: int, h: int) -> int:
    """像素尺寸 → 估算 token（宿主图像 patch 网格口径，≤1024，无效尺寸返回 0）。"""
    W, H = int(round(w)), int(round(h))
    if W <= 0 or H <= 0:
        return 0
    px = W * H
    if px < IMAGE_UPSCALE_AREA:                     # 计费地板：先按面积放大（发送时不放大）
        scale = math.sqrt(IMAGE_UPSCALE_AREA / px)
        W = max(1, math.trunc(W * scale))
        H = max(1, math.trunc(H * scale))
    cur = _project_size(W, H, IMAGE_TOKEN_CAP)
    for _ in range(10):                             # 缩完再投影可能又变，迭代到不动点
        nxt = _project_size(cur[0], cur[1], IMAGE_TOKEN_CAP)
        if nxt == cur:
            return cur[2]
        cur = nxt
    return cur[2]


# ---------------------------------------------------------------- 网格

def effective_size(w: int, h: int) -> tuple[int, int]:
    """宿主**实际送进模型**的像素尺寸（超过 1024 token 会按长宽比缩小）。

    与 `image_tokens` 的分工必须分清：**计费**含 544² 地板（小图按大地皮收钱），
    但发送时不会真的放大 —— 所以「可读性」只能按这里返回的尺寸量，不能按计费尺寸量。

    这条不是理论洁癖：2136×1202 的 16 格拼图计费 968 token（看着没过 1024 上限），
    但那是**缩过之后**的网格数 —— 实际送到模型手里的是 1708×952，每格 427×238，
    短边 238 < 300（这个数用 node 直跑 see-now 的 tokens.js 对过，逐位一致）。不查这一步就会把「缩完不达标」的画布报成 PASS。
    """
    W, H = int(round(w)), int(round(h))
    if W <= 0 or H <= 0:
        return 0, 0
    cur = _project_size(W, H, IMAGE_TOKEN_CAP)
    for _ in range(10):
        nxt = _project_size(cur[0], cur[1], IMAGE_TOKEN_CAP)
        if nxt == cur:
            break
        cur = nxt
    return cur[0], cur[1]


def grid_for(pack: int, canvas_aspect: float = SRC_ASPECT,
             source_aspect: float = SRC_ASPECT) -> tuple[int, int]:
    """给定每张图的帧数，选 (cols, rows)。

    排序键：① 格子比例误差最小（保比例优先）② 空格子最少 ③ 越接近正方越好。
    `cols*rows` 可以大于 `pack`（多出来的格子空着）；空格子会浪费画布面积，
    所以只在比例误差相同时才比较它。
    """
    if pack < 1:
        raise ValueError(f'pack 必须 >= 1，收到 {pack}')
    best: tuple[tuple[float, int, int, int], int, int] | None = None
    for cols in range(1, pack + 1):
        rows = _ceil_div(pack, cols)
        cell_aspect = canvas_aspect * rows / cols
        err = abs(cell_aspect / source_aspect - 1.0)
        key = (round(err, 9), cols * rows - pack, abs(cols - rows), cols)
        if best is None or key < best[0]:
            best = (key, cols, rows)
    assert best is not None
    return best[1], best[2]


# ---------------------------------------------------------------- 画布档位（A3 的成本/清晰取舍）

#: 三档。**默认 ``auto`` = ``readable``**，理由是实测：
#:
#: | 每格 | token/张 | 平均认出字数 | 说明 |
#: |---|---|---|---|
#: | 400×225 | 233 | 10.5 | 现状（800×450×4） |
#: | 533×300 | 407 | 17.0 | **+62% 字数 / +75% token，边际≈1:1** |
#:
#: 边际接近 1:1 时按「能力只增不减」取清晰档：多认 62% 的字，
#: token 只多 75%，且**张数不变、冗余率不变**（每张仍 4 帧）。
#:
#: 想省钱用 ``cheap``：保留 800×450，接受字幕以外的细节读不清。
CANVAS_PROFILES: dict[str, dict] = {
    'cheap': {
        'canvases': ((800, 450),),
        'min_short_side': 0,          # 不要求 300，允许 225
        'label': '省钱（800×450，每格 400×225）',
    },
    'readable': {
        'canvases': ((1066, 600), (1280, 720), (1600, 900)),
        'min_short_side': MIN_SHORT_SIDE,
        'label': '清晰（1066×600，每格 533×300）',
    },
    'max': {
        'canvases': ((1280, 720), (1600, 900)),
        'min_short_side': 360,
        'label': '最大（1280×720，每格 640×360）',
    },
}
DEFAULT_CANVAS_PROFILE = 'readable'


def constraints_for(profile: str = DEFAULT_CANVAS_PROFILE) -> 'Constraints':
    """按档位名生成 Constraints。未知档位抛 ValueError（不静默降级）。"""
    if profile not in CANVAS_PROFILES:
        raise ValueError(f'未知画布档位 {profile!r}，可选: {sorted(CANVAS_PROFILES)}')
    p = CANVAS_PROFILES[profile]
    return Constraints(min_short_side=p['min_short_side'], canvases=p['canvases'])


# ---------------------------------------------------------------- 约束 / 候选

@dataclass(frozen=True)
class Constraints:
    """出图策略的约束。默认值 = 任务书 §边界 写死的那一套。"""

    min_short_side: int = MIN_SHORT_SIDE
    canvases: tuple[tuple[int, int], ...] = CANVAS_LADDER
    frame_counts: tuple[int, ...] = FRAME_COUNTS
    source_aspect: float = SRC_ASPECT
    #: 允许的格子比例误差。**不能写 0**：阶梯上的 1066×600 并不是精确 16:9
    #: （精确值 1066.67×600），格子比例误差 0.06% —— 肉眼不可辨，但 0 容差会把它判红。
    #: 2% 远小于任何一个「真的变形」的组合（pack=2 是 50%，pack=8 是 50%），判别力不受影响。
    aspect_tolerance: float = 0.02
    baseline_canvas: tuple[int, int] = BASELINE_CANVAS
    baseline_pack: int = BASELINE_PACK
    #: A3-3 的绝对锚点（真实番剧 pack4 = 0.509）。None = 不比对绝对值，只做同口径相对比较。
    baseline_distinct_frac: float | None = BASELINE_DISTINCT_FRAC


@dataclass(frozen=True)
class Candidate:
    """一个「画布 × 每张帧数」组合。"""

    canvas: tuple[int, int]
    pack: int
    cols: int
    rows: int
    cell_w: float
    cell_h: float
    short_side: float
    aspect_error: float
    tokens_per_sheet: int
    ok_short_side: bool
    ok_aspect: bool
    ok_distinct: bool
    distinct_frac: float | None = None
    distinct_ref: float | None = None
    distinct_verified: bool = False
    reasons: tuple[str, ...] = ()
    #: 名义格短边（画布÷格子）。`short_side` 是**送进模型的实际**短边（可能被 1024 上限缩小）。
    nominal_short_side: float = 0.0
    #: 实际尺寸是否被 token 上限缩过（缩过就意味着「画布报的数字」不等于「模型看到的」）
    shrunk: bool = False
    #: 这个候选所用的短边阈值（来自它的 Constraints.min_short_side）。
    #: 存在这里是因为 `accept_line` 必须按**候选自己的约束**判定，
    #: 而很多调用方手上只有 Candidate、没有 Constraints。
    short_side_min: float = float(MIN_SHORT_SIDE)
    effective_size: tuple[int, int] = (0, 0)

    # --- 成本（依赖 K，所以是方法而不是字段）
    def sheets(self, n_frames: int) -> int:
        return _ceil_div(max(0, n_frames), self.pack)

    def total_tokens(self, n_frames: int) -> int:
        return self.sheets(n_frames) * self.tokens_per_sheet

    def tokens_per_frame(self) -> float:
        return self.tokens_per_sheet / self.pack

    @property
    def cell(self) -> tuple[int, int]:
        """画布÷格子的**名义**格子尺寸（未考虑 token 上限缩小）。"""
        return int(round(self.cell_w)), int(round(self.cell_h))

    def as_dict(self) -> dict[str, Any]:
        return {
            'canvas': list(self.canvas),
            'pack': self.pack,
            'grid': f'{self.cols}x{self.rows}',
            'empty_cells': self.cols * self.rows - self.pack,
            'cell': list(self.cell),
            'short_side': round(self.short_side, 1),
            'nominal_short_side': round(self.nominal_short_side, 1),
            'effective_size': list(self.effective_size),
            'shrunk_by_token_cap': self.shrunk,
            'aspect_error': round(self.aspect_error, 4),
            'tokens_per_sheet': self.tokens_per_sheet,
            'tokens_per_frame': round(self.tokens_per_frame(), 2),
            'ok_short_side': self.ok_short_side,
            'ok_aspect': self.ok_aspect,
            'ok_distinct': self.ok_distinct,
            'ok': self.ok,
            'distinct_frac': self.distinct_frac,
            'distinct_ref': self.distinct_ref,
            'distinct_verified': self.distinct_verified,
            'reasons': list(self.reasons),
        }

    @property
    def ok(self) -> bool:
        """可行性 = 几何（短边/比例）+ A3-3 单调性推理 三条同时成立。

`ok_distinct` 为 False 一律不可行。注意：整除基准 pack 但**没有实测值**的 pack
（如 pack=2）在这里会按「分组是子集 ⟹ distinct_frac 不降」的推理算通过，
同时 `distinct_verified` 保持 False、`Plan.notes` 里挂 UNVER ——
**验收门必须要求 `Plan.distinct_verified` 才准打 PASS**，不许只看 `ok`。"""
        return self.ok_short_side and self.ok_aspect and self.ok_distinct


def _distinct_frac(X, pack: int) -> float | None:
    """用冻结口径算「拼图内真不同占比」。只读引用 metrics.py，不改它。"""
    if X is None:
        return None
    from . import metrics  # 只读：metrics.py 是冻结口径
    return float(metrics.redundancy(X, pack)['distinct_frac'])


def make_candidate(canvas: tuple[int, int], pack: int, constraints: Constraints,
                   X=None, distinct_table: Mapping[int, float] | None = None) -> Candidate:
    """构造一个候选。X 给定时（ndarray n×D）现算 distinct_frac；否则查 distinct_table。"""
    w, h = canvas
    cols, rows = grid_for(pack, w / h, constraints.source_aspect)
    cell_w = w / cols
    cell_h = h / rows
    nominal_short = min(cell_w, cell_h)
    # **可读性必须按送进模型的实际尺寸量**：超过 1024 token 的画布会被宿主按比例缩小，
    # 名义 2136×1202 的 16 格拼图实际只有 1708×952（每格 427×238 < 300）。
    eff_w, eff_h = effective_size(w, h)
    eff_short = min(eff_w / cols, eff_h / rows)
    short = eff_short
    shrunk = (eff_w, eff_h) != (w, h)
    cell_aspect = cell_w / cell_h
    aspect_error = abs(cell_aspect / constraints.source_aspect - 1.0)

    ok_short = short >= constraints.min_short_side - 1e-9
    ok_aspect = aspect_error <= constraints.aspect_tolerance + 1e-9
    # A3-3 的**可证**条件：pack 变小 ⟹ 组内邻居变少 ⟹ 每帧到最近邻居的距离不减。
    # 但这只在「小组是大组的子集」时成立 —— 即 pack 必须整除现状 pack（1|4、2|4、4|4）。
    # 实测反例（bench 表 4，fanren184 前 30s）：pack=3 的 distinct_frac = 0.644
    # **低于** pack=4 的 0.656 —— 因为 3 与 4 的分组互不嵌套，那是「度量换了」而不是
    # 「帧变差了」。所以 3 这类只能标 UNVER，不许靠推理放行（R99）。
    ok_distinct = (pack <= constraints.baseline_pack
                   and constraints.baseline_pack % pack == 0)

    df = _distinct_frac(X, pack)
    verified = df is not None
    if df is None and distinct_table and pack in distinct_table:
        df = float(distinct_table[pack])
        verified = True
    ref = None
    if X is not None:
        ref = _distinct_frac(X, constraints.baseline_pack)
    elif distinct_table and constraints.baseline_pack in distinct_table:
        ref = float(distinct_table[constraints.baseline_pack])

    if ok_distinct and df is not None and ref is not None and df < ref - 1e-9:
        # 单调性在真实数据上不成立 —— 那 A3-3 就该红，不许靠推理放行（R99）
        ok_distinct = False

    reasons: list[str] = []
    if not ok_short:
        extra = (f'（画布 {w}x{h} 名义每格 {nominal_short:.0f}px，'
                 f'但被 1024 token 上限缩到 {eff_w}x{eff_h}）' if shrunk else '')
        reasons.append(f'A3-1 FAIL: 每格短边 {short:.0f}px < {constraints.min_short_side}px{extra}')
    if not ok_aspect:
        reasons.append(f'保比例 FAIL: 格子比例误差 {aspect_error:.3f} > {constraints.aspect_tolerance}')
    if not ok_distinct:
        if pack > constraints.baseline_pack:
            why = (f'pack={pack} > 现状 pack={constraints.baseline_pack}：组内邻居变多，'
                   f'distinct_frac 会降')
        else:
            why = (f'pack={pack} 不整除现状 pack={constraints.baseline_pack}：'
                   f'两组分组互不嵌套，指标不可比（UNVER）')
        reasons.append('A3-3 FAIL: ' + why
                       + (f'；实测 {df:.3f} < {ref:.3f}'
                          if (df is not None and ref is not None and df < ref - 1e-9) else ''))

    return Candidate(
        canvas=(w, h), pack=pack, cols=cols, rows=rows,
        cell_w=cell_w, cell_h=cell_h, short_side=short, aspect_error=aspect_error,
        tokens_per_sheet=image_tokens(w, h),
        ok_short_side=ok_short, ok_aspect=ok_aspect, ok_distinct=ok_distinct,
        distinct_frac=df, distinct_ref=ref, distinct_verified=verified,
        reasons=tuple(reasons),
        short_side_min=float(constraints.min_short_side),
        nominal_short_side=nominal_short, shrunk=shrunk, effective_size=(eff_w, eff_h),
    )


def enumerate_candidates(constraints: Constraints | None = None, X=None,
                         distinct_table: Mapping[int, float] | None = None
                         ) -> list[Candidate]:
    """穷举 画布阶梯 × 帧数候选 全表。"""
    c = constraints or Constraints()
    out = []
    for canvas in c.canvases:
        for pack in c.frame_counts:
            out.append(make_candidate(canvas, pack, c, X=X, distinct_table=distinct_table))
    return out


# ---------------------------------------------------------------- 画布最优化（证明阶梯已是最优）

def optimal_canvas(pack: int, constraints: Constraints | None = None,
                   max_long_side: int = 6000, empty_slack: int = 2
                   ) -> tuple[int, int, int] | None:
    """全局最优：在 **(cols, rows) 网格形状 × 画布像素** 上找 token 最小的合规画布。

    返回 `(w, h, tokens)`；找不到合规画布返回 `None`。

    **旧版的 bug（独立复核抓到的）**：旧版把画布比例写死成源比例
    （`grid_for(pack, source_aspect, source_aspect)`）。那样「零误差网格」只剩方形
    （1/4/9/16），于是漏掉了「画布比例本身不是 16:9、但每格恰好 16:9」的一整族解
    （5×2、2×5、3×2…）。后果有两个，都会把结论带偏：
      * pack 上限被误报成 **9**（真值 **10**：2800×630 的 5×2，每格 560×315 恰好 16:9）；
      * 「只要 A3-1」的最优被误报成 **98.0 token/帧**（真值 **93.80**：1050×1500 的 2×5）。
    另外旧版只按精确比例反推，与模块自己的 2% 容差判据不一致，也漏掉了
    **1050×600 / 4 帧 = 392 token**（每格 525×300，比例误差 1.56%）。

    本版按 `Constraints.aspect_tolerance` 放宽每格比例，并对每个网格形状枚举格高，
    **最后一律用 `effective_size` 核对缩水后的实际短边**（1024 token 上限会缩）。
    """
    c = constraints or Constraints()
    A = c.source_aspect
    err_lo, err_hi = 1.0 - c.aspect_tolerance, 1.0 + c.aspect_tolerance
    best: tuple[int, int, int] | None = None
    for cols in range(1, pack + 1 + empty_slack):
        for rows in range(1, pack + 1 + empty_slack):
            if not (pack <= cols * rows <= pack + empty_slack):
                continue
            # 16:9 画面下 cell_w > cell_h，所以短边就是 cell_h
            for cell_h in range(c.min_short_side, c.min_short_side + 1200):
                w_lo = math.ceil(cell_h * A * err_lo)
                w_hi = math.floor(cell_h * A * err_hi)
                if w_hi < w_lo or min(w_lo, cell_h) < c.min_short_side:
                    continue
                rail = {w_lo, w_hi, (w_lo + w_hi) // 2}
                for cell_w in rail:
                    W, H = cell_w * cols, cell_h * rows
                    if W > max_long_side or H > max_long_side:
                        continue
                    ew, eh = effective_size(W, H)          # 缩水后才是模型看到的
                    if min(ew / cols, eh / rows) < c.min_short_side - 1e-9:
                        continue
                    if abs((cell_w / cell_h) / A - 1.0) > c.aspect_tolerance + 1e-9:
                        continue
                    t = image_tokens(W, H)
                    if best is None or t < best[2] or (t == best[2] and W * H < best[0] * best[1]):
                        best = (W, H, t)
    return best


# ---------------------------------------------------------------- 顶层 plan

@dataclass(frozen=True)
class Plan:
    n_frames: int
    constraints: Constraints
    recommended: Candidate | None
    baseline: Candidate | None
    candidates: tuple[Candidate, ...] = ()
    ok: bool = False
    distinct_verified: bool = False
    notes: tuple[str, ...] = ()

    # --- 成本对照（A3-2）
    @property
    def sheets(self) -> int:
        return self.recommended.sheets(self.n_frames) if self.recommended else 0

    @property
    def total_tokens(self) -> int:
        return self.recommended.total_tokens(self.n_frames) if self.recommended else 0

    @property
    def baseline_sheets(self) -> int:
        return self.baseline.sheets(self.n_frames) if self.baseline else 0

    @property
    def baseline_tokens(self) -> int:
        return self.baseline.total_tokens(self.n_frames) if self.baseline else 0

    @property
    def token_delta_frac(self) -> float | None:
        """相对现状的总 token 增减（+0.78 = 多 78%）。"""
        if not self.recommended or not self.baseline or not self.baseline_tokens:
            return None
        return self.total_tokens / self.baseline_tokens - 1.0

    @property
    def sheet_delta(self) -> int:
        return self.sheets - self.baseline_sheets

    def as_dict(self) -> dict[str, Any]:
        return {
            'n_frames': self.n_frames,
            'ok': self.ok,
            'distinct_verified': self.distinct_verified,
            'recommended': self.recommended.as_dict() if self.recommended else None,
            'baseline': self.baseline.as_dict() if self.baseline else None,
            'cost': {
                'sheets': self.sheets,
                'total_tokens': self.total_tokens,
                'baseline_sheets': self.baseline_sheets,
                'baseline_tokens': self.baseline_tokens,
                'sheet_delta': self.sheet_delta,
                'token_delta_frac': (round(self.token_delta_frac, 4)
                                     if self.token_delta_frac is not None else None),
            },
            'notes': list(self.notes),
        }

    def feasible_table(self) -> list[dict[str, Any]]:
        return [c.as_dict() for c in self.candidates]


def plan(frames, constraints: Constraints | None = None,
         distinct_table: Mapping[int, float] | None = None) -> Plan:
    """给定关键帧，产出出图计划。

    :param frames: 关键帧个数（int）或帧矩阵（ndarray，形状 (n, D)）。
                   ndarray 时顺带用冻结口径现算 distinct_frac。
    :param constraints: 见 `Constraints`。
    :param distinct_table: 只有 int 输入时用的 {pack: distinct_frac} 查表（来源必须写明）。
    """
    c = constraints or Constraints()
    if hasattr(frames, 'shape'):
        n = int(frames.shape[0])
        X = frames
    else:
        n = int(frames)
        X = None

    cands = enumerate_candidates(c, X=X, distinct_table=distinct_table)
    # baseline 必须**独立构造**，不能从 cands 里找：
    # 画布档位 `readable`/`max` 的 canvases 不含 800×450，从 cands 找会得到 None，
    # 于是 baseline_tokens=0，`token_overhead_pct` 变成 +203400%（实测踩到）。
    # baseline 的语义是「现状是什么」，与本次选的档位无关。
    baseline = make_candidate(c.baseline_canvas, c.baseline_pack, c,
                              X=X, distinct_table=distinct_table)

    feasible = [x for x in cands if x.ok]
    notes: list[str] = []
    if X is None and distinct_table is None:
        notes.append('UNVER: 只有帧数没有帧矩阵 —— distinct_frac 未实测，'
                     'A3-3 只按「pack ≤ 现状 pack」的单调性推理放行。')
    recommended = None
    if feasible:
        # 总 token 优先，其次每格短边更大
        recommended = min(feasible, key=lambda x: (x.total_tokens(n), -x.short_side, x.pack))

    verified = recommended is not None and recommended.distinct_verified
    ok = recommended is not None
    if recommended is not None and not verified:
        notes.append('UNVER: 推荐组合的 distinct_frac 没有实测值，验收脚本必须拒绝打 PASS。')
    if recommended is None:
        notes.append('FAIL: 在当前约束下没有任何组合同时满足 A3-1 / 保比例 / A3-3。')

    return Plan(n_frames=n, constraints=c, recommended=recommended, baseline=baseline,
                candidates=tuple(cands), ok=ok, distinct_verified=verified,
                notes=tuple(notes))


def best_for_pack(pack: int, constraints: Constraints | None = None, X=None,
                  distinct_table: Mapping[int, float] | None = None) -> Candidate | None:
    """只看某一个帧数（如 4 帧/张）时的最优画布。"""
    c = constraints or Constraints()
    cands = [make_candidate(canvas, pack, c, X=X, distinct_table=distinct_table)
             for canvas in c.canvases]
    feasible = [x for x in cands if x.ok]
    if not feasible:
        return None
    return min(feasible, key=lambda x: (x.tokens_per_sheet, -x.short_side))


def accept_line(cand: Candidate) -> str:
    """A3-1 的验收输出行。

    **必须真的会红**（R99）：短边不达标的候选要给 FAIL，不能一律输出 PASS。
    上一版对 400×225 也输出 PASS —— 那是一个永远不会红的闸门。

    PASS 文本与任务书 §验收 要求的形态对齐：
        PASS: 4-frame sheet cell = XXXxYYY px (short side >= 300)
    验收脚本按这一行 grep。

    **判据必须与 `plan()` 的可行性判据同源**（`cand.ok_short_side` /
    `cand.ok_aspect`），否则会出现两类假绿（见 `M4-report.md` §复核）：
      ① 只看名义格子：`800×450 / 2` 名义 400×450（短边 400）→ 假 PASS，
         可是 16:9 塞进 0.89 的格子是变形（`ok_aspect=False`）；
      ② 不看缩水后的实际尺寸：`2136×1202 / 16` 名义 534×300.5 → 假 PASS，
         可是被 1024 token 上限缩到 1708×952 后实际每格只有 427×238。

    **短边阈值来自候选自己的约束，不是全局常量**（画布档位 `cheap`/`readable`/`max`
    的 `min_short_side` 不同；用全局值会让 `cheap` 档假红、或让 `readable` 档假绿）。
    """
    cw, ch = cand.cell_w, cand.cell_h
    cw_s = f'{cw:.0f}' if float(cw).is_integer() else f'{cw:.1f}'
    ch_s = f'{ch:.0f}' if float(ch).is_integer() else f'{ch:.1f}'
    head = f'{cand.pack}-frame sheet cell = {cw_s}x{ch_s} px'
    # 候选把两个判据都算好了（ok_short_side 用的是它自己的 min_short_side）
    thr = cand.short_side_min
    thr_s = f'{thr:.0f}' if float(thr).is_integer() else f'{thr:.1f}'
    if cand.ok_short_side and cand.ok_aspect:
        return f'PASS: {head} (short side >= {thr_s})'
    why: list[str] = []
    if not cand.ok_short_side:
        why.append(f'short side {cand.short_side:.0f} < {thr_s}')
    if not cand.ok_aspect:
        why.append(f'aspect error {cand.aspect_error:.3f} (画面被拉扁)')
    return f'FAIL: {head} ({" ; ".join(why)})'


# ---------------------------------------------------------------- NOTICE（Apache-2.0 归属保留）
#
# `image_tokens` / `_project_size` / `grid_for` 的算法移植自 see-now 的 src/tokens.js
# （Copyright 2026 SAOS-3624，Apache License 2.0）。原项目 NOTICE 摘要：
#
#   Copyright 2026 SAOS-3624
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#       http://www.apache.org/licenses/LICENSE-2.0
#   Unless required by applicable law or agreed to in writing, software distributed
#   under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
#   CONDITIONS OF ANY KIND, either express or implied.
#
# 来源：dsh-see-now（github.com/SAOS-3624/dsh-see-now）
#   - src/tokens.js   （token 公式：14px patch / 42px 格 / rows×(cols+1)+2 / 544² 地板 / 1024 上限）
# 另参考（**未抄代码**，只借问题框架）：src/pack.js 的 DP 装箱。
# 见 docs/status/M4-report.md §未做/降级。
