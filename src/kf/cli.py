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
    ap.add_argument('video', help='视频文件（mp4/mkv/webm/avi… 任何 ffmpeg 能解的）')

    g = ap.add_mutually_exclusive_group(required=True)
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
                         '0.2=九样本实测最优（覆盖代价 +1.4%%、空白降 32%%）；'
                         '0.3=更强均衡但覆盖代价升到 +3.0%%（最坏段 +11%%）。')
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

    # 参数层的错误先挡住，别让它跑 100 秒解码之后才报
    if a.k is not None and a.k <= 0:
        print('FAIL: --k 必须是正整数', file=sys.stderr)
        return 1
    if a.budget is not None and a.budget <= 0:
        print('FAIL: --budget 必须为正', file=sys.stderr)
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
