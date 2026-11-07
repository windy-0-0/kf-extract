"""M1 · k-center 关键帧抽取（正式实现）。

设计文档：docs/design/M1-kcenter.md
验收：README 里的 A1（固定 K 比 maxdist）+ A2（冗余率）

## 和 metrics.py 的关系

本模块**只消费距离**，不定义距离。默认距离 = `metrics.pairwise_l1`（冻结口径）。
M3 可以把光流距离从这里注入（`distance=` 参数），这样两边对照用的是同一套 k-center，
避免「算法不同 + 距离不同」的混淆变量。

## 为什么不是「变化筛选」

`cutAbs` 那种判据优化的是「相邻帧差多少」，运动会算成变化 → 拼图内大量重复。
k-center 优化的是「整段最长盲区」，对运动不敏感。实测差异见 docs/status/M0-baseline.md。

## 单调性（D-008）

最远优先贪心使「最大覆盖距离」对 K **单调不增**，所以「第一次满足预算的 K」
可以在同一轮迭代里直接判，不需要二分。这是本模块最快的路径。
"""
from __future__ import annotations

from typing import Callable, Iterable, Sequence

import numpy as np

from .metrics import maxdist, pairwise_l1, uniform_idx


# ---------------------------------------------------------------- 核心算法
def _as_float(X) -> np.ndarray:
    """把帧特征转成 float32。**这不是性能优化，是正确性要求。**

    实测踩到（2026-11-07，产品化当天）：
    新加的流式解码器交出的是 **uint8**（省内存），直接喂给 k-center 后
    选帧结果**全错且完全正常的样子**：

        float32 输入: keys=[0,24,40,47,55,58,69,83]  maxdist=13.86
        uint8   输入: keys=[0,24,37,43,46,48,61,68]  maxdist=39.10   ← 无声退化

    根因：`np.abs(a - b)` 在 uint8 上先做**无符号减法**再取绝对值，会回绕：
    `abs(uint8(3) - uint8(250)) = 9`，而真实差是 247。于是「差得最远的帧」
    被算成「几乎相同」，k-center 的每一步选择都建立在错的距离上。

    这个 bug 最坏的地方是它**不报错、不崩、结果看着合理**——
    只有把两条路径的数字并排放才看得见（19.1 分差 = maxdist 13.9 vs 39.1）。

    所以在入口处统一 dtype。已实测：float32 路径与冻结基线逐位一致，
    所以这不改变任何已发布的数字。
    """
    return X if X.dtype == np.float32 else np.asarray(X, dtype='float32')


def _adapt(distance, X):
    """把两种距离接口统一成 `keys -> (n, |keys|)`。

    接受：
      A) `distance(X, sel)` → (m,k)   —— metrics.pairwise_l1 的形状
      B) `distance(keys)`   → (n,k)   —— kf.flow.flow_distance_factory 的形状

    为什么要兼容两种：M3 的 flow_distance_factory 是「预计算规范坐标」，
    它只需要 key 下标就能给出距离，不必再传 X。硬性统一会逼 M3 重复算 X。
    """
    import inspect
    try:
        params = len([p for p in inspect.signature(distance).parameters.values()
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])
    except (TypeError, ValueError):
        params = 2
    if params <= 1:
        return lambda keys: np.asarray(distance(keys), dtype='float32')
    return lambda keys: np.asarray(distance(X, X[np.asarray(keys)]), dtype='float32')



def kcenter(X: np.ndarray, k: int, *, seed: int = 0,
            distance: Callable[[np.ndarray, np.ndarray], np.ndarray] | None = None,
            ) -> dict:
    """最远优先贪心：选 k 帧，使最大覆盖距离最小（2-近似）。

    Args:
        X: (n, D) 帧特征
        k: 要选几帧
        seed: 起始帧下标
        distance: (m,D)×(k,D) -> (m,k) 的距离函数；默认 L1 均值

    Returns:
        {'keys': [下标...], 'maxdist': float, 'coverage': 每帧到最近关键帧的距离}
    """
    X = _as_float(X)
    n = len(X)
    if n == 0:
        raise ValueError('X 为空')
    if k <= 0:
        raise ValueError(f'k 必须 >0，收到 {k}')
    k = min(k, n)
    dist = _adapt(distance or pairwise_l1, X)

    keys = [min(seed, n - 1)]
    d = dist([keys[0]])[:, 0]                  # (n,) 到已选集合的最近距离
    if k == 1:
        return {'keys': keys, 'maxdist': float(d.max()), 'coverage': d}

    for _ in range(k - 1):
        i = int(np.argmax(d))
        if d[i] <= 0:
            break                               # 已经覆盖到 0 距离，再选也没意义
        keys.append(i)
        d = np.minimum(d, dist([i])[:, 0])

    return {'keys': sorted(keys), 'maxdist': float(d.max()), 'coverage': d}


