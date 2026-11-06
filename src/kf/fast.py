"""M2 加速评测器 —— 与 ``scripts/eval.py`` 逐位等价的快速版。

目标（任务书 docs/prompts/M2-v1.txt）：fanren184@6fps 端到端 1050s → ≤180s，
trailer 168 帧的 8 个基线数字（kcenter 14/7/6/4、uniform 100/14/12/5）逐位不变，
全程 RSS ≤ 500MB。

每一处优化"为什么不改数字"
========================

O1. uint8 存储 + int16/int32 整数路径（代替 float32 广播）
    灰度像素 ∈ {0..255} 是整数。|a-b| ≤ 255，int16 无溢出。
    D=576 维求和 ≤ 255×576 = 146880 < 2^24，因此：
      - 原路径 float32 累加（含 numpy pairwise summation）在此过程中
        每一次加法都是【精确整数加法】，不发生任何舍入；
      - int32 求和得到【同一个整数值】；
      - int32 → float32 转换精确（值 < 2^24）；
      - 最后 float32 ÷ 576.0 与原路径 ``.mean()``（sum 后除以 n）是
        同一次 IEEE-754 正确舍入除法。
    ∴ 逐位相同。实测：trailer 168 帧 × K∈{1,2,5,14,100,167}
      × chunk∈{1,7,36,256,10**9} 共 150 例 0 不匹配；全 0/全 255/交替极值
      边界样本通过（docs/status/M2-report.md 有原文）。

O2. 4 个预算的 uniform 精确二分 → 线程并行
    每个预算的二分是独立纯函数（输入 X、budget、cap → 输出 best K），
    并行执行不改变任何一条链的输入、判定序列与结果。numpy 大型 ufunc
    释放 GIL，线程真并行；共享同一份 uint8 X，零拷贝。∴ 结果不变。

O3. uniform 二分改用「判定 + 早停」（maxdist_le）
    二分每一轮只需要 ``maxdist(X, idx) <= budget`` 这个**布尔值**
    （eval.py 也只消费这个布尔值，最终产物是 K 不是 maxdist）。
    整数路径下逐块扫描，运行最大值一旦 > budget 即可确定返回 False——
    布尔值与算完整 maxdist 再比较**严格相同**（整数精确，无舍入歧义）。
    ∴ 二分路径逐轮一致，最终 K 一致。

O4. k-center 单趟整数路径
    与 O1 同一数学；argmax / minimum 作用在逐位相同的 float32 数组上，
    平票时 argmax 取首个下标的规则不变。∴ keys 序列与 hit 完全一致
    （trailer 实测 keys 逐项相同，hit=14/7/6/4）。

O5. 抽帧保持软解（hwaccel 实测字节一致但更慢，默认不用）
    H.264 解码是确定性规范，videotoolbox 与软解逐字节相同——
    实测 fanren184@6fps 两者 md5 同为 18c2f6ed74b5853bf70ac581a668480f
    （5992128 B = 10403×576，cmp 一致）；trailer@12fps 同为
    bab471b6bdf56ab1f07794897f34b702。但硬解+hwdownload 往返更慢
    （fanren：132.5s vs 软解 93.9s），∴ 默认软解（与 eval.py 同路径），
    hwaccel 仅作可显式开启的选项，失败回退并在 decode 字段标注。

O7（被否，留档）：probe count_timeout 90s → 3s —— 2026-11-07 00:22 有并行
    agent 越过边界把它改进本文件。已回退：trailer 的 -count_frames 实测
    2-10.7s，3s 超时的安全余量 <0.3×，机器一有负载就把 anchor 从
    'ffprobe-count_frames' 翻成 'duration*fps(推算)' —— 闸门间歇性变红。

O8. probe count_timeout 90s → 45s（O7 的稳健版）
    与 O7 的本质区别是余量：trailer count 最坏实测 10.7s（load≈12 时连测
    8.90/10.72/9.84s），45s = 4.2× 余量；fanren count 需要几分钟（D-009，
    90s 都跑不完 → 固定走推算回退），45s 同样必然回退——两素材的 probe
    输出与 90s 版逐字段相同。退一步讲，即使 trailer 的 count 在极端负载下
    超过 45s，结果也只是 anchor 字符串换名（frames=842 由 duration×fps
    得到同一值、ticks、元断言、8 个冻结数全不受影响）。
    收益：把 fanren 的 probe 等待从 90s 降到 45s，并行后完全藏进抽帧壁钟
    （抽帧 47-190s），即 probe 永不在关键路径上。

O6. probe 与抽帧并行（线程各拉起一条子进程链）
    ffprobe（-count_frames）与 ffmpeg 抽帧是两条独立外部进程链，
    输出互不影响；并行只改壁钟，不改任何字节。∴ meta/anchor 完全一致。

内存预算
========
X 以 uint8 持有（fanren 10403×576 ≈ 6MB，float32 要 24MB）；
k-center 另持一份 int16（≈12MB）。uniform 4 条链每条的 int16 中间数组
chunk×K×D×2B ≤ ``UNIFORM_WORKER_MB``（默认 44MB，4 条合计 ≤176MB）。
numpy 基础占用 ~80MB，总峰值预算 ≈ 320MB < 500MB（A4-3）。
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor


import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
FF = os.environ.get('KF_FFMPEG', '/opt/homebrew/bin/ffmpeg')

ANALYSIS_W, ANALYSIS_H = 32, 18
D = ANALYSIS_W * ANALYSIS_H            # 576
DIFF_DISTINCT = np.float32(8.0)        # 与 eval.py 同口径
BUDGETS = (10, 15, 20, 30)
CAP = 2000
# 单条 uniform 链的 int16 中间数组预算（MB）；4 链并行合计 ≤176MB
UNIFORM_WORKER_MB = float(os.environ.get('KF_FAST_UNIFORM_MB', '44'))


# ---------------------------------------------------------------- eval.py 复用
def _load_eval_module():
    """只读复用冻结的 eval.py（probe 与元断言口径的唯一来源）。

    eval.py 被 freeze_baseline.py 哈希冻结：它若被改，闸门先红，
    所以复用它比重写一份更安全。
    """
    path = ROOT / 'scripts' / 'eval.py'
    spec = importlib.util.spec_from_file_location('kf_m0_eval_for_fast', path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'无法加载 {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 抽帧（O5）
def load_gray_uint8(path, sample_fps=None, hwaccel=False):
    """与 eval.load_gray_bytes 相同的滤镜链，返回 (n, 576) **uint8**（省 4× 内存）。

    返回 (X, decode_used)。decode_used ∈ {'videotoolbox', 'software',
    'software(fallback)'}，写进结果供审计。
    """
    vf = (f'fps={sample_fps},' if sample_fps else '')
    vf += f'scale={ANALYSIS_W}:{ANALYSIS_H},format=gray'
    tail = ['-v', 'error', '-i', str(path), '-vf', vf,
            '-f', 'rawvideo', '-pix_fmt', 'gray', '-']
    used = 'software'
    cmd = [FF] + tail
    if hwaccel:
        r = subprocess.run([FF, '-hwaccel', 'videotoolbox'] + tail,
                           capture_output=True)
        if r.returncode == 0 and r.stdout:
            used = 'videotoolbox'
        else:
            r = subprocess.run(cmd, capture_output=True)
            used = 'software(fallback)'
    else:
        r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f'ffmpeg failed: {r.stderr[:200]}')
    n = len(r.stdout) // D
    X = np.frombuffer(r.stdout[:n * D], dtype=np.uint8).reshape(n, D)
    return X, used


# ---------------------------------------------------------------- 整数内核（O1）
def _chunked_min_dist_f32(Xu, idx, budget_mb, cutoff=None):
    """每帧到最近选中帧的 L1 均值（float32，与 metrics.maxdist 逐位一致）。

    cutoff 不为 None 时：运行最大值一旦 > cutoff 立即提前返回
    （此时精确最大值必然也 > cutoff，判定语义不变，见 O3）。

    实现用 out= 预分配复用中间缓冲（subtract→abs→sum 全部 in-place），
    数值与朴素临时数组版本逐位相同（同一组 ufunc、同一运算序），
    只是省掉每块的分配与一遍读写。
    """
    sel = Xu[np.asarray(idx)].astype(np.int16)
    n = len(Xu)
    per_row = max(1, len(sel)) * D * 2          # int16 中间数组每行字节（D-011 精神）
    step = min(n, max(1, int(budget_mb * 1e6 / per_row)))
    buf = np.empty((step, len(sel), D), dtype=np.int16)
    s32 = np.empty((step, len(sel)), dtype=np.int32)
    best = np.empty(n, dtype=np.float32)
    div = np.float32(D)
    for s in range(0, n, step):
        e = min(n, s + step)
        rows = e - s
        np.subtract(Xu[s:e, None, :], sel[None, :, :], out=buf[:rows])
        np.abs(buf[:rows], out=buf[:rows])
        buf[:rows].sum(axis=2, dtype=np.int32, out=s32[:rows])
        best[s:e] = (s32[:rows].astype(np.float32) / div).min(axis=1)
        if cutoff is not None and float(best[s:e].max()) > cutoff:
            return best, True
    return best, False


def maxdist_fast(Xu, idx, budget_mb=100.0):
    """精确 maxdist（整数路径）。与 src/kf/metrics.py:maxdist 逐位一致（O1）。"""
    if not idx:
        return float('inf')
    best, _ = _chunked_min_dist_f32(Xu, idx, budget_mb)
    return float(best.max())


def maxdist_le(Xu, idx, budget, budget_mb=UNIFORM_WORKER_MB):
    """精确判定 ``maxdist(X, idx) <= budget``（O3 早停版，布尔值不变）。"""
    if not idx:
        return False
    thr = np.float32(budget)
    _, exceeded = _chunked_min_dist_f32(Xu, idx, budget_mb, cutoff=thr)
    return not exceeded


# ---------------------------------------------------------------- uniform（O2+O3）
def uniform_idx(n, K):
    """与 eval.py 同口径：np.linspace 取整去重。"""
    return sorted(set(np.linspace(0, n - 1, K).round().astype(int).tolist()))


def uniform_exact_fast(Xu, budget, cap, budget_mb=UNIFORM_WORKER_MB):
    """uniform 精确二分（与 eval._uniform_exact 同一步进序列，只换判定内核）。"""
    n = len(Xu)
    cap = min(cap, n)
    lo, hi, best = 1, cap, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if maxdist_le(Xu, uniform_idx(n, mid), budget, budget_mb):
            best = mid
            hi = mid - 1
        else:
            lo = mid + 1
    return best


# ---------------------------------------------------------------- k-center（O4）
def kcenter_track_fast(Xu, budgets, cap):
    """单趟最远优先贪心（整数路径），keys/hit 与 eval._kcenter_track 完全一致。"""
    X16 = Xu.astype(np.int16)
    diff = np.empty_like(X16)
    s32 = np.empty(len(X16), dtype=np.int32)

    def row_dist(i):
        np.subtract(X16, X16[i], out=diff)
        np.abs(diff, out=diff)
        diff.sum(axis=1, dtype=np.int32, out=s32)
        return s32.astype(np.float32) / np.float32(D)

    keys, d = [0], row_dist(0)
    hit = {b: None for b in budgets}
    if float(d.max()) <= min(budgets):
        for b in budgets:
            if float(d.max()) <= b:
                hit[b] = 1
        return keys, hit
    for _ in range(1, min(cap, len(Xu))):
        i = int(np.argmax(d))
        keys.append(i)
        d = np.minimum(d, row_dist(i))
        m = float(d.max())
        for b in budgets:
            if hit[b] is None and m <= b:
                hit[b] = len(keys)
        if all(hit[b] is not None for b in budgets):
            break
    return keys, hit


# ---------------------------------------------------------------- redundancy / 切点
def redundancy_fast(Xu, pack):
    """与 eval.redundancy 同口径（整数路径，逐位一致）。"""
    n = len(Xu)
    uniq_frac, mind_all = [], []
    for s in range(0, n - pack + 1, pack):
        g = Xu[s:s + pack].astype(np.int16)
        s32 = np.abs(g[:, None, :] - g[None, :, :]).sum(axis=2, dtype=np.int32)
        d = s32.astype(np.float32) / np.float32(D)
        np.fill_diagonal(d, np.float32(1e9))
        mind = d.min(axis=1)
        mind_all.extend(mind.tolist())
        uniq_frac.append(float((mind > DIFF_DISTINCT).sum()))
    if not uniq_frac:
        return None
    return {
        'pack': pack,
        'distinct_per_pack': round(sum(uniq_frac) / len(uniq_frac), 2),
        'distinct_frac': round(sum(uniq_frac) / (len(uniq_frac) * pack), 3),
        'median_min_dist': round(float(np.median(mind_all)), 2),
    }


def scene_cuts_fast(Xu, ratio=2.5, abs_min=2.0, min_gap=2):
    """与 eval.scene_cuts 同口径（整数路径，逐位一致）。"""
    X16 = Xu.astype(np.int16)
    s32 = np.abs(np.diff(X16, axis=0)).sum(axis=1, dtype=np.int32)
    diff = s32.astype(np.float32) / np.float32(D)
    cuts = []
    for i in range(1, len(diff) - 1):
        if diff[i] > ratio * max(1.0, float(diff[i - 1]), float(diff[i + 1])) \
                and diff[i] > abs_min:
            if not cuts or i - cuts[-1] >= min_gap:
                cuts.append(i)
    return cuts


# ---------------------------------------------------------------- 端到端
def evaluate_fast(path, sample_fps=None, hwaccel=False):
    """eval.evaluate 的加速等价版：输出结构相同，另附 stages_s 与 decode。"""
    ev = _load_eval_module()
    stages = {}
    t0 = time.time()

    # probe 与抽帧是两条独立子进程链，并行不改变任何输出（O6）。
    # 收益：fanren 上 probe 的 -count_frames 必走 90s 超时回退（D-009），
    # 并行后这 90s 不再独占壁钟；meta 与 anchor 结果与串行完全一致。
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_probe = pool.submit(ev.probe, path, 45)   # O8：45s，4.2× 余量
        fut_load = pool.submit(load_gray_uint8, path, sample_fps, hwaccel)
        t_p0 = time.time()
        meta = fut_probe.result()
        stages['ffprobe'] = round(time.time() - t_p0, 1)
        t_l0 = time.time()
        Xu, decode_used = fut_load.result()
        stages['ffmpeg_抽帧'] = round(time.time() - t_l0, 1)
    stages['抽帧+probe_并行壁钟'] = round(time.time() - t0, 1)

    n = len(Xu)
    ticks = meta['frames'] * sample_fps / meta['fps'] if sample_fps else meta['frames']
    frame_ok = abs(n - ticks) <= 1
    out = {
        'file': pathlib.Path(path).name,
        'meta': meta,
        'analysis': {'sample_fps': sample_fps, 'w': ANALYSIS_W, 'h': ANALYSIS_H},
        'frames_processed': n,
        'frames_expected': round(ticks, 1),
        'meta_assert_ok': frame_ok,
        'elapsed_s': None,
        'decode': decode_used,
    }
    if not frame_ok:
        out['error'] = f'A5 元断言失败：处理 {n} ≠ 期望 {ticks:.1f}'
        out['stages_s'] = stages
        out['elapsed_s'] = round(time.time() - t0, 1)
        return out

    cap = min(n, CAP)
    par_start = time.time()

    def _timed(fn, *a):
        s = time.perf_counter()
        r = fn(*a)
        return r, time.perf_counter() - s

    with ThreadPoolExecutor(max_workers=6) as pool:
        fut_kc = pool.submit(_timed, kcenter_track_fast, Xu, BUDGETS, cap)
        fut_un = {b: pool.submit(_timed, uniform_exact_fast, Xu, b, cap)
                  for b in BUDGETS}
        fut_red = {p: pool.submit(_timed, redundancy_fast, Xu, p) for p in (4, 8)}
        fut_cuts = pool.submit(_timed, scene_cuts_fast, Xu)

        (kc_keys, kc_hit), kc_wall = fut_kc.result()
        uniform_values, un_walls = {}, {}
        for b, f in fut_un.items():
            uniform_values[b], un_walls[b] = f.result()
        red_values, red_walls = {}, {}
        for p, f in fut_red.items():
            red_values[p], red_walls[p] = f.result()
        cuts, cuts_wall = fut_cuts.result()
    stages['k-center_单趟'] = round(kc_wall, 1)
    stages['uniform_二分×maxdist'] = round(max(un_walls.values()), 1)
    stages['uniform_各预算'] = {str(b): round(w, 1) for b, w in un_walls.items()}
    stages['redundancy'] = round(max(red_walls.values()), 1)
    stages['scene_cuts'] = round(cuts_wall, 1)
    stages['并行段总壁钟'] = round(time.time() - par_start, 1)

    cov = {}
    for budget in BUDGETS:
        kc = kc_hit.get(budget)
        un = uniform_values.get(budget)
        cov[budget] = {
            'kcenter': kc, 'uniform': un,
            'ratio': (round(un / kc, 2) if (kc and un) else None),
            'note': None if (kc and un) else ('超上限未达到预算' if not (kc and un)
                                              else ('kcenter 未达' if not kc
                                                    else 'uniform 未达')),
        }
    out['coverage'] = cov
    out['redundancy'] = {f'pack{p}': red_values[p] for p in (4, 8)}
    out['scene_cuts'] = {'count': len(cuts),
                         'per_minute': round(len(cuts) / (n / 60), 2)}
    out['stages_s'] = stages
    out['kcenter_keys_count'] = len(kc_keys)
    out['elapsed_s'] = round(time.time() - t0, 1)
    return out


if __name__ == '__main__':
    print('用法: python3 scripts/bench_m2.py --mode optimized '
          '[--corpus eval/fanren184.mp4 --fps 6]', file=sys.stderr)
    sys.exit(2)
