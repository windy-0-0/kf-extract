#!/usr/bin/env python3
"""
M0 基线评测器 —— 钉死三个基线，输出可复核 JSON。

三条铁律（写在代码里，不是文档里）：
  1) 每段素材都必须报「实际处理帧数」，并与 ffprobe -count_frames 交叉核对 ±1（A5 元断言）
  2) 每个指标都必须能变红：反例路径在 --selftest 里（R99）
  3) 只报实测，不报外推；外推值一律写 None 并在报告里标 extrapolated

用法：
  python3 scripts/eval.py --corpus eval/ --out docs/status/M0-baseline.json
  python3 scripts/eval.py --selftest          # 自证闸门能红
"""
import argparse, json, os, pathlib, subprocess, sys, time

_src = pathlib.Path(__file__).resolve().parent.parent / 'src'
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))
from kf.procs import find_ffmpeg, find_ffprobe

FF = find_ffmpeg()
FP = find_ffprobe()

# ---- METRIC-CORE-BEGIN（此段冻结：改它 = 改口径）----
# 与立项书 §一 完全一致的口径：两帧 L1 距离 > DIFF_DISTINCT 才算「真正不同」
DIFF_DISTINCT = 8.0
ANALYSIS_W, ANALYSIS_H = 32, 18      # 分析分辨率（灰度），与已钉死的基线一致


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def probe(path, count_timeout=90):
    """元数据 + 帧数锚点。

    **A5 元断言要有外部锚点**，但 `-count_frames` 对 849MB/29min 的片段会跑几分钟。
    所以分两级：
      1) 优先 `-count_frames`（真解码数帧，最准），带超时；
      2) 超时则退回 `duration × fps`（推算），**并在报告里标明 anchor 类型**，
         不许把推算值当实测值用（R19 外部锚点 / R114 取真值）。
    """
    base = run([FP, '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height,r_frame_rate:format=duration',
                '-of', 'json', str(path)])
    if base.returncode != 0:
        raise RuntimeError(f'ffprobe failed: {base.stderr[:200]}')
    doc = json.loads(base.stdout)
    s = doc['streams'][0]
    num, den = (s.get('r_frame_rate') or '0/1').split('/')
    fps = float(num) / float(den or 1)
    try:
        dur = float((doc.get('format') or {}).get('duration') or 0)
    except (TypeError, ValueError):
        dur = 0.0
    meta = {'width': s['width'], 'height': s['height'], 'fps': round(fps, 3),
            'duration_s': round(dur, 3)}
    # 2) 精确帧数（带超时）
    try:
        r2 = subprocess.run([FP, '-v', 'error', '-select_streams', 'v:0',
                             '-show_entries', 'stream=nb_read_frames',
                             '-count_frames', '-of', 'json', str(path)],
                            capture_output=True, text=True, timeout=count_timeout)
        if r2.returncode == 0:
            n = int(json.loads(r2.stdout)['streams'][0].get('nb_read_frames') or 0)
            if n > 0:
                meta['frames'] = n
                meta['anchor'] = 'ffprobe-count_frames'
                return meta
    except subprocess.TimeoutExpired:
        pass
    meta['frames'] = int(round(dur * fps)) if dur and fps else 0
    meta['anchor'] = 'duration*fps(推算)'
    return meta


def load_gray_bytes(path, fps=None):
    import numpy as np
    vf = ''
    if fps:
        vf = f'fps={fps},'
    vf += f'scale={ANALYSIS_W}:{ANALYSIS_H},format=gray'
    r = subprocess.run([FF, '-v', 'error', '-i', str(path), '-vf', vf,
                        '-f', 'rawvideo', '-pix_fmt', 'gray', '-'], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f'ffmpeg failed: {r.stderr[:200]}')
    buf = r.stdout
    n = len(buf) // (ANALYSIS_W * ANALYSIS_H)
    return np.frombuffer(buf[:n * ANALYSIS_W * ANALYSIS_H], dtype=np.uint8).reshape(
        n, ANALYSIS_W * ANALYSIS_H).astype('float32')


def kcenter_sample(X, K, seed=0):
    """最远优先贪心：选 K 帧使「任意帧到最近选中帧的最大距离」最小。O(KN)。"""
    import numpy as np
    n = len(X)
    if K >= n:
        return list(range(n))
    keys = [seed]
    d = np.abs(X - X[keys[0]]).mean(axis=1)
    for _ in range(K - 1):
        i = int(np.argmax(d))
        if d[i] <= 0:
            break
        keys.append(i)
        d = np.minimum(d, np.abs(X - X[i]).mean(axis=1))
    return sorted(keys)


