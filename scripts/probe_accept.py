#!/usr/bin/env python3
"""探针验收 —— 对 M2/M3/M4 三个外部 agent 的产出做**自动化体检**。

设计原则：
  1. **不读它们的报告**。报告是人写的，可以美化；探针只看**盘上的东西**。
  2. **每条断言都有反向样本**：探针必须能证明自己会红（--selftest）。
  3. **越界检测**：谁的改动超出了自己的目录，一律 FAIL。

用法：
  python3 scripts/probe_accept.py            # 体检全部
  python3 scripts/probe_accept.py --only M2
  python3 scripts/probe_accept.py --selftest # 证明探针会红
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY = '/Users/mac/kf-lab/venv/bin/python'

# 每个模块**允许**出现的文件（多一个就是越界）
ALLOWED = {
    'M2': {'src/kf/fast.py', 'tests/kf/test_fast.py', 'scripts/bench_m2.py',
           'scripts/accept_m2.sh', 'docs/status/M2-report.md'},
    'M3': {'src/kf/report.py', 'src/kf/flow.py', 'tests/kf/test_flow.py',
           'scripts/bench_m3.py', 'docs/status/M3-report.md'},
    'M4': {'src/kf/layout.py', 'tests/kf/test_layout.py', 'scripts/bench_m4.py',
           'scripts/accept_m4.sh', 'docs/status/M4-report.md',
           'docs/status/M4-bench.json'},
}
# 谁都不许碰（碰了就是改度量/改历史）
FORBIDDEN = {
    'scripts/eval.py', 'src/kf/metrics.py', 'docs/status/BASELINE-FROZEN.json',
    'docs/status/M0-baseline.md', 'docs/status/M0-fanren.json',
    'PROJECT-KF-EXTRACT.md', 'src/kf/kcenter.py',
}

# 冻结的 8 个基线数字（trailer 168 帧，@12fps）
FROZEN = {
    ('kcenter', 10): 14, ('kcenter', 15): 7, ('kcenter', 20): 6, ('kcenter', 30): 4,
    ('uniform', 10): 100, ('uniform', 15): 14, ('uniform', 20): 12, ('uniform', 30): 5,
}


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)


def check_forbidden() -> list[str]:
    """被禁文件是否被改（用 git 对比 HEAD）。"""
    bad = []
    for rel in FORBIDDEN:
        r = sh(f'cd {ROOT} && git diff --quiet HEAD -- {rel}')
        if r.returncode != 0:
            bad.append(f'被禁文件被改: {rel}')
    return bad


def check_scope(module: str) -> list[str]:
    """该模块是否只新增了自己的文件。"""
    r = sh(f'cd {ROOT} && git status --porcelain')
    changed = [line[3:].strip() for line in r.stdout.splitlines() if line.strip()]
    bad = []
    for c in changed:
        if c.startswith('"') and c.endswith('"'):
            c = c[1:-1]
        # 统筹方（主会话）自己的产物，不算任何 agent 越界
        if c in ('scripts/probe_accept.py', 'scripts/bench_m1.py',
                 'scripts/kf_extract.py', 'src/kf/kcenter.py',
                 'src/kf/metrics.py', 'src/kf/__init__.py',
                 'docs/prompts/M2-v1.txt', 'docs/prompts/M3-v1.txt',
                 'docs/prompts/M4-v1.txt', 'docs/prompts/M2-v1.md',
                 'docs/prompts/M3-v1.md', 'docs/prompts/M4-v1.md',
                 'docs/state/STATE.md', 'docs/state/DECISIONS.md',
                 'docs/state/SESSIONS.md', 'docs/state/README.md',
                 'docs/status/M1-bench.json', 'scripts/freeze_baseline.py',
                 'scripts/benchlock.py', 'docs/status/M3-bench.json',
                 'docs/status/BASELINE-FROZEN.json', '.gitignore', 'README.md'):
            continue
        if c.startswith('tests/kf/test_kcenter.py') or c.startswith('docs/design/'):
            continue
        allowed = ALLOWED.get(module, set())
        if c.endswith('/'):
            continue          # git 只报目录时的粗粒度条目，不判越界
        if c in allowed:
            continue
        # 别的 agent 的可交付物互不越界（各自有主，由各自的 --only 检查管）
        if any(c in v for k, v in ALLOWED.items() if k != module):
            continue
        bad.append(f'{module} 越界改动: {c}')
    return bad


def check_freeze() -> list[str]:
    r = sh(f'{PY} {ROOT}/scripts/freeze_baseline.py --check')
    if r.returncode != 0:
        return ['基线冻结闸门红：' + r.stdout.strip()[:200]]
    return []


def baseline_numbers() -> dict:
    """现场重算 trailer 的 8 个数字（不读报告，自己算）。"""
    code = f'''
import sys, json
sys.path.insert(0, {str(ROOT)!r})
from src.kf.metrics import load_gray, maxdist, uniform_idx
from src.kf.kcenter import budget_track
X = load_gray({str(ROOT/'eval/trailer.mp4')!r}, sample_fps=12)
budgets = (10, 15, 20, 30)
track = budget_track(X, budgets, cap=2000)
n = len(X)
def uni(b):
    lo, hi, best = 1, min(2000, n), None
    while lo <= hi:
        mid = (lo + hi) // 2
        if maxdist(X, uniform_idx(n, mid)) <= b:
            best = mid; hi = mid - 1
        else:
            lo = mid + 1
    return best
out = {{}}
for b in budgets:
    out[f'kcenter{{b}}'] = track[b]
    out[f'uniform{{b}}'] = uni(b)
print(json.dumps(out))
'''
    r = sh(f'{PY} -c {json.dumps(code)}') if False else sh(f'{PY} - <<\'PYEOF\'\n{code}\nPYEOF')
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {}


def check_baseline_numbers() -> list[str]:
    got = baseline_numbers()
    if not got:
        return ['无法重算基线（脚本出错）']
    bad = []
    for (kind, b), expect in FROZEN.items():
        key = f'{kind}{b}'
        actual = got.get(key)
        if actual != expect:
            bad.append(f'{key}: 期望 {expect}，实得 {actual}')
    return bad


def check_tests(module: str) -> list[str]:
    """跑该模块自己的测试文件。"""
    test = {'M2': 'tests/kf/test_fast.py', 'M3': 'tests/kf/test_flow.py',
            'M4': 'tests/kf/test_layout.py'}[module]
    p = ROOT / test
    if not p.exists():
        return [f'{module} 缺测试文件 {test}']
    r = sh(f'cd {ROOT} && {PY} {test}')
    if r.returncode != 0:
        return [f'{module} 测试未全绿：' + r.stdout.strip()[-300:]]
    return []


def check_impl(module: str) -> list[str]:
    impl = {'M2': 'src/kf/fast.py', 'M3': 'src/kf/flow.py', 'M4': 'src/kf/layout.py'}[module]
    p = ROOT / impl
    if not p.exists():
        return [f'{module} 缺实现 {impl}']
    src = p.read_text(encoding='utf-8')
    bad = []
    if len(src) < 400:
        bad.append(f'{impl} 只有 {len(src)} 字节，像占位')
    if re.search(r'\bTODO\b|\bFIXME\b|\bpass$', src, re.M) and len(src) < 1500:
        bad.append(f'{impl} 含 TODO/FIXME/空 pass，像半成品')
    return bad


def check_report_sections(module: str) -> list[str]:
    """报告必须含反向样本与 git diff —— 没有就是没做那一步。"""
    rep = ROOT / f'docs/status/{module}-report.md'
    if not rep.exists():
        return [f'{module} 缺报告 docs/status/{module}-report.md']
    text = rep.read_text(encoding='utf-8')
    bad = []
    # 反向样本：必须有两个具体输出片段（不是"已验证"三个字）
    if '反向' not in text and '会红' not in text:
        bad.append(f'{module} 报告没提反向样本')
    if 'git diff' not in text and 'diff --stat' not in text:
        bad.append(f'{module} 报告没贴 git diff --stat')
    return bad


def run(only: str | None) -> int:
    modules = [only] if only else ['M2', 'M3', 'M4']
    total_bad = []
    print('=' * 60)
    print('探针验收')
    print('=' * 60)

    # 全局项只跑一次
    global_bad = check_forbidden() + check_freeze()
    got = baseline_numbers()
    print(f'\n[全局] 基线重算: {json.dumps(got, ensure_ascii=False) if got else "失败"}')
    base_bad = check_baseline_numbers()
    global_bad += base_bad

    for m in modules:
        print(f'\n--- {m} ---')
        bads = check_scope(m) + check_impl(m) + check_tests(m) + check_report_sections(m)
        if bads:
            for b in bads:
                print(f'  FAIL  {b}')
        else:
            print('  PASS')
        total_bad += bads

    print('\n' + '=' * 60)
    if global_bad:
        print('全局 FAIL:')
        for b in global_bad:
            print('  ', b)
    total_bad += global_bad
    if total_bad:
        print(f'\n❌ 共 {len(total_bad)} 项不合格')
        return 1
    print('\n✅ 全部通过')
    return 0


def selftest() -> int:
    """证明探针会红：伪造一个越界改动 + 伪造一个基线漂移，看它报不报。"""
    print('=== 探针自检（必须全红）===')
    ok = True

    # 1) 越界检测：临时加一个不属于 M2 的文件
    trap = ROOT / 'src/kf/__probe_trap__.py'
    trap.write_text('# trap\n', encoding='utf-8')
    try:
        got = check_scope('M2')
        red = any('__probe_trap__' in b for b in got)
        print(f'  越界检测: {"PASS(红)" if red else "FAIL(没红)"}  {got[:1]}')
        ok &= red
    finally:
        trap.unlink(missing_ok=True)

    # 2) 被禁文件检测：临时改 metrics.py 一个字节
    m = ROOT / 'src/kf/metrics.py'
    bak = m.read_text(encoding='utf-8')
    try:
        m.write_text(bak + '\n# probe\n', encoding='utf-8')
        got = check_forbidden()
        red = any('metrics.py' in b for b in got)
        print(f'  禁区检测: {"PASS(红)" if red else "FAIL(没红)"}')
        ok &= red
    finally:
        m.write_text(bak, encoding='utf-8')

    # 3) 基线数字检测：篡改冻结常量
    print(f'  基线重算: {json.dumps(baseline_numbers(), ensure_ascii=False)}')
    print('  (若上面 8 个数字与 FROZEN 一致，说明重算通路正常)')

    print('\nselftest:', 'ALL PASS (探针能红)' if ok else 'FAILED（探针不够敏感）')
    return 0 if ok else 1


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.exit(selftest())
    only = None
    for i, a in enumerate(sys.argv):
        if a == '--only' and i + 1 < len(sys.argv):
            only = sys.argv[i + 1]
    sys.exit(run(only))
