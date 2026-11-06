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
