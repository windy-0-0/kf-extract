"""拼图（contact sheet）—— 把 K 张关键帧排成多格图集。

**为什么这是产品化的关键一步**：

整个项目的成本论证（`layout.py` 的 token 数学：一张 1066×600 的四格图 = 407 token，
单张 1920×1080 = 1116 token）只有在**真的把四帧拼成一张图**时才成立。
原型的 `--canvas` 只算了这笔账、打印了预算，**没有一行代码真的拼图**。
所以「每格 533×300」是纸面数字，用户拿到的还是 80 张独立 PNG。

这一层把它落地：

  - 按 `layout.plan()` 给出的 cols/rows/canvas 排版
  - 每格左上角烧一个序号 + 时间戳（**必须能对回原视频**，否则抽出来的帧无法引用）
  - 空格子填底色（不是透明），避免模型看到不规则边缘
  - JPEG 质量可调（图集几乎总是要编码后再送模型）

**标注为什么烧在图上而不是只写 JSON**：
多模态模型看图时并不知道 `frame-00042.png` 对应第几秒。把 `#42 / 00:07:14` 烧进
像素里，模型才能引用「第 42 张、7 分 14 秒那一帧」，而用户在 JSON 里也能查到同一个数。
两边同一个口径 —— 这是让产物自证，不是装饰。

依赖：只用 numpy，**不引入 PIL 作为硬依赖**（JPEG 编码优先用 ffmpeg，已验证可用）。
`PIL` 若存在则用它（更快、支持更多格式），否则走 ffmpeg 路径。
"""
from __future__ import annotations

import pathlib
import subprocess
import tempfile

import numpy as np

from .layout import grid_for
from .procs import find_ffmpeg

__all__ = ['SheetSpec', 'make_sheet', 'sheet_names', 'plan_sheets']

#: 12×6 点阵字（只做大写/数字/冒号/井号/斜杠/点）。**不许用花字库**：
#: 依赖系统字体会在不同平台渲染不同，破坏「同样参数产出同样图」的可复现性。
_GLYPHS: dict[str, tuple[str, ...]] = {
    '0': ('01110', '10001', '10001', '10001', '10001', '01110'),
    '1': ('00100', '01100', '00100', '00100', '00100', '00100'),
    '2': ('01110', '10001', '00100', '01000', '10001', '01110'),
    '3': ('01110', '10001', '00110', '00001', '10001', '01110'),
    '4': ('00010', '00110', '01110', '11111', '00010', '00010'),
    '5': ('11111', '10000', '01110', '00001', '10001', '01110'),
    '6': ('01110', '10000', '01110', '10001', '10001', '01110'),
    '7': ('11111', '00001', '00010', '00100', '01000', '10000'),
    '8': ('01110', '10001', '01110', '10001', '10001', '01110'),
    '9': ('01110', '10001', '00101', '01110', '10001', '01110'),
    'A': ('01110', '10001', '11111', '10001', '10001', '01110'),
    'B': ('11110', '10001', '11110', '10001', '10001', '11110'),
    'C': ('01110', '10001', '10000', '10000', '10001', '01110'),
    'D': ('11110', '10001', '10001', '10001', '10001', '11110'),
    'E': ('11111', '10000', '11110', '10000', '10000', '11111'),
    'F': ('11111', '10000', '11110', '10000', '10000', '10000'),
    'G': ('01110', '10001', '10000', '10111', '10001', '01110'),
    'H': ('10001', '10001', '11111', '10001', '10001', '10001'),
    'I': ('11111', '00100', '00100', '00100', '00100', '11111'),
    'J': ('00111', '00010', '00010', '00010', '10010', '01100'),
    'K': ('10001', '10010', '11100', '10010', '10001', '00100'),
    'L': ('10000', '10000', '10000', '10000', '10000', '11111'),
    'M': ('10001', '11011', '10101', '10101', '11011', '10001'),
    'N': ('10001', '11001', '10101', '10011', '10001', '00100'),
    'O': ('01110', '10001', '10001', '10001', '10001', '01110'),
    'P': ('11110', '10001', '11110', '10000', '10000', '10000'),
    'Q': ('01110', '10001', '10001', '10001', '10010', '00100'),
    'R': ('11110', '10001', '11110', '10010', '10001', '00100'),
    'S': ('01110', '10001', '00110', '00001', '10001', '01110'),
    'T': ('11111', '00100', '00100', '00100', '00100', '00100'),
    'U': ('10001', '10001', '10001', '10001', '10001', '01110'),
    'V': ('10001', '10001', '10001', '10001', '01010', '00100'),
    'W': ('10001', '10101', '10101', '10101', '10101', '01010'),
    'X': ('10001', '01010', '00100', '00100', '01010', '10001'),
    'Y': ('10001', '01010', '00100', '00100', '00100', '00100'),
    'Z': ('11111', '00001', '00010', '00100', '01000', '11111'),
    '#': ('01010', '11111', '01010', '01010', '11111', '01010'),
    ':': ('00000', '00000', '00100', '00000', '00100', '00000'),
    '/': ('00001', '00001', '00010', '00100', '01000', '10000'),
    '.': ('00000', '00000', '00000', '00000', '00000', '00100'),
    '-': ('00000', '00000', '11111', '00000', '00000', '00000'),
    ' ': ('00000', '00000', '00000', '00000', '00000', '00000'),
}
_CH_W, _CH_H = 5, 6


