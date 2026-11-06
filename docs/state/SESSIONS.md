# SESSIONS · 动漫关键帧抽取（KF-Extract）

| 时间 | 里程碑 | 会话 | 模型 | 任务书 | 结果 | 锚点 / 提交 | 备注 |
|---|---|---|---|---|---|---|---|
| 2026-11-06 21:40 | M0 立项 | 本会话 | trae/deepseek-v4.1-flash | `PROJECT-KF-EXTRACT.md` | 立项通过 | 无 commit（工作区） | 三假说已实测，五条验收已定 |
| 2026-11-06 21:49 | M0 素材 | 本会话 | 同上 | `PROJECT-KF-EXTRACT.md` | 下载中 | `eval/fanren184.mp4`（849MB） | 28.9min，30fps |

| 2026-11-06 22:10 | M0 评测器 v1 | 本会话 | trae/deepseek-v4.1-flash | `PROJECT-KF-EXTRACT.md` §三 | 修 4 个 bug 后跑通 trailer | `scripts/eval.py` | 见 D-006~D-009 |
| 2026-11-06 22:20 | M0 30min 素材 | 本会话 | 同上 | 同上 | 下载 `av116976135441055` 849MB | `eval/fanren184.mp4` | 28.9min/30fps/1920x1080 |

| 2026-11-06 23:30 | **M0 完成** | 本会话 | trae/deepseek-v4.1-flash | `PROJECT-KF-EXTRACT.md` §三 | **PASS** | `docs/status/M0-baseline.md` + `M0-fanren.json` | 修 11 条决策；推翻两条初稿预期（D-012/D-014） |
