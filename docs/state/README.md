# 怎么用这个账本

## 一条命令看全貌

```bash
python3 scripts/context_index.py          # 一页：状态表 + 最近决策 + 阻塞
python3 scripts/context_index.py --full   # 加最近会话/文档目录
```

## 新会话/新 agent 的最短开场

```text
先读 kf-lab/docs/state/STATE.md → DECISIONS.md 最近 5 条 → PROJECT-KF-EXTRACT.md（立项与验收）
只改 kf-lab/ 下；验收脚本必须能复跑；决策写回 DECISIONS；进度写回 STATE。
```

## 更新时机（少而准）

| 时机 | 写什么 |
|---|---|
| 模块开工 | STATE 状态改 `in_progress`；SESSIONS 加一行 |
| 模块交付 | STATE 改 `pass/reject` + 数字锚点；SESSIONS 写结果 |
| 任何决策 | DECISIONS 追加，**必须含当时否掉的方案** |
| 决策被推翻 | 旧条目标 `superseded by D-xxx`，不删 |
| 发现阻塞 | STATE「阻塞/风险」写清：现象/证据/等谁 |
| 调研完成 | RESEARCH 加索引 |

## 三条纪律

1. **事实带锚点**：日期、commit、文件:行号、命令输出的原始数字。没有锚点的「已修复」不写。
2. **不重写历史**：错的决策保留并标 superseded，否则下一个人会再犯。
3. **一页能读完**：STATE 超过两屏就拆到 `docs/status/`。
