"""读取 eval/corpus.json —— 评测语料清单的唯一来源。

**为什么单独出来**：`scripts/eval.py` 是冻结文件（度量定义），
加样本不该改度量。改 eval.py 会让 BASELINE-FROZEN 报警。
"""
from __future__ import annotations
import json, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

def load() -> list[dict]:
    p = ROOT / 'eval' / 'corpus.json'
    doc = json.loads(p.read_text(encoding='utf-8'))
    out = []
    for s in doc['samples']:
        f = ROOT / 'eval' / s['file']
        if f.exists():
            out.append({'name': s['name'], 'path': str(f), 'fps': s['fps'],
                        'kind': s.get('kind', '')})
    return out

if __name__ == '__main__':
    for s in load():
        print(f"{s['name']:12} fps={s['fps']:>3} {s['kind']}")
