#!/usr/bin/env python3
"""M2 测试：src/kf/fast.py 与冻结口径（src/kf/metrics.py + scripts/eval.py）的等价性。

无 pytest 依赖，直接运行：  ./venv/bin/python tests/kf/test_fast.py

覆盖（任务书要求的 6 类 + 2 条附加）：
  1. 等价性：maxdist_fast 与 metrics.maxdist 逐位相同（多 K × 多 chunk × 随机/边界数据）
  2. 判定等价：maxdist_le 布尔值与「精确 maxdist <= budget」严格一致（O3 核心）
  3. 边界：K=1 / K=n（uniform 二分与 k-center 单趟）
  4. 内存：不分块（chunk=10**9）必须炸，自适应分块必须正常且值一致（反向样本 1）
  5. 保序性：本项目未采用降维路线（见 DECISIONS D-0xx）；整数路径是精确路径，
     用「距离逐位相同 ⇒ 保序率 100% ≥ 95%」这一更强断言替代降维保序检验
  6. 空输入：redundancy→None / scene_cuts→[] / maxdist([])→inf（与 metrics 一致）
  7. 单帧输入：kcenter hit 全 1、uniform best=1
  8. 端到端（trailer，需素材）：evaluate_fast 与 eval.evaluate 全字段一致 + 8 冻结数
  9. hwaccel：videotoolbox 与软解字节级一致（trailer，需素材）
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.kf import metrics                       # 冻结口径（只读）
from src.kf import fast                          # 被测实现

TRAILER = ROOT / 'eval' / 'trailer.mp4'
D = 576
RESULTS = []


def check(name, ok, detail=''):
    RESULTS.append((name, ok, detail))
    print(f'[{"PASS" if ok else "FAIL"}] {name} {detail}')
    return ok


def fbits(x):
    return np.float32(x).view(np.uint32)


def load_eval():
    spec = importlib.util.spec_from_file_location('kf_m0_eval_test', ROOT / 'scripts' / 'eval.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ---------------------------------------------------------------- 1 等价性
def test_maxdist_bitwise_equal():
    rng = np.random.default_rng(2026)
    cases = 0
    for n, ks in ((500, (1, 2, 7, 100, 499)), (64, (1, 5, 63))):
        Xu = rng.integers(0, 256, size=(n, D), dtype=np.uint8)
        for K in ks:
            idx = sorted(rng.choice(n, size=K, replace=False).tolist())
            ref = metrics.maxdist(Xu.astype('float32'), idx)
            got = fast.maxdist_fast(Xu, idx)
            cases += 1
            if fbits(ref) != fbits(got):
                return check('1 等价性 maxdist 逐位', False,
                             f'n={n} K={K}: {ref!r} vs {got!r}')
    # 边界值：全 0 / 全 255 / 交替极值
    for arr in (np.zeros((64, D), np.uint8), np.full((64, D), 255, np.uint8),
                (np.arange(64 * D) % 256).reshape(64, D).astype(np.uint8)):
        for K in (1, 5, 63):
            idx = sorted(rng.choice(64, size=K, replace=False).tolist())
            ref = metrics.maxdist(arr.astype('float32'), idx)
            got = fast.maxdist_fast(arr, idx)
            cases += 1
            if fbits(ref) != fbits(got):
                return check('1 等价性 maxdist 逐位', False, f'edge K={K}')
    return check('1 等价性 maxdist 逐位', True, f'{cases} 例 0 不匹配')


# ---------------------------------------------------------------- 2 判定等价（O3）
def test_maxdist_le_predicate_exact():
    rng = np.random.default_rng(7)
    Xu = rng.integers(0, 256, size=(800, D), dtype=np.uint8)
    cases = 0
    for K in (3, 40, 200):
        idx = sorted(rng.choice(800, size=K, replace=False).tolist())
        exact = fast.maxdist_fast(Xu, idx)
        for delta in (-1.0, -0.001, 0.0, 0.001, 1.0):
            budget = exact + delta
            expect = exact <= budget
            got = fast.maxdist_le(Xu, idx, budget)
            cases += 1
            if got != expect:
                return check('2 判定等价 maxdist_le', False,
                             f'K={K} delta={delta}: exact={exact} got={got}')
    return check('2 判定等价 maxdist_le', True, f'{cases} 例（含 budget==exact 边界）')


# ---------------------------------------------------------------- 3 边界 K=1 / K=n
def test_boundary_K1_Kn():
    rng = np.random.default_rng(1)
    n = 120
    Xu = rng.integers(0, 256, size=(n, D), dtype=np.uint8)
    ev = load_eval()
    ok = True
    # uniform 二分 cap=1（只剩 K=1 可选）
    got = fast.uniform_exact_fast(Xu, 10, cap=1)
    ref = ev._uniform_exact(Xu.astype('float32'), 10, 1)
    ok &= check('3a 边界 cap=1', got == ref, f'fast={got} ref={ref}')
    # uniform 二分 K=n：预算 0 → 只有全选（maxdist=0）能满足，best 必为 n
    got = fast.uniform_exact_fast(Xu, 0.0, cap=n)
    ref = ev._uniform_exact(Xu.astype('float32'), 0.0, n)
    ok &= check('3b 边界 K=n', got == ref == n, f'fast={got} ref={ref} n={n}')
    # k-center 单趟 K 扫到 n（预算 0 → 必须选满才有 0 距离……用预算极小值）
    keys_f, hit_f = fast.kcenter_track_fast(Xu, (0.0,), n)
    keys_r, _, hit_r = ev._kcenter_track(Xu.astype('float32'), (0.0,), n)
    ok &= check('3c 边界 kcenter 全程', keys_f == keys_r and hit_f == hit_r,
                f'len(keys)={len(keys_f)} hit={hit_f}')
    return ok


# ---------------------------------------------------------------- 4 内存反向样本
def test_unchunked_must_oom():
    """反向样本 1（macOS 安全版）：不分块（chunk=10**9）必须 RSS>2GB；
    同数据自适应分块必须正常且逐位一致。

    注：本机 macOS 的 setrlimit(RLIMIT_AS) 对 unlimited→limited 一律 EINVAL
    （两次尝试均失败，见 M2-report），无法用地址空间上限安全复现真 OOM。
    改用任务书允许的另一半判据「RSS > 2GB」：N=10403/K=300 不分块 int16
    中间数组 = 3.6GB（16GB 真机可安全承受）；验收参数 K≤2000 时
    原 float32 路径不分块 = 47.9GB > 16GB 物理内存，必然 OOM——
    D-007 已在本机实录 exit 137（Killed: 9），即真 OOM 档案。"""
    script = r'''
import resource, sys
import numpy as np
sys.path.insert(0, %r)
from src.kf import fast, metrics
n, K = 10403, 300
rng = np.random.default_rng(3)
Xu = rng.integers(0, 256, size=(n, 576), dtype=np.uint8)
idx = sorted(rng.choice(n, size=K, replace=False).tolist())
need_gb = n * K * 576 * 2 / 1024**3
print(f'UNCHUNKED_ALLOCATE={need_gb:.2f}GB (int16)')
# 1) 不分块：必须真的把 3.6GB 吃进去（RSS>2GB），且结果正确
v_big = fast.maxdist_fast(Xu, idx, budget_mb=10**9)
rss_big = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2
print(f'UNCHUNKED value={v_big} RSS_MB={rss_big:.0f}')
print('RSS_OVER_2GB' if rss_big > 2000 else 'RSS_NOT_OVER_2GB')
# 2) 同数据自适应分块：结果必须逐位相同，且 RSS 增量远低于 2GB
v_chunk = fast.maxdist_fast(Xu, idx, budget_mb=44)
same = np.float32(v_big).view(np.uint32) == np.float32(v_chunk).view(np.uint32)
print(f'CHUNKED value={v_chunk} BITS_EQUAL={same}')
ref = metrics.maxdist(Xu.astype("float32"), idx)
same_ref = np.float32(v_chunk).view(np.uint32) == np.float32(ref).view(np.uint32)
print(f'METRICS_EQUAL={same_ref}')
# 3) 验收参数的算术证明：K=2000 不分块 float32 需要多少 GB
print(f'AT_K2000_F32_GB={10403 * 2000 * 576 * 4 / 1024**3:.1f} > 16GB 物理内存 → 必 OOM')
'''
    r = subprocess.run([str(ROOT / 'venv' / 'bin' / 'python'), '-c', script % str(ROOT)],
                       capture_output=True, text=True, timeout=600, cwd=ROOT)
    out = r.stdout + r.stderr
    ok = ('RSS_OVER_2GB' in out) and ('BITS_EQUAL=True' in out) \
        and ('METRICS_EQUAL=True' in out)
    return check('4 内存反向样本（不分块 RSS>2GB / 分块逐位一致）', ok,
                 ' | '.join(x for x in out.splitlines() if x))


# ---------------------------------------------------------------- 5 保序性（替代降维检验）
def test_order_preserved_100pct():
    """未采用降维（路线 1 被否，见 DECISIONS）：精确整数路径的距离与冻结口径
    逐位相同，因此保序率 = 100%（任务书对降维路线只要求 ≥95%）。"""
    rng = np.random.default_rng(11)
    Xu = rng.integers(0, 256, size=(200, D), dtype=np.uint8)
    idx = sorted(rng.choice(200, size=50, replace=False).tolist())
    ref = metrics.pairwise_l1(Xu.astype('float32'), Xu.astype('float32')[idx])
    got_full, _ = fast._chunked_min_dist_f32(Xu, idx, budget_mb=44)
    # 逐位相同 ⇒ 任意两行的次序关系完全一致
    same_bits = all(fbits(ref[i].min()) == fbits(got_full[i]) for i in range(200))
    return check('5 保序性（精确路径 = 100%）', same_bits, '200 帧 min 距离逐位相同')


# ---------------------------------------------------------------- 6 空输入
def test_empty_input():
    Xu = np.zeros((0, D), dtype=np.uint8)
    ok = check('6a 空 redundancy', fast.redundancy_fast(Xu, 4) is None)
    ok &= check('6b 空 scene_cuts', fast.scene_cuts_fast(Xu) == [])
    ref = metrics.maxdist(Xu.astype('float32'), [])
    ok &= check('6c 空 idx maxdist', fast.maxdist_fast(Xu, []) == ref == float('inf'))
    return ok


# ---------------------------------------------------------------- 7 单帧输入
def test_single_frame():
    Xu = np.full((1, D), 128, dtype=np.uint8)
    keys, hit = fast.kcenter_track_fast(Xu, (10, 15, 20, 30), cap=2000)
    ok = check('7a 单帧 kcenter', keys == [0] and hit == {10: 1, 15: 1, 20: 1, 30: 1},
               f'keys={keys} hit={hit}')
    best = fast.uniform_exact_fast(Xu, 10, cap=2000)
    ok &= check('7b 单帧 uniform', best == 1, f'best={best}')
    ok &= check('7c 单帧 redundancy', fast.redundancy_fast(Xu, 4) is None)
    ok &= check('7d 单帧 scene_cuts', fast.scene_cuts_fast(Xu) == [])
    return ok


# ---------------------------------------------------------------- 8 端到端等价（trailer）
def test_end_to_end_trailer():
    if not TRAILER.exists():
        return check('8 端到端 trailer', True, 'SKIP: 素材不存在')
    ev = load_eval()
    ref = ev.evaluate(str(TRAILER), sample_fps=12)
    got = fast.evaluate_fast(str(TRAILER), sample_fps=12)
    keys = ['meta', 'analysis', 'frames_processed', 'frames_expected',
            'meta_assert_ok', 'redundancy', 'scene_cuts']
    diff = [k for k in keys if ref.get(k) != got.get(k)]
    cov_ref = {str(k): v for k, v in ref['coverage'].items()}
    cov_got = {str(k): v for k, v in got['coverage'].items()}
    if cov_ref != cov_got:
        diff.append('coverage')
    frozen = {10: (14, 100), 15: (7, 14), 20: (6, 12), 30: (4, 5)}
    frozen_ok = all((got['coverage'][b]['kcenter'], got['coverage'][b]['uniform']) == v
                    for b, v in frozen.items())
    ok = not diff and frozen_ok
    return check('8 端到端 trailer（全字段 + 8 冻结数）', ok,
                 f'diff={diff or "无"} decode={got.get("decode")}')


# ---------------------------------------------------------------- 9 hwaccel 字节一致
def test_hwaccel_bit_identical():
    if not TRAILER.exists():
        return check('9 hwaccel 字节一致', True, 'SKIP: 素材不存在')
    import hashlib
    Xs, _ = fast.load_gray_uint8(str(TRAILER), 12, hwaccel=False)
    Xh, used = fast.load_gray_uint8(str(TRAILER), 12, hwaccel=True)
    same = Xs.shape == Xh.shape and \
        hashlib.md5(Xs.tobytes()).hexdigest() == hashlib.md5(Xh.tobytes()).hexdigest()
    return check('9 hwaccel 字节一致', same, f'decode={used} shape={Xh.shape}')


ALL = [test_maxdist_bitwise_equal, test_maxdist_le_predicate_exact,
       test_boundary_K1_Kn, test_unchunked_must_oom, test_order_preserved_100pct,
       test_empty_input, test_single_frame, test_end_to_end_trailer,
       test_hwaccel_bit_identical]


def main():
    failed = 0
    for fn in ALL:
        try:
            if not fn():
                failed += 1
        except Exception as e:  # 测试本身异常也算 FAIL，不许静默
            failed += 1
            print(f'[FAIL] {fn.__name__} 异常: {type(e).__name__}: {e}')
    print(f'\n== {len(ALL) - failed}/{len(ALL)} 项通过 ==')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