def kcenter_balanced(X: np.ndarray, k: int, *, fps: float = 1.0,
                     alpha: float = 0.2, seed: int = 0,
                     distance: Callable | None = None) -> dict:
    """**时间均衡版** k-center：内容覆盖 + 最大空白 双目标。

    ## 为什么需要它（实测，2026-11-07）

    纯 k-center 在番剧上会把帧高度集中，留下**极长的空白**：

    | 素材 | K | 策略 | 最大空白 | 中位间隔 |
    |---|---|---|---|---|
    | fanren184 | 160 | 均匀 | 11.0s | 10.8s |
    | fanren184 | 160 | **k-center** | **132.5s** | 2.8s |

    132 秒空白 = 模型直接跳过两分钟剧情。覆盖率指标（10/10 段）完全看不出这件事。

    ## 做法

    最远优先贪心里，每轮的评分不再是纯内容距离，而是**归一化后线性混合**：

        score = (1-alpha) * norm(内容距离) + alpha * norm(时间到最近已选帧的距离)

    - `alpha=0`   → 纯内容优先（原行为）
    - `alpha=0.2` → **推荐（2026-11-07 九样本重扫后的最优）**：覆盖代价 +1.4%、
      空白降 32%；`alpha=0.3` 的空白只多降 2 个百分点，但覆盖代价翻倍到 +3.0%（最坏段 +11%）
    - `alpha=0.5` → 空白→21.5s，覆盖降 8.1%

    ## 反直觉的实测：alpha 稍大时覆盖反而变好

    fanren184 K=160：alpha=0 时 maxdist=38.3，**alpha=0.2 时 37.9（-1.0%）**。
    原因是「纯内容优先」会被少数高差异画面重复吸引，忽略时间另一端的独特内容；
    加一点时间均衡反而选得更全面。所以这接近双赢，不是「用质量换均匀」。

    ## 为什么默认从 0.3 改成 0.2（2026-11-07）

    0.3 是**只用 6 段动画**扫出来的。加了 3 段真实素材（真人实拍 ×2、录屏 ×1）后重扫：

    | alpha | 覆盖代价中位 | 最坏段代价 | 空白降幅中位 |
    |---|---|---|---|
    | 0.1 | +0.4% | +3.1% | −18.5% |
    | **0.2** | **+1.4%** | **+5.3%** | **−32.5%** |
    | 0.3 | +3.0% | **+11.1%** | −30.6% |

    0.2 在**每一个维度**上都优于 0.3（连空白都降得更多）。所以这是纯改善，不是取舍。
    最坏段是 `liveact`（无切点连续运动）：0.3 时覆盖代价 +11.1%，0.2 时降到 +3.3%。

    Args:
        fps: 用于把帧号换算成秒（只影响报告里的 gap 秒数，不影响选帧逻辑）
        alpha: 时间均衡权重，0=原行为，0.2=推荐

    Returns:
        {'keys', 'maxdist', 'coverage', 'max_gap_sec', 'median_gap_sec', 'alpha'}
    """
    X = _as_float(X)
    n = len(X)
    if n == 0:
        raise ValueError('X 为空')
    if k <= 0:
        raise ValueError(f'k 必须 >0，收到 {k}')
    k = min(k, n)
    dist = _adapt(distance or pairwise_l1, X)

    keys = [min(seed, n - 1)]
    d = dist([keys[0]])[:, 0]
    tpos = np.arange(n, dtype='float32')
    tgap = np.abs(tpos - float(keys[0]))

    for _ in range(k - 1):
        if alpha > 0:
            dm = d.max()
            tm = tgap.max()
            dn = d / dm if dm > 0 else np.zeros_like(d)
            tn = tgap / tm if tm > 0 else np.zeros_like(tgap)
            score = (1.0 - alpha) * dn + alpha * tn
        else:
            score = d
        i = int(np.argmax(score))
        if alpha <= 0 and float(np.ravel(d[i])[0]) <= 0:
            break
        keys.append(i)
        d = np.minimum(d, dist([i])[:, 0])
        tgap = np.minimum(tgap, np.abs(tpos - float(i)))

    ks = sorted(keys)
    gaps = np.diff(ks) if len(ks) > 1 else np.array([n])
    return {
        'keys': ks,
        'maxdist': float(d.max()),
        'coverage': d,
        'max_gap_sec': round(float(gaps.max()) / max(fps, 1e-9), 1),
        'median_gap_sec': round(float(np.median(gaps)) / max(fps, 1e-9), 1),
        'alpha': alpha,
    }


