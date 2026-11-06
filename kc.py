import sys; sys.path.insert(0,'/Users/mac/kf-lab')
from core import load
import numpy as np

def kcenter(X, K, seed=0):
    """最远优先贪心：选 K 帧，使任何帧到最近关键帧的 L1 距离的最大值最小。
       这就是「K 张图能覆盖多少内容」的标准量化口径（facility location / k-center）。"""
    n=len(X)
    keys=[seed]
    d=np.abs(X-X[keys[0]]).mean(axis=1)      # 每帧到已选集合的距离
    for _ in range(K-1):
        i=int(np.argmax(d))
        if d[i]<=0: break
        keys.append(i)
        d=np.minimum(d, np.abs(X-X[i]).mean(axis=1))
    return sorted(keys), d

def uniform(X,K):
    n=len(X); keys=sorted(set(np.linspace(0,n-1,K).round().astype(int).tolist()))
    d=np.min(np.abs(X[:,None,:]-X[None,keys,:]).mean(axis=2),axis=1) if len(keys)<40 else np.array([min(np.abs(X[i]-X[k]).mean() for k in keys) for i in range(n)])
    return keys,d

def report(name, X, budgets=(8,16,32,48,64,96)):
    n=len(X)
    print(f'\n=== {name}  (采样 {n} 帧) ===')
    print(f'{"预算K":>6} | {"k-center":>10} {"maxDist":>8} | {"均匀采样":>10} {"maxDist":>8} | 谁更好')
    print('-'*70)
    for K in budgets:
        k1,d1=kcenter(X,min(K,n)); 
        # 均匀：直接取 linspace
        idx=np.linspace(0,n-1,K).round().astype(int)
        d2=np.array([min(abs(X[i]-X[j]).mean() for j in idx) for i in range(n)])
        w = 'k-center' if d1.max()<d2.max() else 'uniform'
        print(f'{K:>6} | {len(k1):>10} {d1.max():>8.1f} | {len(set(idx)):>10} {d2.max():>8.1f} | {w}')
