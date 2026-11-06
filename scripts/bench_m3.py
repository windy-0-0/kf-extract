#!/usr/bin/env python3
"""M3 基准 —— 光流距离 vs 原始 L1（同 K 的 maxdist 对照）+ 速度 + 反向样本。

用法：
  python3 scripts/bench_m3.py --m05                     # M0.5 四段素材对照表（含时间覆盖率）
  python3 scripts/bench_m3.py --anchors                 # 方向锚点（三种纹理）
  python3 scripts/bench_m3.py --m3                      # M3 主实验（fanren184@6fps）
  python3 scripts/bench_m3.py --all --json /tmp/m3.json # 全跑

## 这个基准刻意做两件事

1. **不把两个度量空间的 maxdist 直接相减**（那是假比较，见 M3-report §3.2）。
2. 每条结论都给**两种读法**：各自空间内比 uniform（相对收益）、用同一条冻结 L1 标尺给两边的
   选帧结果打分（谁选的帧更值）。判据只认后者，因为只有它跨空间可比。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from src.kf import flow as F  # noqa: E402
from src.kf.kcenter import kcenter, time_coverage  # noqa: E402
from src.kf.metrics import ANALYSIS_H, ANALYSIS_W, load_gray, maxdist, uniform_idx  # noqa: E402
from src.kf.report import CORPUS, format_table, main as m05_main, material_rows  # noqa: E402

KS = (40, 80, 160, 320)
MATERIAL = ROOT / 'eval/fanren184.mp4'
SAMPLE_FPS = 6
CACHE = pathlib.Path('/tmp/kf_m3_cache.npz')


# ---------------------------------------------------------------- 工具

def progress(i, n, every=500):
    """长任务的可观测点：别让外部以为进程死了（R35）。"""
    if i % every == 0 or i == n - 1:
        print(f'    flow {i + 1}/{n} ({100 * (i + 1) / n:.0f}%)', flush=True)


def greedy(dist, k: int, n: int):
    """最远优先贪心 —— 与 src/kf/kcenter.py::kcenter 同一算法，只为拿到距离函数直调。"""
    keys = [0]
    d = dist([0])[:, 0]
    for _ in range(k - 1):
        i = int(np.argmax(d))
        if d[i] <= 0:
            break
        keys.append(i)
        d = np.minimum(d, dist([i])[:, 0])
    return sorted(keys), d


def load_frames_and_canonical():
    """32×18 灰度帧 + 规范坐标帧堆。花 2 分钟，落 /tmp 缓存（不进仓库）。"""
    if CACHE.exists():
        z = np.load(CACHE)
        if len(z['X']) == len(z['canon']):
            print(f'  命中缓存 {CACHE}')
            return z['X'], z['canon']
    t0 = time.time()
    X = load_gray(MATERIAL, sample_fps=SAMPLE_FPS)     # (n, 576) 扁平，冻结口径
    n = len(X)
    Xhw = X.reshape(n, ANALYSIS_H, ANALYSIS_W)          # 光流要二维结构
    print(f'  抽帧 {n} 帧 ({time.time() - t0:.0f}s)', flush=True)
    t1 = time.time()
    canon = F.canonical_stack(Xhw, progress=progress)
    print(f'  规范坐标 warp 完成 ({time.time() - t1:.0f}s)', flush=True)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(CACHE, X=X.astype(np.float32), canon=canon.astype(np.float32))
    return X, canon


def speed_report(seconds: float, frames: int) -> dict:
    fps = frames / seconds if seconds > 0 else None
    return {
        'frames': frames, 'seconds': round(seconds, 1),
        'fps': round(fps, 1) if fps else None,
        # 生产判据：A4 要求 30min 素材端到端 ≤180s。这里只报光流这一步的占比。
        'a4_budget_s': 180,
        'verdict': ('不可进生产（光流一步就超 A4 预算）' if fps and 10403 / fps > 180
                    else '可以进生产（光流一步在 A4 预算内）'),
    }


# ---------------------------------------------------------------- 主实验

def run_m3(ks=KS) -> dict:
    print('=== M3 主实验：光流距离 vs 冻结 L1 ===', flush=True)
    t_all = time.time()
    X, canon = load_frames_and_canonical()
    n = len(X)
    flat = X.reshape(n, -1)

    def d_l1(keys):
        return maxdist_matrix(flat, keys)

    def d_flow(keys):
        return maxdist_matrix(canon.reshape(n, -1), keys)

    out = {'material': MATERIAL.name, 'sample_fps': SAMPLE_FPS, 'frames': n, 'rows': []}
    print(f'\n{"K":>4} | {"L1选帧":>9} {"flow选帧":>10} | {"跨空间差":>9} | '
          f'{"flow空间内 L1选/flow选":>22} | 时间分布(L1) 时间分布(flow)')
    print('-' * 96)
    for k in ks:
        if k > n:
            continue
        t0 = time.time()
        keys_l1, d1 = greedy(d_l1, k, n)
        keys_fl, d2 = greedy(d_flow, k, n)
        sel_s = time.time() - t0

        # ① 用同一条冻结 L1 标尺给两边打分（唯一跨空间可比的读数）
        cross_l1_sel = float(d_l1(keys_l1).min(1).max())
        cross_fl_sel = float(d_l1(keys_fl).min(1).max())
        # ② 各自空间内 vs uniform（看相对收益，不可跨空间比）
        u = uniform_idx(n, k)
        own_l1 = float(d_l1(keys_l1).min(1).max())
        own_l1_uniform = float(d_l1(u).min(1).max())
        own_fl = float(d_flow(keys_fl).min(1).max())
        own_fl_uniform = float(d_flow(u).min(1).max())
        own_l1_imp = (own_l1_uniform - own_l1) / own_l1_uniform * 100
        own_fl_imp = (own_fl_uniform - own_fl) / own_fl_uniform * 100
        tcl = time_coverage(keys_l1, n)
        tcf = time_coverage(keys_fl, n)

        delta = (cross_fl_sel - cross_l1_sel) / cross_l1_sel * 100 if cross_l1_sel > 0 else None
        row = {
            'k': k,
            'cross_l1_of_l1_selection': round(cross_l1_sel, 3),
            'cross_l1_of_flow_selection': round(cross_fl_sel, 3),
            'cross_delta_pct': round(delta, 1) if delta is not None else None,
            'own_l1': round(own_l1, 3), 'own_l1_uniform': round(own_l1_uniform, 3),
            'own_l1_improvement_pct': round(own_l1_imp, 1),
            'own_flow': round(own_fl, 3), 'own_flow_uniform': round(own_fl_uniform, 3),
            'own_flow_improvement_pct': round(own_fl_imp, 1),
            'time_l1': tcl, 'time_flow': tcf,
            'kcenter_seconds': round(sel_s, 1),
        }
        out['rows'].append(row)
        print(f'{k:>4} | {cross_l1_sel:>9.2f} {cross_fl_sel:>10.2f} | {delta:>8.1f}% | '
              f'{own_l1:>8.2f}/{own_l1_imp:>5.1f}% '
              f'{own_fl:>6.2f}/{own_fl_imp:>5.1f}% | '
              f'{tcl["buckets_hit"]}/10 {tcf["buckets_hit"]}/10', flush=True)

    # ---- 判据（照抄任务书，不许事后改写）
    worst = max(r['cross_delta_pct'] for r in out['rows'] if r['cross_delta_pct'] is not None)
    best = min(r['cross_delta_pct'] for r in out['rows'] if r['cross_delta_pct'] is not None)
    passed = (best <= -10.0) and (worst <= 5.0)
    out['criterion'] = {
        'rule': 'K∈{40,80,160,320}：至少一项 cross_delta ≤ -10%，且没有任何一项 > +5%',
        'best_delta_pct': best, 'worst_delta_pct': worst, 'pass': bool(passed),
        'note': '标尺 = 冻结口径 L1（src/kf/metrics.py），两边选帧结果用同一条标尺打分',
    }
    print(f'\n判据：best={best:+.1f}%  worst={worst:+.1f}%  → {"PASS" if passed else "FAIL（不达标）"}')

    # ---- 残差与运动的相关（任务书要求原样报）
    t0 = time.time()
    hi = F.load_gray_hw(MATERIAL, sample_fps=SAMPLE_FPS)
    sep = F.motion_separation(hi)
    flow_seconds = time.time() - t0
    out['motion_separation'] = sep
    out['speed'] = speed_report(flow_seconds, len(hi) - 1)
    print(f'\ncorr(残差, 运动量) = {sep["corr"]:.3f}  '
          f'(残差均值 {sep["residual_mean"]:.2f}, 原始 {sep["raw_mean"]:.2f}, 降幅 {sep["drop_pct"]}%)')
    print(f'光流速度：{out["speed"]["fps"]} 对/秒（{out["speed"]["frames"]} 对 / '
          f'{out["speed"]["seconds"]}s）→ {out["speed"]["verdict"]}')
    out['elapsed_s'] = round(time.time() - t_all, 1)
    return out


def maxdist_matrix(P: np.ndarray, keys) -> np.ndarray:
    """(N, K) 距离矩阵，自适应分块（与 metrics.maxdist 同一预算，D-011）。"""
    sel = P[np.asarray(keys)]
    n = len(P)
    per_row = max(1, len(sel)) * P.shape[1] * 4
    chunk = max(1, int(100 * 1e6 / per_row))
    out = np.empty((n, len(sel)), np.float32)
    for s in range(0, n, chunk):
        e = min(n, s + chunk)
        out[s:e] = np.abs(P[s:e, None, :] - sel[None, :, :]).mean(2)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--m05', action='store_true', help='跑 M0.5 对照表')
    ap.add_argument('--anchors', action='store_true', help='跑方向锚点')
    ap.add_argument('--m3', action='store_true', help='跑 M3 主实验')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--json')
    a = ap.parse_args()
    if not (a.m05 or a.anchors or a.m3 or a.all):
        a.all = True

    result = {'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S')}

    if a.anchors or a.all:
        print('\n=== 方向锚点（合成图已知右移 4px）===', flush=True)
        r = F.anchor_self_check(kind='all')
        result['anchors'] = r
        for s in r['sub']:
            print(f'  {s["kind"]:<8} raw={s["raw"]:>8.4f}  correct(x+fx)={s["correct"]:>8.4f}  '
                  f'reversed(x-fx)={s["reversed"]:>8.4f}  '
                  f'{"OK" if s["ok"] else "FAIL"}')
        print(f'  全部通过: {r["ok"]}')

    if a.m05 or a.all:
        print('\n=== M0.5 对照表 ===', flush=True)
        argv = ['--verify-baseline']
        if a.json:
            argv += ['--json', str(pathlib.Path(a.json).with_name('m05-rows.json'))]
        m05_main(argv)

    if a.m3 or a.all:
        result['m3'] = run_m3()

    if a.json:
        p = pathlib.Path(a.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'\n写出 {p}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
