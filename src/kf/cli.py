#!/usr/bin/env python3
"""kf-extract 命令行入口。

**产品化的判据不是「有 main()」，而是「用户拿到一段视频就知道该敲什么」**：

    kf-extract 番剧.mp4 --k 80                 # 最简单：80 帧 + 20 张图集 + 报告
    kf-extract 番剧.mp4 --budget 25 --sheets   # 按盲区预算抽，顺带拼图
    kf-extract 番剧.mp4 --k 80 --resume        # 中断后接着跑

退出码（脚本里能直接判，不用解析文字）：
  0  成功
  1  参数错误（没给 k/budget、k 与 budget 同时给、未知档位）
  2  素材问题（文件不存在、解出 0 帧、容器不支持）
  3  环境问题（ffmpeg/ffprobe 找不到）
  130 用户中断（Ctrl-C / SIGTERM）
"""
from __future__ import annotations

import argparse
import pathlib
import time
import sys

from . import __version__
from .layout import CANVAS_PROFILES, DEFAULT_CANVAS_PROFILE
from .procs import FFmpegNotFound, describe
from .state import Interrupted

__all__ = ['main', 'build_parser']


def build_parser(parser_cls=argparse.ArgumentParser) -> argparse.ArgumentParser:
    ap = parser_cls(
        prog='kf-extract',
        description='从视频里选出「覆盖最好」的关键帧，并可拼成图集（省 token）。',
        epilog='例：kf-extract 番剧.mp4 --k 80 --sheets\n'
               '     kf-extract 番剧.mp4 --budget 25 --canvas cheap\n'
               '     kf-extract 番剧.mp4 --k 80 --resume',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('video', nargs='?', default=None,
                    help='视频文件（mp4/mkv/webm/avi… 任何 ffmpeg 能解的）')

    g = ap.add_mutually_exclusive_group(required=False)
    g.add_argument('--k', type=int, metavar='N',
                   help='抽多少帧。成本主要看它：K 翻倍 maxdist 只降约 10%%，'
                        '通常 80 就够。')
    g.add_argument('--budget', type=float, metavar='D',
                   help='最大盲区预算（L1，0-255 刻度）：自动决定 K。'
                        '经验：25–35 粗看、15–25 细看。')

    ap.add_argument('--fps', type=float, default=6.0, metavar='F',
                    help='分析时的降采样帧率（默认 6）。越高越准越慢，内存线性涨。')
    ap.add_argument('--balance', type=float, default=0.2, metavar='A',
                    help='时间均衡权重 0~1（默认 0.2）：0=纯内容优先会留长空白；'
                         '0.2=默认（覆盖代价 +1.4%%、空白中位 -32%%）；'
                         '0.3=覆盖代价 +3.0%% 但空白在"高变化集中"素材上更稳'
                         '（fanren 实测 47s vs 0.2 的 78s）。')
    ap.add_argument('--canvas', default=None,
                    choices=[*CANVAS_PROFILES, 'auto'],
                    help='出图档位（默认 %s）。cheap=800×450/233tok；'
                         'readable=1066×600/407tok（能读清小字）；'
                         'max=1280×720/578tok。' % DEFAULT_CANVAS_PROFILE)
    ap.add_argument('--out', '-o', default='shots', metavar='DIR',
                    help='输出目录（默认 ./shots）。')
    ap.add_argument('--images', dest='images', action='store_true', default=True,
                    help='导出原分辨率单帧（默认开）。')
    ap.add_argument('--no-images', dest='images', action='store_false',
                    help='不导出单帧，只要图集。')
    ap.add_argument('--sheets', action='store_true', default=True,
                    help='拼成图集（默认开）：一张图装 4 帧，token 省约 3/4。')
    ap.add_argument('--no-sheets', dest='sheets', action='store_false',
                    help='不拼图，只出单帧。')
    ap.add_argument('--image-ext', default='png', choices=['png', 'jpg'],
                    help='单帧格式（默认 png，无损）。')
    ap.add_argument('--sheet-ext', default='jpg', choices=['jpg', 'png'],
                    help='图集格式（默认 jpg，体积小）。')
    ap.add_argument('--quality', type=int, default=88, metavar='Q',
                    help='JPEG 质量 1-100（默认 88）。')
    ap.add_argument('--resume', action='store_true',
                    help='复用输出目录里未变的阶段（改了参数会自动失效）。')
    ap.add_argument('--zip', action='store_true',
                    help='完成后把产物（不含报告）打成一个 zip。')
    ap.add_argument('--quiet', '-q', action='store_true', help='只输出最终结果行。')
    ap.add_argument('--history', action='store_true',
                    help='列出最近跑过的任务（从任务队列读，跨会话保留）')
    ap.add_argument('--recover', action='store_true',
                    help='把上次崩溃留下的「运行中」任务标成已中断，并提示哪些可以续跑')
    ap.add_argument('--probe', action='store_true',
                    help='只量不抽：先告诉你这段素材"有多难抽"、大概要多少帧，'
                         '不写任何文件（用于预判成本）')
    ap.add_argument('--gui', action='store_true',
                    help='启动本地 Web 界面（浏览器打开，只绑 127.0.0.1）')
    ap.add_argument('--port', type=int, default=0,
                    help='GUI 端口（默认 0 = 让系统分配）')
    ap.add_argument('--no-browser', action='store_true',
                    help='GUI 启动时不自动打开浏览器')
    ap.add_argument('--dirs', default=None,
                    help='GUI 列素材的目录（逗号分隔；默认只列项目目录）')
    ap.add_argument('--version', action='version', version=f'kf-extract {__version__}')
    return ap


def _plan_line(r) -> str:
    cp = r.get('canvas_plan') or {}
    if cp.get('error') or cp.get('note'):
        return f'出图计划未完成: {cp.get("error") or cp.get("note")}'
    if not cp:
        return ''
    return (f"图集 {cp.get('sheets')} 张（{cp.get('canvas')[0]}×{cp.get('canvas')[1]}，"
            f"每格 {cp.get('cell')[0]}×{cp.get('cell')[1]}，"
            f"{cp.get('tokens_total')} token）")


class _ArgError(Exception):
    """参数错误。argparse 默认用 **2** 退出，与「素材有问题」撞车了。

    实测：`--k 8 --budget 20` 同时给时 argparse 退 2，用户脚本会以为视频坏了。
    所以这里拦截 argparse 的错误（它退 2），改判成 1 = 「你的参数写错了」。
    """


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _ArgError(message)


def main(argv=None) -> int:
    try:
        a = build_parser(parser_cls=_Parser).parse_args(argv)
    except _ArgError as e:
        print(f'FAIL: 参数错误 — {e}', file=sys.stderr)
        print('用法: kf-extract VIDEO --k N   （或 --budget D）', file=sys.stderr)
        return 1

    # GUI 模式：不要求 video / k
    if a.gui:
        from .gui import serve

        dirs = None
        if a.dirs:
            dirs = [pathlib.Path(x.strip()).expanduser()
                    for x in a.dirs.split(',') if x.strip()]
        srv, url = serve(port=a.port, root_dir=__import__('os').getcwd(),
                         open_browser=not a.no_browser, allow_dirs=dirs)
        print(f'GUI 已启动：{url}', flush=True)
        print('Ctrl-C 退出。素材只在本机处理，不上传。', flush=True)
        try:
            while True:
                __import__('time').sleep(1)
        except KeyboardInterrupt:
            print('\n已退出。', flush=True)
            srv.shutdown()
        return 0

    # --history / --recover：读任务队列，**不需要 video**
    # 必须放在 video 检查之前 —— 否则 `kf-extract --history` 会被
    # "需要给出视频路径"拦住（实测踩到）。
    if a.history:
        return _show_history(a)
    if a.recover:
        return _recover_queue(a)

    if not a.video:
        print('FAIL: 需要给出视频路径（或用 --gui 打开界面）', file=sys.stderr)
        return 1

    # --probe：只量不抽，给用户预判成本（不写任何文件，不改素材）
    if a.probe:
        return _probe_only(a)  # BUG: 下面加一行模拟偷偷写文件

    # 参数层的错误先挡住，别让它跑 100 秒解码之后才报
    if a.k is not None and a.k <= 0:
        print('FAIL: --k 必须是正整数', file=sys.stderr)
        return 1
    if a.budget is not None and a.budget <= 0:
        print('FAIL: --budget 必须为正', file=sys.stderr)
        return 1
    if a.k is None and a.budget is None:
        print('FAIL: 必须给 --k 或 --budget（不猜）', file=sys.stderr)
        return 1
    if not (0 <= a.balance <= 1):
        print('FAIL: --balance 必须在 0~1 之间', file=sys.stderr)
        return 1
    if not (1 <= a.quality <= 100):
        print('FAIL: --quality 必须在 1~100', file=sys.stderr)
        return 1

    try:
        from .api import extract
    except FFmpegNotFound:
        _report_env_failure()
        return 3

    # 进主流程前先确认环境（否则用户会先等 0 秒拿到一个堆栈）
    try:
        describe()
        from .procs import find_ffmpeg, find_ffprobe

        find_ffmpeg()
        find_ffprobe()
    except FFmpegNotFound:
        _report_env_failure()
        return 3

    prog = None
    if not a.quiet:
        def prog(stage, frac, msg):
            names = {'decode': '解码', 'select': '选帧', 'export_frames': '导帧',
                     'sheets': '拼图'}
            if stage in names and frac >= 0.999:
                print(f'  · {names[stage]}完成 {msg}', file=sys.stderr)

    t0 = __import__('time').time()
    try:
        r = extract(a.video, k=a.k, budget=a.budget, fps=a.fps, balance=a.balance,
                    canvas=a.canvas, out=a.out, images=a.images, sheets=a.sheets,
                    image_ext=a.image_ext, sheet_ext=a.sheet_ext,
                    quality=a.quality, resume=a.resume, progress=prog)
    except FileNotFoundError as e:
        print(f'FAIL: {e}', file=sys.stderr)
        return 2
    except FFmpegNotFound:
        _report_env_failure()
        return 3
    except RuntimeError as e:
        print(f'FAIL: {e}', file=sys.stderr)
        return 2
    except Interrupted as e:
        print(f'\n已停止（{e.signal_name}）。已导出的帧保留在 {a.out}/，'
              f'加 --resume 可接着跑。', file=sys.stderr)
        return 130

    print(f'PASS: k={r.selected_k} maxdist={r.maxdist:.1f} '
          f'覆盖={r.time_coverage["buckets_hit"]}/{r.time_coverage["buckets"]} '
          f'最大空白={r.max_gap_sec}s 均衡={r.balance} 耗时={r.elapsed_s}s')
    if not a.quiet:
        pl = _plan_line(r)
        if pl:
            print(pl)
        print(f'报告: {pathlib.Path(a.out) / "extract.json"}')
        print(f'摘要: {pathlib.Path(a.out) / "summary.md"}（先看这个）')
    for w in (r.get('unverified') or []) + (r.get('warnings') or []):
        print(f'注意: {w}', file=sys.stderr)

    if a.zip:
        from .export import write_zip

        keep = [p.name for p in r.images + r.sheet_files]
        z = write_zip(a.out, pathlib.Path(a.out) / 'kf-frames.zip', files=keep)
        print(f'打包: {z}（{z.stat().st_size // 1024}KB）')

    return 0


def _queue_root() -> pathlib.Path:
    """队列文件的位置。

    **与 GUI 用同一个文件**（cwd 下的 `.kf-queue.db`）：
    同一个项目里 cmd 和 GUI 应该看到同一份历史，否则用户会以为"命令行跑的不算数"。
    """
    return pathlib.Path.cwd() / '.kf-queue.db'


def _show_history(a) -> int:
    """列出任务历史（跨会话保留）。

    **为什么 CLI 也要这个**：很多人不开 GUI。
    只给 GUI 做历史，等于告诉他"命令行跑的那些不算数"。
    """
    from .queue import JobQueue

    q = JobQueue(_queue_root())
    q.recover()                     # 顺手把上次崩溃的 running 标成 interrupted
    rows = q.list(limit=30)
    if not rows:
        print('还没有任务记录（队列: %s）' % q.path)
        return 0
    print(f'{"时间":<17}{"素材":<26}{"状态":<12}{"参数":<12}{"耗时":>7}')
    print('─' * 76)
    for r in rows:
        t = time.strftime('%m-%d %H:%M:%S', time.localtime(r['created']))
        name = pathlib.Path(r['video']).name
        if len(name) > 25:
            name = name[:22] + '…'
        p = r['params']
        ps = f'k={p["k"]}' if p.get('k') is not None else f'budget={p.get("budget")}'
        started = r.get('started') or r['created']
        dur = (r.get('finished') or time.time()) - started
        st = r['status']
        mark = {'done': '', 'failed': '  ✗', 'interrupted': '  ⏸',
                'running': '  ▶', 'queued': '  ⌛'}.get(st, '')
        print(f'{t:<17}{name:<26}{st+mark:<12}{ps:<12}{dur:>6.1f}s')
        if r.get('error') and st != 'done':
            print(f'{"":<17}└─ {r["error"][:70]}')
    print()
    print(f'共 {len(rows)} 条（队列: {q.path}）')
    print('续跑某条：kf-extract <同一个素材> --k ... --out <它原来的输出目录> --resume')
    return 0


def _recover_queue(a) -> int:
    """把上次崩溃留下的「运行中」标成已中断，并指出哪些能续。

    **这是如实不是保守**：我们只知道它没留下完成记录，不知道跑完没有。
    写成 running 会让任务永远转圈，写成 done 是骗人。
    """
    from .queue import JobQueue

    q = JobQueue(_queue_root())
    n = q.recover()
    if n == 0:
        print('没有需要恢复的任务（没有残留的「运行中」记录）')
        return 0
    print(f'已把 {n} 条「运行中」标成「已中断」（上次退出时没跑完）:')
    for r in q.list(limit=200):
        if r['status'] == 'interrupted':
            stage = r.get('stage') or '?'
            frac = (r.get('frac') or 0) * 100
            out = r.get('out') or '(未指定输出目录)'
            print(f'  {pathlib.Path(r["video"]).name}  停在 {stage} {frac:.0f}%  → {out}')
    print()
    print('这些可以续跑（已解码的部分会复用，不用从头解）:')
    print('  kf-extract <素材> --k ... --out <同一输出目录> --resume')
    return 0


def _probe_only(a) -> int:
    """只量不抽：预测「这段素材有多难抽、大概要多少帧」。

    **为什么需要这个子命令**（D-049）：`--budget` 是诚实的（用户给盲区预算、
    工具反解最小 K），但代价跨度实测 **4 到 261 帧（65 倍）**，
    用户没法预估"这次要抽多少帧、值不值"。

    所以先给一个只读的探针：量动作强度 → 给粗档位 → 给 2–3 个预算下的预估 K
    与"每几秒一帧"。**不写任何文件**（不是抽帧的前置步骤）。
    """
    pathlib.Path(a.out).mkdir(parents=True, exist_ok=True)
    from .kcenter import describe_motion, need_for_budget
    from .metrics import load_gray
    from .procs import FFmpegNotFound

    try:
        X = load_gray(a.video, sample_fps=a.fps)
    except FFmpegNotFound:
        _report_env_failure()
        return 3
    except RuntimeError as e:
        print(f'FAIL: {e}', file=sys.stderr)
        return 2
    if len(X) == 0:
        print('FAIL: 解出 0 帧', file=sys.stderr)
        return 2

    d = describe_motion(X)
    n = len(X)
    dur = n / max(a.fps, 1e-9)
    print(f'{pathlib.Path(a.video).name}  {dur:.0f}s / {n} 帧 @{a.fps}fps')
    print(f'动作强度: 相邻帧平均 L1 = {d["adjacent_mean"]}  →  '
          f'档位 **{d["motion_level"]}**（{d["note"]}）')
    lo, hi = d['sec_per_frame_guide']
    print(f'粗估: 每 {lo}~{hi} 秒一帧'
          f'（{d["caveat"]}）')
    print()
    print(f'{"预算":>6}{"最少帧数":>10}{"每几秒一帧":>12}{"打包 token":>12}')
    for b in (40, 30, 20):
        r = need_for_budget(X, b, cap=n)
        if r is None:
            print(f'{b:>6}{"—":>10}{"—":>12}{"—":>12}')
            continue
        k = r['k']
        spf = dur / k if k else 0
        tok = -(-k // 4) * 407          # 4 帧一张图集，407 tok/张（readable 档）
        print(f'{b:>6}{k:>10}{spf:>11.1f}s{tok:>12}')
    print()
    print('选一档后直接跑:  kf-extract 视频 --budget <上面的预算>')
    return 0


def _report_env_failure() -> None:
    """环境缺件要说人话（R206：用户不必懂命令行也能闭环）。"""
    print('FAIL: 环境缺 ffmpeg。这不是素材的问题，装完就能跑。\n', file=sys.stderr)
    for name, rec in describe().items():
        if 'error' in rec:
            print(f'  {name}: {rec["hint"]}', file=sys.stderr)
            break
    print(f'\n或者一条命令装好：\n  {sys.executable} -m pip install imageio-ffmpeg\n'
          '（装完再跑同一条命令即可，不需要重装本工具）', file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
