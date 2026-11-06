# Third-Party Notices

本项目整体采用 **MIT** 许可证（见 [LICENSE](LICENSE)）。

但**有一处代码不能改授为 MIT** —— 见下。这一部分仍受其原始许可证约束，
且其条款优先于本项目的 MIT 条款（仅就那部分代码而言）。

---

## 1. dsh-see-now 的 token 估算公式（Apache-2.0）

**受影响文件：** `src/kf/layout.py` 中的以下符号

- `IMAGE_PATCH` / `IMAGE_DOWNSAMPLE` / `IMAGE_CELL` / `IMAGE_TOKEN_CAP` / `IMAGE_UPSCALE_AREA` 常量
- `_pad_to_patch` / `_grid_cells` / `_grid_tokens` / `_project_size` / `image_tokens`
- `grid_for`（列/行选择逻辑）

**来源：**
[dsh-see-now](https://github.com/SAOS-3624/dsh-see-now) — `src/tokens.js`
（Copyright 2026 SAOS-3624）

**移植方式：** 逐行等价移植（Python 重写，算法与常量一致）。

**为什么不能改授：** Apache-2.0 第 4 条要求衍生作品保留原许可证与归属声明，
**不允许**把它重新授权为 MIT。我们曾尝试独立重写以绕开这一限制，
但实测重写版与原件在 2000 组随机尺寸中有 **431 组**结果不同（都发生在触发
1024 token 上限的大尺寸场景）—— 数学上不严格等价，所以**没有采用**。

**该部分代码按 Apache License 2.0 授权：**

```
Copyright 2026 SAOS-3624

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

**如果你需要 MIT-only 的干净版本：** 删除 `src/kf/layout.py` 中上述符号，
用你自己的 token 估算替换 `image_tokens()` 即可。其余代码不受影响。

---

## 2. 仅借鉴思路、未复制代码的部分

以下部分**只借鉴了问题框架或算法思想**，没有复制任何源代码：

| 来源 | 借鉴内容 | 我们的实现 |
|---|---|---|
| dsh-see-now `src/pack.js` | DP 装箱的问题建模（多尺寸格子铺满画布） | `src/kf/layout.py` 的 `grid_for` 只做均匀网格，**没有实现 DP 装箱**（见 M4 报告 §未做） |
| dsh-see-now `src/cuts.js` | 64×36 网格打分的变化量思路 | `src/kf/metrics.py` 用纯 numpy 重写，分辨率与公式独立标定（32×18） |
| PySceneDetect | 场景切分的概念 | 未使用其代码；仅以 `ffmpeg scdet` 作为对照基线 |

---

## 3. 运行时依赖（未随本项目分发）

| 组件 | 用途 | 许可证 |
|---|---|---|
| `ffmpeg` / `ffprobe` | 解码与抽帧 | LGPL/GPL（取决于你的构建） |
| `numpy` | 数值计算 | BSD-3-Clause |
| `opencv-python-headless` | 光流（仅 M3，未接生产） | Apache-2.0 |

这些组件由用户自行安装，本项目**不重新分发**它们的任何二进制。

---

## 4. 测试素材

`eval/` 目录下的视频素材**不包含在本仓库中**（见 `.gitignore`），
它们是从公开平台下载的动画片段，仅用于本地评测。
仓库只提交了 `eval/corpus.json`（素材清单与元数据），不含任何视频内容。

如果你要复跑评测，需自行准备素材并放到 `eval/` 下，
或用 `scripts/kf_extract.py` 跑你自己的视频。
