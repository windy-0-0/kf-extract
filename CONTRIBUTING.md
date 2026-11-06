# 贡献指南

## 这个项目最看重的一件事

**每个数字都要能复跑。**

我们发过几次"看起来很漂亮但其实是假的"结论，都被更长的素材或更严的标尺打回来了
（详见 `docs/state/DECISIONS.md` 的 D-012 / D-014 / D-018）。所以这里有一条硬规矩：

> **提交任何性能或质量改进，必须附带「改前 / 改后」的实测数字，且基线必须逐位不变。**

"感觉更快了"、"看起来更准了" 一律打回。

---

## 提交前自测

```bash
# 1. 单元测试必须全绿
for f in tests/kf/test_*.py; do venv/bin/python $f; done

# 2. 基线冻结检查必须通过
venv/bin/python scripts/freeze_baseline.py --check

# 3. 三模块探针
venv/bin/python scripts/probe_accept.py
```

三条全过才算完成。

---

## 不许改的文件

| 文件 | 为什么 |
|---|---|
| `scripts/eval.py` | 它是**度量定义**。改它 = 所有历史数字作废 |
| `src/kf/metrics.py` | 同上，冻结口径 |
| `docs/status/BASELINE-FROZEN.json` | 冻结指纹 |

如果你确实需要改度量（例如换距离函数），正确的流程是：

1. 在 `docs/state/DECISIONS.md` 写一条新决策，说明**为什么**要改、改了什么、否掉了哪些方案
2. **全量重跑**所有素材，把新数字写进 `docs/status/`
3. `venv/bin/python scripts/freeze_baseline.py --freeze`

**不许把度量变更混在"优化"提交里。**

---

## 新增评测素材

放到 `eval/` 下（视频不进 git），然后在 `eval/corpus.json` 里加一条：

```json
{"name": "...", "file": "...", "fps": 6, "kind": "..."}
```

`fps` 是**分析时的降采样帧率**：长素材用 6，短素材用 12。

---

## 代码风格

- **注释写"为什么"和实测数字**，不写"是什么"。代码已经说了是什么。
- 一个 PR 只做一件事。修 bug 和顺手重构请分开。
- **不引入新运行时依赖**，除非它能省下几倍的开销。本项目的卖点是"轻"。
- 测试要覆盖**失败路径**，不只是正常路径。

---

## 报告格式

PR 描述请按这个结构：

```
1. 改了什么（一句话）
2. 改前 / 改后的实测数字（表格）
3. 基线冻结检查的输出原文
4. 未验证的部分（用 UNVER 标记，不要假装通过）
```

---

## 许可证

提 PR 即表示你的贡献按 **MIT** 授权（见 [LICENSE](LICENSE)）。

但注意：`src/kf/layout.py` 里的 token 估算公式来自 Apache-2.0 的第三方项目
（见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)），
改动那部分代码时**不得移除归属声明**。
