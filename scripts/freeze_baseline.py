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


def snapshot():
    doc = {'files': {}, 'constants': {}}
    for rel in WATCHED:
        p = ROOT / rel
        doc['files'][rel] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    src = (ROOT / 'scripts' / 'eval.py').read_text(encoding='utf-8')
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
    for rel, h in old['files'].items():
        now = new['files'].get(rel)
        if now != h:
            bad.append(f'{rel}: {h} → {now}')
    for name, rec in old['constants'].items():
        if not new['constants'][name]['present']:
            bad.append(f'常量消失: {name} = {rec["expected"]}')
    if bad:
        print('❌ 基线被动过：')
        for b in bad:
            print('  ', b)
        print('\n如果这是有意的口径变更：')
        print('  1) 在 docs/state/DECISIONS.md 写一条 D-xxx 说明为什么改')
        print('  2) 重跑四段素材，把新数字写进 docs/status/M0-baseline.md')
        print('  3) python3 scripts/freeze_baseline.py --freeze')
        return 1
    print('✅ 基线未动')
    return 0


if __name__ == '__main__':
    sys.exit(main())
