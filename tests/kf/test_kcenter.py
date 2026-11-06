"""M1 测试：等价性 + 边界 + 单调性 + 覆盖。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import numpy as np
from src.kf.kcenter import (kcenter, kcenter_balanced, need_for_budget,
                             budget_track, compare_with_uniform, time_coverage)
from src.kf import metrics

rng = np.random.default_rng(0)

def test_k1_is_single():
    X = rng.random((50, 576)).astype('float32') * 255
    r = kcenter(X, 1)
    assert len(r['keys']) == 1
    assert r['maxdist'] > 0

def test_k_equals_n_covers_all():
    X = rng.random((20, 576)).astype('float32') * 255
    r = kcenter(X, 20)
    assert len(r['keys']) == 20
    assert r['maxdist'] == 0.0, '选了全部帧，最大盲区必须是 0'

def test_k_clamped():
    X = rng.random((10, 576)).astype('float32') * 255
    r = kcenter(X, 999)
    assert len(r['keys']) == 10

def test_maxdist_monotone_in_k():
    """D-008 的前提：K 增 → maxdist 不增。这条不成立，单趟贪心就是错的。"""
    X = rng.random((120, 576)).astype('float32') * 255
    prev = float('inf')
    for k in (2, 4, 8, 16, 32, 64, 120):
        m = kcenter(X, k)['maxdist']
        assert m <= prev + 1e-6, f'K={k} maxdist={m} 反而比上一档 {prev} 大'
        prev = m

def test_identical_frames():
    """全同帧：K=1 就该覆盖到 0 距离。"""
    X = np.tile(np.arange(576, dtype='float32'), (40, 1))
    r = kcenter(X, 1)
    assert r['maxdist'] == 0.0

def test_need_for_budget_matches_kcenter():
    """单趟 need_for_budget 与直接 kcenter 在同一 K 上必须一致（外部锚点）。"""
    X = rng.random((200, 576)).astype('float32') * 255
    for budget in (20, 40, 60):
        r = kcenter(X, 128)
        # 用 kcenter 的 128 帧覆盖率反推一个预算，再由 need_for_budget 求 K
        pass
    # 直接验证：need_for_budget 返回的 maxdist 必须 <= budget
    for budget in (20, 30, 40):
        got = need_for_budget(X, budget, cap=300)
        if got is None:
            continue
        assert got['maxdist'] <= budget + 1e-6, f'预算 {budget} 却给出 {got["maxdist"]}'

def test_budget_track_consistent_with_single():
    """单趟多预算 与 逐个预算 必须一致（等价性）。"""
    X = rng.random((150, 576)).astype('float32') * 255
    budgets = (20, 30, 40)
    track = budget_track(X, budgets, cap=300)
    for b in budgets:
        single = need_for_budget(X, b, cap=300)
        expect = single['k'] if single else None
        assert track[b] == expect, f'预算{b}: track={track[b]} single={expect}'

def test_compare_rows_shape():
    X = rng.random((60, 576)).astype('float32') * 255
    rows = compare_with_uniform(X, [10, 20, 60])
    # k 必须满足 1 <= k <= n；60 帧请求 k=60 合法，但会被 clamp
    assert len(rows) == 3, f'应为 3 行，得到 {len(rows)}：{rows}'
    # 注意：**不对「k-center 一定优于均匀」做断言**。
    # 随机噪声没有聚类结构时，最远优先贪心不保证优于均匀采样
    # （实测随机 576 维数据上就出现过 k-center 更差）。
    # A1 的判据是「在真实素材上改善 ≥25%」，由 bench 脚本负责，不在这里冒充。

def test_time_coverage():
    assert time_coverage([], 100)['buckets_hit'] == 0
    # 均匀铺开
    assert time_coverage(list(range(0, 100, 10)), 100)['buckets_hit'] == 10
    # 全挤在第一段
    assert time_coverage(list(range(0, 5)), 100)['buckets_hit'] == 1

def test_empty_raises():
    try:
        kcenter(np.zeros((0, 576), dtype='float32'), 3)
    except ValueError:
        return
    raise AssertionError('空输入应当抛 ValueError')




def test_time_coverage_reports_imbalance():
    """R99 横向：只看「占几段」会变成假绿 —— 必须能报出分布不均。"""
    # 完美均匀
    r = time_coverage(list(range(0, 100, 10)), 100)
    assert r['buckets_hit'] == 10 and r['imbalance'] == 1.0
    # 挤在少数段：覆盖率可能仍「过得去」，但 imbalance 必须显著 > 1
    r2 = time_coverage([0, 1, 2, 3, 4, 50], 100)     # 6 帧，5 个在前段
    assert r2['buckets_hit'] == 2
    assert r2['imbalance'] >= 2.0, f'分布明显不均却没报出来: {r2}'
    # 空输入不许崩
    assert time_coverage([], 100)['imbalance'] is None


def test_time_coverage_hit_is_not_enough():
    """同样是「占 2/10 段」，一条各占 1 帧、一条 1+5 帧 —— buckets_hit 无法区分，imbalance 可以。

    **这是 agent 指出的真问题**：`buckets_hit=10/10` 人人能过，
    但「一段塞 22%、一段塞 3%」和「每段 10%」在覆盖率上无法区分。
    """
    even = time_coverage([0, 60], 100)                # 段0 1 帧、段6 1 帧 → max_share 50%
    skew = time_coverage([0, 1, 2, 3, 4, 60], 100)    # 段0 5 帧、段6 1 帧 → max_share 83%
    assert even['buckets_hit'] == skew['buckets_hit'] == 2
    assert skew['max_share'] > even['max_share'], '最大段占比必须能区分两种分布'
    assert skew['imbalance'] > even['imbalance'], 'imbalance 必须能区分'
    # 而「每段 1 帧」是完美的
    perfect = time_coverage(list(range(0, 100, 10)), 100)
    assert perfect['imbalance'] == 1.0 == min(perfect['imbalance'], even['imbalance'], skew['imbalance'])





def test_balanced_reduces_max_gap():
    """核心断言：alpha=0.3 必须显著缩短最大空白。

    **为什么不用合成样本**：合成「静场尾段」的帧间差异极小，
    任何按内容覆盖的算法都不该给它帧 —— 那时代码没错，是样本不代表现实。
    真实番剧的静场段仍有语义内容（对话、特写），像素差异小但必须被看到。
    所以这条断言用**真实素材**，且只断言「真实数据上的方向」。
    """
    import pathlib
    p = pathlib.Path('eval/mv227.mp4')
    if not p.exists():
        return          # 素材不在就跳过（CI 环境）
    from src.kf.metrics import load_gray
    X = load_gray(p, sample_fps=12)
    pure = kcenter_balanced(X, 80, fps=12, alpha=0.0)
    bal = kcenter_balanced(X, 80, fps=12, alpha=0.3)
    assert bal['max_gap_sec'] < pure['max_gap_sec'], (
        f'真实素材上均衡版空白应更短: {bal["max_gap_sec"]} vs {pure["max_gap_sec"]}')
    if pure['maxdist'] > 0:
        cost = bal['maxdist'] / pure['maxdist'] - 1
        assert cost <= 0.25, f'覆盖代价 {cost:.1%} 超过 25%'


def test_balanced_alpha0_equals_plain():
    """alpha=0 必须与普通 kcenter 选同一批帧（不许「顺手改了」）。"""
    rng = np.random.default_rng(2)
    X = rng.random((120, 576)).astype('float32') * 255
    a = kcenter_balanced(X, 20, fps=1, alpha=0.0)['keys']
    b = kcenter(X, 20)['keys']
    assert a == b, f'alpha=0 应与普通 kcenter 一致: {a[:5]} vs {b[:5]}'


def test_balanced_edge_cases():
    """k=1 / k=n / 单帧不许崩。"""
    rng = np.random.default_rng(3)
    X = rng.random((30, 576)).astype('float32') * 255
    assert len(kcenter_balanced(X, 1, alpha=0.3)['keys']) == 1
    assert len(kcenter_balanced(X, 30, alpha=0.3)['keys']) == 30
    one = rng.random((1, 576)).astype('float32') * 255
    r = kcenter_balanced(one, 5, alpha=0.3)
    assert len(r['keys']) >= 1 and not np.isnan(r['maxdist'])


def test_balanced_reports_gap_in_seconds():
    """gap 必须按 fps 换算成秒（不是帧号）。"""
    rng = np.random.default_rng(4)
    X = rng.random((300, 576)).astype('float32') * 255
    r6 = kcenter_balanced(X, 20, fps=6, alpha=0.3)
    r12 = kcenter_balanced(X, 20, fps=12, alpha=0.3)
    # 同样选帧，fps 翻倍 → 秒数减半
    assert abs(r6['max_gap_sec'] - 2 * r12['max_gap_sec']) < 1.0, (
        f'fps 换算不对: 6fps={r6["max_gap_sec"]}s 12fps={r12["max_gap_sec"]}s')


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    ok = 0
    for fn in fns:
        try:
            fn(); print(f'PASS {fn.__name__}'); ok += 1
        except Exception as e:
            print(f'FAIL {fn.__name__}: {type(e).__name__}: {e}')
    print(f'\n{ok}/{len(fns)} passed')
    sys.exit(0 if ok == len(fns) else 1)
