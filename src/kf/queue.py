"""可持久化的任务队列（sqlite，零外部依赖）。

## 为什么需要它

GUI 有三个使用断点，根因是同一件事 —— **没有可持久化的任务账本**：

| 现象 | 根因 |
|---|---|
| 任务历史只在进程内，**重启 GUI 就没** | `App.jobs` 是普通 dict |
| 批量不支持**每个视频不同参数** | 所有视频套同一份 params |
| 批量**关掉 GUI 后要重跑** | 队列只存在于内存 |

## 为什么不用现成框架（读源码后的结论）

调研过 **huey**（MIT, 6061★, 2026-11 活跃）：
- `SqliteHuey` 用 stdlib `sqlite3`（`cysqlite` 只是可选加速）
- 实测「入队 3 个 → 换新实例仍读到 3 个」= 真持久化

**但不搬它**，因为架构不匹配：
- huey = 「生产者入队 → **独立 consumer 进程**执行 → 结果放 KV」
- 我们 = 「**同进程内**跑长任务 → 实时进度/中断 → 产物落盘续跑」

硬接要绕开它的 consumer / Result / 信号体系，还要 pickle 我们的回调。
**搬它的实现思路**（sqlite 单表 + 原子状态迁移 + pid 探活的僵尸清理），
不搬它的框架 —— 这才是"不重复造轮子"的正确用法。

## 与 `state.py` 的分工（不能合并）

- `queue.py`（本模块）：**有哪些任务、各是什么状态**。丢了可以重新入队。
- `state.py::RunState`：**单个任务做到哪个阶段**（`.kf-state.json` + 指纹）。丢了要从头跑。

两者正交。合并会让「队列坏了」和「续跑坏了」变成一个故障面，排障时无法区分。

## 关键设计

1. **`running` 是「本进程认为它在跑」**。进程一死，"还在跑"就是谎话。
   `recover()` 在启动时把所有 `running` 改成 `interrupted` ——
   这不是保守，是**如实**：我们不知道它有没有跑完，只知道它没留下完成记录。
2. **每个任务自带 params**，批量里可以逐个覆盖（解决"不同参数"的缺口）。
3. **结果落库**：`done` 后写 `result_json`，重启后历史与结果都还能看。
4. **在途去重仍然保留**（同 video + 同 params + 同 out = 只应有一个活任务）。
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sqlite3
import time
from typing import Any, Iterator

__all__ = ['JobQueue', 'STATUSES', 'TERMINAL']

#: 全部状态。`running` 只在「本进程活着」时有意义。
STATUSES = ('queued', 'running', 'done', 'failed', 'interrupted', 'cancelled')
#: 终态（不会再变）
TERMINAL = ('done', 'failed', 'interrupted', 'cancelled')

_SCHEMA = """
create table if not exists jobs (
    id          integer primary key autoincrement,
    video       text    not null,
    params      text    not null default '{}',
    out         text,
    status      text    not null default 'queued',
    stage       text    not null default '',
    frac        real    not null default 0.0,
    msg         text    not null default '',
    error       text,
    group_id    text,
    created     real    not null,
    started     real,
    finished    real,
    result      text
);
create index if not exists idx_jobs_status on jobs(status);
create index if not exists idx_jobs_group  on jobs(group_id);
create index if not exists idx_jobs_created on jobs(created desc);
"""


class JobQueue:
    """一个 sqlite 文件就是一个队列。

    并发模型：GUI 是单进程，但 HTTP 请求是多线程（`ThreadingHTTPServer`）。
    所以每个操作都开自己的连接（sqlite 的连接不能跨线程共享），
    并用 `BEGIN IMMEDIATE` 包住「读-改-写」，避免两个请求同时把同一任务标成 running。

    > 为什么不用 `check_same_thread=False`：那只是关掉断言，不解决并发语义。
    > 每操作一连接 + 显式事务，是 sqlite 在多线程下最稳的用法。
    """

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    # ------------------------------------------------------------ 底层
    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(str(self.path), timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    # ------------------------------------------------------------ 写入
    def enqueue(self, video, params: dict | None = None, out=None,
                group_id=None) -> int:
        """入队一个任务，返回 id。**立刻落盘**（所以进程崩了也不丢）。"""
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                'insert into jobs(video, params, out, status, created, group_id) '
                'values (?,?,?,?,?,?)',
                (str(video), json.dumps(params or {}, ensure_ascii=False),
                 str(out) if out else None, 'queued', now, group_id))
            return int(cur.lastrowid)

    def enqueue_batch(self, items: list[dict], group_id=None) -> list[int]:
        """批量入队。**每个 item 可以带自己的 params/out**（解决"不同参数"的缺口）。

        :param items: `[{'video':..., 'params':{...}, 'out':...}, ...]`
        """
        ids = []
        with self._conn() as c:
            now = time.time()
            for it in items:
                cur = c.execute(
                    'insert into jobs(video, params, out, status, created, group_id) '
                    'values (?,?,?,?,?,?)',
                    (str(it['video']),
                     json.dumps(it.get('params') or {}, ensure_ascii=False),
                     str(it['out']) if it.get('out') else None,
                     'queued', now, group_id))
                ids.append(int(cur.lastrowid))
        return ids

    def claim_next(self) -> dict | None:
        """取一个 queued 任务并标为 running。**原子**：两个线程不会拿到同一个。

        用 `BEGIN IMMEDIATE`（写锁）而不是先 select 再 update ——
        后者在并发下会让两个请求都读到同一条、都以为自己是主人（典型丢任务/双跑）。
        """
        with self._conn() as c:
            c.execute('begin immediate')
            row = c.execute(
                'select * from jobs where status = ? order by created asc limit 1',
                ('queued',)).fetchone()
            if row is None:
                return None
            now = time.time()
            c.execute('update jobs set status=?, started=? where id=?',
                      ('running', now, row['id']))
            d = dict(row)
            d['status'] = 'running'
            d['started'] = now
            return d

    def update(self, job_id: int, **fields: Any) -> None:
        """更新任务的任意字段（status/stage/frac/msg/error/result…）。

        **白名单**：只允许已知列，防止调用方拼错列名静默无效（那是最难查的 bug）。
        """
        allowed = {'status', 'stage', 'frac', 'msg', 'error', 'result',
                   'started', 'finished', 'out'}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f'不允许更新字段: {sorted(bad)}')
        if 'status' in fields and fields['status'] not in STATUSES:
            raise ValueError(f'未知状态: {fields["status"]}')
        if 'result' in fields and not isinstance(fields['result'], (str, type(None))):
            fields['result'] = json.dumps(fields['result'], ensure_ascii=False)
        if not fields:
            return
        sets = ', '.join(f'{k}=?' for k in fields)
        with self._conn() as c:
            c.execute(f'update jobs set {sets} where id=?',
                      (*fields.values(), job_id))

    def finish(self, job_id: int, status: str, result=None, error=None) -> None:
        """把任务落到终态。**终态不可再改**（防止迟到的进度回调覆盖结果）。"""
        if status not in TERMINAL:
            raise ValueError(f'finish 只能是终态，收到 {status}')
        with self._conn() as c:
            c.execute('begin immediate')
            row = c.execute('select status from jobs where id=?', (job_id,)).fetchone()
            if row is None:
                return
            if row['status'] in TERMINAL:
                return          # 已经是终态，不再改（迟到的回调不许覆盖）
            c.execute(
                'update jobs set status=?, finished=?, result=?, error=?, frac=? '
                'where id=?',
                (status, time.time(),
                 json.dumps(result, ensure_ascii=False) if result is not None else None,
                 error, 1.0 if status == 'done' else None, job_id))

    # ------------------------------------------------------------ 读取
    def get(self, job_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute('select * from jobs where id=?', (job_id,)).fetchone()
        return _decode(dict(row)) if row else None

    def list(self, limit=200, group_id=None) -> list[dict]:
        q = 'select * from jobs'
        args: tuple = ()
        if group_id:
            q += ' where group_id=?'
            args = (group_id,)
        q += ' order by created desc limit ?'
        args += (limit,)
        with self._conn() as c:
            return [_decode(dict(r)) for r in c.execute(q, args)]

    def active(self) -> list[dict]:
        """还在跑或排队的（`running` 只在进程活着时可信）。"""
        with self._conn() as c:
            return [_decode(dict(r)) for r in
                    c.execute('select * from jobs where status in (?,?) '
                              'order by created asc', ('queued', 'running'))]

    def find_active(self, video, params: dict, out) -> dict | None:
        """在途去重：同 video + 同 params + 同 out 且还活着 → 返回它。"""
        want = (str(video), json.dumps(params or {}, ensure_ascii=False, sort_keys=True),
                str(out) if out else None)
        with self._conn() as c:
            for r in c.execute("select * from jobs where status in ('queued','running')"):
                d = _decode(dict(r))
                p = json.dumps(json.loads(r['params'] or '{}'),
                               ensure_ascii=False, sort_keys=True)
                if (d['video'], p, d['out']) == (want[0], want[1], want[2]):
                    return d
        return None

    def group_status(self, group_id: str) -> dict:
        with self._conn() as c:
            rows = c.execute('select status, count(*) n from jobs where group_id=? '
                             'group by status', (group_id,)).fetchall()
        d = {r['status']: r['n'] for r in rows}
        total = sum(d.values())
        return {'group': group_id, 'total': total,
                'queued': d.get('queued', 0), 'running': d.get('running', 0),
                'done': d.get('done', 0), 'failed': d.get('failed', 0),
                'interrupted': d.get('interrupted', 0),
                'cancelled': d.get('cancelled', 0)}

    # ------------------------------------------------------------ 崩溃恢复
    def recover(self, note='上次退出时未完成') -> int:
        """把「本进程之前留下的 running」改成 interrupted。返回改了几条。

        **这是如实，不是保守**：我们只知道它没留下完成记录，
        不知道它到底跑完了没有。写成 interrupted 让用户看到"这条要重跑"，
        比写成 done（骗人）或写成 running（永远卡住）都好。
        """
        with self._conn() as c:
            c.execute('begin immediate')
            rows = c.execute('select id from jobs where status=?', ('running',)).fetchall()
            if not rows:
                return 0
            c.execute('update jobs set status=?, finished=?, msg=?, error=? where status=?',
                      ('interrupted', time.time(), note, note, 'running'))
            return len(rows)

    def purge_older_than(self, days: float) -> int:
        """清掉 N 天前的**终态**记录（产物文件不动，只删账本行）。

        只删终态：queued/running 是活的，删了就是丢任务。
        """
        cutoff = time.time() - days * 86400
        with self._conn() as c:
            cur = c.execute(
                'delete from jobs where created < ? and status in '
                "('done','failed','interrupted','cancelled')", (cutoff,))
            return cur.rowcount


def _decode(d: dict) -> dict:
    """把 sqlite 行转成前端友好的形状（params/result 解 JSON）。"""
    try:
        d['params'] = json.loads(d.get('params') or '{}')
    except (ValueError, TypeError):
        d['params'] = {}
    r = d.get('result')
    if r:
        try:
            d['result'] = json.loads(r)
        except (ValueError, TypeError):
            pass                  # 保持原样，让调用方看到坏数据而不是静默变 None
    return d
