#!/usr/bin/env python3
"""一条命令打印项目全貌。--full 额外列出会话/文档目录。"""
import re, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / 'docs' / 'state'

def section(path, title_re, max_lines=None):
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        print(f'(缺 {path})')
        return
    lines = text.splitlines()
    out, keep = [], False
    for line in lines:
        if re.match(title_re, line):
            keep = True
            out.append(line)
            continue
        if keep:
            if line.startswith('## ') and not re.match(title_re, line):
                keep = False
                continue
            out.append(line)
    if max_lines:
        out = out[:max_lines]
    print('\n'.join(out))

def main():
    print('=' * 64)
    print('KF-EXTRACT · 项目全貌')
    print('=' * 64)
    # 1) 状态表 + 阻塞
    section(STATE / 'STATE.md', r'## (模块状态|阻塞 / 风险)')
    # 2) 最近 5 条决策标题
    print('\n## 最近 5 条决策')
    try:
        text = (STATE / 'DECISIONS.md').read_text(encoding='utf-8')
        heads = [l for l in text.splitlines() if l.startswith('## D-')]
        for h in heads[-5:]:
            print(h)
    except OSError:
        print('(缺 DECISIONS.md)')
    # 3) 评测集
    print('\n## 评测集')
    ev = ROOT / 'eval'
    if ev.is_dir():
        for p in sorted(ev.glob('*.mp4')):
            size = p.stat().st_size / 1e6
            print(f'  {p.name:24} {size:8.1f} MB')
    else:
        print('  (无 eval/)')
    # 4) --full
    if '--full' in sys.argv:
        print('\n## 最近会话')
        try:
            text = (STATE / 'SESSIONS.md').read_text(encoding='utf-8')
            rows = [l for l in text.splitlines() if l.startswith('| 2026')]
            for r in rows[-5:]:
                print(' ', r[:160])
        except OSError:
            print('(缺 SESSIONS.md)')
        print('\n## 文档目录')
        for d in ('docs/state', 'docs/status', 'docs/research'):
            print(f'  {d}/')
            for f in sorted((ROOT / d).glob('*.md')) if (ROOT / d).is_dir() else []:
                print(f'    {f.name}')
    print('\n' + '=' * 64)

if __name__ == '__main__':
    main()
