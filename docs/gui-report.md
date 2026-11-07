# GUI 优化报告（v3 · P0 + 批量 + 端到端回归已沉淀）

> **本文档记录到 v0.2.4。** 一轮之内做了三轮：
> v2 = 三个 P0（历史/输出目录/viewer）
> v3 = 缩放、静止段跳转、批量模式、e2e 进仓库、统一验收脚本

---

## 追加：v3 做了什么（都在真浏览器里验证）

| 功能 | 验证证据 |
|---|---|
| **viewer 缩放** | `+` 两次 → 2.3× 且进 zoomed 态；适应回 1.0×；**1:1 按原始像素**（1066 宽 → 1.4×）；滚轮可缩放；切图自动回适应 |
| **静止段一键跳转** | codecast 上点「看这段附近的关键帧」→ viewer 打开 `单帧 21 / 25`（`frame-00015.png`，63.2s 附近）|
| **批量模式** | 勾 2 个 → `批量运行中（0/2）` → `全部完成（2/2）`，历史 2 条，结果展示 |
| **e2e 沉淀** | `tests/e2e/run.mjs`，33 条断言，一条命令跑完 |
| **统一验收** | `bash scripts/accept_all.sh` = 152 单元 + 33 e2e + 3 闸门 |

### 三个真 bug（都是"只有真点才看得见"）

**① 点「批量（可多选）」什么都不发生**
插桩发现：`onclick exit 时 checked=true`，但 `final: false`。
根因：span 包在 `<label>` 里，label 原生转发点击给 checkbox，**我翻一次 + 转发翻一次 = 净零**。
修：`preventDefault + stopPropagation`。

> 之前试了「改用 `cb.click()`」「自己翻 checked 再调 onchange」两种写法**都无效**——
> 两次翻转互相抵消，看起来像 handler 没绑上。**只有插桩能定位。**

**② 批量完成提示被覆盖**
`pollBatch` 先 `setHint('全部完成 2/2')`，紧接着 `selectJob` 又 `setHint('已恢复该任务的结果')`。
用户也看不到批量摘要（不只是测试问题）。修：`selectJob(id, quiet)`。

**③ viewer 的 1:1 算错（1066 的图渲染成 1297px）**
用 `transform: scale()` 时，`getBoundingClientRect().width` **已经包含 scale**，
比例被乘了两次。改：用 `width` 缩放（布局宽度就是缩放后的值）。

### 三次假绿，同一个主题

| # | 我注入的坏行为 | 为什么没红 |
|---|---|---|
| 1 | `mkdir` 一个**已存在**的目录 | 不产生任何可观测变化，本来就不该抓到 |
| 2 | 检查点放在 `finally` **之前** | 那时目录当然还在 |
| 3 | e2e 用 `evaluate` 直接改 `checked` | **根本没点那个 label**，bug 与判据不同频 |

**形成硬规则**：e2e 里凡是"用户动作"，一律用 `page.click / page.type / page.keyboard`；
`page.evaluate` 只能用于**读**，不能用于**做**。

---



> 按 `docs/prompts/GUI-v2.txt` 的验收要求写：每个结论都带证据，不写"测过了"。

## 交付了什么

| 优先级 | 功能 | 状态 |
|---|---|---|
| P0-a | 任务历史（内存态，不落盘）| ✅ |
| P0-b | 输出目录可改 + 白名单校验 + 记住上次 | ✅ |
| P0-c | 图片 viewer（点开/←→/Esc）| ✅ |
| P1-a | 长静默提示（≥10s 无更新会说"仍在工作"）| ✅ |
| P1-b | 键盘操作（Enter 开始 / Esc 关 viewer / ←→ 切图）| ✅ |

`src/kf/gui.py`：587 → **854 行**（后端 441 + 前端模板 413）。

---

## 一、真机验证（无头 Chrome 驱动，不是 curl）

用 puppeteer-core + 本机 Chrome 跑完整交互，**一次跑完三个 P0**：

```json
{
 "jobsCount": 2,
 "jobsText": ["trailer.mp4 | k=16 · 完成 · 2.7s",
              "trailer.mp4 | k=8 · 完成 · 6.5s"],
 "afterSelect": {"hint":"已恢复该任务的结果", "k":"8", "sheets":2},
 "badOut": {"hint":"输出目录不可用：输出目录必须在 /Users/mac 或 /Users/mac/kf-lab 之下。你给的是 /private/etc/shots",
            "focused":"out"},
 "viewer": {"opened":{"on":true,"pos":"拼图 1 / 10"},
            "afterRight":"拼图 2 / 10",
            "closed":true},
 "errors": []
}
```

逐条解读：

- **P0-a**：跑两个任务（k=16、k=8）→ 历史列表 **2 条**且顺序正确（新的在前）
  → 点较早那条 → hint 变成「已恢复该任务的结果」、统计区显示 **k=8**、**2 张拼图**
  （即真的切回了那个任务的结果，不是重跑）
- **P0-b**：填 `/etc/shots` → 出现**可读中文**拒绝原因（含"你给的是 /private/etc/shots"）
  且**焦点自动跳到输入框**（`focused: "out"`），用户不用自己找哪里错了
- **P0-c**：点第 1 张拼图 → viewer 打开显示 `拼图 1 / 10` → `→` 切到 `2 / 10` → `Esc` 关闭
- **JS 错误：无**

> 上次就是因为只 `curl` 不看浏览器，结果 17 张图全 404 却没发现（DECISIONS D-038）。
> 所以这次三个 P0 **全部**用真实点击验证。

### 截图暴露并修掉的布局问题

第一次截图显示：**任务历史挤到首屏最上方占满宽度，主流程被推到下面**，
且长错误文案把「开始抽取/停止」按钮挤变形。

