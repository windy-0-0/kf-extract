# SESSIONS · 动漫关键帧抽取（KF-Extract）

| 时间 | 里程碑 | 会话 | 模型 | 任务书 | 结果 | 锚点 / 提交 | 备注 |
|---|---|---|---|---|---|---|---|
| 2026-11-06 21:40 | M0 立项 | 本会话 | trae/deepseek-v4.1-flash | `PROJECT-KF-EXTRACT.md` | 立项通过 | 无 commit（工作区） | 三假说已实测，五条验收已定 |
| 2026-11-06 21:49 | M0 素材 | 本会话 | 同上 | `PROJECT-KF-EXTRACT.md` | 下载中 | `eval/fanren184.mp4`（849MB） | 28.9min，30fps |

| 2026-11-06 22:10 | M0 评测器 v1 | 本会话 | trae/deepseek-v4.1-flash | `PROJECT-KF-EXTRACT.md` §三 | 修 4 个 bug 后跑通 trailer | `scripts/eval.py` | 见 D-006~D-009 |
| 2026-11-06 22:20 | M0 30min 素材 | 本会话 | 同上 | 同上 | 下载 `av116976135441055` 849MB | `eval/fanren184.mp4` | 28.9min/30fps/1920x1080 |

| 2026-11-06 23:30 | **M0 完成** | 本会话 | trae/deepseek-v4.1-flash | `PROJECT-KF-EXTRACT.md` §三 | **PASS** | `docs/status/M0-baseline.md` + `M0-fanren.json` | 修 11 条决策；推翻两条初稿预期（D-012/D-014） |
| 2026-11-06 23:56 | **M4 出图策略** | M4 会话 | deepseek-v4.1-flash | `docs/prompts/M4-v1.txt` | **PASS** | `src/kf/layout.py` + `tests/kf/test_layout.py`(24) + `scripts/bench_m4.py` + `scripts/accept_m4.sh` + `docs/status/M4-report.md` | A3-1 达标=1066×600/4帧（533×300），代价 token +74.7%；见 D-015。与统筹方会话并发写过同两个文件（R23） |
| 2026-11-07 01:20 | M4 复核修正 | M4 会话 + 独立复核 agent | deepseek-v4.1-flash | `docs/prompts/M4-v1.txt` | **D-015 三处数字被推翻并修正** | `docs/status/M4-report.md` §8 + `DECISIONS.md` D-016 | 修 `accept_line` 假绿 / `optimal_canvas` 画布比例写死；pack 上限 9→10；A3-1-only 98.0→93.80；25 条测试，验收门加 3 处真断言 |
| 2026-11-07 01:35 | **M2 加速** | M2 会话 | dsh/sn-kimi-k3 | `docs/prompts/M2-v1.txt` | **PASS** | `src/kf/fast.py` + `tests/kf/test_fast.py`(9) + `scripts/bench_m2.py` + `scripts/accept_m2.sh` + `docs/status/M2-report.md` | 1050.4s→**111.4s**（9.4×，A4-1）；8/8 逐位不变（A4-2）；RSS 311.7MB（A4-3）；uniform 991.5s→1.0s。见 D-017。并行期间 O7 越界改动已回退留档；计时按负载分档（111–272s） |
| 2026-11-07 02:15 | **M3 + M0.5** | M3 会话（接管并发草稿） | deepseek-v4.1-flash | `docs/prompts/M3-v1.txt` | **M0.5 PASS / M3 判据 FAIL（如实报不可行）** | `src/kf/flow.py` + `src/kf/report.py` + `tests/kf/test_flow.py`(24) + `scripts/bench_m3.py` + `docs/status/M3-report.md` | 同标尺下光流选帧变差 +14.3%~+32.0%（四项全变差）→ 不可行；M0.5 三列交付并纠正「时间分布均匀」假结论（单段最多 37.5%）。见 D-018。接管时 `flow.py` 有半成品草稿（静默截断 1000 帧），已重写；与统筹方会话并发写过同文件（R23） |
