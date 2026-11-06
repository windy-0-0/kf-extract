# RESEARCH · 动漫关键帧抽取（KF-Extract）

> 调研索引。正文在 `docs/research/`。**只记会影响决策的**。

---

## R-001 · 变化筛选系（see-now 及其上游）
- 仓库：`SAOS-3624/dsh-see-now`（Apache-2.0，232★）
- 可抄点：`src/cuts.js`（64×36 HSV 网格打分，0.2ms/帧）、`src/tiers.js`（密度因果化 EMA）、`src/pack.js`（DP 装箱）
- **不可抄点**：`cutAbs=12` + 积分判据 —— 面向实时不漏帧，离线场景冗余 86%–99%
- 风险：上游 3 天内从 0.0.8 跳到 0.1.4（日更），README 自称「macOS 悬浮窗已支持」但实测漏了 `-transparentcolor` 平台守卫
- 结论：**参考算法，不依赖**

## R-002 · 30 分钟评测素材（2026-11-06）
- 来源：B站 `av116976135441055`《凡人修仙传 184 集》
- 元数据实测：`duration=1733.907s`（28.9min）、`1920×1080`、`30fps`、12 个视频格式、文件 849MB
- 下载方式：`yt-dlp --no-check-certificate -f 30080`（**证书校验会失败，必须加此参数**）
- 说明：1080P 高码率需大会员，30080 是免费可下的 1080P

## R-003 · 场景/镜头检测的现成件（prior-art）
| 方案 | 许可 | 状态 | 结论 |
|---|---|---|---|
| ffmpeg `scdet` 滤镜 | 随 ffmpeg | 内置 | 已在基线里用（阈值 3/5 实测命中 137/79 个切点 @14s） |
| ffmpeg `select=gt(scene,T)` | 随 ffmpeg | 内置 | **在这批动漫上全部 0 命中**（T=0.1…0.5），不能用 |
| ffmpeg `mpdecimate` | 随 ffmpeg | 内置 | **195s 只省 1.3–4%**，对动画基本无效 |
| `Breakthrough/PySceneDetect` | BSD-3 | 5334★，2026-11-02 活跃 | 可作对照实现（ContentDetector/AdaptiveDetector） |
| `KaiyangZhou/pytorch-vsumm-reinforce` | — | AAAI'18 | 需训练，不纳入本期 |
| `li-plus/DSNet` | — | Detect-to-Summarize | 需训练，不纳入本期 |

**结论**：现成方案里**没有一个能直接做「原画级」**；镜头级有 ffmpeg 内置可用。

## R-004 · 光流补偿的实测结论（已做，记入决策）
- 方法：Farneback 稠密光流 → warp 后帧到前帧 → 残差
- **方向陷阱**：`remap(c, x-fx)` 是错的（残差反增 46%），正确的是 `remap(c, x+fx)`
  —— 用「已知右移 4px」的合成图锚定才校正
- 结果：降幅 23.6%（预告）/ 48.2%（MV 前 60s），但残差与运动量 **corr=0.575**
- 结论：可作**距离度量改进**，不可作独立检测器
