# eval · 评测语料

**视频文件不在版本库里**（近 1GB，且是外部素材）。这里只有清单。

## 清单

见 [`corpus.json`](corpus.json)。六段素材覆盖不同语言/画风/帧率：

| 素材 | 时长 | 帧率 | 类型 |
|---|---|---|---|
| fanren184 | 28.9 min | 30fps | 国漫 3D |
| fanren027 | 11.6 min | 25fps | 国漫 3D |
| spy_clip | 14.1 min | 30fps | 日漫 2D |
| mv195 | 3.2 min | 60fps | 动画 MV |
| mv227 | 3.8 min | 24fps | 动画 MV |
| trailer | 0.2 min | 60fps | 番剧预告 |

## 自己复跑

把视频放进本目录、按上表命名，然后：

```bash
venv/bin/python scripts/kf_extract.py eval/fanren184.mp4 --k 80 --out shots/
```

或用你自己的视频 —— 工具不依赖特定素材。

## 为什么素材不进库

1. 版权：这些是公开平台的动画片段，只在本机用于评测，不随项目分发
2. 体积：近 1GB，clone 会很慢
