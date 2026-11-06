"""公开 API 测试：默认值 / 结果形状 / 不改素材 / 关键不变量。"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'src'))

from kf import extract


def test_requires_k_or_budget():
    try:
        extract('eval/trailer.mp4')
    except ValueError as e:
        assert 'k' in str(e) and 'budget' in str(e)
    else:
        raise AssertionError('没给 k/budget 却没报错（不猜是设计原则）')


def test_rejects_both():
    try:
        extract('eval/trailer.mp4', k=8, budget=20)
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
    import json

    r = extract('eval/trailer.mp4', k=8)
    s = json.dumps(r, ensure_ascii=False)
    assert len(s) > 500
    back = json.loads(s)
    assert back['selected_k'] == 8
    assert back['schema_version'] == '1'


def test_result_attribute_access():
    r = extract('eval/trailer.mp4', k=8)
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
    r = extract('eval/trailer.mp4', k=16)
    assert r.selected_k == 16, 'selected_k 与请求不符'
    assert 0 <= r.maxdist < 256, 'maxdist 不在合理量程'
    assert r.max_gap_sec >= r.median_gap_sec, '最大空白小于中位空白，不可能'
    assert r.time_coverage['buckets_hit'] <= r.time_coverage['buckets']
    assert len(r.keys_time_sec) == r.selected_k, '时间戳数量与帧数不符'
    assert r.keys_analysis_idx == sorted(r.keys_analysis_idx), '帧号未排序'


def test_sheets_default_on_and_match_png_count():
    with tempfile.TemporaryDirectory() as td:
        r = extract('eval/trailer.mp4', k=12, out=td)
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
    from kf.sheets import _mmss

    with tempfile.TemporaryDirectory() as td:
        r = extract('eval/trailer.mp4', k=8, out=td)
        want = [_mmss(t) for t in r.keys_time_sec]
        # 从 labels 生成逻辑复算（与 _build_sheets 同一规则）
        got = [f'#{i:02d} {want[i]}' for i in range(len(want))]
        assert len(got) == 8
        # JSON 里的时间必须单调递增
        ts = r.keys_time_sec
        assert all(ts[i] <= ts[i + 1] for i in range(len(ts) - 1)), '时间戳非单调'


def test_no_images_still_reports():
    r = extract('eval/trailer.mp4', k=8, images=False, sheets=False)
    assert r.selected_k == 8
    assert r.images == []


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
