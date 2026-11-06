#!/usr/bin/env python3
"""M2 四段耗时量账与 RSS 采样。

基线模式下严格调用 ``scripts/eval.py`` 中未修改的函数，按评测器实际执行顺序
分别计时。四段口径：

1. ffmpeg：ffprobe + 降采样灰度抽帧
2. k-center：四个覆盖预算共用的一趟最远优先贪心
3. uniform：四个预算各自的精确二分，每次候选 K 都调用原始 maxdist
4. redundancy：pack=4 与 pack=8

镜头切点计算量很小，单列在 ``aux`` 中；其耗时也计入总量，但不混入四段。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import resource
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_eval():
    path = ROOT / "scripts" / "eval.py"
    spec = importlib.util.spec_from_file_location("kf_m0_eval", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def peak_rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS 以字节报告；Linux 以 KiB 报告。本项目的验收机是 macOS。
    if sys.platform == "darwin":
        return value / 1024**2
    return value / 1024


def timed(fn, *args, **kwargs):
    started = time.perf_counter()
    value = fn(*args, **kwargs)
    return value, time.perf_counter() - started


def baseline_breakdown(path: pathlib.Path, fps: int, watch_rss: bool) -> dict:
    ev = load_eval()
    wall_start = time.perf_counter()

    probe, probe_s = timed(ev.probe, path)
    frames, frame_s = timed(ev.load_gray_bytes, path, fps=fps)
    ffmpeg_s = probe_s + frame_s

    budgets = (10, 15, 20, 30)
    cap = min(len(frames), 2000)
    (_, _, kc_hit), kcenter_s = timed(ev._kcenter_track, frames, budgets, cap)

    uniform_values = {}
    uniform_s = 0.0
    for budget in budgets:
        value, elapsed = timed(ev._uniform_exact, frames, budget, cap)
        uniform_values[str(budget)] = value
        uniform_s += elapsed

    redundancy_values = {}
    redundancy_s = 0.0
    for pack in (4, 8):
        value, elapsed = timed(ev.redundancy, frames, pack)
        redundancy_values[f"pack{pack}"] = value
        redundancy_s += elapsed

    cuts, aux_s = timed(ev.scene_cuts, frames)
    elapsed = time.perf_counter() - wall_start
    stages = {
        "ffmpeg_抽帧": ffmpeg_s,
        "k-center_单趟": kcenter_s,
        "uniform_二分×maxdist": uniform_s,
        "redundancy": redundancy_s,
    }
    stage_sum = sum(stages.values())
    return {
        "mode": "baseline",
        "file": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
        "fps": fps,
        "frames": len(frames),
        "probe": probe,
        "elapsed_s": round(elapsed, 1),
        "stages_s": {key: round(value, 1) for key, value in stages.items()},
        "stage_sum_s": round(stage_sum, 1),
        "stage_sum_ratio": round(stage_sum / elapsed, 3) if elapsed else None,
        "aux_s": round(aux_s, 1),
        "coverage": {
            "kcenter": {str(key): value for key, value in kc_hit.items()},
            "uniform": uniform_values,
        },
        "redundancy": redundancy_values,
        "scene_cuts": len(cuts),
        "peak_rss_mb": round(peak_rss_mb(), 1) if watch_rss else None,
    }


def optimized_breakdown(path: pathlib.Path, fps: int, watch_rss: bool) -> dict:
    """调用 M2 加速评测；保留独立入口，避免量账和验收走不同代码。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from src.kf.fast import evaluate_fast

    started = time.perf_counter()
    result = evaluate_fast(path, sample_fps=fps)
    elapsed = time.perf_counter() - started
    result = dict(result)
    result.update(
        {
            "mode": "optimized",
            "elapsed_s": round(elapsed, 1),
            "peak_rss_mb": round(peak_rss_mb(), 1) if watch_rss else None,
        }
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default="eval/fanren184.mp4")
    parser.add_argument("--fps", type=int, default=6)
    parser.add_argument("--mode", choices=("baseline", "optimized"), default="baseline")
    parser.add_argument("--watch-rss", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--require-four-stage", action="store_true")
    args = parser.parse_args()
    path = (ROOT / args.corpus).resolve()
    if not path.exists():
        print(f"FAIL: 素材不存在: {path}", file=sys.stderr)
        return 2

    if args.mode == "baseline":
        result = baseline_breakdown(path, args.fps, args.watch_rss)
        stages = result["stages_s"]
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else
              "四段拆账: " + ", ".join(f"{k}={v:.1f}s" for k, v in stages.items()))
        print(
            f"总计: {result['elapsed_s']:.1f}s; 四段和: {result['stage_sum_s']:.1f}s; "
            f"覆盖比: {result['stage_sum_ratio']:.3f}",
            file=sys.stderr if args.json else sys.stdout,
        )
        if args.watch_rss:
            print(f"峰值 RSS: {result['peak_rss_mb']:.1f}MB",
                  file=sys.stderr if args.json else sys.stdout)
        if args.require_four_stage:
            reference = 1050.4
            if abs(result["stage_sum_s"] - reference) / reference > 0.10:
                print(
                    f"FAIL: 四段和 {result['stage_sum_s']:.1f}s 偏离 1050.4s 超过 10%",
                    file=sys.stderr,
                )
                return 1
    else:
        result = optimized_breakdown(path, args.fps, args.watch_rss)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            stages = result.get("stages_s", {})
            if stages:
                # stages 里可能混入嵌套 dict（如 uniform_各预算），只格式化标量
                print("四段拆账(优化): "
                      + ", ".join(f"{k}={v:.1f}s" for k, v in stages.items()
                                  if isinstance(v, (int, float))))
            print(f"优化评估: elapsed={result['elapsed_s']:.1f}s "
                  f"decode={result.get('decode')}")
        elapsed = result["elapsed_s"]
        ok = elapsed <= 180.0 and result.get("meta_assert_ok", False)
        # `--json` 时 stdout 必须是**纯 JSON**（下游要 json.load 它）；
        # 人类可读行一律走 stderr，否则会出现 "Extra data" 解析失败（实测踩过）。
        sink = sys.stderr if args.json else sys.stdout
        if args.watch_rss:
            print(f"峰值 RSS: {result['peak_rss_mb']:.1f}MB", file=sink)
        print(f"{'PASS' if ok else 'FAIL'}: elapsed={elapsed:.1f}s (<=180s)", file=sink)
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
