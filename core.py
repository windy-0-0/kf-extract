import subprocess, numpy as np
FF='/opt/homebrew/bin/ffmpeg'
def load(path, W=32, H=18, fps=None, gray=True):
    vf=(f'fps={fps},' if fps else '')+f'scale={W}:{H},format=gray'
    p=subprocess.run([FF,'-v','error','-i',path,'-vf',vf,'-f','rawvideo','-pix_fmt','gray','-'],capture_output=True)
    b=p.stdout; n=len(b)//(W*H)
    return np.frombuffer(b[:n*W*H],dtype=np.uint8).reshape(n,W*H).astype(np.float32)

def interp_error(X, keys):
    """对每个非关键帧，用两侧最近关键帧做**线性插值**，返回重建误差。这就是'该帧有多冗余'的量化口径。"""
    n=len(X); keys=list(keys)
    err=np.zeros(n); err[keys]=0.0
    for i in range(n):
        if i in keys: continue
        lo=max([k for k in keys if k<=i], default=None)
        hi=min([k for k in keys if k>=i], default=None)
        if lo is None or hi is None or lo==hi:
            err[i]=255.0; continue
        a=(i-lo)/(hi-lo)
        pred=X[lo]*(1-a)+X[hi]*a
        err[i]=np.abs(X[i]-pred).mean()
    return err

def pick_keys(X, budget):
    """二分：要让“最大重建误差”不超过 t，最少需要多少个关键帧？"""
    n=len(X)
    keyinfo=np.abs(np.diff(X,axis=0)).mean(axis=1)  # 每帧变化量
    order=np.argsort(-keyinfo)  # 变化最大的优先当关键帧
    def total_err(sel):
        keys=sorted(set([0, n-1]+list(sel)))
        e=interp_error(X, keys)
        return np.percentile(e,95), e.max()
    lo,hi=0,len(order)
    best=None
    while lo<hi:
        mid=(lo+hi)//2
        sel=set(order[:mid].tolist())
        p95,mx=total_err(sel)
        if p95<=budget: hi=mid; best=(mid,p95,mx)
        else: lo=mid+1
    sel=sorted(set([0,n-1]+order[:hi].tolist())) if best else sorted(set([0,n-1]))
    p95,mx=total_err(sel)
    return sel, p95, mx
