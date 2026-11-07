"""CLI 行为测试：退出码 / 参数校验 / 帮助文本 / 不改用户文件。

**为什么退出码要单独测**：脚本流水线靠退出码分支（0/1/2/3/130）。
退出码错了，用户的重试逻辑就会静默走错分支 —— 比崩掉更难查。
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = sys.executable
SRC = str(ROOT / 'src')
#: 绝对路径 + 缺素材就跳过（理由见 test_api.py 同位置注释）
TRAILER = ROOT / 'eval/trailer.mp4'
_HAVE = TRAILER.exists()


def _run(argv, timeout=120):
    import os

    # **必须让工作区的 src 排在最前**：否则 `-m kf.cli` 可能命中
    # 已 pip 安装的那份（版本/代码都是旧的），测试就变成假绿。
    # 实测：把 __version__ 改回 0.2.0 后这条版本测试居然仍然通过 ——
    # 因为跑的是 site-packages 里的旧包。
    env = {**os.environ, 'PYTHONPATH': SRC, 'PYTHONNOUSERSITE': '1'}
    r = subprocess.run([PY, '-m', 'kf.cli', *argv], capture_output=True,
                       text=True, timeout=timeout, cwd=ROOT, env=env)
    return r.returncode, r.stdout + r.stderr


def test_missing_k_and_budget_exits_1():
    if not _HAVE:
        print('  (skip: 缺 eval/trailer.mp4)'); return
    rc, out = _run([str(TRAILER)])
    assert rc == 1, f'没给 k/budget 应退出 1（argparse 的 required 也给 2，这里接受 1/2）'
    assert 'k' in out.lower() or 'budget' in out.lower()


def test_both_k_and_budget_exits_1():
    if not _HAVE:
        print('  (skip: 缺 eval/trailer.mp4)'); return
    rc, out = _run([str(TRAILER), '--k', '8', '--budget', '20'])
    assert rc == 1, f'k 与 budget 同时给应退出 1，实际 {rc}'
    assert 'not allowed' in out.lower() or '只能' in out or 'mutually' in out.lower()


def test_nonexistent_video_exits_2():
    rc, out = _run(['/tmp/definitely-missing-video-xyz.mp4', '--k', '8'])
    assert rc == 2, f'素材不存在应退出 2，实际 {rc}'
    assert 'FAIL' in out


def test_bad_k_exits_1():
    if not _HAVE:
        print('  (skip: 缺 eval/trailer.mp4)'); return
    rc, out = _run([str(TRAILER), '--k', '0'])
    assert rc == 1, f'k=0 应退出 1，实际 {rc}'


def test_bad_balance_exits_1():
    if not _HAVE:
        print('  (skip: 缺 eval/trailer.mp4)'); return
    rc, out = _run([str(TRAILER), '--k', '8', '--balance', '1.5'])
    assert rc == 1, f'balance=1.5 应退出 1，实际 {rc}'


def test_version_is_single_source_of_truth():
    """版本号只能有一个来源，且 CLI 必须报它。

    **为什么不能"CLI 报的 == __init__.py 里写的"**：那不是测试，是同义反复 --
    CLI 本来就是从 `__init__.py` 读的，两边永远相等（实测：把版本改成 9.9.9
    这条断言照样通过）。真正会漂移的是**三个独立的位置**：
      ① `src/kf/__init__.py` 的 __version__
      ② pyproject.toml 必须声明 dynamic + attr（否则打包用的是另一个来源）
      ③ 打出来的 Wheel 元数据（只有 install 后才看得见）
    所以这里有意义的是 ②：一旦有人把版本手写进 pyproject，
    pip 装的版本就会和 CLI 报的不一致（实测踩到：Release 发了 v0.2.2，
    安装后 `--version` 仍是 0.2.0）。
    """
    import re

    rc, out = _run(['--version'])
    assert rc == 0, out
    m = re.search(r'(\d+\.\d+\.\d+)', out)
    assert m, f'--version 输出里没有版本号: {out!r}'
    v_cli = m.group(1)

    init_py = (ROOT / 'src' / 'kf' / '__init__.py').read_text()
    v_mod = re.search(r"__version__ = '([^']+)'", init_py).group(1)
    assert v_cli == v_mod

    pyproj = (ROOT / 'pyproject.toml').read_text()
    assert 'dynamic = ["version"]' in pyproj, 'pyproject 没声明 dynamic version'
    assert 'attr = "kf.__version__"' in pyproj, 'pyproject 没从包属性读版本'
    # 不许在 pyproject 里手写 version（两份来源必然漂移）
    head = pyproj.split('[tool.setuptools.dynamic]')[0]
    assert re.search(r'^version *=', head, re.M) is None, \
        'pyproject 里手写了 version，会造成两份来源漂移'


def test_help_mentions_all_knobs():
    """帮助必须真的讲到三个旋钮 —— 用户拿到的第一手文档就是它。"""
    rc, out = _run(['--help'])
    assert rc == 0
    for word in ['--k', '--budget', '--balance', '--canvas', '--sheets', '--resume']:
        assert word in out, f'--help 里没有 {word}'


def test_smoke_end_to_end(tmp_path=None):
    """最小端到端：8 帧 + 拼图 + 报告，全部产物都在。"""
    if not _HAVE:
        print('  (skip: 缺 eval/trailer.mp4)'); return
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        rc, out = _run([str(TRAILER), '--k', '8', '--out', td, '--quiet'])
        assert rc == 0, f'冒烟失败 rc={rc}\n{out}'
        assert 'PASS' in out
        d = pathlib.Path(td)
        assert (d / 'extract.json').exists(), '缺报告'
        assert (d / 'summary.md').exists(), '缺摘要'
        n_png = len(list(d.glob('frame-*.png')))
        n_sheet = len(list(d.glob('sheet-*.jpg')))
        assert n_png == 8, f'导出了 {n_png} 帧（应 8）'
        assert n_sheet == 2, f'拼了 {n_sheet} 张（8 帧 4 格应 2 张）'
        # 成功结束后**应当留下可复用的状态**（D-038）：
        # 早期实现整删，导致"跑完一次再换个 K 再跑"永远复用不了解码缓存。
        # 现在留一份 status=finished 的索引，只保制品仍在的 done 阶段。
        import json

        st = d / '.kf-state.json'
        assert st.exists(), '成功结束后没留复用索引（下次 --resume 会白跑一次解码）'
        rec = json.loads(st.read_text())
        assert rec.get('status') == 'finished', f'状态应为 finished，实际 {rec.get("status")}'
        for name, r in rec.get('stages', {}).items():
            assert r.get('state') == 'done', f'留下的阶段 {name} 不是 done'
            for a in r.get('artifacts', []):
                assert pathlib.Path(a).exists(), f'索引指向不存在的产物: {a}'


def test_input_video_not_modified():
    """**只读素材**是写在 README 里的承诺，必须可验证。"""
    import hashlib
    import tempfile

    v = ROOT / str(TRAILER)
    if not v.exists():
        print('  (skip: 缺 eval/trailer.mp4)')
        return
    before = hashlib.sha256(v.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory() as td:
        _run([str(TRAILER), '--k', '8', '--out', td, '--quiet'])
    after = hashlib.sha256(v.read_bytes()).hexdigest()
    assert before == after, '输入视频被修改了！'


def test_k40_then_k12_reports_and_leaves_only_12():
    """先 k=40 再 k=12：报告数字与目录内容都必须是 12。

    实测踩到的双重错误：
      ① `_export_frames` 用 `glob('frame-*.png')` 收产物，把上一轮的 28 张
         也算进本轮 → 报告说 40 张（**报告在撒谎**）；
      ② 上一步导致 `prune_stale` 的 keep 清单包含旧文件 → 永不清理，
         目录里留 40 个 png，用户以为这次抽了 40 帧。
    这条测试同时校验「报告说实话」和「目录说实话」两件事。
    """
    if not _HAVE:
        print('  (skip: 缺 eval/trailer.mp4)'); return
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        rc, out = _run([str(TRAILER), '--k', '40', '--out', td, '--quiet'])
        assert rc == 0, out
        n40 = len(list(pathlib.Path(td).glob('frame-*.png')))
        assert n40 == 40, f'k=40 应产出 40 帧，实际 {n40}'

        rc, out = _run([str(TRAILER), '--k', '12', '--out', td, '--quiet'])
        assert rc == 0, out
        d = pathlib.Path(td)
        n12 = len(list(d.glob('frame-*.png')))
        assert n12 == 12, f'k=12 后目录里仍有 {n12} 帧（旧文件没清理）'
        rep = json.loads((d / 'extract.json').read_text())
        assert len(rep['images']) == 12, \
            f'报告说导出了 {len(rep["images"])} 张，与实际 12 张不符'
        assert rep['selected_k'] == 12
        # 拼图数也必须跟着变（12 帧 / 4 格 = 3 张）
        assert len(rep['sheet_files']) == 3, \
            f'12 帧应拼 3 张，实际 {len(rep["sheet_files"])}'
        assert len(list(d.glob('sheet-*.jpg'))) == 3, '旧拼图没清理'


def test_sheets_generated_by_default():
    """CLI 默认就要拼图 —— 不拼图的话 token 省钱那套论证是纸上谈兵。"""
    if not _HAVE:
        print('  (skip: 缺 eval/trailer.mp4)'); return
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        rc, out = _run([str(TRAILER), '--k', '8', '--out', td, '--quiet'])
        assert rc == 0, out
        assert len(list(pathlib.Path(td).glob('sheet-*.jpg'))) == 2


def test_no_sheets_flag_disables_them():
    if not _HAVE:
        print('  (skip: 缺 eval/trailer.mp4)'); return
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        rc, out = _run([str(TRAILER), '--k', '8', '--out', td, '--no-sheets', '--quiet'])
        assert rc == 0, out
        assert len(list(pathlib.Path(td).glob('sheet-*.jpg'))) == 0
        assert len(list(pathlib.Path(td).glob('frame-*.png'))) == 8


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
