#!/usr/bin/env python3
"""一次性探针：加法均衡 vs CSES 乘积核（不是交付物，结论落 docs/research/）"""
import sys, time
sys.path.insert(0, 'src')
import numpy as np
from kf.metrics import load_gray, uniform_idx, maxdist

def unit(X):
    X = np.asarray(X, dtype='float32')
    nrm = np.linalg.norm(X, axis=1, keepdims=True)
    nrm[nrm == 0] = 1.0
    return X / nrm

def cos_kernel(A, B):
    return np.maximum(A @ B.T, 0.0)

def greedy_cover(X, k, ell=30.0, fps=6.0, kmin=8, eps=0.05):
    """CSES 附录F Algorithm2：乘积核 + 饱和停止"""
    n = len(X)
    U = unit(X)
    t = np.arange(n, dtype='float32') / fps
    w = np.ones(n, dtype='float32')
    Ftot = float(w.sum())
    S, cov = [], np.zeros(n, dtype='float32')
    Kmat_cache = {}
    while len(S) < k:
        if len(S) >= kmin and float((w * cov).sum()) >= (1 - eps) * Ftot:
            break
        j_best, g_best, tie_best = -1, -1.0, None
        for j in range(n):
            if j in S:
                continue
            Kj = Kmat_cache.get(j)
            if Kj is None:
                Kj = cos_kernel(U[j:j+1], U)[0] * np.exp(-((t - t[j]) ** 2) / (2 * ell * ell))
                Kmat_cache[j] = Kj
            g = float((w * np.maximum(Kj - cov, 0)).sum())
            if g > g_best or (abs(g - g_best) < 1e-9 and g > 0 and tie_best is not None):
                if g > g_best:
                    j_best, g_best = j, g
                    tie_best = float(cov[j])
                elif g > 0 and float(cov[j]) < tie_best:   # 平局取离 S 最远
                    j_best, tie_best = j, float(cov[j])
        if g_best <= 0:
            break
        S.append(j_best)
        cov = np.maximum(cov, Kmat_cache[j_best])
    return sorted(S)

def legacy_balanced(X, k, fps=6.0, alpha=0.3, ell=None, kmin=8, eps=0.05):
    """现有 kcenter_balanced 的等价实现 + 可选饱和停止（ell=None 表示 alpha 版）"""
    from kf.kcenter import kcenter_balanced
    return kcenter_balanced(X, k, fps=fps, alpha=alpha)['keys']

if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('video'); ap.add_argument('--fps', type=float, default=6)
    ap.add_argument('--ks', type=int, nargs='+', default=[40, 80])
    ap.add_argument('--ell', type=float, default=30)
    a = ap.parse_args()
    t0 = time.time()
    X = load_gray(a.video, sample_fps=a.fps)
    print(f'frames={len(X)} load={time.time()-t0:.1f}s')
    for k in a.ks:
        t1 = time.time()
        old = legacy_balanced(X, k, fps=a.fps)
        md_old = maxdist(X, old)
        gaps_old = np.diff(sorted(old)); mg_old = gaps_old.max() / a.fps
        t_old = time.time() - t1
        t2 = time.time()
        new = greedy_cover(X, k, ell=a.ell, fps=a.fps)
        md_new = maxdist(X, new)
        gaps_new = np.diff(sorted(new)); mg_new = gaps_new.max() / a.fps
        t_new = time.time() - t2
        print(f'K={k:4d} | legacy alpha=0.3: |S|={len(old):3d} maxdist={md_old:6.2f} maxgap={mg_old:7.1f}s {t_old:6.1f}s')
        print(f'     | CSES kernel l={a.ell}: |S|={len(new):3d} maxdist={md_new:6.2f} maxgap={mg_new:7.1f}s {t_new:6.1f}s')
