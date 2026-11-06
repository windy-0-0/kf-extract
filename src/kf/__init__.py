"""KF-Extract 核心库。

模块边界（多 agent 并行时**严格按目录隔离**）：
  src/kf/metrics.py   距离/覆盖度量 —— 只读，冻结（scripts/eval.py 引用同一口径）
  src/kf/kcenter.py   M1：k-center 算法
  src/kf/fast.py      M2：加速（分块/降维/近似）
  src/kf/flow.py      M3：光流距离度量
  src/kf/layout.py    M4：出图策略（每格多大）
  src/kf/cli.py       M5：端到端抽取器

**每个模块只改自己那个文件 + 自己的测试**，不许改别人的，不许改 scripts/eval.py。
"""
