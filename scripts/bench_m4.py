#!/usr/bin/env python3
"""M4 量账：同一段画面，三种打包方式的「可读性 / 信息量 / 成本」三列表。

任务书 §必须先量账 要求先回答一个可测的问题，再谈设计：

    同一段 30 秒画面，用「8 张单帧大图」vs「2 张 4 帧小图」vs「1 张 8 帧更小图」，
    模型哪一份能读对更多细节？

没有模型，所以用三个代理指标：

  1. **可读性代理** = 每格短边像素（A3-1 硬门槛 300px）
  2. **信息量代理** = `metrics.redundancy()` 的 `distinct_frac`（越高越不浪费格子）
  3. **成本** = 张数 × 每张 token（token 走 `src/kf/layout.image_tokens`，宿主真实口径）

四段素材各跑一遍：trailer(14s) / mv195(195s) / mv227(227s) / fanren184(29min)。
窗口取 `min(--seconds, 素材时长)`，统一 6fps 抽帧 + 32×18 灰度（与 M0 冻结口径同一条 vf）。

输出四张表：
  表 1  三列账（本模块的主产出）
  表 2  设计全表（画布阶梯 × 帧数 → 短边 / 保比例 / token）
  表 3  A3-2 代价（给定 K 个关键帧，现状 vs 达标方案）
  表 4  distinct_frac vs pack（A3-3 单调性的实测曲线）

用法：
  python3 scripts/bench_m4.py                      # 全部四段，30s 窗口
  python3 scripts/bench_m4.py --json               # 额外输出 JSON
  python3 scripts/bench_m4.py --full-material      # 表 4 用 fanren184 全量（约 1 万帧）
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.kf import layout as L                      # noqa: E402
from src.kf import metrics                          # noqa: E402  （冻结口径，只读）

FFMPEG = '/opt/homebrew/bin/ffmpeg'
FPROBE = '/opt/homebrew/bin/ffprobe'
CORPUS = ROOT / 'eval'
CLIPS = ('trailer.mp4', 'mv195.mp4', 'mv227.mp4', 'fanren184.mp4')
FROZEN_JSON = ROOT / 'docs' / 'status' / 'M0-fanren.json'


# ---------------------------------------------------------------- 抽帧

def probe_duration(path: pathlib.Path) -> float:
    r = subprocess.run([FPROBE, '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'csv=p=0', str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'ffprobe failed: {r.stderr[:200]}')
    return float(r.stdout.strip())


def load_window(path: pathlib.Path, seconds: float, fps: int) -> 'object':
    """抽 `seconds` 秒、降采样到 fps、转 32×18 灰度。

    vf 与 `metrics.load_gray` **逐字一致**（口径不许分叉），只多一个输出侧 `-t`。
    """
    import numpy as np
    vf = f'fps={fps},scale={metrics.ANALYSIS_W}:{metrics.ANALYSIS_H},format=gray'
    r = subprocess.run([FFMPEG, '-v', 'error', '-i', str(path), '-t', f'{seconds}',
                        '-vf', vf, '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f'ffmpeg failed: {r.stderr[:200]}')
    n = len(r.stdout) // (metrics.ANALYSIS_W * metrics.ANALYSIS_H)
    return np.frombuffer(r.stdout[:n * metrics.ANALYSIS_W * metrics.ANALYSIS_H],
                         dtype=np.uint8).reshape(
        n, metrics.ANALYSIS_W * metrics.ANALYSIS_H).astype('float32')


# ---------------------------------------------------------------- 表

def _table(headers, rows, aligns=None) -> str:
    cols = len(headers)
    widths = [max(len(str(headers[i])),
                  max((len(str(r[i])) for r in rows), default=0)) for i in range(cols)]
    aligns = aligns or ['<'] * cols

    def line(cells):
        return '  '.join(f'{str(c):{aligns[i]}{widths[i]}}' for i, c in enumerate(cells))

    out = [line(headers), '  '.join('-' * w for w in widths)]
    out += [line(r) for r in rows]
    return '\n'.join(out)


def table1_accounting(clips_data: list[dict]) -> str:
    """表 1 · 三列账。每段素材把**同一批帧**按 1/2/4/8 帧一张打包，比可读性/信息量/成本。"""
    rows = []
    for d in clips_data:
        for cand in d['packagings']:
            pack = cand['pack']
            # pack=1 时 distinct_frac 恒为 1.000 —— **指标退化**：单个格子没有邻居可比，
            # 不是「信息量满格」。`median_min_dist` 被 metrics 填成 1e9 哨兵值，一并标掉。
            degenerate = pack == 1
            rows.append([
                d['clip'], f"{pack}帧/张",
                f"{cand['cell'][0]}x{cand['cell'][1]}",
                f"{cand['short_side']:.0f}",
                cand['sheets'], cand['tokens_per_sheet'],
                cand['total_tokens'], f"{cand['tokens_per_frame']:.1f}",
                ('-' if cand['distinct_frac'] is None
                 else f"{cand['distinct_frac']:.3f}*" if degenerate
                 else f"{cand['distinct_frac']:.3f}"),
                ('n/a' if degenerate or cand['median_min_dist'] is None
                 else f"{cand['median_min_dist']:.2f}"),
                'PASS' if cand['short_side'] >= L.MIN_SHORT_SIDE else 'FAIL',
            ])
    return _table(
        ['素材', '打包', '每格像素', '短边', '张数', '单张token', '总token', 'token/帧',
         'distinct_frac', '组内最小距中位', 'A3-1'],
        rows,
        aligns=['<', '<', '>', '>', '>', '>', '>', '>', '>', '>', '<']) + \
        '\n\n* pack=1 的 distinct_frac 恒为 1.000，是**指标退化**（一格没有邻居可比），不能当「信息量满格」读。'


def table2_design(constraints: L.Constraints) -> str:
    """表 2 · 设计全表：画布阶梯 × 帧数。"""
    rows = []
    for c in L.enumerate_candidates(constraints):
        d = c.as_dict()
        rows.append([
            f'{c.canvas[0]}x{c.canvas[1]}', f'{c.pack}', d['grid'], str(d['empty_cells']),
            f"{d['cell'][0]}x{d['cell'][1]}", f"{d['short_side']:.0f}",
            f"{d['aspect_error']:.4f}", d['tokens_per_sheet'],
            f"{d['tokens_per_frame']:.2f}",
            'PASS' if c.ok_short_side else 'FAIL',
            'OK' if c.ok_aspect else 'FAIL',
            'OK' if c.ok_distinct else f'FAIL(pack>{constraints.baseline_pack})',
            'OK' if c.ok else '-',
        ])
    return _table(
        ['画布', '帧/张', '格子', '空格', '每格像素', '短边', '比例误差', '单张token',
         'token/帧', 'A3-1', '保比例', 'A3-3', '可行'],
        rows,
        aligns=['<', '>', '<', '>', '>', '>', '>', '>', '>', '<', '<', '<', '<'])


def table3_cost(plan: L.Plan, ks: tuple[int, ...]) -> str:
    """表 3 · A3-2 代价：给定 K 个关键帧，现状 vs 达标方案。"""
    rows = []
    for k in ks:
        p = L.plan(k, plan.constraints)
        r, b = p.recommended, p.baseline
        rows.append([
            k,
            f'{b.canvas[0]}x{b.canvas[1]}/{b.pack}帧' if b else '-',
            f'{b.sheets(k)}', f'{b.total_tokens(k)}',
            f'{r.canvas[0]}x{r.canvas[1]}/{r.pack}帧' if r else '无可行解',
            f'{r.sheets(k)}' if r else '-', f'{r.total_tokens(k)}' if r else '-',
            f'{r.cell[0]}x{r.cell[1]}' if r else '-',
            f'{p.token_delta_frac:+.1%}' if p.token_delta_frac is not None else '-',
        ])
    return _table(
        ['K 个关键帧', '现状(画布/帧数)', '现状张数', '现状token', '达标方案', '达标张数',
         '达标token', '达标每格', 'token 增减'],
        rows, aligns=['>', '<', '>', '>', '<', '>', '>', '>', '>'])


def table4_distinct_curve(curve: dict, anchor: dict | None) -> str:
    """表 4 · A3-3 的单调性：同一批真实番剧帧，pack 变大 distinct_frac 怎么走。"""
    rows = []
    for pack in sorted(curve):
        rec = curve[pack]
        rows.append([pack, f"{rec['distinct_frac']:.3f}", f"{rec['median_min_dist']:.2f}",
                     'OK (≤ 现状 pack4)' if pack <= L.BASELINE_PACK
                     else f"降 {rec['distinct_frac'] - curve[L.BASELINE_PACK]['distinct_frac']:+.3f} vs pack4"])
    txt = _table(['每张帧数 pack', 'distinct_frac', '组内最小距中位', 'A3-3 判定'],
                 rows, aligns=['>', '>', '>', '<'])
    if anchor:
        txt += (f"\n\n外部锚点（docs/status/M0-fanren.json，fanren184 @6fps 全量 "
                f"{anchor['frames']} 帧）：pack4={anchor['pack4']:.3f} / pack8={anchor['pack8']:.3f}")
    return txt


def table5_conflict(constraints: L.Constraints) -> str:
    """表 5 · 三条约束各自的边际代价：现状 / 只要 A3-1 / A3-1+A3-3。

    **两个范围必须分开报，不许混说**（独立复核抓到的教训）：
      * **阶梯内** = 任务书 §技术路线 1 的四档画布 × 帧数 {1,2,4,8,16}；
      * **全空间** = 任意画布像素 × 任意 (cols, rows) 网格形状，用 `optimal_canvas` 求全局最优。
    旧版只报了「全空间」的一半（画布比例被写死成源比例），于是把
    帧数上限误报成 9、把 A3-1-only 的最优误报成 98.0；真值是 10 与 93.80。
    """
    base = L.make_candidate(constraints.baseline_canvas, constraints.baseline_pack, constraints,
                            distinct_table={4: 0.509, 8: 0.489})
    ladder = [L.make_candidate(cv, p, constraints, distinct_table={4: 0.509, 8: 0.489})
              for cv in constraints.canvases for p in constraints.frame_counts]
    glob = []
    for p in range(1, 17):
        r = L.optimal_canvas(p, constraints)
        if r is None:
            continue
        c = L.make_candidate((r[0], r[1]), p, constraints, distinct_table={4: 0.509, 8: 0.489})
        if c.tokens_per_sheet == r[2]:
            glob.append(c)

    def _row(scope, label, pick):
        if pick is None:
            return [scope, label, '—', '—', '—', '—', '—', '—']
        return [scope, label, f'{pick.canvas[0]}x{pick.canvas[1]} / {pick.pack}帧',
                f'{pick.cell[0]}x{pick.cell[1]}', f'{pick.short_side:.0f}',
                f'{pick.tokens_per_sheet}', f'{pick.tokens_per_frame():.2f}',
                f'{pick.tokens_per_frame() / base.tokens_per_frame() - 1:+.1%}']

    def _best(cands, pred):
        f = [c for c in cands if pred(c)]
        return min(f, key=lambda c: c.tokens_per_frame()) if f else None

    a31 = lambda c: c.ok_short_side and c.ok_aspect      # noqa: E731
    rows = [
        _row('阶梯内', '现状（只 4 帧/张，800×450）', base),
        _row('阶梯内', '只要 A3-1（放开帧数）', _best(ladder, a31)),
        _row('阶梯内', 'A3-1 + A3-3（帧数 ≤4）', _best(ladder, lambda c: c.ok)),
        _row('全空间', '只要 A3-1（任意画布/网格）', _best(glob, a31)),
        _row('全空间', 'A3-1 + A3-3（帧数 ≤4）', _best(glob, lambda c: c.ok)),
    ]
    txt = _table(['范围', '约束', '最优组合', '每格', '实际短边', '单张token', 'token/帧', '相对现状'],
                 rows, aligns=['<', '<', '<', '>', '>', '>', '>', '>'])
    top = max((c.pack for c in glob), default=0)
    txt += (
        f'\n\n注 1：**A3-1 下「每张帧数」的上限是 {top}**（不是 9）：画布还没大到让每格 ≥300px，'
        f'就先越过 1024 token 上限被缩小；pack≥{top + 1} 穷举无合规画布。\n'
        '注 2：阶梯内 A3-1 与 A3-1+A3-3 是**同一个解**（1066×600/4）—— 因为阶梯的帧数集合里'
        '没有 9/10 档，A3-3 在阶梯内其实没花额外的钱；它的代价只在「全空间」里显形。\n'
        '注 3：全空间最优靠的是 2% 的格子比例容差（523×300 的比例误差 1.94%）。'
        '**若要求零变形，4 帧方案的下界就是 1066×600 / 407 token**（附 B）。')
    return txt


# ---------------------------------------------------------------- 主流程

def frozen_anchor() -> dict | None:
    if not FROZEN_JSON.exists():
        return None
    doc = json.loads(FROZEN_JSON.read_text(encoding='utf-8'))
    for r in doc.get('results', []):
        if r.get('file') == 'fanren184.mp4':
            red = r.get('redundancy') or {}
            if 'pack4' not in red:
                return None
            return {'frames': r.get('frames_processed'),
                    'pack4': red['pack4']['distinct_frac'],
                    'pack8': red['pack8']['distinct_frac']}
    return None


def run_clip(path: pathlib.Path, seconds: float, fps: int, constraints: L.Constraints) -> dict:
    dur = probe_duration(path)
    win = min(seconds, dur)
    t0 = time.perf_counter()
    X = load_window(path, win, fps)
    load_s = time.perf_counter() - t0
    n = len(X)
    expected = win * fps
    meta_ok = abs(n - expected) <= max(2, 0.01 * expected)
    packagings = []
    for pack in (1, 2, 4, 8, 16):
        cand = L.make_candidate((800, 450), pack, constraints, X=X)
        red = metrics.redundancy(X, pack)
        d = cand.as_dict()
        d['sheets'] = cand.sheets(n)
        d['total_tokens'] = cand.total_tokens(n)
        d['distinct_frac'] = red['distinct_frac']
        d['median_min_dist'] = red['median_min_dist']
        packagings.append(d)
    return {
        'clip': path.name, 'duration_s': round(dur, 2), 'window_s': round(win, 2),
        'fps': fps, 'frames': n, 'frames_expected': round(expected, 1),
        'meta_assert_ok': meta_ok, 'load_s': round(load_s, 2),
        'packagings': packagings,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--seconds', type=float, default=30.0)
    ap.add_argument('--fps', type=int, default=6)
    ap.add_argument('--corpus', default=str(CORPUS))
    ap.add_argument('--only', default=None)
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--full-material', action='store_true',
                    help='表 4 用 fanren184 全量算 distinct_frac vs pack 曲线（慢）')
    a = ap.parse_args()

    constraints = L.Constraints()
    corpus = pathlib.Path(a.corpus)
    names = [n for n in CLIPS if (a.only is None or a.only in n)]
    clips_data = []
    print('# M4 量账 · 先出表再设计\n')
    print(f'窗口: min({a.seconds:g}s, 素材时长) @{a.fps}fps；分析口径 '
          f'{metrics.ANALYSIS_W}x{metrics.ANALYSIS_H} 灰度，L1 阈值 {metrics.DIFF_DISTINCT}\n')
    for name in names:
        p = corpus / name
        if not p.exists():
            print(f'!! 素材缺失，跳过: {p}')
            continue
        d = run_clip(p, a.seconds, a.fps, constraints)
        clips_data.append(d)
        print(f"--- {name}: {d['frames']} 帧（期望 {d['frames_expected']}）"
              f" 元断言={'OK' if d['meta_assert_ok'] else 'FAIL'} 抽帧 {d['load_s']}s")

    print('\n## 表 1 · 可读性 / 信息量 / 成本（同一批帧，只换打包方式）\n')
    print(table1_accounting(clips_data))

    print('\n## 表 2 · 设计全表（画布阶梯 × 帧数；A3-1=每格短边≥300，A3-3=pack≤4）\n')
    print(table2_design(constraints))

    print('\n## 表 3 · A3-2 代价（同一段素材要发几张图、总 token 多少）\n')
    ks = (4, 40, 200, 2601, 10403)
    p200 = L.plan(200, constraints)
    print(table3_cost(p200, ks))
    print(f'\n（2601 = fanren184@6fps 全量 10403 帧 / 现状 4 帧一张；'
          f'200 = 立项书 §五 成本表里 k-center 的帧数锚点）')

    print('\n## 表 5 · 三条约束的边际代价（现状 / 只要 A3-1 / A3-1+A3-3）\n')
    print(table5_conflict(constraints))

    print('\n## 表 4 · distinct_frac vs pack（A3-3 的单调性实测）\n')
    anchor = frozen_anchor()
    if a.full_material:
        fp = corpus / 'fanren184.mp4'
        print(f'（正在抽 fanren184 全量 @{a.fps}fps…）', flush=True)
        Xf = metrics.load_gray(fp, sample_fps=a.fps)
        curve = {p: metrics.redundancy(Xf, p) for p in (1, 2, 3, 4, 6, 8, 16)}
        scope = f'fanren184 全量 {len(Xf)} 帧 @{a.fps}fps（本轮实测）'
    else:
        if not clips_data:
            curve, scope = {}, '(无)'
        else:
            d = clips_data[-1]
            Xw = load_window(corpus / d['clip'], d['window_s'], a.fps)
            curve = {p: metrics.redundancy(Xw, p) for p in (1, 2, 3, 4, 6, 8, 16)}
            scope = f"{d['clip']} 前 {d['window_s']:g}s（{len(Xw)} 帧 @{a.fps}fps，本轮实测）"
        print('（表 4 用窗口数据；加 --full-material 走全量）')
    print(f'口径：{scope}\n')
    print(table4_distinct_curve(curve, anchor))

    if a.json:
        import hashlib
        layout_src = (ROOT / 'src' / 'kf' / 'layout.py').read_bytes()
        doc = {'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
               'seconds': a.seconds, 'fps': a.fps, 'clips': clips_data,
               # 产物新鲜度锚点：本 JSON 是 layout.py 的哪一代算出来的（R187 stale 行表）
               'layout_sha256_16': hashlib.sha256(layout_src).hexdigest()[:16],
               'fields': sorted(L.enumerate_candidates(constraints)[0].as_dict().keys()),
               'design_table': [c.as_dict() for c in L.enumerate_candidates(constraints)],
               'cost_table_k200': p200.as_dict(),
               'conflict_table': None,
               'distinct_curve': {str(k): v for k, v in curve.items()},
               'distinct_curve_scope': scope, 'anchor': anchor,
               'full_material': bool(a.full_material)}
        # 表 5 的结论也落盘（否则「上限 10 / 93.80」这些数字没有产物可查）
        glob = {}
        for pack in range(1, 17):
            r = L.optimal_canvas(pack, constraints)
            glob[pack] = None if r is None else {'canvas': [r[0], r[1]], 'tokens': r[2],
                                                 'tokens_per_frame': round(r[2] / pack, 2)}
        doc['conflict_table'] = {'global_optimal_by_pack': glob}
        out = ROOT / 'docs' / 'status' / 'M4-bench.json'
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'\n写出 {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
