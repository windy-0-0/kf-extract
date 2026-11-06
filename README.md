# kf-lab · 动漫关键帧抽取实验台

**立项书：`PROJECT-KF-EXTRACT.md`（先读它，验收标准在里面）**

```
eval/                 评测集（三段动画素材，79MB）
  trailer.mp4           14s  60fps  番剧预告
  mv195.mp4            195s 60fps  动画 MV
  mv227.mp4            227s 24fps  动画 MV
core.py               帧加载 / 插值误差 / 关键帧选取
budget.py             覆盖预算 → 需要的帧数
kc.py                 k-center 覆盖对比
mc.py / mc2.py        光流补偿（含 warp 方向锚点）
redundancy.py         拼图组内冗余量化
sheets/               实出的拼图（用于肉眼看冗余）
venv/                 给 see-now 备用包用的 python（mss+tkinter，61MB）
```

依赖：系统 `ffmpeg`（本机 `/opt/homebrew/bin/ffmpeg`）、`numpy`、`cv2`。

**注意**：`venv/` 不是本实验的依赖，它是为了跑 `dsh-see-now` 备用包的悬浮窗
（需要 `mss` + `tkinter`）。实验脚本直接用系统 python3 + numpy + cv2。