def stamp(img: np.ndarray, text: str, x0: int, y0: int, scale: int = 2,
          color=(255, 220, 0), bg=(0, 0, 0), pad: int = 3) -> None:
    """在 img 左上 (x0,y0) 处烧一串点阵字（原地修改）。

    先画一层半透明底色再画字：直接画黄字叠在亮画面上会看不清（实测亮色场景几乎读不到）。
    """
    text = text.upper()
    w = sum(1 for ch in text if ch in _GLYPHS) * (_CH_W + 1) * scale
    h = _CH_H * scale
    x1 = min(img.shape[1], x0 + w + pad * 2)
    y1 = min(img.shape[0], y0 + h + pad * 2)
    if x1 <= x0 or y1 <= y0:
        return
    roi = img[y0:y1, x0:x1]
    roi[...] = (roi.astype(np.uint16) * 30 // 100 + np.array(bg, np.uint16) * 70 // 100
               ).astype(np.uint8)  # 70% 黑底，保证对比度
    cx = x0 + pad
    for ch in text:
        g = _GLYPHS.get(ch)
        if g is None:
            cx += (_CH_W + 1) * scale
            continue
        for ry, row in enumerate(g):
            for rx, bit in enumerate(row):
                if bit != '1':
                    continue
                py = y0 + pad + ry * scale
                px = cx + rx * scale
                if py >= img.shape[0] or px >= img.shape[1]:
                    continue
                img[py:min(py + scale, img.shape[0]),
                    px:min(px + scale, img.shape[1])] = color
        cx += (_CH_W + 1) * scale


class SheetSpec:
    """一张拼图的排版参数（由 `resolve_spec` 从 layout.Plan 构造）。"""

    def __init__(self, canvas, pack, cols=None, rows=None):
        self.canvas_w, self.canvas_h = int(canvas[0]), int(canvas[1])
        self.pack = int(pack)
        if cols is None or rows is None:
            cols, rows = grid_for(self.pack)
        self.cols, self.rows = int(cols), int(rows)
        self.cell_w = self.canvas_w // self.cols
        self.cell_h = self.canvas_h // self.rows

    @property
    def capacity(self) -> int:
        return self.cols * self.rows

    def __repr__(self) -> str:
        return (f'SheetSpec({self.canvas_w}x{self.canvas_h}, pack={self.pack}, '
                f'grid={self.cols}x{self.rows}, cell={self.cell_w}x{self.cell_h})')

    def to_dict(self) -> dict:
        return {'canvas': [self.canvas_w, self.canvas_h], 'pack': self.pack,
                'cols': self.cols, 'rows': self.rows,
                'cell': [self.cell_w, self.cell_h], 'capacity': self.capacity}


