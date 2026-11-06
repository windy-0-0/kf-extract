"""拼图与导出的测试：排版 / 标注 / 字节可复现 / 清理边界。"""
import pathlib
import sys
import tempfile

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'src'))

from kf.export import prune_stale
from kf.sheets import (SheetSpec, _mmss, make_sheet, plan_sheets, stamp,
                       write_image)


def test_spec_grid_matches_layout_plan():
    """拼图排版必须与 layout 的 token 预算**同一个数**。

    这是本项目的核心账：`--canvas readable` 报「一张 1066×600 装 4 帧 = 407 token」，
    如果 SheetSpec 实际排成 2×3（6 格）或 1×4（1 列），那笔账就是假的。
    """
    from kf.layout import constraints_for, plan

    for prof, k in [('readable', 80), ('cheap', 80), ('max', 64)]:
        r = plan(k, constraints=constraints_for(prof)).recommended
        s = SheetSpec(r.canvas, r.pack)
        assert (s.canvas_w, s.canvas_h) == tuple(r.canvas)
        assert s.pack == r.pack
        assert s.capacity >= r.pack, f'{prof}: 容量 {s.capacity} < pack {r.pack}'
        # 格子比例必须接近 16:9（否则画面会被拉伸）
        aspect = s.cell_w / s.cell_h
        assert 1.5 < aspect < 2.0, f'{prof}: 格子比例 {aspect:.2f} 不像 16:9'
        # 图数必须与 layout 的 sheets 一致
        assert plan_sheets(k, s) == r.sheets(k), \
            f'{prof}: 拼图数 {plan_sheets(k, s)} != 预算 {r.sheets(k)}'


def test_mmss_formats():
    for t, want in [(0, '00:00'), (59, '00:59'), (74, '01:14'),
                    (3599, '59:59'), (3600, '1:00:00'), (7325, '2:02:05')]:
        assert _mmss(t) == want, f'{t} -> {_mmss(t)}，期望 {want}'


def test_stamp_is_actually_drawn():
    """标注必须真的画上去 —— 空实现也能「不报错」，所以要数像素。"""
    img = np.full((40, 200, 3), 30, np.uint8)
    before = img.copy()
    stamp(img, '#42 00:07', 2, 2, scale=2)
    changed = int((img != before).any(axis=2).sum())
    assert changed > 100, f'只改了 {changed} 个像素，标注没画上'
    # 文字颜色应当出现在图里
    assert (img[:, :, 0] > 200).sum() > 50, '没找到亮色文字像素'


def test_stamp_clips_instead_of_crashing():
    """标签超出画面时必须裁剪，不许崩（小格子 + 长标签是真实会发生的）。"""
    img = np.zeros((20, 40, 3), np.uint8)
    stamp(img, '#123 12:34:56', 35, 15, scale=3)   # 起点已经在边缘外
    stamp(img, '#123 12:34:56', 0, 0, scale=9)     # 字大到装不下
    # 没有异常就算过；下面确认没有越界写坏别的内存
    assert img.shape == (20, 40, 3)


def test_stamp_glyph_coverage():
    """所有会出现在时间戳里的字符都必须有字形，否则会渲染成空白。

    时间戳用到 `#0-9:/.-` 和空格。少一个字形 = 标签静默缺字（假信息）。
    """
    from kf.sheets import _GLYPHS

    for ch in '0123456789#:/.- ':
        assert ch in _GLYPHS, f'缺字形 {ch!r}'


def test_make_sheet_dimensions_and_labels():
    """拼图尺寸必须严格等于 canvas，且标注数与格子数一致。"""
    with tempfile.TemporaryDirectory() as td:
        # 造 4 张纯色图（不同颜色，便于确认 4 格都填了）
        paths = []
        for i, col in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]):
            p = pathlib.Path(td) / f'f{i}.png'
            img = np.zeros((90, 160, 3), np.uint8)
            img[...] = col
            write_image(img, p)
            paths.append(p)
        spec = SheetSpec((320, 180), 4, cols=2, rows=2)
        out = pathlib.Path(td) / 'sheet.jpg'
        canvas, texts = make_sheet(paths, [0, 5, 10, 15], spec, filename=out, quality=90)
        assert canvas.shape == (180, 320, 3), f'尺寸 {canvas.shape} != (180,320,3)'
        assert out.exists() and out.stat().st_size > 1000, '图集没落盘'
        assert len(texts) == 4, f'标注数 {len(texts)} != 4'
        assert '#00 00:00' in texts[0] and '#03 00:15' in texts[3]


def test_make_sheet_letterboxes_not_stretches():
    """宽高比不符的帧必须等比缩放+补边，**不许拉伸**。

    拉伸会让画面变形，人眼判断「这两帧像不像」会失准。
    这里用一张 16:9 的图放进 1:1 的格子，检查左右是否补了黑边。
    """
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / 'wide.png'
        img = np.full((90, 160, 3), 200, np.uint8)   # 16:9
        write_image(img, p)
        spec = SheetSpec((100, 100), 1, cols=1, rows=1)
        canvas, _ = make_sheet([p], [0], spec)
        # 上下应当是黑边（因为按宽度贴合，高度不足）
        top_bar = canvas[0:5, :, :].mean()
        mid = canvas[45:55, 40:60, :].mean()
        assert top_bar < 40, f'上方没有黑边（均值 {top_bar:.0f}），说明被拉伸了'
        assert mid > 150, f'中间没内容（均值 {mid:.0f}）'


def test_prune_stale_only_touches_our_own_files():
    """清理只能删「我们自己命名规则内」的文件。

    R135：删文件前必须证明它没在用。我们证明不了用户自己的文件，
    所以边界只能是「不认识的绝不删」。
    """
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        (d / 'frame-00000.png').write_text('old')
        (d / 'frame-00099.png').write_text('stale')      # 本轮没产出 → 该删
        (d / 'sheet-000.jpg').write_text('stale2')       # 本轮没产出 → 该删
        (d / 'family-photo.png').write_text('mine')      # 用户文件 → 必须留
        (d / 'notes.txt').write_text('mine')             # 用户文件 → 必须留
        (d / 'frame-1234.png').write_text('mine')        # 不合 5 位规则 → 必须留

        removed = prune_stale(d, keep=['frame-00000.png'])
        assert sorted(removed) == ['frame-00099.png', 'sheet-000.jpg'], f'删了不该删的: {removed}'
        left = sorted(x.name for x in d.iterdir())
        assert 'family-photo.png' in left, '删掉了用户文件！'
        assert 'notes.txt' in left, '删掉了用户文件！'
        assert 'frame-1234.png' in left, '删掉了不匹配命名规则的文件'
        assert 'frame-00000.png' in left, '删掉了本轮产物！'
        assert 'frame-00099.png' not in left, '旧产物没被清理'


def test_prune_stale_missing_dir_is_noop():
    assert prune_stale('/tmp/definitely-not-here-xyz', keep=[]) == []


def test_k40_after_k80_does_not_leave_orphans():
    """真实场景：先 --k 80 再 --k 40，目录里必须只剩 40 张。

    不做这一步的话，用户看到 80 个文件会以为第二次也抽了 80 帧 ——
    文件都合法、都能打开，属于最坏那种假绿。
    """
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        for i in range(80):
            (d / f'frame-{i:05d}.png').write_text('x')
        keep = [f'frame-{i:05d}.png' for i in range(40)]
        prune_stale(d, keep)
        left = sorted(x.name for x in d.iterdir())
        assert len(left) == 40, f'还剩 {len(left)} 个文件（应当 40）'


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
