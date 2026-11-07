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

    def snapshot(self) -> dict:
        return {'id': self.id, 'status': self.status, 'stage': self.stage,
                'frac': round(self.frac, 4), 'msg': self.msg,
                'elapsed': round(time.time() - self.t0, 1),
                'video': self.video, 'params': self.params,
                'error': self.error,
                'result': self.result if self.status == 'done' else None}


class App:
    def __init__(self, root_dir=None, allow_dirs=None):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.token = secrets.token_hex(16)
        self.root = pathlib.Path(root_dir or os.getcwd()).resolve()
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
        key = self._dedupe_key(video, params)
        with self.lock:
            for j in self.jobs.values():
                if j.key == key and j.status in ('queued', 'running'):
                    return j
        return None

    def start(self, video, params) -> Job:
        """启动任务。**重复请求返回既有任务，不新建** —— 这是连点保护。"""
        existing = self.active_for(video, params)
        if existing is not None:
            return existing
        job = Job(str(video), params)
        job.key = self._dedupe_key(video, params)
        with self.lock:
            self.jobs[job.id] = job
        t = threading.Thread(target=self._run, args=(job,), daemon=True)
        job.thread = t
        t.start()
        return job

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
        try:
            def prog(stage, frac, msg):
                if job.stop_requested:
                    raise Interrupted('GUI_STOP')
                job.stage, job.frac, job.msg = stage, frac, msg

            r = extract(job.video, k=p.get('k'), budget=p.get('budget'),
                        fps=p.get('fps', 6.0), balance=p.get('balance', 0.2),
                        canvas=p.get('canvas'), out=p.get('out'),
                        images=True, sheets=p.get('sheets', True),
                        quality=p.get('quality', 88), progress=prog)
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
        except Interrupted:
            job.status = 'interrupted'
            job.msg = '已停止。已导出的帧保留在输出目录，可重新运行续跑。'
        except Exception as e:                      # noqa: BLE001
            job.status = 'failed'
            job.error = f'{type(e).__name__}: {e}'
            job.msg = job.error

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
        if path == '/api/job':
            if not self._token_ok():
                return self._send(403, {'error': 'forbidden'})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            jid = q.get('id', [''])[0]
            with self.app.lock:
                job = self.app.jobs.get(jid)
            if not job:
                return self._send(404, {'error': 'no such job'})
            return self._send(200, job.snapshot())
        if path.startswith('/files/'):
            return self._serve_file(path)
        return self._send(404, {'error': 'not found'})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._token_ok():
            return self._send(403, {'error': 'forbidden'})
        if path == '/api/start':
            return self._api_start()
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
        # **不许写到系统目录**：只允许用户 home 之下或仓库之内的路径
        op = pathlib.Path(out).resolve()
        if not any(str(op).startswith(str(d.resolve())) for d in
                   [pathlib.Path.home(), self.app.root]):
            return self._send(400, {'error': '输出目录必须在家目录或项目目录下'})
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
_INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KF-Extract · 关键帧抽取</title>
<style>
:root{--bg:#14161a;--panel:#1b1f26;--line:#2b3240;--fg:#e6e9ef;--mut:#8b95a7;
--acc:#4a9eff;--ok:#35c07a;--warn:#f0b400;--bad:#ec5a5a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:22px 18px 60px}
h1{font-size:20px;margin:0 0 4px;letter-spacing:.3px}
.sub{color:var(--mut);margin-bottom:20px;font-size:13px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:16px;margin-bottom:14px}
label{display:block;font-size:12px;color:var(--mut);margin:0 0 5px}
input,select{width:100%;background:#0f1216;border:1px solid var(--line);
color:var(--fg);border-radius:7px;padding:8px 10px;font-size:13px;font-family:inherit}
.row{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
button{background:var(--acc);border:0;color:#fff;padding:10px 18px;border-radius:7px;
font-size:14px;font-weight:600;cursor:pointer}
button:disabled{opacity:.45;cursor:not-allowed}
button.stop{background:var(--bad)}
.bar{height:6px;background:#0f1216;border:1px solid var(--line);border-radius:4px;overflow:hidden}
.bar>i{display:block;height:100%;width:0;background:var(--acc);transition:width .25s}
.stats{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));margin:14px 0}
.stat{background:#0f1216;border:1px solid var(--line);border-radius:8px;padding:10px}
.stat b{display:block;font-size:19px;letter-spacing:.2px}
.stat span{color:var(--mut);font-size:11px}
.gallery{display:grid;gap:8px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.gallery img{width:100%;border-radius:7px;border:1px solid var(--line);display:block}
.warn{border-left:3px solid var(--warn);background:#241f10;padding:9px 12px;
border-radius:6px;margin:8px 0;font-size:13px}
.err{border-left:3px solid var(--bad);background:#241212}
.tag{display:inline-block;font-size:11px;padding:2px 8px;border-radius:99px;
background:#0f1216;border:1px solid var(--line);color:var(--mut);margin-right:6px}
h2{font-size:14px;margin:18px 0 8px;color:var(--mut);font-weight:600}
</style></head><body><div class="wrap">
<h1>KF-Extract</h1>
<div class="sub">从视频选出覆盖最优的关键帧，并拼成图集 · 纯本地，不上传任何素材</div>

<div class="card">
  <h2 style="margin-top:0">1 · 选素材</h2>
  <div class="row">
    <div style="grid-column:1/-1"><label>素材（只会列出本地视频）</label>
      <select id="video"><option value="">正在扫描…</option></select></div>
  </div>
  <div class="row" style="margin-top:12px">
    <div><label>抽多少帧 (--k)</label><input id="k" type="number" value="40" min="1"></div>
    <div><label>时间均衡 (--balance)</label>
      <select id="balance">
        <option value="0">0 · 纯内容优先（会留长空白）</option>
        <option value="0.2" selected>0.2 · 推荐（覆盖代价 +1.4%）</option>
        <option value="0.3">0.3 · 更强均衡（代价 +3%）</option>
      </select></div>
    <div><label>出图档位 (--canvas)</label>
      <select id="canvas">
        <option value="readable" selected>readable · 能读清小字</option>
        <option value="cheap">cheap · 省 token</option>
        <option value="max">max · 最清晰</option>
      </select></div>
    <div><label>分析帧率 (--fps)</label><input id="fps" type="number" value="6" min="1" max="30"></div>
  </div>
  <div style="margin-top:14px;display:flex;gap:10px;align-items:center">
    <button id="go">开始抽取</button>
    <button id="stop" class="stop" disabled>停止</button>
    <span id="hint" class="tag">就绪</span>
  </div>
</div>

<div class="card" id="progcard" style="display:none">
  <h2 style="margin-top:0">2 · 进度</h2>
  <div class="bar"><i id="bar"></i></div>
  <div style="margin-top:8px;color:var(--mut);font-size:12px" id="stage">—</div>
</div>

<div class="card" id="rescard" style="display:none">
  <h2 style="margin-top:0">3 · 结果</h2>
  <div class="stats" id="stats"></div>
  <div id="warns"></div>
  <h2>拼图（每格标了序号与时间）</h2>
  <div class="gallery" id="sheets"></div>
  <h2>单帧</h2>
  <div class="gallery" id="frames"></div>
</div>
</div>
<script>
const TOKEN = "%%KF_TOKEN%%";
const q = (s) => document.querySelector(s);
let JOB = null, TIMER = null;

async function api(path, opt) {
  const u = path + (path.includes('?') ? '&' : '?') + 'token=' + TOKEN;
  const r = await fetch(u, opt);
  if (!r.ok) throw new Error((await r.json().catch(()=>({}))).error || r.status);
  return r.json();
}
function setHint(t, bad) { const h=q('#hint'); h.textContent=t; h.style.color = bad?'var(--bad)':''; }

async function loadVideos() {
  try {
    const d = await api('/api/videos');
    const sel = q('#video');
    if (!d.videos.length) { sel.innerHTML='<option value="">（没找到视频，把素材放进 eval/ 或下载目录）</option>'; return; }
    sel.innerHTML = d.videos.map(v =>
      `<option value="${v.path}">${v.name} · ${v.size_mb}MB</option>`).join('');
  } catch(e) { q('#video').innerHTML = '<option value="">扫描失败: '+e.message+'</option>'; }
}

q('#go').onclick = async () => {
  const video = q('#video').value;
  if (!video) return setHint('先选一个素材', true);
  q('#go').disabled = true; q('#stop').disabled = false;
  q('#rescard').style.display = 'none';
  q('#progcard').style.display = 'block';
  setHint('已提交…');
  try {
    const body = {video, k: Number(q('#k').value), balance: Number(q('#balance').value),
                  canvas: q('#canvas').value, fps: Number(q('#fps').value)};
    const r = await api('/api/start', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    JOB = r.id;
    if (r.deduped) setHint('同一个任务已在跑，复用它（不会重复产出）');
    poll();
  } catch(e) { setHint('提交失败: '+e.message, true); q('#go').disabled=false; q('#stop').disabled=true; }
};

q('#stop').onclick = async () => {
  if (!JOB) return;
  q('#stop').disabled = true;
  setHint('正在停止（已拿到的帧会保留）…');
  await api('/api/stop', {method:'POST', headers:{'Content-Type':'application/json'},
                          body: JSON.stringify({id: JOB})});
};

function poll() {
  clearTimeout(TIMER);
  TIMER = setTimeout(async () => {
    if (!JOB) return;
    let j;
    try { j = await api('/api/job?id=' + encodeURIComponent(JOB)); }
    catch(e) { setHint('查询失败: '+e.message, true); return poll(); }
    q('#bar').style.width = (j.frac*100).toFixed(1)+'%';
    q('#stage').textContent = stageName(j.stage) + ' · ' + (j.msg||'') + ' · ' + j.elapsed + 's';
    if (j.status === 'done') { finish(j); return; }
    if (j.status === 'failed') { fail(j); return; }
    if (j.status === 'interrupted') { interrupted(j); return; }
    setHint(j.status === 'running' ? '运行中…' : '排队中…');
    poll();
  }, 700);
}

function stageName(s) {
  return {decode:'解码', select:'选帧', export_frames:'导出单帧',
          sheets:'拼图', done:'完成'}[s] || s || '—';
}

function finish(j) {
  q('#go').disabled = false; q('#stop').disabled = true;
  setHint('完成 · ' + j.result.elapsed_s + 's');
  q('#rescard').style.display = 'block';
  const r = j.result;
  const st = [
    ['选中帧数', r.selected_k],
    ['覆盖 maxdist', r.maxdist.toFixed(1)],
    ['最大空白', r.max_gap_sec + 's'],
    ['中位空白', r.median_gap_sec + 's'],
    ['时间覆盖', (r.time_coverage?.buckets_hit||0)+'/'+(r.time_coverage?.buckets||10)],
    ['token 合计', (r.canvas_plan?.tokens_total ?? '—')],
  ];
  q('#stats').innerHTML = st.map(([k,v])=>`<div class="stat"><b>${v}</b><span>${k}</span></div>`).join('');
  const w = [];
  if (r.static_segments?.longest && r.static_segments.longest.dur_sec >= 10) {
    const L = r.static_segments.longest;
    w.push(`连续静止 ${L.dur_sec}s（${L.start_sec}s→${L.end_sec}s）—— 整段画面基本不变；`+
           `像素度量对缓慢漂移（字幕/光标）不敏感，建议抽查`);
  }
  if (r.dark_frames?.in_selection) {
    w.push(`选中帧里有 ${r.dark_frames.in_selection} 帧近乎全黑，占图集容量但不含信息`);
  }
  (r.warnings||[]).slice(0,4).forEach(x => w.push(x));
  q('#warns').innerHTML = w.map(x=>`<div class="warn">${x}</div>`).join('');
  // **整段路径一起编码**（含斜杠）：否则 `/files//Users/...` 变成双斜杠，
  // 服务端切出来的相对路径会丢一段（实测 404）。
  const fileUrl = (name) =>
    '/files/' + encodeURIComponent(r.out.replace(/\/+$/,'') + '/' + name) +
    '?token=' + TOKEN;
  q('#sheets').innerHTML = r.sheets.map(n=>
    `<img src="${fileUrl(n)}" alt="${n}">`).join('');
  q('#frames').innerHTML = r.images.slice(0,12).map(n=>
    `<img src="${fileUrl(n)}" alt="${n}">`).join('');
  if (r.images.length > 12) {
    q('#frames').innerHTML += `<div class="tag">还有 ${r.images.length-12} 张，见输出目录</div>`;
  }
}

function fail(j) {
  q('#go').disabled = false; q('#stop').disabled = true;
  setHint('失败', true);
  q('#rescard').style.display = 'block';
  q('#stats').innerHTML = '';
  q('#warns').innerHTML = `<div class="warn err">${j.error||j.msg}</div>`;
}

function interrupted(j) {
  q('#go').disabled = false; q('#stop').disabled = true;
  setHint('已停止（不是失败）', false);
  q('#rescard').style.display = 'block';
  q('#stats').innerHTML = '';
  q('#warns').innerHTML = `<div class="warn">${j.msg}</div>`;
}

loadVideos();
</script></body></html>
"""
