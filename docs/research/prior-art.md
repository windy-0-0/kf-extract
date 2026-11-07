# Prior Art · 视频关键帧选取（市场对标记录）

> 依据 `open-source-first` skill；结论同步进 `docs/state/DECISIONS.md` D-030 ~ D-032。
> 记录时间：2026-11-07。**查证方式：GitHub API 元数据 + 读论文全文 + 同标尺实测。**

## 需求词（用于搜索，不是产品名）
`video keyframe selection` · `video summarization keyframe` · `shot detection keyframe`
· `video frame sampler` · `contact sheet storyboard video` · `coverage-driven keyframe`

## 候选清单

### 1. CSES — Coverage-Driven Adaptive Keyframe Selection
- **repo/来源**：https://arxiv.org/abs/2608.00714 （预印本，2026-08-01，**未经同行评审**）
- **license**：CC BY-NC-ND 4.0（论文）；**未找到开源代码仓库**
- **维护**：2026-08
- **体量**：论文 753KB；依赖 BLIP-large 权重（~1.5GB）
- **可抄点**：附录 F `Algorithm 2` 的贪心增量更新与**第 10 行平局规则**
  （`minimize max_{s∈S} K_js` = 选离已选集最远者）
- **风险**：**需要 GPU**；代码未开源；NC-ND 许可证**禁止演绎**，代码不可抄只能读设计
- **结论：读设计，不抄代码**。乘积核与饱和停止**实测否决**（D-031）

### 2. FOCUS — Efficient Keyframe Selection for Long Video Understanding
- **repo/来源**：https://proceedings.iclr.cc/paper_files/paper/2026/file/a010577f93a8301dada61f0b97cda923-Paper-Conference.pdf （ICLR 2026）
- **license**：论文；代码未见开源
- **体量**：BLIP ITM 前向；论文计时 H100 80GB
- **可抄点**：「粗筛时间段 → 段内分配」的层级结构（消融证明增益主要来自粗筛）
- **风险**：**强 query-aware**，无 query 算法塌掉；**无时间覆盖概念**（附录 H 自认）
- **结论：reimplement 不适用**（赛道不同）；只借鉴「时间粗到细」的思路

### 3. PySceneDetect
- **repo**：https://github.com/Breakthrough/PySceneDetect
- **license**：BSD-3-Clause ✅
- **维护**：2026-11 活跃（5400★，Python）
- **可抄点**：`scenedetect.detectors.ContentDetector` 的帧差加权
- **风险**：只给切点，不给「等长 K 帧」；对无切点连续运动（街拍）无效
- **结论：** 作为**基线竞品**用了（`scripts/bench_market.py`），**未引入依赖到产品**

### 4. video-keyframe-detector
- **repo**：https://github.com/joelibaceta/video-keyframe-detector
- **license**：**GPL-3.0** ❌
- **维护**：2025-06（216★）
- **可抄点**：峰值估计（frame difference peak）
- **风险**：**GPL 不可并入 MIT 项目**
- **结论：** 只做方法对照（`diffpeak`），**代码一行未并入**

### 5. ttharden/Keyframe-Extraction-for-video-summarization
- **repo**：https://github.com/ttharden/Keyframe-Extraction-for-video-summarization
- **license**：**无 LICENSE** ❌（当"不可用"）
- **维护**：2024-09（69★）
- **可抄点**：聚类 + 镜头内去冗余的结构
- **结论：** 只读设计，不抄代码

### 6. jooray/video-slide-extractor
- **repo**：https://github.com/jooray/video-slide-extractor
- **license**：Unlicense ✅
- **维护**：2026-09（5★，4KB Shell）
- **可抄点**：人脸过滤（我们没做，但场景太窄）
- **结论：** 否决 —— 只适合幻灯片，且体量不足参考

### 7. ffmpeg `select` 滤镜
- **license**：LGPL/GPL（取决于编译选项）
- **结论：depend** —— 已是我们的底座（`-vf select=`），非竞品

## 许可证闸门结论

| 项目 | License | 能否抄进代码 | 我们的处理 |
|---|---|---|---|
| PySceneDetect | BSD-3 | ✅ | 仅作基线对照 |
| video-keyframe-detector | GPL-3.0 | ❌ | 仅作方法对照 |
| ttharden | 无 | ❌ | 仅读设计 |
| CSES / FOCUS | 论文（NC-ND / 未见码）| ❌ | 仅读算法 |
| jooray | Unlicense | ✅ | 否决（太窄）|

**没有任何外部代码进入本项目。** 新增依赖仅 `scenedetect`（**装在评测 venv，非产品依赖**，
`pyproject.toml` 里没有它）。

## 决策汇总

| 问题 | 结论 |
|---|---|
| 我们是最优方案吗？ | **不是最新，但在同标尺下多数最好**（vlog 好 1.9–2.1×；liveact 输 5.9%）|
| 要改算法吗？ | **乘积核否决**（7/7 变差）；**饱和自适应 K 否决**（K 掉一半、覆盖崩 8–18×）|
| 要上 BLIP/CLIP 吗？ | **不上** —— 会把 CPU-only 变成 GPU 依赖，而我们的场景没有 query |
| 要上 bandit 吗？ | **不上** —— FOCUS 自己的消融显示增益主要来自粗筛，置信半径只 +0.6~1.5 |
| 最该补的是什么？ | **不是算法，是"看不见局部变化"这个盲区**（D-032，已量化为 `static_segments`）|
