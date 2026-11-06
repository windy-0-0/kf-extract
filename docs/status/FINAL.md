# 最终交付报告 · 动漫关键帧抽取（KF-Extract）

**2026-11-07**　仓库：`/Users/mac/kf-lab`　commit `65a18ac`

---

## 一句话

**24 分钟番剧 → 150 秒 → 80 张关键帧（原分辨率），时间覆盖 10/10 段，组内冗余降到一半。**

---

## 全链路验收（一条命令）

```bash
python3 scripts/probe_accept.py
```

```
--- M2 ---  PASS
--- M3 ---  PASS
--- M4 ---  PASS
✅ 全部通过
```

探针自带 selftest（`--selftest` 证明它会红：越界检测、禁区检测都能抓住）。

---

## 六个模块

| # | 模块 | 状态 | 关键数字 | 证据 |
|---|---|---|---|---|
| M0 | 评测器 + 基线 | ✅ | 四段素材、8 个冻结数字 | `M0-baseline.md` |
| M1 | k-center | ✅ | 收益 **21–38%**，时间覆盖 **10/10** | `M1-report.md` |
| M2 | 加速 | ✅ | **1096s → 98–135s（9.4×）**，基线 8/8 不变，RSS 311MB | `M2-report.md` |
| M3 | 光流距离 | ⚠️ **判据 FAIL（不可行）** | 同标尺下选帧**变差 +14.3%~+32.0%**，corr 0.513 仍混运动 | `M3-report.md` / D-018 |
| M4 | 出图策略 | ✅ | 达标 **1066×600×4帧**（533×300），代价 **+75% token** | `M4-report.md` |
| M5 | 端到端 CLI | ✅ | `scripts/kf_extract.py`，出原分辨率 PNG | 本文下方 |

---

## 端到端实测（真实番剧 28.9 分钟）

```bash
python3 scripts/kf_extract.py eval/fanren184.mp4 --k 80 --out shots --fps 6
```

```
PASS: k=80 maxdist=42.1 时间覆盖=10/10 耗时=150.5s
```

- **80 张 PNG**，每张 **1920×1080**（原分辨率，不是拼图）
- `extract.json` 含：K / maxdist / 时间覆盖率 / 每帧时间戳 / `unverified`（空 = 无未验证项）

---

## 三条诚实结论（都是负数）

### 1. 「省十倍」在真实番剧上不成立
k-center 在番剧上只赢 **21–38%**（短 MV 能赢 91%）。能承诺的是「同覆盖质量省 25–35%」。

### 2. A3（每格 ≥300px）要 +75% token
唯一可行组合是 `1066×600 / 4 帧`（533×300）。画布从 800×450 换成 1066×600，
token 从 233/张 涨到 407/张。**帧数不变、张数不变，只有 token 涨。**

### 3. 光流不能进生产
覆盖率改善 78–89% 那个数是**跨刻度相减的假比较**（已推翻，见 D-018）；同标尺下选帧变差
（+14.3%~+32.0%，四项全变差），且 **全帧率 325s / 6fps 下也吃 81s**，比优化后的整条链路还慢。
**结论：M3 本路线关闭。** 旧版提的「先 scdet 切镜头、再镜头内光流」**不再作为可行路径推荐**——
它建立在「光流距离更好」这个已被推翻的前提上；若将来要做，必须在该设定下重跑同口径判据。

---

## 两个真实 bug（都是「假绿」形态）

| bug | 表现 |
|---|---|
| M4 `accept_line` | 访问不存在的 `cand.cell` → 崩溃；修好后**对 400×225 也输出 PASS**（R99 永远不会红的闸门） |
| M2 `bench_m2 --json` | JSON 后混入人类可读行 → 下游 `json.load` 报 "Extra data" |

---

## 一个环境污染事故（已登记 R374）

同一份代码四次测得 164/133.7/**316.8**/102.9 秒，我先误判为 decode 路径差异。
真根因：**孤儿 ffmpeg**（父进程已死）按 395% CPU 跑了 5 分钟，`load average` 20.18。

修复：`scripts/benchlock.py`（单例锁 + 孤儿扫描 `--kill`）。

**教训：计时器无法区分「我的代码快了」和「抢我 CPU 的进程没了」。**

---

## 目录

```
kf-lab/
├── PROJECT-KF-EXTRACT.md    立项书（含 A1–A5 验收标准）
├── docs/state/              账本：STATE / DECISIONS(18 条) / SESSIONS / RESEARCH
├── docs/prompts/            三份任务书（M2/M3/M4，合同式）
├── docs/status/             每模块报告 + FINAL（本文）
├── src/kf/                  metrics(冻结) kcenter( M1) fast(M2) flow(M3) layout(M4)
├── scripts/                 eval( M0) bench_m1..m4 kf_extract(M5) probe_accept benchlock freeze_baseline
├── tests/kf/                test_kcenter(10) test_fast(9) test_flow(24) test_layout(8)
└── eval/                    四段素材（含 849MB 番剧）
```

---

## 待决策（3 条）

1. **A3 的 +75% token 要不要做成参数？** 建议 `--canvas auto|small|readable`，默认 auto。
2. **光流要不要走「先切镜头再光流」？** 预计 12s，待验证。
3. **A2（组内真不同 ≥70%）归属应改到 M4** —— 立项书 §三分工写错，A2 与选帧算法无关。
