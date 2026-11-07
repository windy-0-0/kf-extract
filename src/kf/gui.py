"""本地 Web GUI —— 把 kf-extract 做成「打开浏览器就能用」的东西。

## 形态决策（见 DECISIONS D-037）

不用 Tk 桌面窗口，用**本地 Web 界面**，因为：
  1. 产物本来就是图片，用户最想看的是**拼图**，浏览器天生擅长图片画廊；
  2. 长任务进度有 SSE/轮询两条现成通道（Tk 要手搓线程 + after()，且 R297 禁止阻塞主线程）；
  3. `http.server` 是 stdlib，**零新依赖**。

## 安全约束（写死在代码里，不是注释里）

- **只绑 127.0.0.1**：绝不绑 0.0.0.0。`server_bind` 时断言地址是回环。
- **一次性 token**：启动时生成，URL 带上；每个请求都要校验。
  防同机其他进程/网页（CSRF 式）随便调用。
- **POST 只接受白名单参数**，且**没有任何删除端点**。
- 产物只写到用户指定目录下的 `shots/`，不接受任意路径覆写系统文件。

## 长任务怎么做（G1 的核心难点）

抽 29 分钟素材要 85 秒。GUI 必须做到：
  - **不阻塞**：请求立刻返回 `job_id`，前端轮询进度
  - **可中断**：独立的 stop 端点，且停止是安全的（不删已拿到的）
  - **不谎报**：中断的任务状态是 `interrupted`，绝不显示成完成
  - **在途去重**：同一个目录 + 同一参数重复点击，只跑一个任务
    （R283：连点 5 次只能产生 1 份结果）
"""
from __future__ import annotations

import json
import mimetypes
import os
import pathlib
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__all__ = ['serve', 'create_app']

_LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1'}


class Job:
    """一个抽取任务的内存态。

    **为什么状态在内存里而不落盘**：GUI 是短生命周期进程；
    落盘会引入「上次崩溃留下的僵尸任务」，对单机工具是净负担。
    但**产物目录里的 `.kf-state.json` 仍然会落**（那是 extract 自己的续跑机制）。
    """

    _seq = 0
    _lock = threading.Lock()

    def __init__(self, video, params):
        with Job._lock:
            Job._seq += 1
            self.id = f'j{Job._seq}-{secrets.token_hex(3)}'
        self.video = video
        self.params = params
        self.status = 'queued'      # queued|running|done|failed|interrupted
        self.stage = ''
        self.frac = 0.0
        self.msg = ''
        self.result = None
        self.error = None
        self.t0 = time.time()
        self.stop_requested = False   # 循环内检查（R299：不能只靠杀线程）
        self.thread = None
        self.key = ''                 # 在途去重用的键
        self.started = time.time()     # 历史列表要显示"几点开始的"
        self.name = pathlib.Path(video).name
        self.last_progress = time.time()   # 最后一次进度更新的时刻
        self.group = None                  # 批量组名（单个任务为 None）
        #: 队列里的持久 id（整数）。`self.id` 是运行时字符串 id，两者对应但用途不同：
        #: 前者用于数据库读写，后者用于前端 URL/去重。保留两个不合并的原因见 D-060。
        self.qid: int | None = None

    def snapshot(self) -> dict:
        return {'id': self.id, 'status': self.status, 'stage': self.stage,
                'frac': round(self.frac, 4), 'msg': self.msg,
                'elapsed': round(time.time() - self.t0, 1),
                'video': self.video, 'params': self.params,
                'error': self.error,
                # 距上次进度更新多久：前端 >10s 时要显示"仍在工作（已 N 秒未更新）"
                'stale_seconds': round(time.time() - self.last_progress, 1),
                'result': self.result if self.status == 'done' else None}

    def brief(self) -> dict:
        """历史列表用：只要够显示一行，不带 result（那可能很大）。

        **为什么单独一个 brief 而不是复用 snapshot**：历史列表可能有几十条，
        每条都带完整的 images/sheets/warnings 会让一次轮询搬运几百 KB。
        列表只需要"这是什么、跑到哪、好没好"。
        """
        return {
            'id': self.id,
            'name': self.name,
            'status': self.status,
            'stage': self.stage,
            'frac': round(self.frac, 4),
            'elapsed': round(time.time() - self.t0, 1),
            'started': round(self.started, 1),
            'params': {k: self.params.get(k) for k in ('k', 'budget', 'canvas', 'balance')},
            'has_result': self.result is not None,
            'error': self.error,
            'group': self.group,
        }

    def stale_seconds(self) -> float:
        """距离上一次有进度更新过了多久（秒）。

        **为什么需要**：29 分钟素材解码要 40 秒，纯百分比看着像卡死。
        前端要能区分「在动」和「真的没动」（R35 长静默必须自己喊出来）。
        """
        return round(time.time() - self.last_progress, 1)


