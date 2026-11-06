"""产物落盘：命名、幂等、清理旧文件、打包 zip、清单。

**这里藏着一个会让用户误判的真陷阱**（不是理论风险）：

先跑 `--k 80`，输出目录里有 `frame-00000.png … frame-00079.png`；
再跑 `--k 40`，产出 `frame-00000.png … frame-00039.png`。
后 40 个文件**留在原地**，名字、格式、分辨率都合法，排在 `frame-0039` 后面。
用户打开目录看到 80 张，会以为第二次也抽了 80 张 —— 这就是假绿。

所以 `prune_stale()` 是必需的：只删**我们自己命名规则**内、且不在本次产物清单里的文件。
不碰用户的其它文件（不认识的绝不删 —— R135：删之前必须证明它没在用，我们做不到，
所以「只删自己造的」是唯一安全的边界）。
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import zipfile

__all__ = ['IMAGE_PATTERNS', 'SHEET_PATTERNS', 'prune_stale', 'write_zip',
           'manifest', 'sha256_file', 'summary_markdown']

#: 本工具独占的命名规则。**只在这两个模式内做增删**。
IMAGE_PATTERNS = (r'^frame-\d{5}\.png$',)
SHEET_PATTERNS = (r'^sheet-\d{3}\.(jpg|jpeg|png)$',)

_RX = tuple(re.compile(p) for p in IMAGE_PATTERNS + SHEET_PATTERNS)


def _is_ours(name: str) -> bool:
    return any(rx.match(name) for rx in _RX)


def prune_stale(outdir, keep: list) -> list[str]:
    """删掉本次产物清单之外的、我们自己造的文件。返回被删列表。

    `keep` 是本轮产物的文件名（不是绝对路径 —— 我们只在自己目录里工作）。
    目录不存在或为空时直接返回。
    """
    d = pathlib.Path(outdir)
    if not d.is_dir():
        return []
    keep_set = {pathlib.Path(k).name for k in keep}
    removed = []
    for p in d.iterdir():
        if not p.is_file() or not _is_ours(p.name) or p.name in keep_set:
            continue
        try:
            p.unlink()
            removed.append(p.name)
        except OSError:
            # 删不掉不是致命错误（例如另一个进程正开着它），但**必须让它可见**，
            # 不能静默跳过然后用户以为什么都清理干净了。
            removed.append(f'!{p.name}')
    return removed


def sha256_file(path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def manifest(files: list) -> list[dict]:
    """每个产物的 (名字, 字节数, sha256)。**用于让别人复核我们真的产出了这些东西。**"""
    out = []
    for f in files:
        p = pathlib.Path(f)
        if not p.exists():
            out.append({'file': p.name, 'error': 'missing'})
            continue
        st = p.stat()
        out.append({'file': p.name, 'bytes': st.st_size,
                    'sha256': sha256_file(p)[:32]})
    return out


def write_zip(indir, zip_path, files: list | None = None) -> pathlib.Path:
    """把产物打成 zip（可选）。默认打全部产物，**不含 `.kf-state.json`**（那是内部状态）。"""
    d = pathlib.Path(indir)
    z = pathlib.Path(zip_path)
    if z.exists():
        z.unlink()
    with zipfile.ZipFile(z, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        names = files if files is not None else [
            p.name for p in sorted(d.iterdir())
            if p.is_file() and p.name != '.kf-state.json']
        for name in names:
            p = d / pathlib.Path(name).name
            if p.exists():
                zf.write(p, arcname=p.name)
    return z


def summary_markdown(report: dict, sheets: list, images: list) -> str:
    """给人看的 `summary.md` —— 30 秒读完，不必翻 JSON。

    每一项都带「这个数是什么口径」，否则用户会把 maxdist 当成像素或帧数。
    """
    L = []
    L.append(f'# 关键帧抽取结果 · {pathlib.Path(report.get("video", "?")).name}')
    L.append('')
    status = report.get('status', 'ok')
    if status != 'ok':
        L.append(f'> **状态：{status}** —— 这不是完整结果。已产出的部分见下；'
                 f'按 `--resume` 可继续。')
        L.append('')
    L.append(f'- 选中帧数：**{report.get("selected_k")}**')
    L.append(f'- 覆盖质量 maxdist：**{report.get("maxdist")}**'
             f'（32×18 灰度上的 L1 均值，0–255 刻度；越小 = 盲区越小）')
    L.append(f'- 时间空白：最大 **{report.get("max_gap_sec")}s**，'
             f'中位 {report.get("median_gap_sec")}s')
    cov = report.get('time_coverage') or {}
    if cov:
        L.append(f'- 时间覆盖：{cov.get("buckets_hit")}/{cov.get("buckets")} 段'
                 f'（均匀 = 每段都有）')
    L.append(f'- 时间均衡权重：{report.get("balance")}')
    dk = report.get('dark_frames') or {}
    if dk.get('in_selection'):
        L.append(f'- 黑场：选中帧里 **{dk["in_selection"]} 帧近乎全黑**'
                 f'（全片共 {dk["in_video"]} 帧黑场）—— 它们占图集格子但不含信息')
    cp = report.get('canvas_plan') or {}
    if cp.get('error') or cp.get('note'):
        L.append(f'- 出图计划：**未完成** {cp.get("error") or cp.get("note")}')
    elif cp:
        L.append(f'- 出图档位：{cp.get("profile")}（{cp.get("label")}）'
                 f' → {cp.get("sheets")} 张图集，共 **{cp.get("tokens_total")} token**'
                 f'（同帧数基线 {cp.get("baseline_tokens")}，'
                 f'相对 {cp.get("token_overhead_pct")}%）')
    L.append(f'- 耗时：{report.get("elapsed_s")}s')
    warns = report.get('unverified') or report.get('warnings') or []
    if warns:
        L.append('')
        L.append('## 需要你注意（未验证项）')
        for w in warns:
            L.append(f'- {w}')
    if images or sheets:
        L.append('')
        L.append('## 产物')
        if images:
            L.append(f'- 原分辨率关键帧 {len(images)} 张：`{pathlib.Path(images[0]).name}` …')
        if sheets:
            L.append(f'- 拼图 {len(sheets)} 张：`{pathlib.Path(sheets[0]).name}` …'
                     f'（每格左上角烧了 `#序号 分:秒`，可对回原视频）')
    L.append('')
    L.append('> 所有数字可在 `extract.json` 复核；拼图上的时间戳与 JSON 的 '
             '`keys_time_sec` 一一对应。')
    return '\n'.join(L) + '\n'


def write_json(path, doc) -> pathlib.Path:
    """报告落盘（原子写，见 state.atomic_write_json 的同款理由）。"""
    from .state import atomic_write_json

    p = pathlib.Path(path)
    atomic_write_json(p, doc)
    return p


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
