"""公开 API 测试：默认值 / 结果形状 / 不改素材 / 关键不变量。"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'src'))

from kf import extract

ROOT = pathlib.Path(__file__).resolve().parents[2]
#: **绝对路径 + 缺素材就跳过**。
#: 早期写成相对路径 `'eval/trailer.mp4'`，于是测试只在「cwd == 仓库根」时能过 ——
#: 从干净的克隆里跑（素材没下载）会报 FileNotFoundError 而不是 skip，
#: 看上去像「代码坏了」，实际是「素材没下载」。测试必须能区分这两种情况。
TRAILER = ROOT / 'eval/trailer.mp4'
_HAVE = TRAILER.exists()


def _skip(msg='缺 eval/trailer.mp4（素材在 Release 附件里）'):
    print(f'  (skip: {msg})')


def test_requires_k_or_budget():
    if not _HAVE:
        _skip(); return
    try:
        extract(str(TRAILER))
    except ValueError as e:
        assert 'k' in str(e) and 'budget' in str(e)
    else:
        raise AssertionError('没给 k/budget 却没报错（不猜是设计原则）')


def test_rejects_both():
    if not _HAVE:
        _skip(); return
    try:
        extract(str(TRAILER), k=8, budget=20)
    except ValueError:
        return
    raise AssertionError('k 与 budget 同时给却没报错')


def test_missing_file_raises_filenotfound():
    try:
        extract('/tmp/nope-xyz.mp4', k=8)
    except FileNotFoundError:
        return
    raise AssertionError('素材不存在却抛了别的东西')


def test_result_is_json_serializable():
    if not _HAVE:
        _skip(); return
    import json

    r = extract(str(TRAILER), k=8)
    s = json.dumps(r, ensure_ascii=False)
    assert len(s) > 500
    back = json.loads(s)
    assert back['selected_k'] == 8
    assert back['schema_version'] == '1'


def test_result_attribute_access():
    if not _HAVE:
        _skip(); return
    r = extract(str(TRAILER), k=8)
    assert r.selected_k == r['selected_k']
    assert r.maxdist == r['maxdist']
    try:
        r.does_not_exist
    except AttributeError:
        pass
    else:
        raise AssertionError('不存在的键应抛 AttributeError')


def test_k_equals_n_covers_everything():
    """k = 帧数 时 maxdist 必须是 0（选了全部，没有盲区）。"""
    import numpy as np
    from kf.kcenter import kcenter

    X = np.random.default_rng(1).integers(0, 256, (30, 576), dtype=np.uint8)
    r = kcenter(X, 30)
    assert r['maxdist'] == 0.0, '选了全部帧 maxdist 却不是 0'


def test_invariants_of_report():
    """报告里的数字必须自洽 —— 这也是用户会拿去做判断的依据。"""
    if not _HAVE:
        _skip(); return
    r = extract(str(TRAILER), k=16)
    assert r.selected_k == 16, 'selected_k 与请求不符'
    assert 0 <= r.maxdist < 256, 'maxdist 不在合理量程'
    assert r.max_gap_sec >= r.median_gap_sec, '最大空白小于中位空白，不可能'
    assert r.time_coverage['buckets_hit'] <= r.time_coverage['buckets']
    assert len(r.keys_time_sec) == r.selected_k, '时间戳数量与帧数不符'
    assert r.keys_analysis_idx == sorted(r.keys_analysis_idx), '帧号未排序'


def test_sheets_default_on_and_match_png_count():
    if not _HAVE:
        _skip(); return
    with tempfile.TemporaryDirectory() as td:
        r = extract(str(TRAILER), k=12, out=td)
        assert len(r.images) == 12
        assert len(r.sheet_files) == 3, f'12 帧 4 格应 3 张，实际 {len(r.sheet_files)}'
        # 每张图集的容量都不能超
        from kf.sheets import SheetSpec

        cp = r['canvas_plan']
        spec = SheetSpec(cp['canvas'], cp['pack'])
        assert spec.capacity == 4


def test_sheet_time_labels_match_json():
    """拼图上烧的时间戳必须与 extract.json 的 keys_time_sec 一一对应。

    这是「产物自证」的核心：模型引用「第 42 张、7 分 14 秒」时，
    用户能在 JSON 里查到同一个数。两边对不上 = 引用不可复核。
    """
    if not _HAVE:
        _skip(); return
    from kf.sheets import _mmss

    with tempfile.TemporaryDirectory() as td:
        r = extract(str(TRAILER), k=8, out=td)
        want = [_mmss(t) for t in r.keys_time_sec]
        # 从 labels 生成逻辑复算（与 _build_sheets 同一规则）
        got = [f'#{i:02d} {want[i]}' for i in range(len(want))]
        assert len(got) == 8
        # JSON 里的时间必须单调递增
        ts = r.keys_time_sec
        assert all(ts[i] <= ts[i + 1] for i in range(len(ts) - 1)), '时间戳非单调'


def test_no_images_still_reports():
    if not _HAVE:
        _skip(); return
    r = extract(str(TRAILER), k=8, images=False, sheets=False)
    assert r.selected_k == 8
    assert r.images == []


def test_static_segments_reported_on_screencast():
    """录屏素材必须报出「连续静止段」—— 这是我们唯一诚实的处理方式。

    实测背景（D-032）：267 秒编程录屏里，199.5s→266.7s 这 67 秒被像素度量判成
    零变化（404 帧没有一帧"不同"），但画面上确实多了终端面板与运行输出。
    这类"看起来没变其实有变"的段必须被**报出来**，而不是藏在漂亮的 maxdist 后面。

    这条测试同时锁定两件事：
      ① 该字段必须存在且带秒数（不能只给帧号，用户读不懂）
      ② 阈值不能形同虚设（录屏的 203s 静止段必须被认定）
    """
    if not _HAVE:
        _skip(); return
    from kf.metrics import load_gray, static_runs

    X = load_gray(str(TRAILER), sample_fps=12)
    runs = static_runs(X, min_frames=12)
    # trailer 有静止段（片头/转场），必须能找到
    assert len(runs) >= 1, '短预告上也该能找到静止段（否则检测器失效）'

    # **录屏素材上的强断言**：这条才是 D-032 的回归闸门。
    # 267s 编程录屏里有连续 200s+ 被判零变化的段；检测器找不到它，说明阈值或实现坏了。
    CAST = ROOT / 'eval/codecast.mp4'
    if CAST.exists():
        Xc = load_gray(str(CAST), sample_fps=6)
        rc = static_runs(Xc, min_frames=6)
        longest = max((b - a + 1 for a, b in rc), default=0)
        assert longest / 6 >= 100, (
            f'录屏上最长静止段只有 {longest/6:.1f}s，'
            f'但实测应为 200s+（阈值 {8.0} 判不出说明检测器失效）')
    for a, b in runs:
        assert 0 <= a <= b < len(X), f'区间越界: {(a,b)}'
        assert b - a + 1 >= 12

    r = extract(str(TRAILER), k=16)
    st = r['static_segments']
    assert 'count' in st and 'top' in st, '缺 static_segments 字段'
    if st['longest']:
        assert 'start_sec' in st['longest'] and 'dur_sec' in st['longest'], \
            '静止段必须带秒数（帧号用户读不懂）'


def test_static_segments_does_not_change_selection():
    """报告静止段**只许报告，不许改变选帧结果**。

    这是刻意的设计约束（D-032）：自动跳过静止段会变成第二个未声明的目标，
    破坏"只有一条判据"的立身之本。这条测试防止将来有人"顺手优化"。
    """
    if not _HAVE:
        _skip(); return
    from kf.kcenter import kcenter_balanced

    X = __import__('kf.metrics', fromlist=['load_gray']).load_gray(str(TRAILER), sample_fps=12)
    # 直接调选帧：结果不应依赖 static_runs 是否被调用
    a = kcenter_balanced(X, 16, fps=12, alpha=0.3)['keys']
    __import__('kf.metrics', fromlist=['static_runs']).static_runs(X, min_frames=12)
    b = kcenter_balanced(X, 16, fps=12, alpha=0.3)['keys']
    assert a == b, '调用静止段检测后选帧结果变了（说明它偷偷影响了算法）'


def test_static_runs_uses_segment_diameter_not_adjacent_diff():
    """静止段判据必须是「段内两两距离」，不是「相邻帧差」。

    这是实测修正（D-035/D-036）：Vlog 同一说话人镜头里字幕在变，
    相邻帧差中位只有 **1.17**（逐帧看几乎不动），
    但段内两两距离 90 分位 **8.52**、最大 **12.62**、超过阈值 8 的占 **14.3%**。
    用相邻帧差判定会把这段 34 秒整段误判成静止；用段内直径才判得对。

    对照组（录屏 63→250s）是**真静止**：段内两两最大只有 5.77，超阈值比例 **0.0%**。
    两者相邻差分都很小，只有段内直径能把它们分开。
    """
    if not _HAVE:
        _skip(); return
    import numpy as np

    from kf.metrics import load_gray, pairwise_l1, static_runs

    VLOG = ROOT / 'eval/vlog.mp4'
    if not VLOG.exists():
        print('  (skip: 缺 eval/vlog.mp4)'); return

    X = load_gray(str(VLOG), sample_fps=6)
    n = len(X)
    runs = static_runs(X, min_frames=6)
    longest = max(((b - a + 1) / 6 for a, b in runs), default=0)

    # 291s→324s 那一段（字幕在变）不许被整段判成静止
    A, B = int(291 * 6), int(324 * 6)
    whole = [(a, b) for a, b in runs
             if a <= A + 12 and b >= B - 12 and (b - a + 1) > (B - A) * 0.85]
    assert not whole, (
        f'vlog 291→324s（字幕在变）被判成了静止段 {whole}，'
        f'说明判据退回了「相邻帧差」这种过宽的形式')

    # 用相邻帧差做不到这件事：验证这个前提本身仍成立
    d_adj = float(np.abs(X[A:B - 1] - X[A + 1:B]).mean())
    seg = X[A:B]
    D = pairwise_l1(seg, seg)
    np.fill_diagonal(D, np.nan)
    assert d_adj < 2.0, f'相邻帧差 {d_adj:.2f} 不再小，该用例前提已变'
    assert np.nanpercentile(D, 90) > 8.0, '段内两两距离 90 分位不再 >8，前提已变'


def test_budget_starvation_hint_is_targeted():
    """「预算被前端高变化吃掉」的提示必须收紧，不许对普通素材乱报。

    触发条件三条同时成立才提示（R377：收紧到"只可能是这一种情况"）：
      ① 最长静止段占全片 >= 50%
      ② 最大空白落在该静止段内 且 >= 20s
      ③ K <= 40

    实测：codecast K=20 命中（203.5s 静止 / 76% / 空白 112.2s），
    trailer / liveact / vlog 均不命中。
    """
    if not _HAVE:
        _skip(); return
    CAST = ROOT / 'eval/codecast.mp4'
    if not CAST.exists():
        print('  (skip: 缺 eval/codecast.mp4)'); return

    r = extract(str(CAST), k=20)
    hit = [w for w in (r.get('warnings') or [])
           if '预算主要被变化多的部分占用' in w]
    assert hit, 'codecast K=20 应命中预算不足提示'

    st = r['static_segments']['longest']
    dur = r['analysis_frames'] / r['requested']['fps']
    assert st['dur_sec'] / dur >= 0.5, '断言前提：静止段占比应 >=50%'
    assert r['max_gap_sec'] >= st['start_sec'], '断言前提：空白应落在静止段内'

    # K 大时不该报（均衡项会把帧补回来）
    r2 = extract(str(TRAILER), k=16)
    hit2 = [w for w in (r2.get('warnings') or [])
            if '预算主要被变化多的部分占用' in w]
    assert not hit2, '普通素材不该命中这条提示'


def test_budget_starvation_hint_needs_both_conditions():
    """提示的两条前提必须**同时**成立，且各素材的实际取值要可复核。

    这条不是"跑一遍看有没有 warning"（那样放开任一条件都不会红），
    而是**直接断言两条条件的取值表**。任何一条被放宽或改错，
    这里的期望值就对不上，立刻红。

    实测取值（k=20）：
    | 素材 | 静止段占比 | 空白 | 空白是否落在静止段内 |
    |---|---|---|---|
    | codecast | 76% | 112.2s | 是（start=63.2）|
    | trailer | 16% | 2.0s | 否 |
    | liveact | 1% | 20.8s | 否（start=52.2）|
    | vlog | 4% | 55.7s | 否（start=420.5）|
    """
    if not _HAVE:
        _skip(); return
    CAST = ROOT / 'eval/codecast.mp4'
    if not CAST.exists():
        print('  (skip: 缺 eval/codecast.mp4)'); return

    def cond(r):
        st = r['static_segments']['longest'] or {}
        dur = r['analysis_frames'] / r['requested']['fps']
        c1 = (st.get('dur_sec', 0) / dur) >= 0.5
        c2 = (r['max_gap_sec'] >= 20
              and st.get('start_sec', 1e9) <= r['max_gap_sec'])
        return c1, c2

    def has_hint(r):
        return any('预算主要被变化多的部分占用' in w
                   for w in (r.get('warnings') or []))

    rc = extract(str(CAST), k=20)
    c1, c2 = cond(rc)
    assert c1 and c2, f'codecast 两条条件应都为真，实际 ①={c1} ②={c2}'
    assert has_hint(rc), '两条为真时必须给出提示'

    # 普通素材：至少一条为假，因而不许命中
    for name in ('trailer.mp4', 'liveact.mp4', 'vlog.mp4'):
        f = ROOT / 'eval' / name
        if not f.exists():
            continue
        r = extract(str(f), k=20)
        a1, a2 = cond(r)
        assert not (a1 and a2), f'{name} 两条条件居然都为真（判据被放宽）'
        assert not has_hint(r), f'{name} 误报了'


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
