#!/usr/bin/env python3
"""
基线冻结器 —— 任何 agent 想改评测口径，必须先过这一关。

冻结内容 = scripts/eval.py 的 sha256 + 四个关键常量的值。
任何 PR 提交若改了 scripts/eval.py、core.py 的度量常量，此脚本必须报警。

用法：
  python3 scripts/freeze_baseline.py --check     # 非零退出 = 基线被动过
  python3 scripts/freeze_baseline.py --freeze    # 在你确认"这是有意改动"后重新冻
"""
import hashlib, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FROZEN = ROOT / 'docs' / 'status' / 'BASELINE-FROZEN.json'

# 冻结文件清单：这些文件决定度量口径
WATCHED = [
    'scripts/eval.py',
]
# 关键常量（字面匹配；改了就是改口径）
CONSTANTS = [
    ('ANALYSIS_W, ANALYSIS_H', '32, 18'),
    ('DIFF_DISTINCT', '8.0'),
    ('budgets = (10, 15, 20, 30)', 'budgets = (10, 15, 20, 30)'),
]

#: 度量口径真正居住的区域：`METRIC_BEGIN` 到 `METRIC_END` 之间的文本才参与哈希。
#:
#: **为什么不能整文件哈希**（实测踩到）：产品化时把硬编码的
#: `FFMPEG = '/opt/homebrew/bin/ffmpeg'` 改成跨平台自动定位
#: （纯环境适配，一行数学都没动），闸门照样报警 —— 于是每次无害改动都得
#: 「重新冻结」，而这个闸门一旦变成「习惯性盖章」，它就再也没有拦住过任何东西。
#:
#: 真正会作废历史数字的是：分析分辨率、距离定义、"真正不同"阈值、预算序列、
#: 以及计算这些量的函数体。所以只冻结**那一段**，并在常量上继续做字面校验。
METRIC_BEGIN = '# ---- METRIC-CORE-BEGIN（此段冻结：改它 = 改口径）----'
METRIC_END = '# ---- METRIC-CORE-END ----'


def metric_core(src: str) -> str:
    """取出「度量口径」那一段。缺标记 = 配置错误，直接报（不许静默退化成空串）。"""
    i = src.find(METRIC_BEGIN)
    j = src.find(METRIC_END)
    if i < 0 or j < 0 or j <= i:
        raise SystemExit(
            f'❌ scripts/eval.py 缺 {METRIC_BEGIN!r} / {METRIC_END!r} 标记。\n'
            '   这两个标记定义「哪一段属于度量口径」。缺了就没法验证口径没变 ——\n'
            '   比让闸门静默通过要好。')
    return src[i + len(METRIC_BEGIN):j]


def snapshot():
    doc = {'files': {}, 'constants': {}}
    for rel in WATCHED:
        p = ROOT / rel
        doc['files'][rel] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    src = (ROOT / 'scripts' / 'eval.py').read_text(encoding='utf-8')
    # 口径哈希：只算度量段（环境适配改动不再触发）
    doc['metric_core_sha'] = hashlib.sha256(
        metric_core(src).encode('utf-8')).hexdigest()[:16]
    for name, expect in CONSTANTS:
        doc['constants'][name] = {'expected': expect, 'present': expect in src}
    return doc


def main():
    if '--freeze' in sys.argv:
        FROZEN.parent.mkdir(parents=True, exist_ok=True)
        FROZEN.write_text(json.dumps(snapshot(), indent=2), encoding='utf-8')
        print(f'已冻结 → {FROZEN}')
        return 0
    if not FROZEN.exists():
        print('未冻结。先跑 --freeze')
        return 2
    old = json.loads(FROZEN.read_text(encoding='utf-8'))
    new = snapshot()
    bad = []
    # ① 度量段哈希（真正决定数字的那部分）
    if old.get('metric_core_sha'):
        if new['metric_core_sha'] != old['metric_core_sha']:
            bad.append(f'度量口径段被改: {old["metric_core_sha"]} → {new["metric_core_sha"]}')
    else:
        bad.append('冻结记录缺 metric_core_sha：请重新 --freeze')
    # ② 关键常量字面存在性
    for name, rec in old['constants'].items():
        if not new['constants'][name]['present']:
            bad.append(f'常量消失: {name} = {rec["expected"]}')
    # ③ 整文件哈希只**报告**不拦截（它是环境适配的合法改动对象）
    env_changed = []
    for rel, h in old['files'].items():
        now = new['files'].get(rel)
        if now != h:
            env_changed.append(f'{rel}: {h} → {now}')

    if bad:
        print('❌ 基线被动过：')
        for b in bad:
            print('  ', b)
        print('\n如果这是有意的口径变更：')
        print('  1) 在 docs/state/DECISIONS.md 写一条 D-xxx 说明为什么改')
        print('  2) 重跑四段素材，把新数字写进 docs/status/M0-baseline.md')
        print('  3) python3 scripts/freeze_baseline.py --freeze')
        return 1
    if env_changed:
        print('ℹ️  文件有改动，但度量口径段未变（视为环境适配，不算破基线）：')
        for e in env_changed:
            print('   ', e)
    print('✅ 基线未动（度量口径一致）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
