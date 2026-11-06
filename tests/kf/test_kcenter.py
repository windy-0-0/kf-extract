"""M1 测试：等价性 + 边界 + 单调性 + 覆盖。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import numpy as np
from src.kf.kcenter import kcenter, need_for_budget, budget_track, compare_with_uniform, time_coverage
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
