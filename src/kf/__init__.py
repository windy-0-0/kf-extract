"""KF-Extract 核心库。

模块边界（多 agent 并行时**严格按目录隔离**）：
  kf/metrics.py   距离/覆盖度量 —— 只读，冻结（scripts/eval.py 引用同一口径）
  kf/kcenter.py   M1：k-center 算法
  kf/fast.py      M2：加速（分块/降维/近似）
  kf/flow.py      M3：光流距离度量（实验结论：不如直接 k-center，见 M3-report）
  kf/layout.py    M4：出图策略（每格多大、几张）
  kf/procs.py     外部程序定位（ffmpeg/ffprobe 跨平台）
  kf/sheets.py    拼图：把关键帧排成图集（多模态模型吃的那种）
  kf/export.py    产物落盘：png/jpg、目录/zip、命名与幂等
  kf/state.py     长任务状态：原子落盘 + 断点续跑
  kf/api.py       稳定公开 API（`from kf import extract`）

**每个模块只改自己那个文件 + 自己的测试**，不许改别人的，不许改 scripts/eval.py。

公共 API 只暴露 `api.py` 里的东西。其余模块是实现细节，可自由重构 ——
这样下游用户不会因为我们换内部结构而炸。
"""
__version__ = '0.2.5'

# ---------------------------------------------------------------- 公共 API
# 对外只暴露这两个名字。其余模块是实现细节，可自由重构。
from .api import ExtractResult, extract  # noqa: E402

__all__ = ['extract', 'ExtractResult', '__version__']
