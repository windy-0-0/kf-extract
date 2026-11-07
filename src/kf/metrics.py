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


# ---------------------------------------------------------------- 静态段（假绿探测器）
def static_runs(X: np.ndarray, threshold: float = DIFF_DISTINCT,
                min_frames: int = 3) -> list[tuple[int, int]]:
    """找出「连续多少帧彼此几乎相同」的区段（帧号区间，闭区间）。

    **为什么必须报出来**（实测踩到的假绿，2026-11-07）：

    在一段 267 秒的编程录屏上，k-center 报 **maxdist = 3.84**（看上去完美覆盖），
    同时报 **最大空白 67.2 秒**。两个数字并排才暴露真相：

    - 199.5s→266.7s 这一整段，段内任意帧到段首的 L1 距离中位 **2.89**、最大 **3.51**
    - 该段 404 帧里，「与其他帧不同（>8）」的有 **0 帧**
    - 但肉眼看画面对比：198s 是代码窗口，265s 是**同一个窗口多了终端面板 + 运行输出**

    也就是说：**这 67 秒里画面上确实新增了信息，而我们的度量判定它「零变化」。**
    模型按我们的选帧结果看，会以为这两分钟什么都没发生。

    根因不是分辨率（把 640×360 也测过，L1=3.64 一样判成重复），
    而是 **L1 均值先天稀释局部变化**：终端面板只占 ~15% 面积，加上字符是高频细节，
    平均下来只剩 2.9。

    同类问题在所有纯像素方法上都存在（PySceneDetect 在同一素材上只报 **2 个切点**）。

    **所以本项目的诚实做法不是「假装解决」，而是「把它报出来」**：
    有这么长的静态段时，用户应当知道「这段时间的选帧结果可能不可信」，
    而不是被一个漂亮的 maxdist 骗过去。

    Returns: [(start, end)]，帧号区间，end 含在内。
    """
    n = len(X)
    if n < 2:
        return []
    # 相邻帧距离（一次算完，不分块：n 行 × 2 列，内存可忽略）
    d = np.abs(X[:-1] - X[1:]).mean(axis=1)
    runs: list[tuple[int, int]] = []
    start = None
    for i, v in enumerate(d):
        if v <= threshold:
            if start is None:
                start = i
        else:
            if start is not None and (i - start) + 1 >= min_frames:
                runs.append((start, i))
            start = None
    if start is not None and (n - 1 - start) + 1 >= min_frames:
        runs.append((start, n - 1))
    return runs
