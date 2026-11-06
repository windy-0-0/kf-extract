import sys; sys.path.insert(0,'/Users/mac/kf-lab')
from core import load
import numpy as np

def kcenter(X, K, seed=0):
    n=len(X); keys=[seed]
    d=np.abs(X-X[keys[0]]).mean(axis=1)
    for _ in range(K-1):
        i=int(np.argmax(d))
        if d[i]<=0: break
        keys.append(i); d=np.minimum(d, np.abs(X-X[i]).mean(axis=1))
    return sorted(keys), d

def need_for(X, budget, cap=None):
    """给定最大可接受距离，最少需要多少帧（二分）。"""
    n=len(X); cap=cap or n
    lo,hi=1,cap
    best=cap
    while lo<=hi:
        mid=(lo+hi)//2
        _,d=kcenter(X,mid)
        if d.max()<=budget: best=mid; hi=mid-1
        else: lo=mid+1
    return best

def uniform_need_for(X,budget,cap):
    idx=np.linspace(0,len(X)-1,cap).round().astype(int)
    for K in range(1,cap+1):
        j=np.linspace(0,len(X)-1,K).round().astype(int)
        d=np.min(np.abs(X[:,None,:]-X[None,j,:]).mean(axis=2),axis=1)
        if d.max()<=budget: return K
    return cap
