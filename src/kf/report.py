"""M0.5 · 对照表生成器（A1 新判据的可读化）。

任务书 `docs/prompts/M3-v1.txt` §M0.5：
  - `docs/status/M0-baseline.md` 只有 maxdist 的**改善百分比**，缺两列：
    ① k-center 的**绝对 maxdist**（不是百分比）
    ② 给定 K 时，选中帧的**时间分布**（是否挤在某几段）

第 ② 列才是关键诊断：80 帧全选在前 5 分钟，覆盖指标再漂亮，
对「按顺序理解剧情」也是坏的。

## 口径（一个都不许自己另起）

- 距离：`src/kf/metrics.py::pairwise_l1`（32×18 灰度，L1 均值，冻结口径）
- k-center：`src/kf/kcenter.py::kcenter`（最远优先贪心，与 M1 同一套）
- uniform：`src/kf/metrics.py::uniform_idx`（linspace 取整去重，与 M0 同口径）
- maxdist：`src/kf/metrics.py::maxdist`（自适应分块，D-007/D-011）

**时间覆盖率** = 把视频时长均分 10 段，选中帧落在几段里（10/10 = 均匀）。
额外给两列更狠的分布诊断：
  - `占最大段%`：单段最多装了多少比例的选中帧（10.0 = 完全均匀）
  - `最大空档`：相邻选中帧之间的最长帧间隔，占全片比例（越小越均匀）

## 为什么不用 eval.py 里的私有函数

`scripts/eval.py` 是**冻结**的（`freeze_baseline.py` 盯 sha256），不许 import 也不许改。
本模块只消费 `src/kf/` 下的正式模块；两者算的是同一口径，并用 `--verify-baseline`
现场对 M0 的冻结数字做一致性校验（R19 外部锚点）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import time

import numpy as np

try:
    from .kcenter import kcenter
except ImportError:  # 允许 `python src/kf/report.py` 直接跑
    from kcenter import kcenter
try:
    from .metrics import load_gray, maxdist, uniform_idx
except ImportError:
    from metrics import load_gray, maxdist, uniform_idx

# 四段素材与 M0/M1 完全一致的抽样率（改这里 = 改口径，必须写 DECISIONS）
CORPUS = [
    ('trailer', 'eval/trailer.mp4', 12),
    ('mv195', 'eval/mv195.mp4', 12),
    ('mv227', 'eval/mv227.mp4', 12),
    ('fanren184', 'eval/fanren184.mp4', 6),
]
KS = (40, 80, 160, 320, 640)
BUCKETS = 10

# M0 冻结的改善百分比（docs/status/M0-baseline.md §二），用于一致性校验
FROZEN_IMPROVEMENT = {
    'trailer': {40: 64, 80: 95, 160: 100},
    'mv195': {40: 42, 80: 59, 160: 67, 320: 79, 640: 78},
    'mv227': {40: 33, 80: 33, 160: 48, 320: 69, 640: 91},
    'fanren184': {40: 38, 80: 37, 160: 21, 320: 30, 640: 28},
}


def time_distribution(keys, n: int, buckets: int = BUCKETS) -> dict:
    """选中帧的时间分布（M0.5 的核心诊断列）。"""
    keys = sorted(int(k) for k in keys)
    if not keys or n <= 0:
        return {'buckets_hit': 0, 'buckets': buckets, 'coverage': 0.0,
                'max_bucket_share': None, 'max_gap_frac': None, 'counts': []}
    b = np.minimum((np.asarray(keys) * buckets) // n, buckets - 1)
    counts = np.bincount(b, minlength=buckets).astype(int)
    hit = int((counts > 0).sum())
    max_share = float(counts.max() / len(keys))
    # **必须把首尾边界算进空档**：否则「80 帧全选在前 5 分钟」这种最坏情况
    # 量不出空档（尾部那段空档根本不产生相邻对）。这是本列唯一能红的前提。
    bounds = np.concatenate([[0], np.asarray(keys), [n - 1]])
    max_gap = float(np.diff(bounds).max()) / n
    return {'buckets_hit': hit, 'buckets': buckets,
            'coverage': round(hit / buckets, 3),
            'max_bucket_share': round(max_share, 3),
            'max_gap_frac': round(max_gap, 3),
            'counts': counts.tolist()}


def row_for(X: np.ndarray, k: int) -> dict:
    """单个 K 的三列 + 分布诊断。k-center 与 uniform 用**同一个 maxdist 实现**。"""
    n = len(X)
    kc = kcenter(X, k)
    u_idx = uniform_idx(n, k)
    kc_md = float(maxdist(X, kc['keys']))
    u_md = float(maxdist(X, u_idx))
    imp = (u_md - kc_md) / u_md if u_md > 0 else None
    return {
        'k_requested': k,
        'k_effective': len(kc['keys']),
        'uniform_effective': len(u_idx),
        'kcenter_maxdist': round(kc_md, 3),
        'uniform_maxdist': round(u_md, 3),
        'improvement_pct': round(imp * 100, 1) if imp is not None else None,
        'kcenter_time': time_distribution(kc['keys'], n),
        'uniform_time': time_distribution(u_idx, n),
    }


def material_rows(name: str, path, fps: int, ks=KS, *, verbose=True) -> dict:
    X = load_gray(path, sample_fps=fps)
    n = len(X)
    if verbose:
        print(f'  [{name}] {n} 帧 @{fps}fps', flush=True)
    rows = [row_for(X, k) for k in ks if k <= n]
    return {'material': name, 'path': str(path), 'sample_fps': fps, 'frames': n, 'rows': rows}


def format_table(materials: list[dict]) -> str:
    """任务书要求的表头：用 K / kcenter_maxdist / uniform_maxdist / 改善% / 时间覆盖率。"""
    lines = []
    head = (f'{"素材":<11}{"K":>5}  {"kcenter_maxdist":>16}{"uniform_maxdist":>16}'
            f'{"改善%":>8}  {"时间覆盖率*":>11}{"占最大段%":>10}{"最大空档":>9}')
    lines.append(head)
    lines.append('-' * len(head))
    for m in materials:
        for r in m['rows']:
            kt = r['kcenter_time']; ut = r['uniform_time']
            lines.append(
                f'{m["material"]:<11}{r["k_requested"]:>5}  {r["kcenter_maxdist"]:>16.3f}'
                f'{r["uniform_maxdist"]:>16.3f}{r["improvement_pct"]:>7.1f}%  '
                f'{kt["buckets_hit"]:>4}/{kt["buckets"]:<2}     '
                f'{kt["max_bucket_share"] * 100:>9.1f}%{kt["max_gap_frac"] * 100:>8.1f}%'
                f'   (uniform: {ut["buckets_hit"]}/{ut["buckets"]})')
    lines.append('')
    lines.append('*时间覆盖率 = 把时长均分 10 段，k-center 选中帧落在几段里（10/10 = 均匀）')
    lines.append('*占最大段%   = 选中帧里最多的一段占多少（理想 = 10.0%）')
    lines.append('*最大空档   = 相邻选中帧的最长间隔 / 全片帧数（理想 → 1/K）')
    return '\n'.join(lines)


def verify_against_m0(materials: list[dict]) -> list[str]:
    """把改善% 与 M0 冻结值逐个对。差 >1 个百分点就报（R19 外部锚点）。"""
    bad = []
    for m in materials:
        for r in m['rows']:
            k = r['k_requested']
            frozen = FROZEN_IMPROVEMENT.get(m['material'], {}).get(k)
            if frozen is None:
                continue
            if r['improvement_pct'] is None:
                bad.append(f'{m["material"]} K={k}: 改善为 None，冻结值 {frozen}%')
                continue
            if abs(r['improvement_pct'] - frozen) > 1.0:
                bad.append(f'{m["material"]} K={k}: 本次 {r["improvement_pct"]}% vs 冻结 {frozen}%')
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='M0.5 对照表（固定 K 比 maxdist + 时间分布）')
    ap.add_argument('--corpus', default='eval')
    ap.add_argument('--only', help='只跑文件名含该子串的素材')
    ap.add_argument('--ks', default=','.join(str(k) for k in KS))
    ap.add_argument('--json', help='把明细写到该路径')
    ap.add_argument('--verify-baseline', action='store_true',
                    help='与 M0 冻结的改善百分比逐个对账（差 >1 点即失败）')
    a = ap.parse_args(argv)
    ks = tuple(int(x) for x in a.ks.split(','))

    root = pathlib.Path(__file__).resolve().parents[2]
    t0 = time.time()
    materials = []
    for name, rel, fps in CORPUS:
        if a.only and a.only not in name:
            continue
        p = root / rel
        if not p.exists():
            print(f'  SKIP {name}: 素材不存在 {p}')
            continue
        materials.append(material_rows(name, p, fps, ks))

    print()
    print(format_table(materials))
    print(f'\n耗时 {time.time() - t0:.0f}s')

    if a.verify_baseline:
        bad = verify_against_m0(materials)
        if bad:
            print('\n❌ 与 M0 冻结值不一致：')
            for b in bad:
                print('  ', b)
            return 1
        print('\n✅ 与 M0 冻结的改善% 一致（逐 K 对账）')

    if a.json:
        out = pathlib.Path(a.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({'generated_s': round(time.time() - t0, 1),
                                   'materials': materials}, ensure_ascii=False, indent=2),
                       encoding='utf-8')
        print(f'写出 {out}')
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