class App:
    def __init__(self, root_dir=None, allow_dirs=None, queue_path=None):
        #: **本进程的活任务运行时句柄**（线程、停止标志、进度时间戳）。
        #: 持久状态在 `self.queue` 里；这里的 key 与队列 id 一致（字符串形式）。
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.token = secrets.token_hex(16)
        self.root = pathlib.Path(root_dir or os.getcwd()).resolve()
        #: 任务账本（sqlite）。**默认落在项目目录**，不进版本库（.gitignore 已配）。
        from .queue import JobQueue

        # 队列文件位置：显式参数 > 环境变量 KF_QUEUE > 项目目录下默认文件。
        # **必须认 KF_QUEUE**：CLI 用它是约定的（D-060），GUI 不认就会两边指向
        # 不同文件 —— 实测：e2e 传了 KF_QUEUE 却被忽略，读到了开发者的生产历史
        # （断言"历史 2 条"实际 16 条）。
        self.queue = JobQueue(
            queue_path
            or os.environ.get('KF_QUEUE')
            or (self.root / '.kf-queue.db'))
        #: **把所有上次留下的 running 改成 interrupted** ——
        #: 进程死了就不可能还在跑；写成 running 会让任务永远卡在"运行中"，
        #: 写成 done 是骗人。只有 interrupted 是如实。
        self.recovered = self.queue.recover()
        #: 允许选素材的目录。
        #:
        #: **默认只列项目目录**（不含 ~/Downloads / ~/Movies）。
        #: 实测踩到：默认把下载目录也列进来后，下拉框里混进了 16 个视频，
        #: 里面有我没有案情的截图、别人的 MV、甚至一个 360MB 的新闻录屏——
        #: 用户要在一堆无关文件里找自己的素材。**这是个真问题，不是洁癖**：
        #: 素材列表的作用是「让我选我要的那个」，混入无关项就直接废了这个控件。
        #: 想用别的目录就显式传 `--dirs`（或把文件放进项目目录）。
        self.allow_dirs = allow_dirs or [self.root / 'eval', self.root]

    # ---------------------------------------------------------- 在途去重
    def _dedupe_key(self, video, params) -> str:
        """同素材 + 同参数 + 同输出目录 = 只应有一个任务在跑（R283）。"""
        return json.dumps({'v': str(video), 'p': params}, sort_keys=True,
                          ensure_ascii=False)

    def active_for(self, video, params) -> Job | None:
        """在途去重。

        **两处都要查**：
        - 内存里的活任务（正常情况，最快）
        - 队列里的 queued/running（**进程重启后仍能查到现在排队的任务**）

        只查内存的话，重启后同一个任务会被重复入队 —— 用户会看到跑两遍。
        """
        key = self._dedupe_key(video, params)
        with self.lock:
            for j in self.jobs.values():
                if j.key == key and j.status in ('queued', 'running'):
                    return j
        hit = self.queue.find_active(video, params,
                                     params.get('out') or None)
        if hit:
            # 队列里有、内存里没有（重启前入队的）：包一个只读 Job 给调用方
            j = Job(hit['video'], hit['params'])
            j.qid = hit['id']
            j.status = hit['status']
            j.group = hit.get('group_id')
            return j
        return None

    def start(self, video, params, group=None) -> Job:
        """启动任务。**重复请求返回既有任务，不新建** —— 这是连点保护。

        顺序很重要：**先入队（落盘）再起线程**。
        反过来的话，线程可能已经跑完而队列里还没有这条记录，
        重启后历史里就查不到它（实测隐患，不是理论问题）。
        """
        existing = self.active_for(video, params)
        if existing is not None:
            return existing

        qid = self.queue.enqueue(video, params, out=params.get('out'), group_id=group)
        job = Job(str(video), params)
        job.key = self._dedupe_key(video, params)
        job.group = group
        job.qid = qid
        with self.lock:
            self.jobs[job.id] = job
        t = threading.Thread(target=self._run, args=(job,), daemon=True)
        job.thread = t
        t.start()
        return job

    def start_batch(self, items, group=None) -> list[Job]:
        """批量：为每个视频**各起一个独立任务**，共用同一个组名。

        :param items: `[{'video':..., 'params': {...}, 'out': ...}, ...]`
                      **每个 item 可以带自己的 params** —— 这是"批量不支持
                      每个视频不同参数"那个断点的解法。

        **为什么不做成"一个任务里循环多个视频"**：
        - 单个视频失败会连累整批，且已跑完的产物与失败状态混在一个报告里
        - 停止语义会变模糊（停在第 3 个之后，前 2 个算完成吗？）
        - 每个视频的 extract.json / summary.md 都需要独立落盘（续跑依赖它）
        """
        out = []
        for it in items:
            v = it['video']
            p = dict(it.get('params') or {})
            # 每个视频的输出目录要分开，否则会互相覆盖产物
            if it.get('out'):
                p['out'] = str(it['out'])
            else:
                base = p.get('out') or str(pathlib.Path(v).parent / 'shots')
                p['out'] = str(pathlib.Path(base) / pathlib.Path(v).stem)
            out.append(self.start(v, p, group=group))
        return out

    def batch_status(self, group) -> dict:
        """把一组任务聚合成「N 个里完成几个 / 失败几个 / 还在跑几个」。

        **从队列算，不从内存算**：重启后内存里没有这些活句柄，
        但队列里全都有 —— 这样"组进度"在重启前后是同一个口径。
        """
        st = self.queue.group_status(group) if group else {
            'total': 0, 'done': 0, 'failed': 0, 'interrupted': 0,
            'running': 0, 'queued': 0, 'cancelled': 0}
        st['running'] = st.get('running', 0) + st.get('queued', 0)
        return st

    def stop(self, job_id) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
        if not job or job.status not in ('queued', 'running'):
            return False
        # **只置标志，不杀线程**（R299 / R312）：
        # 杀掉线程会让已经导出的帧和报告处于半截状态，标志能让 extract 走续跑路径
        job.stop_requested = True
        return True

    def _run(self, job: Job) -> None:
        from .api import extract
        from .state import Interrupted

        p = job.params
        job.status = 'running'
        job.stage = 'decode'
        if job.qid is not None:
            self.queue.update(job.qid, status='running', started=time.time())
        _last_db = [0.0]

        try:
            def prog(stage, frac, msg):
                if job.stop_requested:
                    raise Interrupted('GUI_STOP')
                job.stage, job.frac, job.msg = stage, frac, msg
                job.last_progress = time.time()
                # **进度落库要节流**：解码时每 256 帧回调一次，
                # 29 分钟素材会有几十次；每次都写 sqlite 没必要。
                # 但也不能太稀 —— 重启后用户要能看到"跑到哪了"。取 2 秒。
                if job.qid is not None and time.time() - _last_db[0] >= 2.0:
                    _last_db[0] = time.time()
                    try:
                        self.queue.update(job.qid, stage=stage,
                                          frac=float(frac), msg=str(msg)[:400])
                    except Exception:
                        pass          # 进度写不进去不该毁掉任务本身

            r = extract(job.video, k=p.get('k'), budget=p.get('budget'),
                        fps=p.get('fps', 6.0), balance=p.get('balance', 0.2),
                        canvas=p.get('canvas'), out=p.get('out'),
                        images=True, sheets=p.get('sheets', True),
                        quality=p.get('quality', 88), progress=prog,
                        record=False)   # GUI 自己管队列，别再记一条（否则历史重复）
            job.result = {
                'selected_k': r.get('selected_k'),
                'maxdist': r.get('maxdist'),
                'max_gap_sec': r.get('max_gap_sec'),
                'median_gap_sec': r.get('median_gap_sec'),
                'time_coverage': r.get('time_coverage'),
                'balance': r.get('balance'),
                'canvas_plan': r.get('canvas_plan'),
                'static_segments': r.get('static_segments'),
                'dark_frames': r.get('dark_frames'),
                'keys_time_sec': r.get('keys_time_sec'),
                'images': [pathlib.Path(x).name for x in (r.get('images') or [])],
                'sheets': [pathlib.Path(x).name for x in (r.get('sheet_files') or [])],
                'out': str(p.get('out')),
                'elapsed_s': r.get('elapsed_s'),
                'warnings': (r.get('unverified') or []) + (r.get('warnings') or []),
            }
            job.status = 'done'
            job.stage, job.frac, job.msg = 'done', 1.0, '完成'
            if job.qid is not None:
                self.queue.finish(job.qid, 'done', result=job.result)
        except Interrupted:
            job.status = 'interrupted'
            job.msg = '已停止。已导出的帧保留在输出目录，可重新运行续跑。'
            if job.qid is not None:
                self.queue.finish(job.qid, 'interrupted', error=job.msg)
        except Exception as e:                      # noqa: BLE001
            job.status = 'failed'
            job.error = f'{type(e).__name__}: {e}'
            job.msg = job.error
            if job.qid is not None:
                self.queue.finish(job.qid, 'failed', error=job.error)

    # ---------------------------------------------------------- 任务历史
    def list_jobs(self, limit=200) -> list[dict]:
        """任务历史，最新的在前。**从队列读** —— 所以重启 GUI 后历史还在。

        形状与 `Job.brief()` 对齐（前端不用改），并补上队列里才有、内存里没有的
        情况（例如重启前入队但没跑完的任务）。

        > 早期版本只存内存（D-037 的选择），代价是"关掉就忘"。
        > 现在队列持久化了，这个代价就不该再付 —— 见 D-060。
        """
        rows = self.queue.list(limit=limit)
        out = []
        with self.lock:
            live = {j.qid: j for j in self.jobs.values() if j.qid is not None}
        for r in rows:
            j = live.get(r['id'])
            started = r.get('started') or r.get('created') or 0
            elapsed = ((r.get('finished') or time.time()) - started
                       if started else 0)
            item = {
                'id': str(r['id']),
                'name': pathlib.Path(r['video']).name,
                'status': r['status'],
                'stage': (j.stage if j else (r.get('stage') or '')),
                'frac': (round(j.frac, 4) if j else float(r.get('frac') or 0)),
                'elapsed': round(max(0, elapsed), 1),
                'started': round(r.get('created') or 0, 1),
                'params': {k: r['params'].get(k) for k in ('k', 'budget', 'canvas', 'balance')},
                'has_result': r.get('result') is not None,
                'error': r.get('error'),
                'group': r.get('group_id'),
                'out': r.get('out'),
                'video': r['video'],
            }
            out.append(item)
        return out

    def out_dir_error(self, out) -> str | None:
        """检查输出目录是否可用。返回**可读的中文原因**，None = 可用。

        **为什么把校验搬到 App 里**：原来只写在 `_api_start` 里、直接返回
        `{'error': '输出目录必须在家目录或项目目录下'}`，前端只能把它当字符串弹出来，
        用户不知道"到底什么被禁止了、该怎么改"。这里返回结构化原因。
        """
        p = pathlib.Path(out).expanduser()
        try:
            rp = p.resolve()
        except (OSError, RuntimeError) as e:
            return f'这个路径无法解析：{e}'
        allowed = [pathlib.Path.home().resolve(), self.root.resolve()]
        if not any(str(rp).startswith(str(d)) for d in allowed):
            return (f'输出目录必须在 {allowed[0]} 或 {allowed[1]} 之下。'
                    f'你给的是 {rp}')
        if rp.exists() and not rp.is_dir():
            return f'{rp} 已存在且不是目录，选一个别的名字'
        return None

    # ---------------------------------------------------------- 列素材
    def list_videos(self) -> list[dict]:
        out = []
        seen = set()
        for d in self.allow_dirs:
            if not d.is_dir():
                continue
            for f in sorted(d.glob('*.mp4')) + sorted(d.glob('*.mkv')) \
                    + sorted(d.glob('*.webm')) + sorted(d.glob('*.mov')):
                rp = str(f.resolve())
                if rp in seen:
                    continue
                seen.add(rp)
                out.append({'path': rp, 'name': f.name,
                            'size_mb': round(f.stat().st_size / 1e6, 1)})
        return out[:200]


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    app: App = None          # 由 create_app 注入
    server_version = 'kf-gui/0.1'

    def log_message(self, fmt, *args):        # 去掉默认的每请求 stderr 噪音
        pass

    # ---- 工具
    def _token_ok(self) -> bool:
        q = urllib.parse.urlparse(self.path).query
        tok = urllib.parse.parse_qs(q).get('token', [''])[0]
        return tok == self.app.token

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        b = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        # 同源约束：不返回任何允许跨站的头
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(b)

    def _json_body(self) -> dict:
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return {}
        if n <= 0 or n > 1 << 20:            # 超过 1MB 直接拒（不给内存攻击面）
            return {}
        try:
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---- 路由
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/' or path == '/index.html':
            if not self._token_ok():
                self._send(403, {'error': 'token 缺失或错误'})
                return
            # **不能用 str.format**：整份 HTML/CSS/JS 里到处是花括号，
            # format 会把 `{--bg:#...}` 当占位符直接 KeyError（实测踩到：空响应）。
            # 用一次性占位符替换。
            self._send(200, _INDEX_HTML.replace('%%KF_TOKEN%%', self.app.token),
                       'text/html; charset=utf-8')
            return
        if path == '/api/videos':
            if not self._token_ok():
                return self._send(403, {'error': 'forbidden'})
            return self._send(200, {'videos': self.app.list_videos()})
        if path == '/api/jobs':
            if not self._token_ok():
                return self._send(403, {'error': 'forbidden'})
            return self._send(200, {'jobs': self.app.list_jobs()})
        if path == '/api/job':
            if not self._token_ok():
                return self._send(403, {'error': 'forbidden'})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            jid = q.get('id', [''])[0]
            j = self._job_snapshot(jid)
            if not j:
                return self._send(404, {'error': 'no such job'})
            return self._send(200, j)
        if path == '/favicon.ico' or path == '/favicon.svg':
            # 内联 SVG favicon：不加这个的话浏览器会一直请求 /favicon.ico 拿 404，
            # 而 e2e 把 "console error" 当失败信号 —— 一条无害的 404 会让闸门永远红。
            return self._send(200, _FAVICON, 'image/svg+xml')
        if path.startswith('/files/'):
            return self._serve_file(path)
        return self._send(404, {'error': 'not found'})

    def _job_snapshot(self, jid) -> dict | None:
        """任务快照：**内存优先，队列兜底**。

        为什么两处都查：正在跑的任务，内存里有实时进度（更准）；
        重启后的历史任务，内存里没有，但队列里有结果（否则"点回旧任务"就失效了）。
        """
        with self.app.lock:
            job = self.app.jobs.get(str(jid))
        if job:
            return job.snapshot()
        # 队列兜底（id 是整数）
        try:
            qid = int(jid)
        except (TypeError, ValueError):
            return None
        rec = self.app.queue.get(qid)
        if not rec:
            return None
        started = rec.get('started') or rec.get('created') or 0
        elapsed = ((rec.get('finished') or time.time()) - started) if started else 0
        return {
            'id': str(rec['id']), 'status': rec['status'],
            'stage': rec.get('stage') or '', 'frac': float(rec.get('frac') or 0),
            'msg': rec.get('msg') or rec.get('error') or '',
            'elapsed': round(max(0, elapsed), 1),
            'video': rec['video'], 'params': rec['params'],
            'error': rec.get('error'),
            'stale_seconds': 0,
            'result': rec.get('result'),
        }

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._token_ok():
            return self._send(403, {'error': 'forbidden'})
        if path == '/api/start':
            return self._api_start()
        if path == '/api/start_batch':
            return self._api_start_batch()
        if path == '/api/stop':
            body = self._json_body()
            ok = self.app.stop(str(body.get('id', '')))
            return self._send(200, {'stopped': ok})
        return self._send(404, {'error': 'not found'})

    # ---- 白名单参数（**绝不接受任意 kwargs**）
    _ALLOWED = {'video', 'k', 'budget', 'fps', 'balance', 'canvas',
                'out', 'sheets', 'quality'}

    def _api_start(self):
        body = self._json_body()
        extra = set(body) - self._ALLOWED
        if extra:
            return self._send(400, {'error': f'不接受参数: {sorted(extra)}'})
        video = body.get('video')
        if not video or not pathlib.Path(video).exists():
            return self._send(400, {'error': '素材不存在'})
        k, budget = body.get('k'), body.get('budget')
        if (k is None) == (budget is None):
            return self._send(400, {'error': 'k 与 budget 必须且只能给一个'})
        out = body.get('out') or str(pathlib.Path(video).parent / 'shots')
        bad = self.app.out_dir_error(out)
        if bad:
            # **结构化错误**：code 让前端能按类型处理，message 是给用户看的原话
            return self._send(400, {'error': bad, 'code': 'bad_out_dir'})
        op = pathlib.Path(out).expanduser().resolve()
        params = {'k': int(k) if k is not None else None,
                  'budget': float(budget) if budget is not None else None,
                  'fps': float(body.get('fps') or 6.0),
                  'balance': float(body.get('balance') if body.get('balance') is not None else 0.2),
                  'canvas': body.get('canvas') or None,
                  'out': str(op),
                  'sheets': bool(body.get('sheets', True)),
                  'quality': int(body.get('quality') or 88)}
        # deduped 必须表示「**复用了已存在的任务**」，而不是「这个任务已经开始跑了」。
        # 早期写成 `job.status != 'queued'`：任务跑得快时（本机 6 秒）第二次请求
        # 拿到的是已经 done 的任务，仍报 deduped=true —— 用户会以为自己点重了。
        existing = self.app.active_for(video, params)
        job = self.app.start(video, params)
        self._send(200, {'id': job.id, 'deduped': existing is not None})

    _BATCH_ALLOWED = {'videos', 'k', 'budget', 'fps', 'balance', 'canvas',
                      'out', 'overrides'}

    def _api_start_batch(self):
        """批量：一次提交多个视频。

        **硬约束**：
        - 视频数上限 50（防止误选整个目录把机器跑爆）
        - 每个视频的输出目录**必须各自不同**（否则产物互相覆盖）
        - 所有校验都要在**提交前**做完并一次性报错，不能跑到一半才发现
        """
        body = self._json_body()
        extra = set(body) - self._BATCH_ALLOWED
        if extra:
            return self._send(400, {'error': f'不接受参数: {sorted(extra)}'})
        vids = body.get('videos') or []
        if not isinstance(vids, list) or not vids:
            return self._send(400, {'error': 'videos 必须是至少一个视频路径的数组'})
        if len(vids) > 50:
            return self._send(400, {'error': f'一次最多 50 个（你给了 {len(vids)} 个）'})
        missing = [v for v in vids if not pathlib.Path(v).exists()]
        if missing:
            return self._send(400, {'error': f'有 {len(missing)} 个素材不存在: '
                                             + ', '.join(pathlib.Path(x).name for x in missing[:5]),
                                    'code': 'missing_video'})
        k, budget = body.get('k'), body.get('budget')
        if (k is None) == (budget is None):
            return self._send(400, {'error': 'k 与 budget 必须且只能给一个'})
        out = body.get('out')
        if out:
            bad = self.app.out_dir_error(out)
            if bad:
                return self._send(400, {'error': bad, 'code': 'bad_out_dir'})
        params = {'k': int(k) if k is not None else None,
                  'budget': float(budget) if budget is not None else None,
                  'fps': float(body.get('fps') or 6.0),
                  'balance': float(body.get('balance') if body.get('balance') is not None else 0.2),
                  'canvas': body.get('canvas') or None,
                  'out': out or None}
        group = f'g{int(time.time())}-{secrets.token_hex(2)}'
        # **每个视频可以带自己的覆盖参数**（`overrides` 是可选字段）：
        #   {"videos": [...], "params": 公共, "overrides": {"/path/a.mp4": {"k": 40}}}
        # 这是"批量不支持每个视频不同参数"那个断点的解法。
        ov = body.get('overrides') or {}
        if not isinstance(ov, dict):
            return self._send(400, {'error': 'overrides 必须是 {视频路径: 参数对象}'})
        items = []
        for v in vids:
            pv = dict(params)
            pv.update(ov.get(v) or {})
            items.append({'video': v, 'params': pv})
        jobs = self.app.start_batch(items, group=group)
        self._send(200, {'group': group,
                         'ids': [j.id for j in jobs],
                         'total': len(jobs)})

    def _serve_file(self, path):
        """只服务**输出目录里**的产物（防目录穿越）。"""
        if not self._token_ok():
            return self._send(403, {'error': 'forbidden'})
        rel = urllib.parse.unquote(path[len('/files/'):])
        cand = pathlib.Path(rel)
        # 前端传的是**绝对路径**（产物目录可能在任何位置）。
        # 所以先按绝对路径解析；相对路径才按 root 拼。
        target = (cand if cand.is_absolute() else (self.app.root / cand)).resolve()
        # **目录穿越防护**：只允许 home 之下或仓库之内
        allowed = [self.app.root, pathlib.Path.home()]
        if not any(str(target).startswith(str(d.resolve())) for d in allowed):
            return self._send(403, {'error': '超出允许目录'})
        if not target.is_file():
            return self._send(404, {'error': 'not found'})
        ctype = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
        self._send(200, target.read_bytes(), ctype)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # **安全闸门**：只许绑回环地址。绑 0.0.0.0 会让同局域网的人能用你的机器抽帧，
        # 这类"顺手打开"是安全事故的常见来源（R108）。
        host = self.server_address[0]
        if host not in _LOCAL_HOSTS:
            raise RuntimeError(
                f'拒绝绑定 {host}：本 GUI 只允许 127.0.0.1（本机）。'
                '要远程用请通过 SSH 隧道。')
        super().server_bind()


