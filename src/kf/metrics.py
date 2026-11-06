"""距离与覆盖度量 —— **冻结口径**。

这里的每个函数都对应 docs/status/BASELINE-FROZEN.json 里冻结的那套数字。
改这里的任何一个行为，等于改口径，必须先在 DECISIONS.md 写一条。

口径：
  - 分析分辨率 32×18 灰度
  - 距离 = L1 均值（0–255 刻度）
  - 「真正不同」阈值 > 8.0
"""
from __future__ import annotations

import numpy as np

ANALYSIS_W = 32
ANALYSIS_H = 18
DIFF_DISTINCT = 8.0
# 覆盖质量的默认分块预算（MB），见 D-011
CHUNK_BUDGET_MB = 100


def load_gray(path, sample_fps=None, ffmpeg=None,
              w=ANALYSIS_W, h=ANALYSIS_H) -> np.ndarray:
    """抽成 w×h 灰度，返回 (n, w*h) float32

    `ffmpeg=None` = 自动定位（见 `src/kf/procs.py`）。显式传路径仍然有效，
    老调用点不受影响。**默认值不再是本机绝对路径** —— 那是「我这台机器能跑」，
    不是「别人装完能跑」。
    """
    import subprocess
    if ffmpeg is None:
        from .procs import find_ffmpeg
        ffmpeg = find_ffmpeg()
    vf = ''
    if sample_fps:
        vf = f'fps={sample_fps},'
    vf += f'scale={w}:{h},format=gray'
    r = subprocess.run([ffmpeg, '-v', 'error', '-i', str(path), '-vf', vf,
                        '-f', 'rawvideo', '-pix_fmt', 'gray', '-'], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f'ffmpeg failed: {r.stderr[:200]}')
    n = len(r.stdout) // (w * h)
    return np.frombuffer(r.stdout[:n * w * h], dtype=np.uint8).reshape(n, w * h).astype('float32')


def pairwise_l1(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """(m,D) × (k,D) → (m,k) 的 L1 均值距离"""
    return np.abs(a[:, None, :] - b[None, :, :]).mean(axis=2)


def maxdist(X: np.ndarray, idx, chunk: int | None = None,
            budget_mb: float = CHUNK_BUDGET_MB) -> float:
    """覆盖质量：任意帧到最近选中帧的最大 L1。

    **必须分块**（D-007/D-011）：朴素广播会生成 N×K×D 的中间数组，
    30min@6fps（N≈10400,K≈1200,D=576）≈ 29GB，进程被 OOM kill。
    """
    if not idx:
        return float('inf')
    sel = X[np.asarray(idx)]
    n = len(X)
    if chunk is None:
        per_row = max(1, len(sel)) * X.shape[1] * 4
        chunk = max(1, int(budget_mb * 1e6 / per_row))
    best = np.empty(n, dtype='float32')
    for start in range(0, n, chunk):
        stop = min(n, start + chunk)
        d = pairwise_l1(X[start:stop], sel)
        best[start:stop] = d.min(axis=1)
    return float(best.max())


def redundancy(X: np.ndarray, pack: int = 4) -> dict:
    """拼图内的「真正不同」占比（与 M0 基线同口径）。"""
    n = len(X)
    uniq, mins = [], []
    for s in range(0, n - pack + 1, pack):
        g = X[s:s + pack]
        d = pairwise_l1(g, g)
        np.fill_diagonal(d, 1e9)
        m = d.min(axis=1)
        mins.extend(m.tolist())
        uniq.append(float((m > DIFF_DISTINCT).sum()))
    if not uniq:
        return {'pack': pack, 'distinct_frac': 0.0, 'median_min_dist': 0.0}
    return {
        'pack': pack,
        'distinct_frac': round(sum(uniq) / (len(uniq) * pack), 3),
        'median_min_dist': round(float(np.median(mins)), 2),
    }


def uniform_idx(n: int, k: int) -> list[int]:
    """均匀采样下标（与 M0 基线同口径：np.linspace 取整去重）"""
    return sorted(set(np.linspace(0, n - 1, k).round().astype(int).tolist()))
