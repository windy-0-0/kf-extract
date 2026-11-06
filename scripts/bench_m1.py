#!/usr/bin/env python3
"""M1 基准：在四段素材上跑 k-center 对照（A1 判据）。

输出：每段素材 × 每个 K 的 kcenter/uniform maxdist + 改善 + 时间覆盖率。
"""
import argparse, json, sys, time, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import numpy as np
from src.kf.metrics import load_gray
from src.kf.kcenter import kcenter, compare_with_uniform, time_coverage

CORPUS = [
    ('trailer',   'eval/trailer.mp4',   12),
    ('mv195',     'eval/mv195.mp4',     12),
    ('mv227',     'eval/mv227.mp4',     12),
    ('fanren184', 'eval/fanren184.mp4',  6),
]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', help='只跑名字含该子串的素材')
    ap.add_argument('--ks', default='40,80,160,320,640')
    ap.add_argument('--out')
    a = ap.parse_args()
    ks = [int(x) for x in a.ks.split(',')]
    rows = []
    for name, path, fps in CORPUS:
        if a.only and a.only not in name:
            continue
        p = pathlib.Path(path)
        if not p.exists():
            print(f'SKIP {name}: 素材不存在'); continue
        t0 = time.time()
        X = load_gray(p, sample_fps=fps)
        n = len(X)
        print(f'\n=== {name} ({n} 帧 @{fps}fps) ===')
        print(f'{"K":>5} {"kc_maxdist":>11} {"uni_maxdist":>12} {"改善":>7} {"时间覆盖":>9}')
        for k in ks:
            if k > n: continue
            r = compare_with_uniform(X, [k])[0]
            cov = time_coverage(kcenter(X, k)['keys'], n)
            print(f'{k:>5} {r["kcenter_maxdist"]:>11.1f} {r["uniform_maxdist"]:>12.1f} '
                  f'{r["improvement"]*100:>6.0f}% {cov["buckets_hit"]:>4}/10')
            rows.append({'material': name, 'n': n, **r, **cov})
        print(f'  ({time.time()-t0:.0f}s)')
    if a.out:
        pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(a.out).write_text(json.dumps(rows, indent=2), encoding='utf-8')
        print(f'\n写出 {a.out}')
    return 0

if __name__ == '__main__':
    sys.exit(main())