根因与修法：
1. 媒体查询里 `.side{order:-1}` 让小屏时侧栏跑到了**上面** → 改 `order:2`（主内容先）
2. 提示文案和按钮在同一行 → 抽出独立 `.hintline`（独占一行、可换行）
3. `.job .nm` 没 `min-width:0`，长文件名把 meta 挤掉 → 加 `flex:1 1 auto; min-width:0`
4. 「默认」按钮被挤成两行 → `flex:0 0 auto; white-space:nowrap`

修后截图确认：主流程在前、历史在下方、按钮不再变形。

---

## 二、每条新闸门都证明过会红（5/5）

按任务书 §4.2 要求，逐个注入坏行为 → 跑测试 → 确认变红 → 恢复。

| 注入的退化 | 结果 | 抓到它的测试 |
|---|---|---|
| 去掉回环地址检查（绑 0.0.0.0）| **13/14** | `test_binds_loopback_only` |
| 删掉 `/api/jobs` 路由 | **12/14**（2 条红）| `test_jobs_history_lists_and_orders` + `test_token_required_everywhere` |
| 绕过输出目录校验 | **13/14** | `test_api_start_returns_structured_error` |
| 任务名不转义（`esc(j.name)` → `j.name`）| **13/14** | `test_template_escapes_user_strings` |
| 删掉 viewer 元素 | **13/14** | `test_template_renders_and_has_core_elements` |
| 去掉长静默阈值判断 | **13/14** | `test_template_has_long_silence_notice` |

### 一次假绿，已修

「长静默」第一次注入**没变红**：原断言只查 `'stale_seconds' in out`，
但该字段在模板里出现两处（判断处 + 文案拼接处），删掉判断那行字段名仍在。

改成**断言判断逻辑本身**：
```python
assert re.search(r'j\.stale_seconds\s*>=\s*\d+', out)   # 必须真的做比较
m = re.search(r'j\.stale_seconds\s*>=\s*(\d+)', out)
assert 5 <= int(m.group(1)) <= 30                          # 阈值不能摆着看
```
再注入 → 立刻变红。

> 这与 D-051 是同一族教训：**判据必须问"逻辑还在不在"，不能问"字段名还在不在"**。

---

## 三、硬约束遵守情况（§2）

| 约束 | 遵守 | 证据 |
|---|---|---|
| 不引入新依赖 | ✅ | 仍是 `http.server` + 原生 JS；`pyproject.toml` 的 dependencies 只有 numpy |
| 只绑回环 | ✅ | `test_binds_loopback_only`，注入实测变红 |
| token 校验（含新端点）| ✅ | `test_token_required_everywhere` 覆盖 `/api/jobs` |
| 目录白名单 | ✅ | `test_file_route_blocks_traversal` + `test_out_dir_validation_*` |
| **无删除端点** | ✅ | `test_no_delete_endpoint` 扫 `os.remove`/`shutil.rmtree`/`/api/delete` |
| 连点去重不回归 | ✅ | `test_dedupe_uses_active_jobs_only`（运行中复用、已完成不复用）|
| 停止不杀线程 | ✅ | `test_stop_sets_flag_not_kills_thread` |
| 不谎报 | ✅ | `interrupted` 显示「已停止（不是失败）」，界面无"完成"字样 |
| 单文件优先 | ✅ | 前端仍在 `_INDEX_HTML` 里 |
| 中文界面 | ✅ | 所有报错都是人话（见 §1 的 badOut 输出）|

---

## 四、全量回归（贴真实输出）

```
test_api.py       15/15 passed
test_cli.py       14/14 passed
test_fast.py       9/9 项通过
test_flow.py      24/24 passed
test_gui.py       14/14 passed     ← 新增
test_kcenter.py   21/21 passed
test_layout.py    30/30 passed
test_sheets.py    10/10 passed
test_state.py     15/15 passed
                                  ──────── 合计 152
✅ 基线未动（度量口径一致）
✅ 无产物入库（检查了 2 个 eval/ 下的文件）
```

`check_no_artifacts` 尤其重要：GUI 默认把产物写到素材旁边，
我上次就是这样把 84 个 png 提交进仓库（D-043）。

---

## 五、没碰的文件（§5）

`scripts/eval.py`、`src/kf/metrics.py`、`docs/status/BASELINE-FROZEN.json`、
`docs/status/M0-baseline.md`、`src/kf/kcenter.py` —— **一个都没动**，
冻结闸门 ✅ 就是证据。

---

## 六、遗留问题（诚实列出）

1. **viewer 没有缩放/平移**。只能整图 fit；要看 1066×600 里的小字，
   还是得下载原图。加 zoom 会引入手势处理复杂度，本轮没做。
2. **任务历史只在本进程内**。刷新页面能看到（走 `/api/jobs`），但**重启 GUI 就没了**。
   这是 D-037 的有意选择（避免僵尸任务），不是疏忽。
3. **没有批量处理**。一次一个视频。
4. **`static_segments` 警告不能点击跳转**到对应时间点（P2 项，时间盒内优先做了 P0）。
5. **前端没有自动化回归**。`test_gui.py` 只测后端 + 模板结构；
   真实点击流程靠上面的 puppeteer 脚本，那个脚本**没有进仓库**，
   下次要重跑得重新写。这是已知缺口 —— 如果要长期维护，应把脚本收进 `tests/e2e/`。

---

## 七、下一步建议（按价值排序）

1. **把 puppeteer 脚本收进仓库**（`tests/e2e/gui.mjs`），否则前端回归全靠人肉
2. viewer 加滚轮缩放（对"读清小字"这个卖点是直接收益）
3. 批量模式：选一个目录，对所有视频跑同一套参数
