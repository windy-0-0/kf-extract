"""稳定公开 API。

    import kf
    r = kf.extract('番剧.mp4', k=80, out='shots/')
    print(r['maxdist'], r['sheets'])

**为什么需要这一层**（而不是让用户直接 `from kf.kcenter import ...`）：

内部模块为了做实验会频繁改签名（`kcenter` 已经有过三轮：加 `_adapt`、加
`kcenter_balanced`、`distance` 参数从「选帧列表」改成「距离函数」）。
下游如果直接依赖内部函数，我们每做一次实验就炸一次用户。

所以：**内部随便改，`extract()` 的面孔不动**。这是库和脚本的分界线。

返回字典是纯 JSON 可序列化的（没有 numpy 对象、没有 Path），
因为最常见用法就是 `json.dump(kf.extract(...))` 存下来给别的程序吃。
"""
from __future__ import annotations

import os
import pathlib
import time

__all__ = ['extract', 'ExtractResult', 'version']

_SCHEMA = '1'


def version() -> str:
    from . import __version__

    return __version__


class ExtractResult(dict):
    """`dict` 的子类：既能当 JSON 用，也能属性访问（`r.maxdist`）。

    子类化（而不是包一层）是为了让 `json.dumps(r)` 直接可用 ——
    多一层 `.data` 只会让每个调用点多写一次 `.data`。
    """

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def save(self, path) -> pathlib.Path:
        """存成 JSON 文件（原子写）。"""
        from .export import write_json

        return write_json(path, dict(self))

    def summary(self) -> str:
        from .export import summary_markdown

        return summary_markdown(dict(self), self.get('sheet_files') or [],
                                self.get('images') or [])


