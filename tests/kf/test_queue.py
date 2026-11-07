"""任务队列测试：持久化 / 原子性 / 崩溃恢复 / 去重 / 终态保护。"""
import json
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'src'))

from kf.queue import JobQueue, STATUSES, TERMINAL


def _q():
    d = tempfile.mkdtemp(prefix='kfq-')
    return JobQueue(pathlib.Path(d) / 'q.db')


def test_enqueue_persists_across_restart():
    """**核心需求**：重启后任务还在（这是"历史只在进程内"断点的解法）。"""
    d = pathlib.Path(tempfile.mkdtemp(prefix='kfq-'))
    q = JobQueue(d / 'q.db')
    i1 = q.enqueue('/v/a.mp4', {'k': 20}, out='/o/a')
    i2 = q.enqueue('/v/b.mp4', {'k': 40}, out='/o/b')
    # 换一个新实例 = 模拟 GUI 重启
    q2 = JobQueue(d / 'q.db')
    rows = q2.list()
    assert len(rows) == 2, f'重启后只剩 {len(rows)} 条'
    assert {r['video'] for r in rows} == {'/v/a.mp4', '/v/b.mp4'}
    assert q2.get(i2)['params'] == {'k': 40}, '参数没持久化'


def test_each_job_can_have_own_params():
    """**核心需求**：每个任务能带自己的参数（这是"批量不支持不同参数"的解法）。"""
    q = _q()
    ids = q.enqueue_batch([
        {'video': '/v/a.mp4', 'params': {'k': 8}, 'out': '/o/a'},
        {'video': '/v/b.mp4', 'params': {'k': 40, 'canvas': 'max'}, 'out': '/o/b'},
    ], group_id='g1')
    assert q.get(ids[0])['params'] == {'k': 8}
    assert q.get(ids[1])['params'] == {'k': 40, 'canvas': 'max'}
    assert q.get(ids[1])['out'] == '/o/b'


def test_claim_is_atomic():
    """claim 不能让两个线程拿到同一个任务（那会双跑）。"""
    q = _q()
    q.enqueue_batch([{'video': f'/v/{i}.mp4'} for i in range(3)])
    got = [q.claim_next(), q.claim_next(), q.claim_next(), q.claim_next()]
    ids = [g['id'] for g in got if g]
    assert len(ids) == 3, f'应有 3 个，实际 {len(ids)}'
    assert len(set(ids)) == 3, 'claim 拿到了重复任务'
    assert got[3] is None, '队列空了还返回任务'


def test_recover_turns_running_into_interrupted():
    """**核心需求**：崩溃后 running 必须变 interrupted，不许永远转圈。

    这是如实而不是保守：我们只知道它没留下完成记录，
    不知道跑完没有。写 running 会永远转圈，写 done 是骗人。
    """
    d = pathlib.Path(tempfile.mkdtemp(prefix='kfq-'))
    q = JobQueue(d / 'q.db')
    i = q.enqueue('/v/a.mp4', {'k': 8})
    q.update(i, status='running')
    q2 = JobQueue(d / 'q.db')
    n = q2.recover()
    assert n == 1, f'recover 应改 1 条，实际 {n}'
    rec = q2.get(i)
    assert rec['status'] == 'interrupted'
    assert '未完成' in (rec.get('error') or ''), '缺可读原因'


def test_recover_leaves_terminal_alone():
    """recover 不许动已经完成/失败的任务（那是真实结果）。"""
    q = _q()
    a = q.enqueue('/v/a.mp4'); b = q.enqueue('/v/b.mp4')
    q.finish(a, 'done', result={'x': 1})
    q.finish(b, 'failed', error='boom')
    assert q.recover() == 0
    assert q.get(a)['status'] == 'done'
    assert q.get(b)['status'] == 'failed'


def test_terminal_state_is_final():
    """迟到回调不许覆盖终态（否则结果会被后到的失败消息抹掉）。"""
    q = _q()
    i = q.enqueue('/v/a.mp4')
    q.update(i, status='running')
    q.finish(i, 'done', result={'maxdist': 3.8})
    q.finish(i, 'failed', error='迟到的失败')
    rec = q.get(i)
    assert rec['status'] == 'done', f'终态被覆盖成 {rec["status"]}'
    assert rec['result'] == {'maxdist': 3.8}
    assert rec['error'] is None


def test_finish_rejects_non_terminal():
    """finish 只能落终态 —— '把它 finish 成 running' 是无意义的调用。"""
    q = _q()
    i = q.enqueue('/v/a.mp4')
    try:
        q.finish(i, 'running')
    except ValueError:
        return
    raise AssertionError('finish 接受了非终态')


def test_update_whitelists_columns():
    """拼错列名必须报错 —— 静默无效是最难查的一类 bug。"""
    q = _q()
    i = q.enqueue('/v/a.mp4')
    for bad in ({'nope': 1}, {'status': 'weird'}):
        try:
            q.update(i, **bad)
        except ValueError:
            continue
        raise AssertionError(f'没有拦住 {bad}')


def test_find_active_only_live_jobs():
    """在途去重只认 queued/running；已完成的不该被复用。"""
    q = _q()
    assert q.find_active('/v/a.mp4', {'k': 8}, '/o') is None
    i = q.enqueue('/v/a.mp4', {'k': 8}, out='/o')
    hit = q.find_active('/v/a.mp4', {'k': 8}, '/o')
    assert hit and hit['id'] == i
    q.finish(i, 'done')
    assert q.find_active('/v/a.mp4', {'k': 8}, '/o') is None


def test_group_status_aggregates():
    q = _q()
    ids = q.enqueue_batch([{'video': f'/v/{i}.mp4'} for i in range(4)], group_id='g')
    q.finish(ids[0], 'done'); q.finish(ids[1], 'failed')
    q.update(ids[2], status='running')
    st = q.group_status('g')
    assert st['total'] == 4 and st['done'] == 1 and st['failed'] == 1
    assert st['running'] == 1 and st['queued'] == 1


def test_purge_only_terminal_and_old():
    """清理只删"旧 + 终态"：活任务删了就是丢任务。"""
    q = _q()
    old = q.enqueue('/v/old.mp4')
    live = q.enqueue('/v/live.mp4')
    q.finish(old, 'done')
    # 手动把 created 改成很久以前
    with q._conn() as c:
        c.execute('update jobs set created=? where id=?', (time.time() - 40 * 86400, old))
    n = q.purge_older_than(30)
    assert n == 1
    assert q.get(old) is None, '旧的终态应被清掉'
    assert q.get(live) is not None, '活任务被误删'


def test_db_file_is_real_sqlite():
    """落盘必须是真 sqlite（不是 pickle/json 伪装）。"""
    import sqlite3

    q = _q()
    q.enqueue('/v/a.mp4')
    con = sqlite3.connect(str(q.path))
    try:
        tables = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table'")]
        assert 'jobs' in tables
    finally:
        con.close()


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
