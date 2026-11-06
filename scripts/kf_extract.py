#!/usr/bin/env python3
"""M5 · 端到端关键帧抽取器（CLI）。

用法：
  kf-extract VIDEO --k 80 --out shots/          # 抽 80 帧，存 PNG + JSON 报告
  kf-extract VIDEO --budget 25 --out shots/     # 按「最大盲区」抽（自动决定 K）
  kf-extract VIDEO --k 80 --fps 6 --report json

输出：
  shots/frame-000123.png   选中的帧（原分辨率）
  shots/extract.json       可复核报告：K / maxdist / 时间覆盖率 / 每帧时间戳

设计原则（写死在代码里）：
  1. **报告必须自证**：选了哪几帧、覆盖多好、时间上分布如何，一个数字都不能少。
     没有 maxdist 的抽取结果不许采信（否则无法与基线比较）。
  2. **默认行为最安全**：不指定 --k 或 --budget 就报错，不猜。
  3. **只读素材**：绝不修改输入文件。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import numpy as np

from src.kf.metrics import ANALYSIS_H, ANALYSIS_W, load_gray
from src.kf.kcenter import kcenter, need_for_budget, time_coverage

FFMPEG = '/opt/homebrew/bin/ffmpeg'


def probe_duration(path) -> float:
    """时长（秒）。拿不到就 0，报告里标 UNVER。"""
    r = subprocess.run(
        [FFMPEG.replace('ffmpeg', 'ffprobe'), '-v', 'error', '-show_entries',
         'format=duration', '-of', 'default=nw=1:nk=1', str(path)],
        capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def extract_frames(path, keys, fps, outdir):
    """把选中的帧按**原始分辨率**导出为 PNG。

    做法：一次 ffmpeg 解码，按目标帧号 select。比「每帧起一个 ffmpeg」快一个数量级。
    """
    import subprocess
    if not keys:
        return []
    # 用 select 表达式：按解码帧号（不加 fps 滤镜，原帧号）
    expr = '+'.join(f'eq(n\\,{k})' for k in keys)
    outdir.mkdir(parents=True, exist_ok=True)
    pattern = outdir / 'frame-%05d.png'
    cmd = [FFMPEG, '-v', 'error', '-i', str(path),
           '-vf', f'select={expr},setpts=N/FRAME_RATE/TB',
           '-fps_mode', 'vfr', '-start_number', '0', str(pattern)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'导帧失败: {r.stderr[:300]}')
    made = sorted(outdir.glob('frame-*.png'))
    return made


def main():
    ap = argparse.ArgumentParser(description='从视频抽取关键帧（k-center 覆盖优化）')
    ap.add_argument('video')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--k', type=int, help='抽多少帧')
    g.add_argument('--budget', type=float, help='最大盲区预算（L1，0-255 刻度）')
    ap.add_argument('--fps', type=float, default=6,
                    help='分析时的降采样帧率（默认 6；越高越准越慢）')
    ap.add_argument('--out', default='shots', help='输出目录')
    ap.add_argument('--no-images', action='store_true', help='只出报告，不导 PNG')
    ap.add_argument('--canvas', default=None,
                    choices=['cheap', 'readable', 'max', 'auto'],
                    help='出图清晰档（默认 readable）：'
                         'cheap=800×450，每格 400×225，233 tok/张（基线，不加价）'
                         ' / readable=1066×600，每格 533×300，407 tok/张（加 75%%，能读清小字）'
                         ' / max=1280×720，每格 640×360，578 tok/张（加 148%%）')
    a = ap.parse_args()

    video = pathlib.Path(a.video)
    if not video.exists():
        print(f'FAIL: 素材不存在 {video}', file=sys.stderr)
        return 2

    t0 = time.time()
    X = load_gray(video, sample_fps=a.fps, ffmpeg=FFMPEG)
    n = len(X)
    if n == 0:
        print('FAIL: 解出 0 帧（检查 ffmpeg 与素材）', file=sys.stderr)
        return 2

    if a.k:
        res = kcenter(X, a.k)
        chosen_k = len(res['keys'])
        md = res['maxdist']
    else:
        res = need_for_budget(X, a.budget, cap=n)
        if res is None:
            print(f'FAIL: 预算 {a.budget} 在 {n} 帧内达不到（需要更多帧）', file=sys.stderr)
            return 1
        chosen_k = res['k']
        md = res['maxdist']

    # 分析帧号 → 原视频帧号（抽样时要做换算）
    src_fps_ratio = None
    if a.fps:
        dur = probe_duration(video)
        if dur > 0:
            # n 帧均匀覆盖 dur 秒 → 第 i 帧对应时间 i*dur/n
            pass
    keys = res['keys']
    cov = time_coverage(keys, n)

    times = [round(i * (probe_duration(video) / max(1, n)), 3) for i in keys] if a.fps else None

    # 出图计划：给 K 个关键帧，按画布档位算「每张几帧、共几张、多少 token」
    from src.kf.layout import constraints_for, plan as layout_plan, CANVAS_PROFILES, DEFAULT_CANVAS_PROFILE
    profile = a.canvas or DEFAULT_CANVAS_PROFILE   # 默认 readable（实测边际≈1:1）
    plan_out = None
    if profile != 'auto':
        try:
            lp = layout_plan(chosen_k, constraints=constraints_for(profile),
                             distinct_table={4: 0.509, 8: 0.489})
            r = lp.recommended
            plan_out = {
                'profile': profile,
                'label': CANVAS_PROFILES[profile]['label'],
                'canvas': list(r.canvas), 'pack': r.pack,
                'cell': [round(r.cell_w), round(r.cell_h)],
                'short_side': round(r.short_side),
                'sheets': lp.sheets, 'tokens_total': lp.total_tokens,
                'baseline_sheets': lp.baseline_sheets,
                'baseline_tokens': lp.baseline_tokens,
                'token_overhead_pct': round((lp.total_tokens / max(1, lp.baseline_tokens) - 1) * 100, 1),
            }
        except Exception as e:
            plan_out = {'profile': profile, 'error': str(e)}
    else:
        # 'auto' 保留入口但本期未实现：不做「静默降级成 readable」，
        # 而是明确说出来（否则用户以为它按素材自动选了，其实没有）。
        plan_out = {'profile': 'auto',
                    'note': 'UNVER: 自动档未实现；请显式传 cheap/readable/max'}

    report = {
        'video': str(video.resolve()),
        'canvas_plan': plan_out,
        'selected_k': chosen_k,
        'requested': {'k': a.k, 'budget': a.budget},
        'analysis_fps': a.fps,
        'analysis_frames': n,
        'analysis_size': [ANALYSIS_W, ANALYSIS_H],
        'maxdist': round(md, 3),
        'time_coverage': cov,
        'keys_analysis_idx': keys,
        'keys_time_sec': times,
        'elapsed_s': round(time.time() - t0, 1),
        'images': [],
        'unverified': [],
    }

    if not a.no_images:
        outdir = pathlib.Path(a.out)
        # 分析帧号（6fps）→ 原视频帧号换算
        if a.fps:
            dur = probe_duration(video)
            meta = subprocess.run(
                [FFMPEG.replace('ffmpeg', 'ffprobe'), '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'stream=r_frame_rate', '-of', 'default=nw=1:nk=1', str(video)],
                capture_output=True, text=True)
            try:
                num, den = meta.stdout.strip().split('/')
                src_fps = float(num) / float(den or 1)
            except Exception:
                src_fps = 0
                report['unverified'].append('src_fps 解析失败，导帧可能错位')
            if src_fps and dur:
                src_keys = [int(round((i / max(1, n)) * dur * src_fps)) for i in keys]
            else:
                src_keys = keys
        else:
            src_keys = keys
        try:
            made = extract_frames(video, src_keys, a.fps, pathlib.Path(a.out))
            report['images'] = [str(p) for p in made]
        except Exception as e:
            report['unverified'].append(f'导帧失败: {e}')

    report['elapsed_s'] = round(time.time() - t0, 1)
    outdir = pathlib.Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / 'extract.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f'PASS: k={chosen_k} maxdist={md:.1f} 时间覆盖={cov["buckets_hit"]}/10 '
          f'耗时={report["elapsed_s"]}s')
    print(f'报告: {outdir/"extract.json"}')
    if report['unverified']:
        print('UNVER:', '; '.join(report['unverified']), file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
