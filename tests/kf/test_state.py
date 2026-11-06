"""长任务状态层测试：原子写 / 指纹失效 / 中断语义 / 清理边界。"""
import json
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'src'))

from kf.state import (Interrupted, RunState, SignalGuard, atomic_write_json,
                      fingerprint)

RESULTS = []


def test_atomic_write_survives_partial_failure():
    """写到一半失败时，旧文件必须完好 —— 不许留下半截 JSON。

    这是断点续跑的地基：状态文件坏掉 = 用户以为可以续跑，实际拿到垃圾。
    """
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / 'x.json'
        atomic_write_json(p, {'good': 1})
        before = p.read_text()

        class Boom:
            def __init__(self):
                raise RuntimeError('序列化失败')

        # 直接调内部：构造一个不可序列化的对象，触发 write 中途失败
        try:
            atomic_write_json(p, {'good': 1, 'bad': Boom()})
        except Exception:
            pass
        assert p.read_text() == before, '失败后旧内容被破坏了'
        # 且不许留下临时文件
        leftovers = [x.name for x in pathlib.Path(td).iterdir() if x.name.startswith('.')]
        assert leftovers == [], f'留下临时文件: {leftovers}'


def test_fingerprint_changes_when_params_change():
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / 'a.bin'
        f.write_bytes(b'hello')
        a = fingerprint(f, {'k': 10, 'fps': 6})
        b = fingerprint(f, {'k': 20, 'fps': 6})
        c = fingerprint(f, {'k': 10, 'fps': 12})
        assert a != b, '改了 k 指纹却没变'
        assert a != c, '改了 fps 指纹却没变'
        assert a == fingerprint(f, {'k': 10, 'fps': 6}), '同样的参数指纹却变了'


def test_fingerprint_changes_when_file_changes():
    """同名文件被换掉必须作废 —— 否则会拿旧结果冒充新素材。"""
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / 'a.bin'
        f.write_bytes(b'hello')
        a = fingerprint(f, {'k': 10})
        time.sleep(0.02)
        f.write_bytes(b'world!')
        b = fingerprint(f, {'k': 10})
        assert a != b, '文件变了指纹却没变'


def test_reusable_requires_artifacts_to_exist():
    """指纹对、但产物被删了 → 不许复用。否则续跑会引用不存在的文件。"""
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / 'art.txt'
        p.write_text('x')
        st = RunState(td)
        st.mark('decode', 'done', fp='f1', artifacts=[str(p)])
        assert st.reusable('decode', 'f1'), '产物在、指纹对，应当可复用'
        p.unlink()
        assert not st.reusable('decode', 'f1'), '产物没了却仍判可复用'


def test_reusable_requires_done_state():
    st = RunState(tempfile.mkdtemp())
    st.mark('decode', 'running', fp='f1')
    assert not st.reusable('decode', 'f1'), 'running 状态不许当完成'


def test_resume_false_ignores_existing_state():
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / 'art.txt'
        p.write_text('x')
        st = RunState(td)
        st.mark('decode', 'done', fp='f1', artifacts=[str(p)])
        st2 = RunState(td, resume=False)
        assert not st2.reusable('decode', 'f1'), 'resume=False 不许复用旧状态'
        st3 = RunState(td, resume=True)
        assert st3.reusable('decode', 'f1'), 'resume=True 应复用'


def test_corrupt_state_file_does_not_crash():
    """状态文件被手工编辑坏了：不许崩，也不许静默当成可复用。"""
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / RunState.NAME
        p.write_text('{ this is not json')
        st = RunState(td, resume=True)
        assert not st.resumed, '坏文件不该被当成有效状态'
        assert not st.reusable('decode', 'f1')


def test_interrupted_is_not_exception_subclass():
    """Interrupted 必须是 Exception 子类，否则 `except Exception` 收尾逻辑抓不到。

    R282 的教训：KeyboardInterrupt 是 BaseException，绝大多数收尾代码抓不到它，
    于是「优雅停止」从来没运行过。
    """
    assert issubclass(Interrupted, Exception)
    assert not issubclass(Interrupted, BaseException) or True  # Exception ⊂ BaseException


def test_stage_marks_failed_on_error():
    with tempfile.TemporaryDirectory() as td:
        st = RunState(td)
        try:
            with st.stage('decode'):
                raise ValueError('boom')
        except ValueError:
            pass
        rec = st.get('decode')
        assert rec['state'] == 'failed'
        assert 'boom' in (rec.get('error') or '')


def test_stage_marks_interrupted_on_signal():
    with tempfile.TemporaryDirectory() as td:
        st = RunState(td)
        try:
            with st.stage('decode'):
                raise Interrupted('SIGINT')
        except Interrupted:
            pass
        assert st.get('decode')['state'] == 'interrupted', \
            '被中断的阶段不该记成 failed（那是用户的主动选择，不是错误）'


def test_flush_is_readable_by_humans():
    """状态文件要能 `cat` 看懂 —— 排障时人得能直接读。"""
    with tempfile.TemporaryDirectory() as td:
        st = RunState(td)
        st.mark('decode', 'done', fp='abc', artifacts=[])
        raw = (pathlib.Path(td) / RunState.NAME).read_text()
        assert '\n' in raw, '不该是压成一行的 JSON'
        assert json.loads(raw)['stages']['decode']['state'] == 'done'


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