def extract(video, k=None, budget=None, fps=6.0, balance=0.2,
            canvas=None, out=None, images=True, sheets=True,
            image_ext='png', sheet_ext='jpg', quality=88,
            resume=False, progress=None, record=True) -> ExtractResult:
    """从视频选出 K 个关键帧（可选拼成图集）并导出。

    :param video: 视频路径（任何 ffmpeg 能解的东西）
    :param k: 抽多少帧；与 `budget` 二选一
    :param budget: 最大盲区预算（L1，0–255 刻度）。给了就自动决定 K
    :param fps: 分析时的降采样帧率（默认 6）。越高越准越慢，内存线性涨
    :param balance: 时间均衡权重 0–1（默认 0.2）。0 = 纯内容优先（会留长空白）
    :param canvas: `cheap` / `readable` / `max`；默认按 `readable`
    :param out: 输出目录；`None` 则只算不落盘（纯库用法）
    :param images: 是否导出原分辨率 PNG
    :param sheets: 是否拼成图集（**这是省 token 的关键**）
    :param image_ext: 单帧格式 `png`/`jpg`
    :param sheet_ext: 图集格式 `jpg`/`png`
    :param quality: JPEG 质量 1–100
    :param resume: 复用输出目录里未变的阶段（改了参数会自动失效）
    :param progress: 可选回调 `progress(stage, frac, msg)`，用于接进度条 UI
    :return: `ExtractResult`（dict），键见 README「报告字段」

    **不抛裸异常**：ffmpeg 缺失抛 `procs.FFmpegNotFound`（退出码 3 的语义），
    素材有问题抛 `RuntimeError`，用户中断抛 `state.Interrupted`。
    """
    from .export import manifest, prune_stale, summary_markdown, write_json
    from .kcenter import (describe_motion, kcenter, kcenter_balanced,
                             need_for_budget, time_coverage)
    from .layout import CANVAS_PROFILES, DEFAULT_CANVAS_PROFILE, constraints_for, plan
    from .metrics import load_gray, static_runs
    from .procs import FFmpegNotFound, describe, find_ffmpeg, find_ffprobe
    from .state import Interrupted, RunState, SignalGuard, fingerprint, stream_gray
    from . import sheets as S

    t0 = time.time()
    video = pathlib.Path(video)
    dur_hint = 0.0
    if not video.exists():
        raise FileNotFoundError(f'素材不存在: {video}')
    if k is None and budget is None:
        raise ValueError('必须给 k 或 budget 之一（不猜）')
    if k is not None and budget is not None:
        raise ValueError('k 与 budget 只能给一个')

    stages: dict[str, float] = {}

    def tick(name, t_since):
        stages[name] = round(time.time() - t_since, 2)
        if progress:
            progress(name, 1.0, f'{name} 完成 {stages[name]}s')

    ff = find_ffmpeg()
    # ffprobe 是**可选**的：只有 ffmpeg 时降级运行，并把「这些数是推算的」说出来。
    # 实测确认 imageio-ffmpeg 只带 ffmpeg 不带 ffprobe —— 所以这条降级路径
    # 不是一个假想的兜底，是真实用户会走到的分支。
    try:
        fp = find_ffprobe()
    except FFmpegNotFound:
        fp = None
    # 时长在解码前就取：一是给进度条算分母，二是导帧换算要用。
    # 早期版本要到导帧那一步才取，于是进度条全程没有分母（只能显示「已收 N 帧」）。
    dur_hint = _probe_duration(fp, video, ff)

    outdir = pathlib.Path(out) if out else None
    if outdir:
        # 先把目录建出来：解码缓存、状态文件、报告全要写在这里。
        # 晚建会让 cache.npz 在写的时候才炸（实测踩到，报错点在离根因 3 层之外）。
        outdir.mkdir(parents=True, exist_ok=True)
    state = RunState(outdir, resume=resume) if outdir else None
    if state:
        # 立刻落盘一次：否则「在解码阶段被 Ctrl-C」会连一个状态文件都不留，
        # 用户看到空目录，以为工具什么都没做（实测踩到）。
        state.data['video'] = str(video.resolve())
        state.data['params'] = {'k': k, 'budget': budget, 'fps': fps,
                                'balance': balance, 'canvas': canvas}
        state.flush()

    # ---- 阶段 1：解码成 32×18 灰度
    #
    # 优先级**必须**是：完整缓存 > 部分缓存续解 > 从头解码
    # 早期版本把「续解」写在了 `reusable('decode', ...)` 里面，而 `reusable`
    # 要求该阶段 `state == 'done'` —— 崩溃后状态是 running，于是续解分支
    # **永远进不去**，每次都从头解（实测：强杀后重跑仍要 55 秒）。
    # 三者的判据必须分开写，不能嵌套。
    fp_decode = fingerprint(video, {'fps': fps, 'w': 32, 'h': 18})
    # **outdir 可能是 None**（纯库用法：`kf.extract(video, k=8)` 只算不落盘）。
    # 早期重构时直接 `pathlib.Path(outdir)`，把纯库 API 整个弄崩了
    # （TypeError: not 'NoneType'，8 条测试同时红）。判空必须在最前面。
    npz = pathlib.Path(outdir) / '.kf-cache-gray.npz' if outdir else None
    partial = pathlib.Path(outdir) / '.kf-partial.npy' if outdir else None
    X = None

    # ① 完整缓存（已验证 done + 指纹一致 + 文件在）
    if state and npz and state.reusable('decode', fp_decode) and npz.exists():
        import numpy as np

        X = np.load(npz)['X'].astype('float32')
        tick('decode', time.time())
        state.warn('复用上次解码结果（参数未变）')

    # ② 部分缓存 → **续解**（不是重新开始）
    if X is None and state and partial and partial.exists():
        import numpy as np

        try:
            P = np.load(partial)
            got = len(P)
            # 合理范围：太小没意义，太大说明文件坏了
            if 0 < got < 200000:
                t_partial = time.time()
                state.warn(f'续用上次已解出的 {got} 帧，从第 {got} 帧继续')
                rest = stream_gray(video, sample_fps=fps, ffmpeg=ff,
                                   skip_seconds=got / max(fps, 1e-9))
                if len(rest):
                    X = np.concatenate([P.astype('uint8'), rest.astype('uint8')])
                else:
                    X = P.astype('uint8')
                tick('decode', t_partial)
                np.savez_compressed(npz, X=X.astype('uint8'))
                try:
                    partial.unlink()          # 完整缓存已就位，部分缓存可以删了
                except OSError:
                    pass
                if 'decode' in state.data.get('stages', {}):
                    state.data['stages']['decode'].pop('partial_frames', None)
                state.mark('decode', 'done', fp=fp_decode, artifacts=[str(npz)])
        except Exception:                     # noqa: BLE001
            # 续解失败就老实重来；**不许静默拿半截数据继续**（那是最坏的一种假绿）
            state.warn('续解失败，本次完整重新解码')
            X = None

    # ③ 从头解码（带增量落盘，防 SIGKILL）
    if X is None:
        t = time.time()
        if state:
            state.mark('decode', 'running', fp=fp_decode)
        expect = max(1, int(round(_probe_duration(fp, video) * fps))) if dur_hint else 0

        _chunks: list = []
        #: **增量落盘**：SIGKILL（强杀/拔电）绕过一切 Python 代码，
        #: `except BaseException` 救不了 —— 必须边解边存。
        _SAVE_EVERY = 1024
        _saved = [0]

        def _flush_partial():
            """把当前已解出的帧写到 `.kf-partial.npy`（原子替换）。

            用 `.npy` 不用 `.npz`：npz 是 zip，每存一次都要全量重压，
            29 分钟素材会越存越慢。npy 未压缩，写 10403×576 只要 ~6MB / 几十毫秒。
            """
            if not state or not _chunks:
                return
            try:
                import numpy as np

                part = np.concatenate(_chunks)
                tmp = pathlib.Path(outdir) / '.kf-partial.npy.tmp'
                with open(tmp, 'wb') as f:
                    np.save(f, part)
                os.replace(tmp, partial)       # 原子替换：不会留半个文件
                _saved[0] = len(part)
                state.mark('decode', 'running', fp=fp_decode,
                           artifacts=[str(partial)], partial_frames=int(len(part)))
            except Exception:                  # noqa: BLE001
                pass                           # 落盘失败不该毁掉解码本身

        def _on_chunk(n_got, arr):
            _chunks.append(arr)
            if n_got - _saved[0] >= _SAVE_EVERY:
                _flush_partial()
            if progress and expect:
                progress('decode', min(1.0, n_got / expect), f'已解码 {n_got}/{expect} 帧')
            elif progress:
                progress('decode', 0.0, f'已解码 {n_got} 帧')

        try:
            with SignalGuard():
                X = stream_gray(video, sample_fps=fps, ffmpeg=ff, on_chunk=_on_chunk)
        except BaseException:
            # 正常中断（Ctrl-C / 停止按钮）走这里；SIGKILL 走不到，靠上面的增量落盘
            if state and _chunks:
                try:
                    _flush_partial()
                except Exception:              # noqa: BLE001
                    pass
            raise
        if len(X) == 0:
            raise RuntimeError(f'解出 0 帧：{video}（检查 ffmpeg 是否能解这个容器）')
        tick('decode', t)
        if state:
            import numpy as np

            np.savez_compressed(npz, X=X.astype('uint8'))
            if 'decode' in state.data.get('stages', {}):
                state.data['stages']['decode'].pop('partial_frames', None)
            state.mark('decode', 'done', fp=fp_decode, artifacts=[str(npz)])
            try:
                partial.unlink()              # 完整缓存就位，部分缓存清掉
            except OSError:
                pass

    n = len(X)
    if progress:
        progress('decode', 1.0, f'{n} 帧已解码')

    # ---- 阶段 2：选帧
    t = time.time()
    fp_select = fingerprint(video, {'fps': fps, 'k': k, 'budget': budget,
                                    'balance': balance, 'n': n})
    res = None
    if state and state.reusable('select', fp_select):
        res = state.get('select').get('result')
    if res is None:
        with SignalGuard():
            if k is not None:
                res = (kcenter_balanced(X, k, fps=fps, alpha=balance) if balance > 0
                       else kcenter(X, k))
            else:
                got = need_for_budget(X, budget, cap=n)
                if got is None:
                    raise RuntimeError(
                        f'预算 {budget} 在 {n} 帧（{fps}fps）内达不到。'
                        f'放宽 budget，或提高 --fps 增加候选帧。')
                res = got
        if state:
            state.mark('select', 'done', fp=fp_select,
                       result={kk: res[kk] for kk in ('keys', 'maxdist') if kk in res})
    tick('select', t)

    keys = list(res['keys'])
    chosen_k = len(keys)
    md = float(res['maxdist'])

    # ---- 黑场诊断（**只报告，不改变选帧**）
    # 实测：trailer 选中的 8 帧里有 1 帧灰度均值 0.0（9.17s 处的纯黑转场）。
    # 它占掉 1/4 的图集格子，但对人和模型都提供 0 信息 —— 407 token 白花。
    # 但**不能自动剔除**：整段夜景的片子、片尾黑屏转场，黑就是内容本身。
    # 所以只把它变成可见的数字，取舍交给用户（D-023）。
    import numpy as _np

    _dark = X.mean(axis=1)
    dark_total = int((_dark < 8.0).sum())
    dark_sel = [int(k) for k in keys if _dark[k] < 8.0]

    # ---- 静态段（假绿探测器，见 metrics.static_runs 的 docstring）
    # **只报告，绝不改选帧**：这类"看起来没变化其实有变化"的段，
    # 只有在用户知情时才有价值；自动规避会变成第二个未声明的目标。
    _runs = static_runs(X, min_frames=max(2, int(round(fps))))
    _runs_info = [{'start_sec': round(a / fps, 1), 'end_sec': round(b / fps, 1),
                   'dur_sec': round((b - a) / fps, 1), 'frames': int(b - a + 1)}
                  for a, b in _runs]
    _runs_info.sort(key=lambda r: -r['dur_sec'])
    _longest_static = _runs_info[0] if _runs_info else None

    # ---- 成本锚点：这段素材"有多难抽"（用户运行前就该知道大概要多少帧）
    motion = describe_motion(X)
    result_motion = motion

    # ---- 派生指标（与选帧解耦，便于复核）
    cov = time_coverage(keys, n)
    ks = sorted(keys)
    import numpy as np

    gaps = np.diff(ks) if len(ks) > 1 else np.array([n])
    max_gap_sec = round(float(gaps.max()) / max(fps, 1e-9), 1)
    median_gap_sec = round(float(np.median(gaps)) / max(fps, 1e-9), 1)

    # ---- 出图计划
    profile = canvas or DEFAULT_CANVAS_PROFILE
    if profile == 'auto':
        plan_out = {'profile': 'auto',
                    'note': 'UNVER: 自动档未实现；请显式传 cheap/readable/max'}
        spec = None
    else:
        lp = plan(chosen_k, constraints=constraints_for(profile),
                  distinct_table={4: 0.509, 8: 0.489})
        r = lp.recommended
        spec = S.SheetSpec(r.canvas, r.pack)
        plan_out = {'profile': profile, 'label': CANVAS_PROFILES[profile]['label'],
                    'canvas': list(r.canvas), 'pack': r.pack,
                    'grid': [spec.cols, spec.rows],
                    'cell': [spec.cell_w, spec.cell_h],
                    'short_side': round(r.short_side),
                    'sheets': lp.sheets, 'tokens_total': lp.total_tokens,
                    'baseline_sheets': lp.baseline_sheets,
                    'baseline_tokens': lp.baseline_tokens,
                    'token_overhead_pct': round(
                        (lp.total_tokens / max(1, lp.baseline_tokens) - 1) * 100, 1)}

    # ---- 时长与帧率（导帧换算需要）。dur 在解码前已取（进度条分母），这里复用
    dur = dur_hint if dur_hint > 0 else _probe_duration(fp, video, ff)
    src_fps = _probe_fps(fp, video, ff)

    times = [round(i * (dur / max(1, n)), 3) if dur else None for i in keys]

    result = ExtractResult({
        'schema_version': _SCHEMA,
        'tool_version': version(),
        'video': str(video.resolve()),
        'requested': {'k': k, 'budget': budget, 'fps': fps, 'balance': balance,
                      'canvas': profile, 'images': images, 'sheets': sheets},
        'selected_k': chosen_k,
        'analysis_frames': n,
        'analysis_size': [32, 18],
        'maxdist': round(md, 3),
        'time_coverage': cov,
        'max_gap_sec': max_gap_sec,
        'median_gap_sec': median_gap_sec,
        'static_segments': {'count': len(_runs_info), 'longest': _longest_static,
                            'top': _runs_info[:5]},
        'motion': result_motion,
        'dark_frames': {'in_video': dark_total, 'in_selection': len(dark_sel),
                        'selection_idx': dark_sel,
                        'threshold_mean_gray': 8.0},
        'balance': balance,
        'canvas_plan': plan_out,
        'keys_analysis_idx': ks,
        'keys_time_sec': times,
        'ffmpeg': describe(),
        'stages_s': stages,
        'warnings': [],
        'unverified': [],
        'images': [], 'sheets': [], 'sheet_files': [], 'manifest': [],
        'elapsed_s': 0.0,
        'status': 'ok',
    })

    if not fp:
        result['warnings'].append(
            '没有 ffprobe：时长/帧率是从 `ffmpeg -i` 的输出里解析的（精度略低）。'
            '时间戳与导帧位置可能有 ±1 帧的偏差。装 ffmpeg 全套可消除此警告。')
    if dur <= 0:
        result['warnings'].append('拿不到时长：时间戳列可能错位（导帧仍会尝试）')
    # 「时间预算被前端高变化吃掉」的定向提示。
    # 触发条件收紧到「只可能是这一种情况」（R377）：
    #   ① 最长静止段占全片 >= 50%
    #   ② 最大空白 >= 20s，且**确实落在这个静止段里**
    #      （`start_sec <= max_gap` 这个判据代替了"K <= 40"——
    #       实测：K=60 时空白 48s 但落在静止段之外，条件②自己就不成立。
    #       所以 K 上限是多余条件，删掉，不留"永远不会红"的闸门）
    # 两条同时成立，才可能真的是"变化全集中在前段"。其余一律不猜。
    _dur = n / max(fps, 1e-9)
    if (_longest_static and _dur > 0
            and _longest_static['dur_sec'] / _dur >= 0.5
            and max_gap_sec >= 20
            and _longest_static['start_sec'] <= max_gap_sec):
        result['warnings'].append(
            f"这段视频有 {_longest_static['dur_sec']}s（占 "
            f"{round(100 * _longest_static['dur_sec'] / _dur)}%）画面基本不变，"
            f"而最大空白 {max_gap_sec}s 也落在这里。"
            f"当前 K={chosen_k} 的预算主要被变化多的部分占用了。"
            f"若你想让静止段也有更多代表帧，用 --balance 0.5 或加大 --k。")

    if _longest_static and _longest_static['dur_sec'] >= 20:
        result['warnings'].append(
            f"发现一段连续 {_longest_static['dur_sec']}s 几乎静止的画面"
            f"（{_longest_static['start_sec']}s→{_longest_static['end_sec']}s）。"
            "像素度量对「缓慢漂移」不敏感（底部字幕、光标、进度条这类小面积变化），"
            "但整段画面确实基本不变。若这段里有字幕/操作细节，建议人工抽查。")

    if len(dark_sel) > 0:
        pct = round(100.0 * len(dark_sel) / max(1, chosen_k), 1)
        result['warnings'].append(
            f'选中的 {chosen_k} 帧里有 {len(dark_sel)} 帧近乎全黑（灰度均值<8，占 {pct}% 的图集容量）；'
            f'这些帧几乎不含信息。若这不是你要的画面，可提高 K 或换 --fps。')

    # ---- 阶段 3+4：导帧 / 拼图（没有 out 就跳过一切落盘）
    if outdir:
        keep: list[str] = []
        try:
            # **整个导段/拼图段都必须在 SignalGuard 内**。
            # 实测踩到：只包住 decode/select 时，用户在第 3 阶段按 Ctrl-C
            # 会拿到 `subprocess.py` 的裸 traceback（真正的原因是
            # `subprocess.run` 内部的 communicate 抛了默认的 KeyboardInterrupt），
            # 并且状态文件停在 'running' —— 用户不知道自己是「中断了」还是「崩了」。
            with SignalGuard():
                if images or sheets:
                    t = time.time()
                    made = _export_frames(ff, video, keys, n, dur, src_fps,
                                          outdir, image_ext, quality, resume=None)
                    result['images'] = [str(p) for p in made]
                    keep += [p.name for p in made]
                    tick('export_frames', t)

                if sheets and spec is not None and result['images']:
                    t = time.time()
                    sf = _build_sheets(result['images'], times, spec, outdir,
                                       sheet_ext, quality, progress)
                    result['sheet_files'] = [str(p) for p in sf]
                    result['sheets'] = [p.name for p in sf]
                    keep += [p.name for p in sf]
                    tick('sheets', t)
        except Interrupted as e:
            result['status'] = 'interrupted'
            result['warnings'].append(f'被 {e.signal_name} 中断：已导出 '
                                      f'{len(result["images"])} 帧，可 --resume 续跑')
            # 中断也要落盘「部分结果」：用户按了 Ctrl-C，但他已经拿到的 40 帧
            # 不该因为报告没写而变成孤儿文件。落一份 status=interrupted 的报告，
            # 文件里**不出现 PASS**，并留 state 让 --resume 知道进行到哪。
            result['elapsed_s'] = round(time.time() - t0, 1)
            result['manifest'] = manifest(result['images'] + result['sheet_files'])
            write_json(outdir / 'extract.json', dict(result))
            if state:
                state.finish('interrupted')
            raise
        finally:
            # 无论成功/中断/失败，都要清理「上次留下但这次没产出」的自造文件，
            # 否则用户看到的目录内容与本轮参数不符（最坏那种假绿，见模块 docstring）
            removed = prune_stale(outdir, keep)
            if removed:
                result['pruned'] = removed
                if any(r.startswith('!') for r in removed):
                    result['warnings'].append(
                        f'有 {sum(1 for r in removed if r.startswith("!"))} 个旧产物删不掉'
                        '（可能被其它程序占用），它们**不属于本轮结果**')

        # ---- 阶段 5：报告
        result['elapsed_s'] = round(time.time() - t0, 1)
        result['manifest'] = manifest(result['images'] + result['sheet_files'])
        write_json(outdir / 'extract.json', dict(result))
        (outdir / 'summary.md').write_text(summary_markdown(dict(result), result['sheet_files'],
                                                            result['images']), encoding='utf-8')
        if state:
            state.finish('ok')
            state.clear()
    else:
        result['elapsed_s'] = round(time.time() - t0, 1)

    if progress:
        progress('done', 1.0, '完成')

    # ---- 记入任务队列（让 `--history` 能看到命令行跑过的任务）
    #
    # `record=False` 供 GUI 使用：GUI 自己管队列（要管进度、组、取消），
    # 这里再记一条会让历史里出现**两条重复记录**（实测隐患）。
    if not record:
        return result
    #
    # **为什么 CLI 也要记**：早期只有 GUI 写队列，于是 `kf-extract --history`
    # 对命令行用户永远显示"还没有任务记录" —— 等于这个命令不存在。
    # 实测踩到：抽了两次，队列里 0 条。
    #
    # 默认落在 **cwd**（与 GUI 同一个文件），用户可以用 KF_QUEUE 指定别处。
    # 失败不致命：记不了账不该让已经成功的抽取变成失败。
    try:
        from .queue import JobQueue

        qpath = pathlib.Path(os.environ.get('KF_QUEUE')
                             or pathlib.Path.cwd() / '.kf-queue.db')
        q = JobQueue(qpath)
        # 不重复记账：同 video+params+out 且已有终态记录时不重复插
        # （但用户可能就是想再跑一次，所以这里**只在没有记录时插**，不拦第二次）
        qid = q.enqueue(video, {'k': k, 'budget': budget, 'fps': fps,
                                'balance': balance, 'canvas': canvas},
                        out=str(outdir) if outdir else None)
        # **必须补 started**：CLI 是一次性调用，不经过 claim_next，
        # 不补的话历史里耗时永远显示 0.0s（实测）。
        # 用「现在 - 本次实际耗时」反推开始时刻，这样时长才对得上。
        import time as _t

        q.update(qid, status='running',
                 started=_t.time() - float(result.get('elapsed_s') or 0))
        status = 'interrupted' if result.get('status') == 'interrupted' else 'done'
        # 队列的 result 只存**可复核的摘要**，不存整个 images 列表
        # （几百个路径塞进数据库没有意义，产物本身在目录里）
        brief = {kk: result.get(kk) for kk in
                 ('selected_k', 'maxdist', 'max_gap_sec', 'median_gap_sec',
                  'time_coverage', 'canvas_plan', 'elapsed_s', 'status')}
        brief['images'] = len(result.get('images') or [])
        brief['sheets'] = len(result.get('sheet_files') or [])
        if status == 'done':
            q.finish(qid, 'done', result=brief)
        else:
            q.finish(qid, 'interrupted', error='被中断')
    except Exception:                     # noqa: BLE001
        pass                              # 记账失败不毁掉抽取结果

    return result


