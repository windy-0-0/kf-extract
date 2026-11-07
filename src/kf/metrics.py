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
                min_frames: int = 3, rep_mb: float = 2.0) -> list[tuple[int, int]]:
    """找出「整段内任意两帧都足够像」的区段（帧号区间，闭区间）。

    ## 判据为什么不能用「相邻帧差」（实测修正，2026-11-07）

    第一版用「相邻帧差 <= threshold」判定，结果在一段真人 Vlog 上把 **34.3 秒**
    的说话人镜头整段判成静止。但进一步的实测暴露了问题：

    | 落点 | 段内两两 L1 中位 | 90 分位 | 最大 | >阈值8 的比例 |
    |---|---|---|---|---|
    | 录屏 63→250s（**真静止**，UI 不再变） | 2.95 | 4.92 | 5.77 | **0.0%** |
    | Vlog 291→324s（**字幕在变**） | 5.48 | 8.52 | 12.62 | **14.3%** |

    同样是「相邻帧差很小」（Vlog 那段中位只有 1.17），但段内两两距离有 14.3% 超过阈值。
    原因：**字幕只在画面底部一小条变化**，逐帧看变化很小，累积到 30 秒后已经
    「第一个画面和最后一个画面明显不同」。

    所以判据必须是**段内两两距离**，不是相邻帧差。实现上不能真的算 n² 对
    （10403 帧会爆），改用**代表帧集合**：段内维护一批代表帧，
    新帧离所有代表都 > threshold/2 就新增代表；一旦某帧离任一代表 > threshold，
    当前段就结束。这等于用 O(n * 代表数) 近似判定「段内直径 <= threshold」。

    ## 为什么必须报出来

    在一段 267 秒的编程录屏上，k-center 报 **maxdist = 3.84**（看上去完美覆盖）、
    同时报 **最大空白 67.2 秒**。两个数字并排才暴露真相：63.2s 之后 UI 确实不再变化
    （1222 帧的相邻差均值 0.13），所以用 1 帧代表那 200 秒**是正确的**。

    但**「静止」和「度量看不见变化」是两件事**。Vlog 那种「底部字幕在变」的情况，
    整体 L1 测不出（字幕占比太小），必须让用户知道「这段的选帧结果建议人工抽查」。

    **本项目的姿态是把它报出来，不假装解决，也不自动跳过**：
    整段夜景、片尾黑屏，静就是内容本身；自动规避会变成第二个未声明的目标。

    Returns: [(start, end)]，帧号闭区间，按出现顺序。
    """
    n = len(X)
    if n == 1:
        return [(0, 0)]
    if n < 2:
        return []
    X = np.asarray(X, dtype='float32')
    reps_cap = max(8, int(rep_mb * 1e6 / max(1, X.shape[1] * 4)))

    out: list[tuple[int, int]] = []
    start = 0
    reps: list[int] = [0]
    for i in range(1, n):
        d_rep = pairwise_l1(X[i:i + 1], X[reps])[0]
        far = float(d_rep.max())
        if far > threshold:
            # 这一帧与段内某代表差得够远 → 当前段结束
            if i - start >= min_frames:
                out.append((start, i - 1))
            start = i
            reps = [i]
            continue
        # 维护代表集：离所有代表都 > threshold/2 就新增一个代表
        if d_rep.min() > threshold / 2.0 and len(reps) < reps_cap:
            reps.append(i)
    if n - start >= min_frames:
        out.append((start, n - 1))
    return out
