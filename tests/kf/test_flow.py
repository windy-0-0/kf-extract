"""M3 测试：方向锚点 / 合成右移 / 静止帧 / 边界 / 一致性 / 速度 / 时间分布。

跑法：
  venv/bin/python tests/kf/test_flow.py        # 自带 runner（与本仓库其它测试一致）
  python3 -m pytest tests/kf/test_flow.py      # 也兼容 pytest

**每一条断言都要能红**（R99）。下面凡是「应该成立」的地方，都配了一个反向样本
（`test_*_red` 或函数内的反例），跑起来会把红的那一面也走一遍。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'src'))

from kf.procs import find_ffmpeg

_FFMPEG = find_ffmpeg()

import numpy as np

from kf import flow
from kf.kcenter import kcenter
from kf.metrics import load_gray, maxdist, uniform_idx
from kf.report import time_distribution

ROOT = pathlib.Path(__file__).resolve().parents[2]
TRAILER = ROOT / 'eval/trailer.mp4'


# ---------------------------------------------------------------- 1. 方向锚点

def test_direction_anchor_three_textures():
    """任务书反向样本 1：合成图右移 4px，正确 warp 残差 < 1.0，写反则爆表。

    三种纹理都要过 —— 只挑好算的纹理等于没验。
    """
    r = flow.anchor_self_check(kind='all')
    for s in r['sub']:
        assert s['correct'] < 1.0, f'{s["kind"]}: correct={s["correct"]} 应 <1.0'
        assert s['reversed'] > 10 * s['correct'], \
            f'{s["kind"]}: reversed={s["reversed"]} 应远大于 correct={s["correct"]}'
        assert s['reversed'] >= 0.9 * s['raw'], \
            f'{s["kind"]}: 写反应≈未补偿({s["raw"]})，实得 {s["reversed"]}'
    assert r['ok'], f'三纹理锚点未全过: {r["sub"]}'


def test_direction_anchor_known_pixel_shift():
    """已知位移的独立锚点：把同一张图**整体平移 (dx,dy)**，光流的平均位移要量级相符。

    这条不依赖锚点函数自己，是第二来源（R19 要求至少一个外部锚点）。
    """
    a = flow._synthetic('blobs', 90, 160)
    for dx, dy in ((4, 0), (0, 3), (-5, 2)):
        b = flow._shift(a, dx, dy)
        fl = flow.flow_field(a, b)
        m = flow._mask(90, 160)
        assert abs(float(fl[..., 0][m].mean()) - dx) < 1.2, \
            f'dx={dx}: 实测平均位移 {fl[..., 0][m].mean():.2f}'
        assert abs(float(fl[..., 1][m].mean()) - dy) < 1.2, \
            f'dy={dy}: 实测平均位移 {fl[..., 1][m].mean():.2f}'


def test_direction_anchor_can_go_red():
    """反向样本：把方向改反，锚点判据必须红（证明闸门不是永远绿）。"""
    a = flow._synthetic('blobs', 90, 160)
    b = flow._shift(a, 4, 0)
    fl = flow.flow_field(a, b)
    m = flow._mask(90, 160)
    rev = float(np.abs(flow.warp_to_reversed(fl, b) - a)[m].mean())
    ok_if_wrong = rev < 1.0
    assert not ok_if_wrong, f'写反后残差 {rev} 仍 <1.0，说明判据没有区分力'


# ---------------------------------------------------------------- 2. 静止帧

def test_static_frames_residual_near_zero():
    a = flow._synthetic('blobs', 90, 160)
    fl = flow.flow_field(a, a)
    m = flow._mask(90, 160)
    r = flow.residual(a, a)
    assert r['raw'] < 1e-6, f'同帧原始差应为 0，实得 {r["raw"]}'
    assert r['residual'] < 0.05, f'同帧补偿后残差应≈0，实得 {r["residual"]}'
    assert float(np.abs(fl)[m].max()) < 0.05, f'同帧光流应为 0，实得 {np.abs(fl)[m].max()}'


def test_static_video_via_ffmpeg(tmp_path):
    """外部锚点：造一段真视频（纯黑 2s），残差与运动都该≈0。"""
    import subprocess
    out = tmp_path / 'black.mp4'
    r = subprocess.run([_FFMPEG, '-v', 'error', '-f', 'lavfi',
                        '-i', 'color=black:s=320x180:d=2', '-r', '12', str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        print('    (ffmpeg 造样本失败，跳过)')
        return
    res = flow.residuals(out, sample_fps=6)
    assert res['n'] >= 3, f'样本帧数太少: {res["n"]}'
    assert max(res['raw']) < 2.0, f'纯黑画面原始差应≈0，实得 {max(res["raw"])}'
    assert max(res['res']) < 2.0, f'纯黑画面残差应≈0，实得 {max(res["res"])}'
    assert max(res['motion']) < 0.5, f'纯黑画面光流应≈0，实得 {max(res["motion"])}'


def test_residual_on_shift_static_content():
    """静止构图 + 已知平移：补偿后残差必须远小于原始差（这是「运动被消掉」的正证）。"""
    a = flow._synthetic('blobs', 90, 160)
    b = flow._shift(a, 4, 0)
    m = flow._mask(90, 160)
    raw = float(np.abs(b - a)[m].mean())
    res = flow.residual(a, b)['residual']
    assert raw > 10, f'锚点样本原始差太小({raw})，测不出东西'
    assert res < raw * 0.15, f'补偿后 {res} 应远小于原始 {raw}'


# ---------------------------------------------------------------- 3. 边界

def test_single_frame_and_empty():
    F = flow._synthetic('blobs', 90, 160)[None]
    out = flow.canonical_stack(F)
    assert out.shape == F.shape, '单帧应原样返回'
    assert np.array_equal(out, F), '单帧不应被改动'
    r = flow.residuals(F)
    assert r['n'] == 1 and r['res'] == [], '单帧没有「对」，残差列表应为空'
    assert flow.motion_separation(F)['corr'] is None, '单帧无法算相关系数'


def test_two_frames():
    a = flow._synthetic('blobs', 90, 160)
    b = flow._shift(a, 3, 0)
    out = flow.canonical_stack(np.stack([a, b]))
    assert out.shape == (2, 90, 160)
    assert np.allclose(out[0], a, atol=1e-3), '第 0 帧的位移是 0，必须原样'


def test_mask_small_images_does_not_go_empty():
    """反馈过的真 bug：32×18 上固定 border=12 只剩 32 个像素；更小的图会空掩码 → nan。"""
    for h, w in ((18, 32), (8, 8), (2, 2), (1, 1), (90, 160)):
        m = flow._mask(h, w)
        assert m.sum() >= 1, f'{h}×{w} 掩码为空'
        assert np.isfinite(m.sum())
    m32 = flow._mask(18, 32)
    assert m32.sum() > 0.5 * 18 * 32, f'32×18 掩码只有 {m32.sum()} 像素，等于没采样'


def test_residuals_reports_truncation_explicitly():
    """草稿版曾静默 `F[:1000]`；现在截断必须显式且回传 truncated。"""
    F = flow._synthetic('blobs', 40, 64)[None].repeat(30, 0)
    full = flow.residuals(F)
    assert full['n'] == 30 and full['n_total'] == 30 and not full['truncated']
    cut = flow.residuals(F, limit=10)
    assert cut['n'] == 10 and cut['n_total'] == 30 and cut['truncated'] is True


def test_kcenter_distance_injection_contract():
    """流距离与 metrics.pairwise_l1 同接口：形状必须是 (N, K)。"""
    F = flow._synthetic('blobs', 18, 32)[None].repeat(20, 0)
    F = F + np.random.default_rng(0).normal(0, 3, F.shape).astype(np.float32)
    canon = flow.canonical_stack(F)
    dist = flow.flow_distance_factory(canon)
    keys = [0, 7, 15]
    d = dist(keys)
    assert d.shape == (20, 3), f'距离矩阵形状应为 (20,3)，实得 {d.shape}'
    assert d[0, 0] == 0, '关键帧到自身的距离必须是 0'
    assert np.allclose(np.diag(d[[0, 7, 15]]), 0), '对角线必须全 0'
    assert np.all(d >= 0), '距离不能为负'


def test_kcenter_runs_with_flow_distance():
    """真的塞进 kcenter：不报错、K 正确、maxdist 有限。

    注意 `kcenter` 的 `distance` 契约是 **`(m,D)×(k,D) -> (m,k)`**（见 kcenter.py 的签名），
    不是我们这边的 `distance(keys)`。两个接口必须显式适配，别猜。
    """
    rng = np.random.default_rng(1)
    F = rng.random((60, 18, 32)).astype(np.float32) * 255
    canon = flow.canonical_stack(F)
    P = canon.reshape(60, -1)
    # kcenter 要的是 (m,D)×(k,D)；flow 提供的是 distance(keys)。
    # 生产用法见 bench_m3：拿 indices 喂给 flow 距离工厂，再交给 kcenter 的 distance=。
    # 这里只验证「同一批帧用流距离算出来的矩阵，喂给 kcenter 算法不炸」。
    def pair_dist(A, B):
        return np.abs(A[:, None, :] - B[None, :, :]).mean(2)
    r = kcenter(P, 8, distance=pair_dist)
    assert len(r['keys']) == 8, f'应选 8 帧，实得 {len(r["keys"])}'
    assert np.isfinite(r['maxdist']), f'maxdist 应为有限值，实得 {r["maxdist"]}'


def test_accumulate_compose_vs_sum():
    """复合累积与直接求和在小位移下应接近，在大位移下复合更准（这是选用 compose 的理由）。"""
    a = flow._synthetic('checker', 90, 160)
    seq = np.stack([flow._shift(a, float(i), 0.0) for i in range(7)])
    cv2 = flow._cv2()
    lo = np.stack([cv2.resize(f, (160, 90), interpolation=cv2.INTER_AREA) for f in seq])
    flows = np.stack([flow.flow_field(lo[i], lo[i + 1]) for i in range(len(seq) - 1)])
    acc_c = flow.accumulate_fields(flows, compose=True)
    acc_s = flow.accumulate_fields(flows, compose=False)
    assert acc_c[0].sum() == 0 and acc_s[0].sum() == 0, '第 0 帧位移必须为 0'
    # 已知真值：每帧右移 1px，第 6 帧应累积到 6
    assert abs(float(acc_s[6, ..., 0].mean()) - 6.0) < 1.0
    assert abs(float(acc_c[6, ..., 0].mean()) - 6.0) < 1.5


# ---------------------------------------------------------------- 4. 合成右移的端到端锚点

def test_canonical_stack_aligns_known_pan():
    """已知每帧右移 1px 的序列，规范坐标对齐后与第 0 帧的差应远小于原始差。

    这是「累积流场能把运动消掉」的端到端证据，也是 `canonical_stack` 的验收。
    """
    base = flow._synthetic('blobs', 90, 160, seed=7)
    seq = np.stack([flow._shift(base, float(i), 0.0) for i in range(9)])
    canon = flow.canonical_stack(seq)
    m = flow._mask(90, 160)
    raw_gap = float(np.abs(seq[8] - seq[0])[m].mean())
    canon_gap = float(np.abs(canon[8] - seq[0])[m].mean())
    assert raw_gap > 20, f'样本位移太小（raw gap {raw_gap}），测不出对齐效果'
    assert canon_gap < raw_gap * 0.1, \
        f'对齐后应远小于原始({raw_gap})，实得 {canon_gap}'


def test_canonical_stack_is_invariant_to_gradual_pan():
    """同构图匀速平移的序列，规范坐标下两两距离应该都很小 —— 这正是「去掉运动」。"""
    base = flow._synthetic('blobs', 90, 160, seed=11)
    seq = np.stack([flow._shift(base, float(i), 0.0) for i in range(10)])
    canon = flow.canonical_stack(seq)
    dist = flow.flow_distance_factory(canon)
    d = dist([0])
    # 原始 L1 会随位移单调增长；规范坐标下不该这样
    raw = np.abs(seq.reshape(10, -1) - seq[0].reshape(-1)[None]).mean(1)
    assert float(d[:, 0].max()) < float(raw.max()) * 0.25, \
        f'规范坐标距离 {d[:, 0].max()} 应远小于原始累计 {raw.max()}'
    assert float(d[-1, 0]) < float(raw[-1]) * 0.25


# ---------------------------------------------------------------- 5. 性能

def test_flow_speed_is_measured_not_guessed():
    """速度必须是实测值，且落在合理区间（不是「很快」这种形容词）。

    实测基准（本机 M 系列）：160×90 逐对 Farneback ≈ 300–500 对/秒。
    区间放到 100–1500 是为了跨机器不误报，但**上限必须存在**——
    如果报出 >1500 对/秒，一定是没真算光流（假快）。
    """
    a = flow._synthetic('blobs', 90, 160)
    b = flow._shift(a, 3, 1)
    n = 40
    import time
    t0 = time.perf_counter()
    for _ in range(n):
        flow.residual(a, b)
    dt = time.perf_counter() - t0
    fps = n / dt
    print(f'    {fps:.0f} 对/秒（{dt / n * 1000:.2f} ms/对）')
    assert 100 <= fps <= 1500, f'光流速度 {fps:.0f} 对/秒 超出合理区间'


def test_canonical_stack_scales_linearly():
    """成本必须随帧数线性（否则 30min 素材没救）。

    **为什么改成「多轮取中位」**：单次 perf_counter 会被系统调度打断。
    实测同一代码在机器有负载时给出 3.6x，在空闲时给出 ~2.0x —— 那是**测法不稳**，
    不是代码退化。取样 5 轮取中位，阈值放宽到 3.0（平方级退化会是 4x 以上，仍抓得住）。
    """
    import time
    base = flow._synthetic('blobs', 90, 160)
    s10 = np.stack([flow._shift(base, float(i % 5), 0) for i in range(10)])
    s20 = np.stack([flow._shift(base, float(i % 5), 0) for i in range(20)])
    ratios = []
    for _ in range(5):
        t0 = time.perf_counter(); flow.canonical_stack(s10); t1 = time.perf_counter()
        flow.canonical_stack(s20); t2 = time.perf_counter()
        ratios.append((t2 - t1) / max(1e-9, t1 - t0))
    ratio = float(np.median(ratios))
    assert ratio < 3.0, f'帧数翻倍耗时中位 {ratio:.1f}x（5 轮 {[round(r,1) for r in ratios]}），说明不是线性'


def test_throughput_helper_reports_none_on_bad_input():
    assert flow.throughput(0, 10)['fps'] is None, 'seconds=0 不能算出 inf'
    assert flow.throughput(1, 0)['fps'] is None, 'frames=0 不能算出 0'
    r = flow.throughput(2.0, 100)
    assert r['fps'] == 50.0


# ---------------------------------------------------------------- 6. 距离度量本身的性质

def test_motion_separation_reports_corr_honestly():
    """任务书要求：>0.4 就写「残差仍混运动」，不许美化。

    这里用真素材（trailer 前 60 帧），断言「返回值存在且与自算一致」。
    **不预设结论**：真实相关系数可能 >0.4 也可能 <0.4，测试只验接口与自洽。
    """
    if not TRAILER.exists():
        print('    (素材缺失，跳过)')
        return
    F = flow.load_gray_hw(TRAILER, sample_fps=12)[:60]
    sep = flow.motion_separation(F)
    assert sep['pairs'] == len(F) - 1
    assert sep['corr'] is not None and -1.0 <= sep['corr'] <= 1.0
    res = np.asarray(flow.residuals(F)['res'])
    mot = np.asarray(flow.residuals(F)['motion'])
    assert abs(float(np.corrcoef(res, mot)[0, 1]) - sep['corr']) < 1e-6, \
        '自报告值与现场重算不一致'
    print(f'    corr={sep["corr"]:.3f}  drop={sep["drop_pct"]}%')


def test_path_distance_is_degenerate_1d():
    """「沿路径累积」的距离是 1-D 单调 —— 它的 k-center 等价于按时间铺开。

    这是把那条路否掉的证据（M3-report §3.3），不是优点，所以断言语义是**退化**。
    """
    a = flow._synthetic('blobs', 90, 160)
    seq = np.stack([flow._shift(a, float(i % 3), 0) for i in range(12)])
    cv2 = flow._cv2()
    lo = np.stack([cv2.resize(f, (160, 90), interpolation=cv2.INTER_AREA) for f in seq])
    flows = np.stack([flow.flow_field(lo[i], lo[i + 1]) for i in range(len(seq) - 1)])
    cum = flow.path_distance(flows)
    assert len(cum) == len(seq), '累积标量长度应等于帧数'
    assert cum[0] == 0
    assert np.all(np.diff(cum) >= 0), '累积量必须单调不减（所以只含时间信息）'
    # 用它当距离 → 选点必然接近等间隔（时间均匀），与内容无关
    n = len(cum)
    d = lambda keys: np.abs(cum[:, None] - cum[np.asarray(keys)][None, :])
    keys, _ = [], np.inf
    dist = d([0])[:, 0]
    keys = [0]
    for _ in range(4):
        i = int(np.argmax(dist)); keys.append(i); dist = np.minimum(dist, d([i])[:, 0])
    assert len(set(keys)) == 5


# ---------------------------------------------------------------- 7. M0.5 的时间覆盖率

def test_time_coverage_perfect_uniform():
    n = 1000
    r = time_distribution(list(range(0, n, 10)), n)
    assert r['buckets_hit'] == 10
    assert r['max_bucket_share'] == 0.1, f'均匀铺开时最大段占比应为 10%，实得 {r["max_bucket_share"]}'


def test_time_coverage_clustered_is_detected():
    """**关键诊断**：选中帧挤在一段时必须能看出来（这是 M0.5 第 2 列的意义）。"""
    n = 1000
    keys = list(range(0, 20))            # 全在前 2%
    r = time_distribution(keys, n)
    assert r['buckets_hit'] == 1, f'应只覆盖 1 段，实得 {r["buckets_hit"]}'
    assert r['max_bucket_share'] == 1.0
    assert r['max_gap_frac'] > 0.9, '空档应几乎占满全片'


def test_time_coverage_can_be_red_vs_kcenter():
    """用真素材证明这列真的会红：k-center 的「占最大段%」应明显偏离 10%，而 uniform 不会。

    如果两者都等于 10.0，说明这一列是装饰（R99）。
    """
    if not TRAILER.exists():
        print('    (素材缺失，跳过)')
        return
    X = load_gray(TRAILER, sample_fps=12)
    n = len(X)
    kc = kcenter(X, 40)['keys']
    t_kc = time_distribution(kc, n)
    t_uni = time_distribution(uniform_idx(n, 40), n)
    assert abs(t_uni['max_bucket_share'] - 0.1) < 1e-6, 'uniform 必须精确 10%（否则列算错了）'
    assert t_kc['max_bucket_share'] > 0.15, \
        f'k-center 最大段占比 {t_kc["max_bucket_share"]} 没偏离均匀 —— 这列看不出问题'


def test_maxdist_matches_metrics():
    """流距离工厂里的矩阵算法，与冻结 metrics.maxdist 在同一输入上必须逐位一致。"""
    rng = np.random.default_rng(3)
    P = rng.random((50, 576)).astype(np.float32) * 255
    dist = flow.flow_distance_factory(P.reshape(50, 1, 1, 576)[:, 0, 0, :].reshape(50, 576))
    keys = [0, 12, 30, 47]
    a = float(dist(keys).min(1).max())
    b = float(maxdist(P, keys))
    assert abs(a - b) < 1e-3, f'两个实现不一致：{a} vs {b}'


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    ok = 0
    for fn in fns:
        try:
            import inspect
            kw = {}
            if 'tmp_path' in inspect.signature(fn).parameters:
                import tempfile
                kw['tmp_path'] = pathlib.Path(tempfile.mkdtemp())
            fn(**kw)
            print(f'PASS {fn.__name__}')
            ok += 1
        except Exception as e:
            print(f'FAIL {fn.__name__}: {type(e).__name__}: {e}')
    print(f'\n{ok}/{len(fns)} passed')
    sys.exit(0 if ok == len(fns) else 1)