def create_app(root_dir=None, allow_dirs=None) -> App:
    return App(root_dir=root_dir, allow_dirs=allow_dirs)


def serve(port=0, root_dir=None, open_browser=True,
          allow_dirs=None) -> tuple[ThreadingHTTPServer, str]:
    """起服务并返回 (server, url)。`port=0` 表示让系统分配（推荐，避免端口冲突）。"""
    app = create_app(root_dir, allow_dirs=allow_dirs)
    handler = type('BoundHandler', (Handler,), {'app': app})
    srv = _Server(('127.0.0.1', port), handler)
    host, real_port = srv.server_address[0], srv.server_address[1]
    url = f'http://{host}:{real_port}/?token={app.token}'
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:                       # noqa: BLE001
            pass
    return srv, url


# ---------------------------------------------------------------- 前端
#: 内联 SVG 图标（一个"关键帧"符号：边框 + 实心方块）。零依赖，不引外部文件。
_FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            '<rect x="3" y="6" width="26" height="20" rx="3" fill="none" '
            'stroke="#4a9eff" stroke-width="2"/>'
            '<rect x="8" y="12" width="7" height="8" rx="1" fill="#4a9eff"/>'
            '<rect x="18" y="12" width="7" height="8" rx="1" fill="#8b95a7"/></svg>')

_INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KF-Extract · 关键帧抽取</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
:root{--bg:#14161a;--panel:#1b1f26;--line:#2b3240;--fg:#e6e9ef;--mut:#8b95a7;
--acc:#4a9eff;--ok:#35c07a;--warn:#f0b400;--bad:#ec5a5a;--run:#d94a4a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:22px 18px 60px;display:grid;
gap:16px;grid-template-columns:minmax(0,1fr) 300px;align-items:start}
/* 窄屏时历史列表放到**下面**：主内容（选素材/参数/结果）才是用户首先要用到的。
   放到上面会把整个主流程推到首屏之外（实测截图确认了这点）。 */
@media(max-width:860px){.wrap{grid-template-columns:1fr}.side{order:2}.main{order:1}}
h1{font-size:20px;margin:0 0 4px;letter-spacing:.3px}
.sub{color:var(--mut);margin-bottom:18px;font-size:13px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:16px;margin-bottom:14px}
label{display:block;font-size:12px;color:var(--mut);margin:0 0 5px}
input,select{width:100%;background:#0f1216;border:1px solid var(--line);
color:var(--fg);border-radius:7px;padding:8px 10px;font-size:13px;font-family:inherit}
input:focus,select:focus,button:focus-visible{outline:2px solid var(--acc);
outline-offset:1px}
.row{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
button{background:var(--acc);border:0;color:#fff;padding:10px 18px;border-radius:7px;
font-size:14px;font-weight:600;cursor:pointer;font-family:inherit}
button:disabled{opacity:.45;cursor:not-allowed}
button.stop{background:var(--bad)}
button.ghost{background:#0f1216;border:1px solid var(--line);color:var(--fg)}
.bar{height:6px;background:#0f1216;border:1px solid var(--line);border-radius:4px;overflow:hidden}
.bar>i{display:block;height:100%;width:0;background:var(--acc);transition:width .25s}
.stats{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));margin:14px 0}
.stat{background:#0f1216;border:1px solid var(--line);border-radius:8px;padding:10px}
.stat b{display:block;font-size:19px;letter-spacing:.2px}
.stat span{color:var(--mut);font-size:11px}
.gallery{display:grid;gap:8px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}
.gallery img{width:100%;border-radius:7px;border:1px solid var(--line);display:block;
cursor:zoom-in;transition:border-color .15s}
.gallery img:hover{border-color:var(--acc)}
.warn{border-left:3px solid var(--warn);background:#241f10;padding:9px 12px;
border-radius:6px;margin:8px 0;font-size:13px}
.err{border-left:3px solid var(--bad);background:#241212}
.tag{display:inline-block;font-size:11px;padding:2px 8px;border-radius:99px;
background:#0f1216;border:1px solid var(--line);color:var(--mut);margin-right:6px}
/* 提示行独占一行且可换行：放在按钮同一行时，长错误文案会把按钮挤变形（实测） */
.hintline{font-size:12px;color:var(--mut);margin-top:9px;line-height:1.5;
word-break:break-word}
h2{font-size:14px;margin:18px 0 8px;color:var(--mut);font-weight:600}
h2:first-child{margin-top:0}

/* ---- 历史列表 ---- */
.joblist{display:flex;flex-direction:column;gap:6px;max-height:60vh;overflow:auto}
.job{background:#0f1216;border:1px solid var(--line);border-radius:8px;
padding:9px 11px;cursor:pointer;display:flex;gap:8px;align-items:center}
.job:hover{border-color:var(--acc)}
.job.sel{border-color:var(--acc);background:#12202f}
.job .nm{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
min-width:0;flex:1 1 auto}
.job .meta{flex:0 0 auto}
.job .meta{color:var(--mut);font-size:11px;white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;flex:0 0 8px}
.dot.done{background:var(--ok)} .dot.failed{background:var(--bad)}
.dot.interrupted{background:var(--warn)} .dot.queued{background:var(--mut)}
.dot.running{background:var(--run);box-shadow:0 0 0 3px rgba(217,74,74,.25)}
.job.running{background:#2b1618;border-color:var(--run)}

/* ---- viewer ---- */
#viewer{position:fixed;inset:0;background:rgba(8,9,12,.94);z-index:99;
display:none;flex-direction:column}
#viewer.on{display:flex}
#viewer .top{padding:10px 14px;display:flex;gap:12px;align-items:center;
color:var(--mut);font-size:12px;border-bottom:1px solid var(--line)}
#viewer .body{flex:1;display:flex;align-items:center;justify-content:center;
overflow:auto;padding:12px}
#viewer .body{position:relative}
#viewer img{max-width:100%;max-height:100%;object-fit:contain;border-radius:6px;
transition:width .12s ease-out;cursor:zoom-in}
#viewer.zoomed img{cursor:zoom-out}
#viewer.zoomed .body{overflow:auto;cursor:grab}
#viewer.zoomed .body.dragging{cursor:grabbing}
.zoombar{display:flex;gap:6px;align-items:center;margin-left:auto}
.zoombar .lvl{font-variant-numeric:tabular-nums;min-width:46px;text-align:center}
#viewer button{background:#0f1216;border:1px solid var(--line);color:var(--fg);
padding:5px 12px;font-size:13px}
</style></head><body>
<div class="wrap">
<div class="main">
  <h1>KF-Extract</h1>
  <div class="sub">从视频选出覆盖最优的关键帧，并拼成图集 · 纯本地，不上传任何素材</div>

  <div class="card">
    <h2>1 · 选素材</h2>
    <div>
      <label style="display:flex;justify-content:space-between;align-items:center">
        <span>素材（只列出本地视频）</span>
        <span style="display:flex;gap:6px;align-items:center">
          <input type="checkbox" id="batch" style="width:auto;margin:0">
          <span style="font-size:11px;cursor:pointer" id="batchlabel">批量（可多选）</span>
        </span>
      </label>
      <select id="video"><option value="">正在扫描…</option></select>
      <div id="multi" style="display:none;margin-top:8px">
        <div style="color:var(--mut);font-size:11px;margin-bottom:5px">
          勾选要跑的视频（每个视频输出到独立子目录，不会互相覆盖）
        </div>
        <div id="multiList" style="max-height:180px;overflow:auto;background:#0f1216;
             border:1px solid var(--line);border-radius:7px;padding:8px">
        </div>
        <div style="color:var(--mut);font-size:11px;margin-top:5px" id="multiCount">已选 0 个</div>
      </div>
    </div>
    <div class="row" style="margin-top:12px">
      <div><label>抽多少帧 (--k)</label><input id="k" type="number" value="40" min="1"></div>
      <div><label>时间均衡</label>
        <select id="balance">
          <option value="0">0 · 纯内容优先（会留长空白）</option>
          <option value="0.2" selected>0.2 · 推荐（覆盖代价 +1.4%）</option>
          <option value="0.3">0.3 · 更稳（代价 +3%）</option>
        </select></div>
      <div><label>出图档位</label>
        <select id="canvas">
          <option value="readable" selected>readable · 能读清小字</option>
          <option value="cheap">cheap · 省 token</option>
          <option value="max">max · 最清晰</option>
        </select></div>
      <div><label>分析帧率</label><input id="fps" type="number" value="6" min="1" max="30"></div>
    </div>
    <div style="margin-top:12px">
      <label>输出目录
        <span class="tag" id="outtag">默认：素材旁的 shots/</span></label>
      <div style="display:flex;gap:8px">
        <input id="out" placeholder="留空 = 素材旁的 shots/" autocomplete="off">
        <button class="ghost" id="outclear" title="恢复默认"
                style="flex:0 0 auto;white-space:nowrap">默认</button>
      </div>
      <div style="color:var(--mut);font-size:11px;margin-top:5px">
        只允许家目录或项目目录之下（防止误写到系统目录）
      </div>
    </div>
    <div style="margin-top:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <button id="go">开始抽取</button>
      <button id="stop" class="stop" disabled>停止</button>
    </div>
    <div id="hint" class="hintline">就绪</div>
  </div>

  <div class="card" id="progcard" style="display:none">
    <h2>2 · 进度</h2>
    <div class="bar"><i id="bar"></i></div>
    <div style="margin-top:8px;color:var(--mut);font-size:12px" id="stage">—</div>
  </div>

  <div class="card" id="rescard" style="display:none">
    <h2>3 · 结果</h2>
    <div class="stats" id="stats"></div>
    <div id="warns"></div>
    <h2>拼图（点开可放大）</h2>
    <div class="gallery" id="sheets"></div>
    <h2>单帧（点开可放大）</h2>
    <div class="gallery" id="frames"></div>
  </div>
</div>

<div class="side">
  <div class="card">
    <h2>任务历史</h2>
    <div class="joblist" id="jobs">
      <div class="meta">还没有任务</div>
    </div>
  </div>
</div>
</div>

<div id="viewer">
  <div class="top">
    <button id="vprev">←</button>
    <span id="vpos">—</span>
    <button id="vnext">→</button>
    <span id="vname" style="flex:1;overflow:hidden;text-overflow:ellipsis"></span>
    <div class="zoombar">
      <button id="vzo">−</button>
      <span class="lvl" id="vlvl">1.0×</span>
      <button id="vzi">+</button>
      <button id="vzfit">适应</button>
      <button id="vz1">1:1</button>
    </div>
    <button id="vclose">关闭 (Esc)</button>
  </div>
  <div class="body"><img id="vimg" alt=""></div>
</div>

<script>
const TOKEN = "%%KF_TOKEN%%";
const q = (s) => document.querySelector(s);
let JOB = null, TIMER = null, JOBS = [], VIEWS = [], VI = -1;

async function api(path, opt) {
  const u = path + (path.includes('?') ? '&' : '?') + 'token=' + TOKEN;
  const r = await fetch(u, opt);
  let body = null;
  try { body = await r.json(); } catch(e) {}
  if (!r.ok) {
    const err = new Error((body && body.error) || ('HTTP ' + r.status));
    err.code = body && body.code;
    throw err;
  }
  return body;
}
function setHint(t, bad) {
  const h = q('#hint');
  h.textContent = t;
  h.style.color = bad ? 'var(--bad)' : 'var(--mut)';
}

let VIDEOS = [];
async function loadVideos() {
  try {
    const d = await api('/api/videos');
    VIDEOS = d.videos || [];
    const sel = q('#video');
    if (!VIDEOS.length) { sel.innerHTML='<option value="">（没找到视频，把素材放进 eval/ 或下载目录）</option>'; return; }
    sel.innerHTML = VIDEOS.map(v =>
      `<option value="${escapeAttr(v.path)}">${esc(v.name)} · ${v.size_mb}MB</option>`).join('');
    q('#multiList').innerHTML = VIDEOS.map((v, i) =>
      `<label style="display:flex;gap:8px;align-items:center;padding:3px 0;font-size:12px;margin:0">
         <input type="checkbox" class="mVid" value="${escapeAttr(v.path)}"
                style="width:auto;margin:0">
         <span>${esc(v.name)} <span style="color:var(--mut)">· ${v.size_mb}MB</span></span>
       </label>`).join('');
    q('#multiList').querySelectorAll('.mVid').forEach(c => c.onchange = syncMultiCount);
  } catch(e) { q('#video').innerHTML = '<option value="">扫描失败: '+e.message+'</option>'; }
}
function escapeAttr(s) { return String(s).replace(/"/g, '&quot;'); }

function syncMultiCount() {
  const n = q('#multiList').querySelectorAll('.mVid:checked').length;
  q('#multiCount').textContent = `已选 ${n} 个`;
}
q('#batch').onchange = () => {
  const on = q('#batch').checked;
  q('#multi').style.display = on ? 'block' : 'none';
  q('#video').disabled = on;
  syncMultiCount();
};
q('#batchlabel').onclick = (ev) => {
  // **必须 preventDefault**：这个 span 包在 <label> 里，
  // label 的原生行为是「点击 label → 转发给内部 checkbox」。
  // 不阻止的话，一次点击会翻转两次（我翻转一次 + label 转发翻一次）→ 净效果为零。
  // 实测：处理器内部 checked 已经是 true，退出后又变回 false。
  ev.preventDefault();
  ev.stopPropagation();
  const cb = q('#batch');
  cb.checked = !cb.checked;
  cb.onchange();
};

// ---- 输出目录：记住上次选择
const OUTKEY = 'kf.out';
function syncOutTag() {
  const v = q('#out').value.trim();
  q('#outtag').textContent = v ? '自定义' : '默认：素材旁的 shots/';
}
q('#out').addEventListener('input', syncOutTag);
q('#outclear').onclick = () => { q('#out').value=''; localStorage.removeItem(OUTKEY); syncOutTag(); };
const savedOut = localStorage.getItem(OUTKEY);
if (savedOut) { q('#out').value = savedOut; }
syncOutTag();
q('#out').addEventListener('change', () => {
  const v = q('#out').value.trim();
  if (v) localStorage.setItem(OUTKEY, v); else localStorage.removeItem(OUTKEY);
});

q('#go').onclick = async () => {
  const batch = q('#batch').checked;
  const common = {k: Number(q('#k').value), balance: Number(q('#balance').value),
                  canvas: q('#canvas').value, fps: Number(q('#fps').value)};
  const outv = q('#out').value.trim();
  if (outv) common.out = outv;

  if (batch) {
    const vids = [...q('#multiList').querySelectorAll('.mVid:checked')].map(c => c.value);
    if (!vids.length) return setHint('批量模式下至少勾选一个视频', true);
    if (vids.length > 50) return setHint('一次最多 50 个', true);
    q('#go').disabled = true; q('#stop').disabled = false;
    q('#rescard').style.display = 'none';
    q('#progcard').style.display = 'block';
    setHint(`已提交 ${vids.length} 个任务…`);
    try {
      const r = await api('/api/start_batch', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({videos: vids, ...common})});
      GROUP = r.group; JOB = r.ids[0];
      pollBatch();
    } catch(e) {
      setHint('批量提交失败: ' + e.message, true);
      q('#go').disabled=false; q('#stop').disabled=true;
    }
    return;
  }

  const video = q('#video').value;
  if (!video) return setHint('先选一个素材', true);
  q('#go').disabled = true; q('#stop').disabled = false;
  q('#rescard').style.display = 'none';
  q('#progcard').style.display = 'block';
  setHint('已提交…');
  try {
    const body = {video, ...common};
    const r = await api('/api/start', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    JOB = r.id; GROUP = null;
    if (r.deduped) setHint('同一个任务已在跑，复用它（不会重复产出）');
    poll(); loadJobs();
  } catch(e) {
    // 输出目录错误要给出可读原因（不是原始 JSON）
    setHint(e.code === 'bad_out_dir' ? '输出目录不可用：' + e.message : '提交失败: ' + e.message, true);
    q('#go').disabled=false; q('#stop').disabled=true;
    if (e.code === 'bad_out_dir') q('#out').focus();
  }
};

q('#stop').onclick = async () => {
  q('#stop').disabled = true;
  setHint('正在停止（已拿到的帧会保留）…');
  if (GROUP) {
    // 批量：停掉这一组里**还在跑的每一个**（不能只停当前显示的那个）
    const d = await api('/api/jobs').catch(() => ({jobs: []}));
    const mine = (d.jobs || []).filter(j => j.group === GROUP &&
      (j.status === 'running' || j.status === 'queued'));
    for (const j of mine) {
      await api('/api/stop', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({id: j.id})}).catch(() => {});
    }
    setHint(`已请求停止 ${mine.length} 个任务（已拿到的帧保留）`);
    return;
  }
  if (!JOB) return;
  await api('/api/stop', {method:'POST', headers:{'Content-Type':'application/json'},
                          body: JSON.stringify({id: JOB})});
};

function stageName(s) {
  return {decode:'解码', select:'选帧', export_frames:'导出单帧',
          sheets:'拼图', done:'完成'}[s] || s || '—';
}

let GROUP = null;
function pollBatch() {
  clearTimeout(TIMER);
  TIMER = setTimeout(async () => {
    if (!GROUP) return;
    let d;
    try { d = await api('/api/jobs'); } catch(e) { return pollBatch(); }
    const mine = (d.jobs || []).filter(j => j.group === GROUP);
    const total = mine.length;
    const done = mine.filter(j => j.status === 'done').length;
    const failed = mine.filter(j => j.status === 'failed').length;
    const inter = mine.filter(j => j.status === 'interrupted').length;
    const running = total - done - failed - inter;
    const finished = done + failed + inter;
    q('#bar').style.width = (total ? finished / total * 100 : 0).toFixed(1) + '%';
    q('#stage').textContent =
      `批量：${finished}/${total} 完成` +
      (failed ? ` · ${failed} 失败` : '') +
      (inter ? ` · ${inter} 已停止` : '') +
      (running ? ` · ${running} 在跑` : '');
    loadJobs();
    if (finished >= total) {
      q('#go').disabled = false; q('#stop').disabled = true;
      setHint(failed ? `${done}/${total} 完成，${failed} 失败` : `全部完成（${done}/${total}）`, failed > 0);
      // 组里第一个成功的任务，把它的结果展示出来
      const ok = mine.find(j => j.status === 'done');
      // quiet：别把"全部完成（N/N）"覆盖成"已恢复该任务的结果"
      if (ok) { JOB = ok.id; selectJob(ok.id, true); }
      GROUP = null;
      return;
    }
    setHint(`批量运行中（${finished}/${total}）`);
    pollBatch();
  }, 900);
}

function poll() {
  clearTimeout(TIMER);
  TIMER = setTimeout(async () => {
    if (!JOB) return;
    let j;
    try { j = await api('/api/job?id=' + encodeURIComponent(JOB)); }
    catch(e) { setHint('查询失败: '+e.message, true); return poll(); }
    q('#bar').style.width = (j.frac*100).toFixed(1)+'%';
    let line = stageName(j.stage) + ' · ' + (j.msg||'') + ' · ' + j.elapsed + 's';
    // 长静默必须自己喊出来（R35）：29min 素材解码 40 秒，纯百分比像卡死
    if (j.status === 'running' && j.stale_seconds >= 10) {
      line += ' —— 仍在工作（已 ' + Math.round(j.stale_seconds) + ' 秒无更新）';
    }
    q('#stage').textContent = line;
    if (j.status === 'done') { finish(j); return; }
    if (j.status === 'failed') { fail(j); return; }
    if (j.status === 'interrupted') { interrupted(j); return; }
    setHint(j.status === 'running' ? '运行中…' : '排队中…');
    poll();
  }, 700);
}

// ---- 每个任务的结果缓存（历史点回来用）
const RES = {};

function finish(j) {
  q('#go').disabled = false; q('#stop').disabled = true;
  setHint('完成 · ' + j.result.elapsed_s + 's');
  RES[j.id] = j;
  renderResult(j);
  loadJobs();
}

function renderResult(j) {
  q('#rescard').style.display = 'block';
  const r = j.result;
  const st = [
    ['选中帧数', r.selected_k],
    ['覆盖 maxdist', (r.maxdist ?? 0).toFixed(1)],
    ['最大空白', r.max_gap_sec + 's'],
    ['中位空白', r.median_gap_sec + 's'],
    ['时间覆盖', (r.time_coverage?.buckets_hit||0)+'/'+(r.time_coverage?.buckets||10)],
    ['token 合计', (r.canvas_plan?.tokens_total ?? '—')],
  ];
  q('#stats').innerHTML = st.map(([k,v])=>`<div class="stat"><b>${v}</b><span>${k}</span></div>`).join('');

  const warns = [];     // [{text, sec}] —— sec 有值时可点击跳到那一帧
  if (r.static_segments?.longest && r.static_segments.longest.dur_sec >= 10) {
    const L = r.static_segments.longest;
    warns.push({
      text: `连续静止 ${L.dur_sec}s（${L.start_sec}s→${L.end_sec}s）—— 整段画面基本不变；` +
            `像素度量对缓慢漂移（字幕/光标）不敏感，建议抽查`,
      // 跳到静止段**起点附近**的第一个选中帧：让用户直接看"这段开头长什么样"
      sec: L.start_sec,
      jumpLabel: '看这段附近的关键帧',
    });
  }
  if (r.dark_frames?.in_selection) {
    const idx = r.dark_frames.selection_idx || [];
    const sec = idx.length && r.keys_time_sec ? r.keys_time_sec[idx[0]] : null;
    warns.push({
      text: `选中帧里有 ${r.dark_frames.in_selection} 帧近乎全黑，占图集容量但不含信息`,
      sec: sec, jumpLabel: sec != null ? '看那帧' : null,
    });
  }
  (r.warnings||[]).slice(0,4).forEach(x => warns.push({text: x}));

  // keys_time_sec 与 galleries 的顺序一致（images 和 sheets 都是按时间排的），
  // 所以"跳到某一秒"= 找到时间最接近的那张**单帧**
  const nearestIdx = (sec) => {
    if (sec == null || !r.keys_time_sec) return -1;
    let best = -1, bestD = Infinity;
    r.keys_time_sec.forEach((t, i) => {
      const d = Math.abs(t - sec);
      if (d < bestD) { bestD = d; best = i; }
    });
    return best;
  };
  q('#warns').innerHTML = warns.map((w, i) => {
    const clickable = w.sec != null && r.keys_time_sec;
    const btn = clickable
      ? `<button class="ghost jump" data-wi="${i}" style="margin-left:8px;padding:3px 10px;font-size:12px">${esc(w.jumpLabel||'跳过去')}</button>`
      : '';
    return `<div class="warn">${esc(w.text)}${btn}</div>`;
  }).join('');
  q('#warns').querySelectorAll('button.jump').forEach(b => {
    b.onclick = () => {
      const w = warns[Number(b.dataset.wi)];
      const fi = nearestIdx(w.sec);
      if (fi < 0) return;
      // #frames 里的第 fi 张 = images[fi]（VIEWS 里单帧紧跟在拼图之后）
      const targetView = VIEWS.findIndex(v => v.kind === 'frame' && v.idx === fi);
      if (targetView >= 0) { openViewer(targetView); return; }
      // 单帧画廊只展示前 12 张；超出时如实告诉用户去哪看，而不是静默无反应
      setHint(`第 ${fi + 1} 张单帧（${w.sec}s 附近）不在预览里，见输出目录 ${r.out}`, false);
    };
  });

  // 图片 URL：整段路径一起编码（含斜杠），否则服务端切出的相对路径会丢层级
  const fileUrl = (name) =>
    '/files/' + encodeURIComponent(r.out.replace(/\/+$/,'') + '/' + name) +
    '?token=' + TOKEN;
  VIEWS = [];
  const addViewer = (el, arr, label, kind) => {
    arr.forEach((nm,i) => {
      const im = document.createElement('img');
      im.src = fileUrl(nm); im.alt = nm;
      im.dataset.vi = VIEWS.length;
      // kind/idx 用于「从警告跳转到某张单帧」时反查 viewer 下标
      VIEWS.push({src: fileUrl(nm), name: nm, label, kind, idx: i});
      im.onclick = () => openViewer(Number(im.dataset.vi));
      el.appendChild(im);
    });
  };
  q('#sheets').innerHTML = ''; q('#frames').innerHTML = '';
  addViewer(q('#sheets'), r.sheets, '拼图', 'sheet');
  addViewer(q('#frames'), r.images, '单帧', 'frame');
  if (r.images.length > 12) {
    q('#frames').innerHTML += `<div class="tag">还有 ${r.images.length-12} 张，见输出目录</div>`;
  }
}

function esc(s) { return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function fail(j) {
  q('#go').disabled = false; q('#stop').disabled = true;
  setHint('失败', true); q('#rescard').style.display = 'block';
  q('#stats').innerHTML = '';
  q('#warns').innerHTML = `<div class="warn err">${esc(j.error||j.msg)}</div>`;
  loadJobs();
}

function interrupted(j) {
  q('#go').disabled = false; q('#stop').disabled = true;
  setHint('已停止（不是失败）', false);
  q('#rescard').style.display = 'block';
  q('#stats').innerHTML = '';
  q('#warns').innerHTML = `<div class="warn">${esc(j.msg)}</div>`;
  loadJobs();
}

// ---- 任务历史
const STATUS_CN = {queued:'排队', running:'运行中', done:'完成',
                   failed:'失败', interrupted:'已停止'};
async function loadJobs() {
  try { const d = await api('/api/jobs'); JOBS = d.jobs || []; }
  catch(e) { return; }
  const el = q('#jobs');
  if (!JOBS.length) { el.innerHTML = '<div class="meta">还没有任务</div>'; return; }
  el.innerHTML = JOBS.map(j => {
    const on = (j.id === JOB) ? ' sel' : '';
    const run = (j.status === 'running') ? ' running' : '';
    const kk = j.params.k != null ? `k=${j.params.k}` : `budget=${j.params.budget}`;
    return `<div class="job${on}${run}" data-id="${j.id}">
      <span class="dot ${j.status}"></span>
      <span class="nm">${esc(j.name)}</span>
      <span class="meta">${kk} · ${STATUS_CN[j.status]||j.status} · ${j.elapsed}s</span>
    </div>`;
  }).join('');
  el.querySelectorAll('.job').forEach(d => d.onclick = () => selectJob(d.dataset.id));
}

async function selectJob(id, quiet = false) {
  // quiet=true 时**不改提示文案**：批量完成的那一刻要保住"全部完成（N/N）"，
  // 不能被随后的"已恢复该任务的结果"覆盖掉（实测：等待文案的用例因此永远超时，
  // 而且用户也看不到批量结果摘要）。
  JOB = id; loadJobs();
  const say = (t, bad) => { if (!quiet) setHint(t, bad); };
  const cached = RES[id];
  if (cached) { renderResult(cached); say('已恢复该任务的结果'); return; }
  try {
    const j = await api('/api/job?id=' + encodeURIComponent(id));
    if (j.status === 'done' && j.result) { RES[id]=j; renderResult(j); say('已恢复该任务的结果'); }
    else { say('该任务状态：' + (STATUS_CN[j.status]||j.status)); }
  } catch(e) { say('找不到该任务', true); }
}

// ---- viewer
// ---- viewer 缩放
let VZOOM = 1;
function applyZoom() {
  const v = q('#viewer');
  const im = q('#vimg');
  if (VZOOM <= 1.001) {
    v.classList.remove('zoomed');
    im.style.width = ''; im.style.height = '';
    im.style.maxWidth = '100%'; im.style.maxHeight = '100%';
  } else {
    v.classList.add('zoomed');
    // **用 width 缩放而不是 transform**：transform 会让
    // getBoundingClientRect().width 包含 scale，导致"按原始像素"算不准
    // （实测：1066 的图被算成 1.2× 却渲染成 1297px）。
    // 用 width 时，元素的实际布局宽度就是缩放后的值，量一次就对。
    if (!im.dataset.baseW) {
      // 适应态下的渲染宽度作为基准（只在第一次进放大态时量）
      im.style.maxWidth = '100%'; im.style.maxHeight = '100%';
      const w = im.getBoundingClientRect().width;
      im.dataset.baseW = String(Math.round(w));
    }
    const base = Number(im.dataset.baseW);
    im.style.maxWidth = 'none'; im.style.maxHeight = 'none';
    im.style.width = (base * VZOOM) + 'px';
    im.style.height = 'auto';
  }
  q('#vlvl').textContent = VZOOM.toFixed(1) + '×';
}
function setZoom(z) {
  // 夹到 [1, 8]：小于 1 没有意义（已经 fit），大于 8 像素糊成方块
  VZOOM = Math.min(8, Math.max(1, z));
  applyZoom();
}
function zoomBy(f) { setZoom(VZOOM * f); }
function zoomFit() { setZoom(1); }

function openViewer(i) {
  if (i < 0 || i >= VIEWS.length) return;
  VI = i;
  const v = VIEWS[i];
  q('#vimg').src = v.src;
  q('#vname').textContent = v.name;
  q('#vpos').textContent = `${v.label} ${i+1} / ${VIEWS.length}`;
  q('#viewer').classList.add('on');
  delete q('#vimg').dataset.baseW;   // 换图了，基准宽度要重新量
  setZoom(1);          // 切图后回到适应窗口（否则在 8× 下切图会看不到东西）
}
function closeViewer() { q('#viewer').classList.remove('on'); setZoom(1); }
function stepViewer(d) { if (VI >= 0) openViewer(VI + d); }
q('#vclose').onclick = closeViewer;
q('#vprev').onclick = () => stepViewer(-1);
q('#vnext').onclick = () => stepViewer(1);
q('#vzi').onclick = () => zoomBy(1.5);
q('#vzo').onclick = () => zoomBy(1/1.5);
q('#vzfit').onclick = zoomFit;
// **1:1 的真实含义是"按原始像素显示"**，不是"缩放 1.0"。
// 图片是 1066×600，容器可能更窄 —— 所以 1:1 要算出「让 1 个图片像素 = 1 个屏幕像素」的比例。
q('#vz1').onclick = () => {
  const im = q('#vimg');
  if (!im.naturalWidth) return;
  // 基准宽度（适应态下的渲染宽度）已经记在 dataset 里，直接算比例
  if (!im.dataset.baseW) { setZoom(1); }
  const base = Number(im.dataset.baseW) || im.getBoundingClientRect().width;
  const ratio = im.naturalWidth / Math.max(1, base);
  setZoom(Math.max(1, ratio));     // 1:1 = 渲染宽度等于图片原始像素宽
};
// 点图本身 = 在 适应 和 2× 之间切换（最常见的用法）
q('#vimg').onclick = () => setZoom(VZOOM <= 1.001 ? 2 : 1);
// 滚轮缩放（对"读清小字"最顺手）
q('#viewer .body').addEventListener('wheel', (e) => {
  if (!q('#viewer').classList.contains('on')) return;
  e.preventDefault();
  zoomBy(e.deltaY < 0 ? 1.2 : 1/1.2);
}, { passive: false });
// 放大后拖拽平移
(function(){
  const body = q('#viewer .body');
  let drag = null;
  body.addEventListener('mousedown', (e) => {
    // 只在放大态下可拖；适应窗口时没有可滚动的空间，拖拽无意义
    if (!q('#viewer').classList.contains('zoomed')) return;
    // 别和「点图切换缩放」抢：按住不动是点击（由 img.onclick 处理），
    // 移动超过 4px 才算拖拽。这里只记录起点，实际区分在 mousemove 里做。
    e.target.dataset.maybeDrag = '1';
    drag = { x: e.clientX, y: e.clientY, sl: body.scrollLeft, st: body.scrollTop };
    body.classList.add('dragging');
    e.preventDefault();
  });
  window.addEventListener('mousemove', (e) => {
    if (!drag) return;
    body.scrollLeft = drag.sl - (e.clientX - drag.x);
    body.scrollTop  = drag.st - (e.clientY - drag.y);
  });
  window.addEventListener('mouseup', () => {
    drag = null; body.classList.remove('dragging');
  });
})();
q('#viewer').addEventListener('click', (e) => { if (e.target.id === 'viewer') closeViewer(); });

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { if (q('#viewer').classList.contains('on')) closeViewer(); return; }
  if (q('#viewer').classList.contains('on')) {
    if (e.key === 'ArrowLeft') { stepViewer(-1); e.preventDefault(); }
    if (e.key === 'ArrowRight') { stepViewer(1); e.preventDefault(); }
    return;
  }
  // 参数区里 Enter = 开始，Ctrl/Cmd+Enter 也触发
  if (e.key === 'Enter' && !q('#go').disabled) { q('#go').click(); }
});

loadVideos();
loadJobs();
setInterval(loadJobs, 1200);
</script></body></html>
"""