def _load_rgb_sized(path, w: int, h: int) -> np.ndarray:
    """读图并**同时**缩放到目标尺寸 —— 一次 ffmpeg 调用搞定读+缩放。

    直接用 ffmpeg 的 scale 滤镜，比「读原图再 numpy 缩放」快一个数量级，
    而且不需要知道原图尺寸（这正是 PIL 缺席时的难点）。
    """
    try:
        from PIL import Image

        # **等比缩放 + 居中补黑**，不是 resize((w,h))：直接 resize 会把 16:9 的帧
        # 压成格子比例，画面变形后「两帧不同」的视觉判断会失真。
        with Image.open(path) as im:
            im2 = im.convert('RGB')
            iw, ih = im2.size
            scale = min(w / iw, h / ih)
            nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
            small = np.asarray(im2.resize((nw, nh), Image.LANCZOS))
        out = np.zeros((h, w, 3), np.uint8)
        oy, ox = (h - nh) // 2, (w - nw) // 2
        out[oy:oy + nh, ox:ox + nw] = small
        return out
    except ImportError:
        pass

    ff = find_ffmpeg()
    cmd = [ff, '-v', 'error', '-i', str(path),
           '-vf', f'scale={w}:{h}:force_original_aspect_ratio=decrease,'
                  f'pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black',
           '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or len(r.stdout) < w * h * 3:
        raise RuntimeError(f'读图失败 {path}: {r.stderr[:200]}')
    return np.frombuffer(r.stdout[:w * h * 3], dtype=np.uint8).reshape(h, w, 3)


def make_sheet(frames: list, times: list, spec: SheetSpec, labels: list | None = None,
               filename: pathlib.Path | None = None, quality: int = 88,
               bg=(20, 20, 24)) -> tuple[np.ndarray, list[str]]:
    """拼一张图集。返回 (RGB ndarray, 本张收录的标注文本)。

    :param frames: 图片路径列表（最多 spec.capacity 张；多出的不管，由 caller 切片）
    :param times: 对应的时间戳（秒），用于烧在图上
    :param labels: 可选自定义标注（默认生成 `#序号 分:秒`）
    """
    W, H = spec.canvas_w, spec.canvas_h
    canvas = np.empty((H, W, 3), np.uint8)
    canvas[...] = bg
    texts: list[str] = []

    for cell in range(min(len(frames), spec.capacity)):
        r, c = divmod(cell, spec.cols)
        y0, x0 = r * spec.cell_h, c * spec.cell_w
        img = _load_rgb_sized(frames[cell], spec.cell_w, spec.cell_h)
        canvas[y0:y0 + spec.cell_h, x0:x0 + spec.cell_w] = img

        if labels:
            text = labels[cell]
        else:
            t = times[cell] if cell < len(times) and times[cell] is not None else -1
            text = f'#{cell:02d} {_mmss(t)}' if t >= 0 else f'#{cell:02d}'
        texts.append(text)
        # 标注随格子尺寸缩放，且**不小于 1**（1px 字在 533×300 格子上仍可读）
        sc = max(1, min(4, spec.cell_h // 110))
        stamp(canvas, text, x0 + 4, y0 + 4, scale=sc)

    if filename is not None:
        write_image(canvas, filename, quality=quality)
    return canvas, texts


def _mmss(seconds: float) -> str:
    """秒 → MM:SS（超过 1 小时显示 H:MM:SS）。时间戳必须能对回原视频。"""
    s = int(round(float(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f'{h}:{m:02d}:{sec:02d}' if h else f'{m:02d}:{sec:02d}'


def write_image(img: np.ndarray, path, quality: int = 88) -> pathlib.Path:
    """写图。JPEG 走 PIL（有的话）或 ffmpeg；PNG 同理。**不用 `-y` 覆盖已有文件以外的路径**。"""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    h, w = img.shape[:2]

    try:
        from PIL import Image

        im = Image.fromarray(img)
        if p.suffix.lower() in ('.jpg', '.jpeg'):
            im.save(p, quality=quality, subsampling=0)
        else:
            im.save(p)
        return p
    except ImportError:
        pass

    ff = find_ffmpeg()
    with tempfile.TemporaryDirectory() as td:
        raw = pathlib.Path(td) / 'in.raw'
        img.tofile(raw)
        cmd = [ff, '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
               '-s', f'{w}x{h}', '-i', str(raw), '-frames:v', '1', '-y']
        if p.suffix.lower() in ('.jpg', '.jpeg'):
            cmd += ['-q:v', str(max(2, min(31, int(round(31 - quality * 0.29)))))]
        cmd += [str(p)]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f'写图失败 {p}: {r.stderr[:200]}')
    return p


def plan_sheets(n_frames: int, spec: SheetSpec) -> int:
    """这些帧要拼几张图。"""
    if n_frames <= 0:
        return 0
    return (n_frames + spec.capacity - 1) // spec.capacity


def sheet_names(n_frames: int, spec: SheetSpec, stem: str = 'sheet',
                ext: str = 'jpg') -> list[str]:
    """图集文件名列表（与 `plan_sheets` 一致）。"""
    return [f'{stem}-{i:03d}.{ext}' for i in range(plan_sheets(n_frames, spec))]