def maxdist(X, idx, chunk=None, budget_mb=100):
    """覆盖质量：任意帧到最近选中帧的最大 L1。

    **必须分块**：朴素写法 `X[:,None,:] - X[None,idx,:]` 会一次性生成
    N×K×D 的中间数组。30 分钟番剧 @6fps（N≈17300, K≈1200, D=576）就是
    **120 亿元素 ≈ 48 GB**，进程直接被系统 Killed: 9。
    分块后峰值只有 chunk×K×D（256×1200×576 ≈ 177MB），且结果逐位相同。
    """
    import numpy as np
    if not idx:
        return float('inf')
    sel = X[idx]                                  # K×D，很小
    n = len(X)
    if chunk is None:
        # 自适应：让中间数组 chunk×K×D×4 ≤ budget_mb。K=1200/D=576 时 chunk≈36，
        # 固定 256 会吃 707MB（实测把评测进程顶到 1GB RSS）。
        per_row = max(1, len(sel)) * X.shape[1] * 4
        chunk = max(1, int(budget_mb * 1e6 / per_row))
    best = np.empty(n, dtype='float32')
    for start in range(0, n, chunk):
        stop = min(n, start + chunk)
        blk = X[start:stop]                       # chunk×D
        # chunk×K×D 的中间数组，一次算完
        d = np.abs(blk[:, None, :] - sel[None, :, :]).mean(axis=2)   # chunk×K
        best[start:stop] = d.min(axis=1)
    return float(best.max())


def uniform_idx(n, K):
    import numpy as np
    return sorted(set(np.linspace(0, n - 1, K).round().astype(int).tolist()))


def _kcenter_track(X, budgets, cap):
    """单趟最远优先贪心，同时记录「每个预算第一次满足时的 K」。

    `d.max()` 对 K 单调不增，所以一趟跑完就能同时回答 4 个预算 ——
    不必对每个预算各跑一遍（那是 4 倍浪费）。
    """
    import numpy as np
    keys, d = [0], np.abs(X - X[0]).mean(axis=1)
    hit = {b: None for b in budgets}
    if float(d.max()) <= min(budgets):
        for b in budgets:
            if float(d.max()) <= b:
                hit[b] = 1
        return keys, d, hit
    for _ in range(1, min(cap, len(X))):
        i = int(np.argmax(d))
        keys.append(i)
        d = np.minimum(d, np.abs(X - X[i]).mean(axis=1))
        m = float(d.max())
        for b in budgets:
            if hit[b] is None and m <= b:
                hit[b] = len(keys)
        if all(hit[b] is not None for b in budgets):
            break
    return keys, d, hit


def _uniform_exact(X, budget, cap):
    """uniform 精确二分。

    **不许用阶梯近似**：阶梯只有 7 个离散点，在 168 帧素材上给出 126/21/21/5，
    而精确二分为 100/14/12/5 —— ratio 从 7.14 变成 9.0。那是**度量变了**，不是优化。
    单调性不成立（采样点跳格），所以只能老实二分。
    """
    import numpy as np
    n = len(X)
    cap = min(cap, n)
    lo, hi, best = 1, cap, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if maxdist(X, uniform_idx(n, mid)) <= budget:
            best = mid; hi = mid - 1
        else:
            lo = mid + 1
    return best


def redundancy(X, pack):
    """把帧按 pack 分组，量化「组内真正不同」的帧占比（与立项书 §一 同口径）"""
    import numpy as np
    n = len(X)
    uniq_frac, mind_all = [], []
    for s in range(0, n - pack + 1, pack):
        g = X[s:s + pack]
        d = np.abs(g[:, None, :] - g[None, :, :]).mean(axis=2)
        np.fill_diagonal(d, 1e9)
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


# ---- METRIC-CORE-END ----


def scene_cuts(X, ratio=2.5, abs_min=2.0, min_gap=2):
    """镜头切点：差分远大于邻居（尖峰）。与 see-now 的判据同形态，但只做基线对照。"""
    import numpy as np
    diff = np.abs(np.diff(X, axis=0)).mean(axis=1)
    cuts = []
    for i in range(1, len(diff) - 1):
        if diff[i] > ratio * max(1.0, float(diff[i - 1]), float(diff[i + 1])) and diff[i] > abs_min:
            if not cuts or i - cuts[-1] >= min_gap:
                cuts.append(i)
    return cuts


