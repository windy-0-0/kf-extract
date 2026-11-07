"""GUI 测试：后端可测的部分 + 前端关键逻辑的静态断言。

## 这里测什么、不测什么（诚实说明）

- **测**：后端接口语义（任务历史、输出目录校验、在途去重、停止、状态不谎报）
- **测**：前端模板里**必须存在的结构**（viewer 元素、长静默提示、结构性转义）
- **不测**：真实浏览器里的点击行为 —— 那需要 puppeteer，不能在纯 Python 测试里跑。
  真机验证记录在 `docs/gui-report.md`（含具体断言输出）。

**为什么还要测前端结构**：上次我改前端时，`str.format` 撞上 CSS 花括号导致首页空响应；
另一处是 `/files/` 编码错导致 17 张图全 404。这两类问题都能被"结构存在性 + 端到端
HTTP 请求"抓到，不需要浏览器。
"""
import json
import pathlib
import re
import sys
import threading
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from kf.gui import App, Handler, _INDEX_HTML  # noqa: E402
from kf import gui as G  # noqa: E402

RESULTS = []


def _make_server(root=None):
    """起一个真服务（随机端口），返回 (srv, app, base_url, token)。

    **队列文件必须是临时的**：如果共享仓库根目录那个 `.kf-queue.db`，
    单元测试会与 e2e 留下的任务互相污染（实测：断言"初始应为空"直接失败）。
    这也说明生产队列路径本身没问题 —— 问题在测试不该用生产文件。
    """
    import tempfile

    td = tempfile.mkdtemp(prefix='kf-gui-test-')
    app = App(root_dir=root or ROOT, queue_path=pathlib.Path(td) / 'q.db')
    handler = type('H', (Handler,), {'app': app})
    srv = G._Server(('127.0.0.1', 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    return srv, app, f'http://127.0.0.1:{port}', app.token


def _get_raw(url, token):
    sep = '&' if '?' in url else '?'
    req = urllib.request.Request(f'{url}{sep}token={token}')
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read().decode('utf-8', 'replace')


def _get(url, token):
    """取 JSON。401/403 会抛 HTTPError，测试里按需捕获。"""
    code, text = _get_raw(url, token)
    try:
        return code, json.loads(text)
    except ValueError:
        return code, text


def _post(url, token, body):
    sep = '&' if '?' in url else '?'
    data = json.dumps(body).encode()
    req = urllib.request.Request(f'{url}{sep}token={token}', data=data,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


# ---------------------------------------------------------------- 安全（不许回归）
def test_binds_loopback_only():
    """只许绑 127.0.0.1。绑 0.0.0.0 会让同局域网的人用你的机器抽帧。"""
    try:
        G._Server(('0.0.0.0', 0), Handler)
    except RuntimeError as e:
        assert '127.0.0.1' in str(e), '拒绝信息没说明原因'
        return
    raise AssertionError('绑 0.0.0.0 竟然成功了 —— 安全闸门失效')


def test_token_required_everywhere():
    """无 token / 错 token 一律 403，包含新增的 /api/jobs。"""
    srv, app, base, tok = _make_server()
    try:
        for path in ('/', '/api/videos', '/api/jobs', '/api/job?id=x'):
            for bad in ('', 'wrong'):
                try:
                    _get(f'{base}{path}', bad)
                except urllib.error.HTTPError as e:
                    assert e.code == 403, f'{path} 用 {bad!r} 应 403，实际 {e.code}'
                    continue
                raise AssertionError(f'{path} 用 {bad!r} 竟然放行了')
        # 正确 token 要能过
        for path in ('/', '/api/videos', '/api/jobs'):
            code, _ = _get_raw(f'{base}{path}', tok)
            assert code == 200, f'{path} 用正确 token 应 200，实际 {code}'
    finally:
        srv.shutdown()


def test_file_route_blocks_traversal():
    """/files/ 不许越出 home 与项目目录。"""
    srv, app, base, tok = _make_server()
    try:
        for rel in ('/etc/passwd', '%2fetc%2fpasswd', '../../etc/passwd'):
            try:
                _get(f'{base}/files/{rel}', tok)
            except urllib.error.HTTPError as e:
                assert e.code in (403, 404), f'{rel} 返回 {e.code}'
                continue
            raise AssertionError(f'{rel} 竟然读到了')
    finally:
        srv.shutdown()


def test_no_delete_endpoint():
    """GUI **没有任何删除端点** —— 它不该能删用户文件（R135）。"""
    src = (ROOT / 'src' / 'kf' / 'gui.py').read_text()
    for bad in ('/api/delete', '/api/remove', '/api/unlink', 'os.remove', 'shutil.rmtree'):
        assert bad not in src, f'GUI 里出现了破坏性调用: {bad}'


# ---------------------------------------------------------------- P0-a 任务历史
def test_jobs_history_lists_and_orders():
    """任务历史：最新的在前，**且重启后还在**（这是本轮修的断点之一）。

    旧版把 Job 塞进内存 dict 就完事；现在历史从 sqlite 队列读，
    所以这条测试直接写队列，再**换一个新的 App 实例**（模拟重启）验证。
    """
    srv, app, base, tok = _make_server()
    try:
        code, d = _get(f'{base}/api/jobs', tok)
        assert code == 200 and d['jobs'] == [], '初始应为空'

        # 直接入队（不真跑 extract，避免测试依赖素材）
        q1 = app.queue.enqueue('/tmp/a.mp4', {'k': 10}, out='/tmp/o1')
        q2 = app.queue.enqueue('/tmp/b.mp4', {'k': 20}, out='/tmp/o2')
        app.queue.finish(q1, 'done', result={'selected_k': 10, 'images': ['x.png'] * 50})
        # q2 保持 queued

        code, d = _get(f'{base}/api/jobs', tok)
        jobs = d['jobs']
        assert len(jobs) == 2, f'应有 2 条，实际 {len(jobs)}'
        assert jobs[0]['name'] == 'b.mp4', '最新的应在最前'
        assert jobs[1]['name'] == 'a.mp4'
        # 列表不带完整 result（那可能很大），只给 has_result
        assert 'result' not in jobs[0], '历史列表带了 result，会让轮询变重'
        assert jobs[1]['has_result'] is True
        assert jobs[0]['status'] == 'queued'
        assert jobs[1]['status'] == 'done'

        # **重启验证**：换新 App 指向同一个队列文件
        app2 = App(root_dir=app.root, queue_path=app.queue.path)
        jobs2 = app2.list_jobs()
        assert len(jobs2) == 2, f'重启后历史丢了，只剩 {len(jobs2)} 条'
        assert {j['name'] for j in jobs2} == {'a.mp4', 'b.mp4'}
        assert app2.queue.get(q1)['result'] is not None, '重启后结果也要还在'
    finally:
        srv.shutdown()


def test_history_survives_gui_restart_with_running_job():
    """重启时「running」必须变成 interrupted —— 不许永远卡在"运行中"。

    **为什么这是如实而不是保守**：我们只知道它没留下完成记录，
    不知道它到底跑完没有。写成 running 会让任务永远转圈，
    写成 done 是骗人。只有 interrupted 能让用户知道"这条要重跑"。
    """
    srv, app, base, tok = _make_server()
    try:
        q = app.queue.enqueue('/tmp/c.mp4', {'k': 8}, out='/tmp/o3')
        app.queue.update(q, status='running')
        # 模拟「GUI 被杀」：直接新建 App 指向同一个队列
        app2 = App(root_dir=app.root, queue_path=app.queue.path)
        rec = app2.queue.get(q)
        assert rec['status'] == 'interrupted', \
            f'重启后 running 应被改成 interrupted，实际 {rec["status"]}'
        assert '退出时未完成' in (rec.get('error') or ''), '缺可读的原因说明'
        assert app2.recovered >= 1, 'recover() 没报告改了几条'
    finally:
        srv.shutdown()


def test_job_brief_has_fields_frontend_needs():
    """前端历史列表要用的字段一个都不能少（少了就是空白行）。"""
    j = G.Job('/tmp/x.mp4', {'k': 8})
    b = j.brief()
    for k in ('id', 'name', 'status', 'stage', 'frac', 'elapsed', 'started',
              'params', 'has_result'):
        assert k in b, f'brief 缺字段 {k}'


# ---------------------------------------------------------------- P0-b 输出目录
def test_out_dir_validation_gives_readable_reason():
    """非法输出目录必须给出**可读中文原因**，不能只说 400。"""
    app = App(root_dir=ROOT)
    # 系统目录必须拒
    msg = app.out_dir_error('/etc/shots')
    assert msg and '之下' in msg, f'拒绝原因不可读: {msg!r}'
    assert '/etc' in msg or 'private/etc' in msg, '没告诉用户他给的是哪个路径'
    # home 之下必须放行
    assert app.out_dir_error(str(pathlib.Path.home() / 'kf-test-out')) is None
    # 已存在且不是目录，也要说清
    f = ROOT / '__not_a_dir__'
    f.write_text('x')
    try:
        msg = app.out_dir_error(str(f))
        assert msg and '不是目录' in msg, f'非目录原因不可读: {msg!r}'
    finally:
        f.unlink()


def test_api_start_returns_structured_error():
    """拒绝时要有 code 字段，前端才能针对性处理（聚焦输入框）。"""
    srv, app, base, tok = _make_server()
    try:
        code, d = _post(f'{base}/api/start', tok,
                        {'video': '/nonexistent-xyz.mp4', 'k': 8})
        assert code == 400 and 'error' in d
        # 输出目录的拒绝要有 code
        fake = ROOT / 'eval' / 'trailer.mp4'
        if fake.exists():
            code, d = _post(f'{base}/api/start', tok,
                            {'video': str(fake), 'k': 8, 'out': '/etc/shots'})
            assert code == 400, f'应 400，实际 {code}'
            assert d.get('code') == 'bad_out_dir', f'缺 code 字段: {d}'
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- 在途去重 / 停止
def test_dedupe_uses_active_jobs_only():
    """在途去重只能复用**还在跑**的任务；已完成的不算。"""
    app = App(root_dir=ROOT)
    p1 = {'k': 8, 'out': '/x'}
    j = app.active_for('/tmp/v.mp4', p1)
    assert j is None
    j = G.Job('/tmp/v.mp4', p1)
    j.key = app._dedupe_key('/tmp/v.mp4', p1)
    j.status = 'running'
    with app.lock:
        app.jobs[j.id] = j
    assert app.active_for('/tmp/v.mp4', p1) is j, '运行中的应当被复用'
    j.status = 'done'
    assert app.active_for('/tmp/v.mp4', p1) is None, '已完成的**不该**被复用'


def test_stop_sets_flag_not_kills_thread():
    """停止只置标志（R312）。杀线程会让已导出的帧和报告处于半截状态。"""
    app = App(root_dir=ROOT)
    j = G.Job('/tmp/v.mp4', {'k': 8})
    j.status = 'running'
    with app.lock:
        app.jobs[j.id] = j
    assert app.stop(j.id) is True
    assert j.stop_requested is True, '停止没置标志'
    assert j.thread is None, '停止不应该去动线程'
    # 已结束的任务不该能停
    j.status = 'done'
    assert app.stop(j.id) is False


# ---------------------------------------------------------------- 前端结构
def test_template_renders_and_has_core_elements():
    """模板必须能渲染，且三个 P0 的界面元素都在。

    上次 `str.format` 撞上 CSS 花括号导致首页空响应 —— 这条防的是整类问题：
    模板必须能**正常替换 token** 且产出非空 HTML。
    """
    out = _INDEX_HTML.replace('%%KF_TOKEN%%', 'TOK123')
    assert 'TOK123' in out, 'token 没注入'
    assert '%%KF_TOKEN%%' not in out, '残留占位符'
    for need in ('id="jobs"', 'id="out"', 'id="viewer"', 'id="vimg"',
                 'id="hint"', 'loadJobs', 'openViewer'):
        assert need in out, f'模板缺 {need}'


def test_template_has_long_silence_notice():
    """长静默必须自己喊出来（R35）：29 分钟素材解码 40 秒，纯百分比像卡死。

    **断言必须检查"判断逻辑"而不只是字段名**（实测教训）：
    第一版只断言 `'stale_seconds' in out`，但模板里 `stale_seconds` 出现在两处，
    把条件判断那行删掉后字段名仍在，测试**照样通过**（假绿）。
    这里同时断言：① 条件比较存在 ② 阈值不是摆着看的 ③ 有一句人话提示。
    """
    out = _INDEX_HTML
    # ① 必须真的拿 stale_seconds 做比较（不是只出现在字符串拼接里）
    assert re.search(r'j\.stale_seconds\s*>=\s*\d+', out), \
        '前端没拿 stale_seconds 做阈值比较（长静默不会喊出来）'
    # ② 阈值必须在合理范围（10~30 秒；太小会刷屏，太大等于没提示）
    m = re.search(r'j\.stale_seconds\s*>=\s*(\d+)', out)
    assert 5 <= int(m.group(1)) <= 30, f'阈值不合理: {m.group(1)}s'
    # ③ 必须有一句用户读得懂的话
    assert '仍在工作' in out, '缺"仍在工作"文案'
    assert '无更新' in out, '缺"已 N 秒无更新"文案'


def test_template_escapes_user_strings():
    """所有来自后端的字符串都要过 esc() —— 文件名里可能有 < & "。"""
    out = _INDEX_HTML
    assert 'function esc(' in out, '缺转义函数'
    # 任务名必须转义（这是唯一由用户文件系统决定、进 DOM 的字符串）
    assert 'esc(j.name)' in out, '任务名没转义'


def test_no_delete_ui():
    """界面上不许出现"删除"按钮（这个 GUI 不删用户任何文件）。"""
    out = _INDEX_HTML
    for bad in ('>删除<', 'clear-all', '确认删除'):
        assert bad not in out, f'界面上出现了删除相关：{bad}'


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
