# 三个使用断点的根因分析（动工前的设计）

> 用户报："这三条都一次性解决掉，否则实际使用起来经常断掉，体验很差"

## 三条各是什么，根因各在哪

| # | 现象 | 直接根因 | 真正缺的能力 |
|---|---|---|---|
| 1 | 任务历史只在进程内，**重启 GUI 就没** | `App.jobs` 是普通 dict | **任务账本要跨进程持久** |
| 2 | 批量不支持**每个视频不同参数** | `start_batch` 把所有视频套同一份 params | **任务要能各自带参数** |
| 3 | 批量**关掉 GUI 后要重跑** | 队列只存在于内存 | **队列要能从磁盘恢复** |

**三者其实是同一件事**：没有「可持久化的任务队列」。

## prior-art 结论（读了源码，不是只看 README）

调研对象 **huey**（MIT, 6061★, 2026-11 活跃）：
- `SqliteHuey` 用 **stdlib sqlite3**（`cysqlite` 只是可选加速）—— 依赖层面完全可接受
- 实测：入队 3 个 → **换新实例仍能读到 3 个**（真持久化，`/tmp/hqtest/t2.py`）
- **但不搬它**，原因是架构不匹配：
  - huey 是「生产者入队 → **独立 consumer 进程**执行 → 结果放 KV」
  - 我们是「**在同进程内**跑长任务 → 要实时进度/中断 → 产物落盘续跑」
  - 硬接的话要绕开它的 consumer/Result/信号体系，还要 pickle 序列化我们的回调 —— 得不偿失
- **搬它的实现思路**（这才是"不重复造轮子"的正确用法）：
  sqlite 单表 + 原子状态迁移 + `pid` 存活的僵尸清理

## 设计

新模块 `src/kf/queue.py`，一个 sqlite 文件（默认 `<root>/.kf-queue.db`）：

```
jobs(id, video, params_json, out, status, stage, frac, msg, error,
     group_id, created, started, finished, result_json)
```

- **status**：`queued | running | done | failed | interrupted | cancelled`
- **恢复语义**：启动时把所有 `running` 改成 `interrupted`（进程死了就不可能还在跑）
  并记 `note='上次 GUI 退出时未完成'`
- **每个任务自带 params**（解决 #2），支持 `batch` 里逐个覆盖
- **结果**：`done` 后把 `result_json` 落库（解决 #1：重启后历史+结果都还在）

## 与现有 `RunState` 的关系（不重复造）

`RunState` 是**单任务、单输出目录**的续跑账本（`.kf-state.json`），
记录"哪个阶段做完了、指纹多少"。**它继续负责续跑**。
新队列只管"有哪些任务、各是什么状态"，两者正交：
- 队列丢了 → 任务还在，可以重新入队
- RunState 丢了 → 任务要从头跑

**所以不能合并**（合并会让"队列坏了"和"续跑坏了"变成一个故障面）。