def need_for_budget(X: np.ndarray, budget: float, *, cap: int | None = None,
                    distance: Callable | None = None,
                    ) -> dict | None:
    """给定最大盲区预算，最少要几帧（单趟贪心，不二分 —— D-008）。"""
    n = len(X)
    cap = n if cap is None else min(cap, n)
    dist = _adapt(distance or pairwise_l1, X)
    keys = [0]
    d = dist([0])[:, 0]
    if float(d.max()) <= budget:
        return {'k': 1, 'maxdist': float(d.max()), 'keys': keys}
    for _ in range(1, cap):
        i = int(np.argmax(d))
        keys.append(i)
        d = np.minimum(d, dist([i])[:, 0])
        if float(d.max()) <= budget:
            return {'k': len(keys), 'maxdist': float(d.max()), 'keys': sorted(keys)}
    return None                                 # 达到 cap 仍未满足


def budget_track(X: np.ndarray, budgets: Iterable[float], *, cap: int | None = None,
                 distance: Callable | None = None,
                 ) -> dict:
    """单趟跑完多个预算，记录每个预算第一次满足时的 K。

    比「每个预算各跑一遍」省 (len(budgets)-1) × O(K·N·D)。
    """
    budgets = sorted(set(budgets))
    if not budgets:
        return {}
    n = len(X)
    cap = n if cap is None else min(cap, n)
    dist = _adapt(distance or pairwise_l1, X)

    hit = {b: None for b in budgets}
    keys = [0]
    d = dist([0])[:, 0]
    m = float(d.max())
    for b in budgets:
        if hit[b] is None and m <= b:
            hit[b] = 1
    if all(hit[b] is not None for b in budgets):
        return hit

    for _ in range(1, cap):
        i = int(np.argmax(d))
        keys.append(i)
        d = np.minimum(d, dist([i])[:, 0])
        m = float(d.max())
        for b in budgets:
            if hit[b] is None and m <= b:
                hit[b] = len(keys)
        if all(hit[b] is not None for b in budgets):
            break
    return hit


# ---------------------------------------------------------------- 对照工具

def compare_with_uniform(X: np.ndarray, ks: Sequence[int],
                         distance: Callable | None = None) -> list[dict]:
    """同 K 下 k-center 与均匀采样的 maxdist 对照（A1 判据的实现）。

    返回每条：{'k', 'kcenter_maxdist', 'uniform_maxdist', 'improvement'}
    improvement = (uniform - kcenter) / uniform，越大越好
    """
    n = len(X)
    rows = []
    for k in ks:
        if k <= 0 or k > n:
            continue
        kc = kcenter(X, k, distance=distance)['maxdist']
        u = maxdist(X, uniform_idx(n, k))
        rows.append({
            'k': k,
            'kcenter_maxdist': round(kc, 3),
            'uniform_maxdist': round(u, 3),
            'improvement': round((u - kc) / u, 4) if u > 0 else None,
        })
    return rows


def time_coverage(keys: Sequence[int], n: int, buckets: int = 10) -> dict:
    """时间分布：把 n 帧均分 buckets 段，报告「占几段」**和**「分布是否均匀」。

    **只看 buckets_hit 会变成新的假绿**（实测踩到，agent 指出的）：
    10/10 人人能过（10 段里有帧就算），但「一段塞 22%、一段仅 3%」仍然是不均。
    均匀采样恒为 10.0%；k-center 实测 22–24% —— **覆盖率满分，分布却两倍不均**。

    所以必须同时报：
      - `buckets_hit`  ：占了几段（覆盖广度）
      - `max_share`    ：单段最多占多少（**分布均匀性**，理想 = 1/buckets）
      - `empty_buckets`：空段数
      - `imbalance`    ：max_share / (1/buckets)，1.0 = 完美均匀
    """
    keys = list(keys)
    if not keys:
        return {'buckets_hit': 0, 'buckets': buckets, 'ratio': 0.0,
                'max_share': 0.0, 'empty_buckets': buckets, 'imbalance': None}
    b = np.minimum((np.asarray(keys) * buckets) // max(1, n), buckets - 1)
    counts = np.bincount(b, minlength=buckets)
    hit = int((counts > 0).sum())
    mx = float(counts.max()) / len(keys)
    ideal = 1.0 / buckets
    return {
        'buckets_hit': hit,
        'buckets': buckets,
        'ratio': round(hit / buckets, 3),
        'max_share': round(mx, 3),
        'empty_buckets': int((counts == 0).sum()),
        'imbalance': round(mx / ideal, 2) if len(keys) else None,
    }