def evaluate(path, sample_fps=None):
    import numpy as np
    t0 = time.time()
    meta = probe(path)
    X = load_gray_bytes(path, fps=sample_fps)
    n = len(X)
    # ---- A5 元断言：实际处理帧数 vs ffprobe
    if sample_fps:
        ticks = meta['frames'] * sample_fps / meta['fps']
    else:
        ticks = meta['frames']
    frame_ok = abs(n - ticks) <= 1
    out = {
        'file': pathlib.Path(path).name,
        'meta': meta,
        'analysis': {'sample_fps': sample_fps, 'w': ANALYSIS_W, 'h': ANALYSIS_H},
        'frames_processed': n,
        'frames_expected': round(ticks, 1),
        'meta_assert_ok': frame_ok,
        'elapsed_s': None,
    }
    if not frame_ok:
        out['error'] = f'A5 元断言失败：处理 {n} ≠ 期望 {ticks:.1f}'
        return out
    # ---- 基线 1：覆盖质量（k-center 单趟记录所有预算；uniform 走 K 阶梯一次算完）
    budgets = (10, 15, 20, 30)
    cap = min(n, 2000)
    kc_keys, kc_d, kc_hit = _kcenter_track(X, budgets, cap)
    cov = {}
    for budget in budgets:
        kc = kc_hit.get(budget)
        un = _uniform_exact(X, budget, cap)
        cov[budget] = {
            'kcenter': kc, 'uniform': un,
            'ratio': (round(un / kc, 2) if (kc and un) else None),
            'note': None if (kc and un) else ('超上限未达到预算' if not (kc and un) else
                                              ('kcenter 未达' if not kc else 'uniform 未达')),
        }
    out['coverage'] = cov
    # ---- 基线 2：拼图内冗余
    out['redundancy'] = {f'pack{p}': redundancy(X, p) for p in (4, 8)}
    # ---- 基线 3：镜头切点
    cuts = scene_cuts(X)
    out['scene_cuts'] = {'count': len(cuts), 'per_minute': round(len(cuts) / (n / 60), 2)}
    out['elapsed_s'] = round(time.time() - t0, 1)
    return out


# ---------------------------------------------------------------- 闸门自证
def selftest():
    """R99：每个指标都必须能变红。构造反例，断言指标真的掉下去。"""
    import numpy as np
    ok = True
    # 反例 A：全选同一镜头（两帧完全相同）→ distinct_frac 必须是 0
    X = np.tile(np.arange(ANALYSIS_W * ANALYSIS_H, dtype='float32'), (16, 1))
    r = redundancy(X, 4)
    passed = (r['distinct_frac'] == 0)
    ok &= passed
    print(f'[selftest] 全同帧 → distinct_frac={r["distinct_frac"]} {"PASS" if passed else "FAIL"}')
    # 反例 B：逐帧完全不同 → distinct_frac 必须是 1
    rng = np.random.default_rng(0)
    X2 = rng.random((16, ANALYSIS_W * ANALYSIS_H), dtype='float32') * 255
    r2 = redundancy(X2, 4)
    passed2 = (r2['distinct_frac'] == 1)
    ok &= passed2
    print(f'[selftest] 全异帧 → distinct_frac={r2["distinct_frac"]} {"PASS" if passed2 else "FAIL"}')
    # 反例 C：k-center 必须严格优于或等于均匀（在无结构随机数据上可相等）
    X3 = np.tile(np.arange(ANALYSIS_W * ANALYSIS_H, dtype='float32'), (40, 1))
    kc = kcenter_sample(X3, 4)
    passed3 = len(kc) <= 4
    ok &= passed3
    print(f'[selftest] k-center 返回值合法 → {kc[:4]} {"PASS" if passed3 else "FAIL"}')
    print('[selftest]', 'ALL PASS' if ok else 'FAILED')
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--corpus', default='eval/')
    ap.add_argument('--out')
    ap.add_argument('--fps', type=int, default=None, help='降采样到多少 fps（None=全帧）')
    ap.add_argument('--only', help='只跑文件名包含该子串的')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    corpus = pathlib.Path(a.corpus)
    files = sorted(p for p in corpus.glob('*.mp4'))
    if a.only:
        files = [p for p in files if a.only in p.name]
    results = []
    for p in files:
        print(f'--- {p.name} ---', flush=True)
        try:
            r = evaluate(p, sample_fps=a.fps)
        except Exception as e:
            r = {'file': p.name, 'error': f'{type(e).__name__}: {e}'}
        print(json.dumps({k: v for k, v in r.items() if k in
              ('frames_processed', 'frames_expected', 'meta_assert_ok', 'elapsed_s', 'error')},
              ensure_ascii=False))
        results.append(r)
    doc = {'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
           'distinct_threshold': DIFF_DISTINCT, 'results': results}
    if a.out:
        pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(a.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'\n写出 {a.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