# ---------------------------------------------------------------- 内部

def _probe_duration(fp, video, ff=None) -> float:
    """时长（秒）。没有 ffprobe 时退回解析 `ffmpeg -i` 的 stderr。

    降级路径**必须自己说出来**（调用方会在报告里标 unverified），
    不允许静默返回一个看起来精确的数。
    """
    import re
    import subprocess

    if fp:
        r = subprocess.run([fp, '-v', 'error', '-show_entries', 'format=duration',
                            '-of', 'default=nw=1:nk=1', str(video)],
                           capture_output=True, text=True)
        try:
            return float(r.stdout.strip())
        except ValueError:
            pass
    if ff:
        # `ffmpeg -i` 把 "Duration: 00:00:14.03, ..." 打在 stderr
        r = subprocess.run([ff, '-i', str(video)], capture_output=True, text=True)
        m = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', r.stderr)
        if m:
            h, mi, se = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mi * 60 + se
    return 0.0


def _probe_fps(fp, video, ff=None) -> float:
    """源帧率。没有 ffprobe 时退回解析 `ffmpeg -i` 的 stderr。"""
    import re
    import subprocess

    if fp:
        r = subprocess.run([fp, '-v', 'error', '-select_streams', 'v:0',
                            '-show_entries', 'stream=r_frame_rate',
                            '-of', 'default=nw=1:nk=1', str(video)],
                           capture_output=True, text=True)
        try:
            num, den = r.stdout.strip().split('/')
            return float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            pass
    if ff:
        r = subprocess.run([ff, '-i', str(video)], capture_output=True, text=True)
        m = re.search(r'(\d+(?:\.\d+)?)\s+fps', r.stderr)
        if m:
            return float(m.group(1))
    return 0.0


