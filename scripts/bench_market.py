#!/usr/bin/env python3
"""市场对标实测 —— 与真实竞品方法在同素材、同标尺下对比。

**为什么要有这个脚本**（而不是引一段网上的数字）：

竞品的宣传数字是在**它们自己的数据集和指标**上测的（VQA 准确率、人工评分）。
我们的是像素覆盖距离（maxdist）。**跨指标比较是同义反复** ——
这正是本项目历史上最贵的一课（D-018 的"跨刻度比较"）。

所以这里只做一件事：**把竞品的方法实现出来，用我们的标尺量它们**。

参与对比的方法：
  1. `uniform`   —— 均匀采样（"每 N 秒抽一帧"，最常见的天真做法）
  2. `scenedetect` —— PySceneDetect 的镜头切点（BSD-3，5400★，业界主流）
  3. `diffpeak`  —— 帧差峰值（see-now 风格 / video-keyframe-detector 的方法）
  4. `kcenter`   —— 本项目（纯内容覆盖）

统一标尺（冻结口径，与 scripts/eval.py 完全一致）：
  - 32×18 灰度，L1 均值，0-255 刻度
  - maxdist = 任意帧到最近选中帧的最大距离（越小越好）
  - distinct_frac = 拼图内"真不同"格子占比（阈值 > 8.0）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))

import numpy as np

from kf.kcenter import kcenter_balanced, time_coverage
from kf.metrics import load_gray, maxdist, pairwise_l1, redundancy, uniform_idx

METHODS = ('uniform', 'scenedetect', 'diffpeak', 'kcenter')


def probe_dur(fp, v):
    import subprocess
    r = subprocess.run([fp, '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'default=nw=1:nk=1', str(v)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- 方法实现
def m_uniform(X, k, fps):
    return uniform_idx(len(X), k)


def m_scenedetect(X, k, fps, path=None):
    """PySceneDetect 的镜头切点：取每个镜头的中点（业界最常见的用法）。

    **必须等长到 K**：切点少于 K 时补均匀帧，多于 K 时按镜头时长选最长的 K 个。
    这样对比是公平的（所有方法都产出恰好 K 帧）。
    """
    try:
        from scenedetect import ContentDetector, detect
    except ImportError:
        raise RuntimeError('需要 scenedetect：venv/bin/pip install scenedetect')

    scenes = detect(str(path), ContentDetector(threshold=27), show_progress=False)
    n = len(X)
    dur = probe_dur('/opt/homebrew/bin/ffprobe', path) or 0
    picks = []
    for s, e in scenes:
        mid = (s.get_frames() + e.get_frames()) // 2
        if dur > 0:
            mid = int(round((s.get_seconds() + e.get_seconds()) / 2 / dur * n))
        picks.append(min(max(mid, 0), n - 1))

    if len(picks) > k:
        # 保留最长的镜头（信息量假设：长镜头里有更多内容变化）
        lens = [e.get_seconds() - s.get_seconds() for s, e in scenes]
        order = sorted(range(len(picks)), key=lambda i: -lens[i])
        picks = sorted(picks[i] for i in order[:k])
    elif len(picks) < k:
        # 补帧：用均匀采样补齐，去重后仍不足再按最远优先补
        picks = sorted(set(picks))
        need = k - len(picks)
        if need > 0:
            cand = [i for i in uniform_idx(n, k) if i not in set(picks)]
            picks = sorted(set(picks) | set(cand[:need]))
        if len(picks) < k:
            picks = fill_greedy(X, picks, k)
    return sorted(set(picks))[:k] if len(set(picks)) >= k else fill_greedy(X, picks, k)


def fill_greedy(X, sel, k):
    """最远优先补齐（与 k-center 同规则，只用于让对比公平）。

    **必须分块**：不分块的 `pairwise_l1(X, X[sel])` 在 10403 帧 × 上千已选帧时
    会生成 N×K×D 的中间数组（实测 29GB 级，直接 OOM）。
    与 metrics.maxdist 同一个理由（D-007/D-011）。
    """
    X = np.asarray(X, dtype='float32')
    sel = list(sel) or [0]
    n = len(X)
    d = np.empty(n, dtype='float32')
    for s0 in range(0, n, 4096):
        d[s0:s0 + 4096] = pairwise_l1(X[s0:s0 + 4096], X[sel]).min(axis=1)
    while len(set(sel)) < k:
        i = int(d.argmax())
        if d[i] <= 0:
            break
        sel.append(i)
        d = np.minimum(d, pairwise_l1(X, X[[i]])[:, 0])
        if len(set(sel)) >= n:
            break
    return sorted(set(sel))


def m_diffpeak(X, k, fps):
    """帧差峰值：与相邻帧差异最大的一批帧（see-now / video-keyframe-detector 同形态）。

    `ratio`/`abs_min` 取自 see-now 的判据（cutAbs=12 一族），这里用纯帧差。
    """
    X = np.asarray(X, dtype='float32')
    if len(X) < 3:
        return list(range(min(k, len(X))))
    prev = X[:-2]
    nxt = X[2:]
    d = np.abs(prev - nxt).mean(axis=1)          # 前后帧差（尖峰检测同形态）
    order = np.argsort(-d)
    picks = sorted(int(i) + 1 for i in order[:k])  # +1 因为 d 从第 1 帧开始
    if len(picks) < k:
        picks = fill_greedy(X, picks, k)
    return picks[:k]


def m_kcenter(X, k, fps):
    r = kcenter_balanced(X, k, fps=fps, alpha=0.3)
    return r['keys']


# ---------------------------------------------------------------- 评测
def evaluate(X, keys, fps, n):
    md = maxdist(X, keys)
    red = redundancy(X[keys], pack=4)
    cov = time_coverage(keys, n)
    ks = sorted(keys)
    gaps = np.diff(ks) if len(ks) > 1 else np.array([n])
    return {
        'k': len(keys), 'maxdist': round(md, 3),
        'distinct_frac': red['distinct_frac'],
        'max_gap_sec': round(float(gaps.max()) / max(fps, 1e-9), 1),
        'coverage': f'{cov["buckets_hit"]}/{cov["buckets"]}',
    }


def render_table(rows, title):
    print(f'\n=== {title} ===')
    print(f'{"方法":<12} {"maxdist":>9} {"相对best":>9} {"真不同":>8} {"最大空白":>9} {"覆盖":>7}')
    best = min(r['maxdist'] for r in rows.values() if r.get('maxdist') is not None)
    for name, r in rows.items():
        if r.get('maxdist') is None:
            print(f'{name:<12} {"FAIL":>9}  {r.get("note","")[:40]}')
            continue
        rel = (r['maxdist'] / best - 1) * 100
        mark = ' ←' if r['maxdist'] == best else ''
        print(f'{name:<12} {r["maxdist"]:>9.2f} {rel:>8.1f}% {r["distinct_frac"]:>8.2f} '
              f'{r["max_gap_sec"]:>9.1f} {r["coverage"]:>7}{mark}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('video', nargs='*', default=None)
    ap.add_argument('--k', type=int, nargs='+', default=[20, 40, 80])
    ap.add_argument('--fps', type=float, default=None,
                    help='分析帧率；默认按 corpus.json 里该样本的 fps')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()

    files = a.video or [str(p) for p in sorted((ROOT / 'eval').glob('*.mp4'))]
    out = {}
    for f in files:
        f = pathlib.Path(f)
        if not f.exists():
            continue
        fps = a.fps or 6.0
        t0 = time.time()
        X = load_gray(f, sample_fps=fps)
        n = len(X)
        pair = f'{n} 帧 @ {fps}fps'
        print(f'\n{"="*70}\n{f.name}  ({pair}, 时长 {probe_dur("/opt/homebrew/bin/ffprobe", f):.0f}s)')
        out[f.name] = {}
        for k in a.k:
            print(f'  K={k} ...', flush=True)
            rows = {}
            for m in METHODS:
                t = time.time()
                try:
                    fn = {'uniform': m_uniform, 'diffpeak': m_diffpeak, 'kcenter': m_kcenter}[m]
                    keys = fn(X, k, fps)
                    rows[m] = evaluate(X, keys, fps, n)
                except Exception as e:
                    rows[m] = {'maxdist': None, 'note': f'{type(e).__name__}: {e}'}
                rows[m]['t'] = round(time.time() - t, 2)
            # scenedetect 单独处理（需要文件路径、且更慢）
            try:
                t = time.time()
                keys = m_scenedetect(X, k, fps, path=f)
                rows['scenedetect'] = evaluate(X, keys, fps, n)
                rows['scenedetect']['t'] = round(time.time() - t, 2)
            except Exception as e:
                rows['scenedetect'] = {'maxdist': None, 'note': f'{type(e).__name__}: {e}'}
            out[f.name][k] = rows
            render_table(rows, f'{f.name} K={k}')
        print(f'\n  总耗时 {time.time()-t0:.1f}s')

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
