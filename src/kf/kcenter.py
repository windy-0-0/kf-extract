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
    """时间覆盖率：把 n 帧均分 buckets 段，选中帧落在几段里。

    这是「按顺序理解剧情」的关键诊断：即使覆盖指标好看，
    若 80 帧全选在前 5 分钟，对叙事用途就是坏的。
    """
    if not keys:
        return {'buckets_hit': 0, 'buckets': buckets, 'ratio': 0.0}
    b = np.minimum((np.asarray(keys) * buckets) // max(1, n), buckets - 1)
    hit = len(set(b.tolist()))
    return {'buckets_hit': hit, 'buckets': buckets, 'ratio': round(hit / buckets, 3)}