def _export_frames(ff, video, keys, n, dur, src_fps, outdir, ext, quality,
                   resume=None) -> list:
    """一次 ffmpeg 解码，按帧号 select 导出原分辨率帧。

    **必须换算到原视频帧号**：分析是在 6fps 上做的，第 i 个分析帧对应
    原视频第 `(i/n)*dur*src_fps` 帧。不换算就会抽到错误的画面 ——
    而且**看起来完全正常**（就是一张别处的帧），是最难发现的那类错。
    """
    import subprocess

    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    if dur > 0 and src_fps > 0:
        # 分析帧 i 的时刻 = i*(dur/n)。源帧号 = 该时刻 * src_fps。
        # 用 round 不用 int（向下取整会让整段抽样系统性偏早半帧）。
        src_keys = {max(0, int(round(i / n * dur * src_fps)))
                    for i in keys}
        src_keys = sorted(src_keys)
    else:
        src_keys = sorted(keys)
    expr = '+'.join(f'eq(n\\,{k})' for k in src_keys)
    pattern = outdir / f'frame-%05d.{ext}'
    cmd = [ff, '-v', 'error', '-i', str(video),
           '-vf', f'select={expr},setpts=N/FRAME_RATE/TB',
           '-fps_mode', 'vfr', '-start_number', '0']
    if ext in ('jpg', 'jpeg'):
        cmd += ['-q:v', str(max(2, min(31, int(round(31 - quality * 0.29)))))]
    cmd.append(str(pattern))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'导帧失败: {r.stderr[:300]}')

    # **只认本轮真正产出的文件**（按 `-start_number 0` 的连续编号）。
    # 早期实现用 `outdir.glob('frame-*.png')` —— 那会把上一轮 k=40 留下的
    # frame-0012..0039 也当成「本轮产物」，于是：
    #   ① result['images'] 报 40 张（实际只导了 12 张，**报告撒谎**）；
    #   ② prune_stale 的 keep 清单包含它们，于是永远不清理（目录里留 40 张）。
    # 实测踩到：先 --k 40 再 --k 12，目录仍是 40 个 png。
    n_expected = len(src_keys)
    made = []
    for i in range(n_expected):
        p = outdir / (f'frame-{i:05d}.{ext}')
        if p.exists():
            made.append(p)
    if len(made) != n_expected:
        raise RuntimeError(
            f'导帧数量不符：预期 {n_expected} 张，实际产出 {len(made)} 张'
            f'（ffmpeg 可能因 seek 精度漏帧）。这是必须报错的，不能少给。')
    return made


def _build_sheets(image_paths, times, spec, outdir, ext, quality, progress=None) -> list:
    from .sheets import make_sheet

    outdir = pathlib.Path(outdir)
    made = []
    total = (len(image_paths) + spec.capacity - 1) // spec.capacity
    for si in range(total):
        chunk = image_paths[si * spec.capacity:(si + 1) * spec.capacity]
        # 时间戳按**全局**帧序继续编号，不是每张图从 #00 重来
        labels = []
        for j in range(len(chunk)):
            gi = si * spec.capacity + j
            t = times[gi] if gi < len(times) and times[gi] is not None else -1
            labels.append(f'#{gi:02d} {_mmss(t)}' if t >= 0 else f'#{gi:02d}')
        out = outdir / f'sheet-{si:03d}.{ext}'
        make_sheet(chunk, times[:], spec, labels=labels, filename=out, quality=quality)
        made.append(out)
        if progress:
            progress('sheets', (si + 1) / total, f'拼图 {si + 1}/{total}')
    return made


def _mmss(seconds: float) -> str:
    from .sheets import _mmss as f

    return f(seconds)
