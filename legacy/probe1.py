import subprocess, numpy as np
def frames(path,W=48,H=27,fps=24):
    vf=f"fps={fps},scale={W}:{H},format=gray"
    p=subprocess.run(['/opt/homebrew/bin/ffmpeg','-v','error','-i',path,'-vf',vf,'-f','rawvideo','-pix_fmt','gray','-'],capture_output=True)
    b=p.stdout; n=len(b)//(W*H)
    return np.frombuffer(b[:n*W*H],dtype=np.uint8).reshape(n,W*H).astype(np.float32), W,H
X,W,H=frames("/Users/mac/Downloads/Kars on X_ _Preview clip of the _Mahoutsukai no Yoru_ anime film featuring Alice shown at KYOMAF 2026. https___t.co_QOvWLhyZ2u https___t.co_kl9EsfBrnl_ _ X.mp4")
n=len(X)
# 1) 逐帧 L1 差分
d1=np.abs(X[1:]-X[:-1]).mean(axis=1)
# 2) 全局运动（整帧平移）估计：用 1D 投影位移
def proj_shift(a,b,axis, rng=6):
    pa=a.reshape(H,W).sum(0) if axis=='x' else a.reshape(H,W).sum(1)
    pb=b.reshape(H,W).sum(0) if axis=='x' else b.reshape(H,W).sum(1)
    best=(1e18,0)
    for s in range(-rng,rng+1):
        if s>0: diff=np.abs(pa[s:]-pb[:-s]).mean()
        elif s<0: diff=np.abs(pa[:s]-pb[-s:]).mean()
        else: diff=np.abs(pa-pb).mean()
        if diff<best[0]: best=(diff,s)
    return best[1]
shifts=[proj_shift(X[i],X[i+1],'x') for i in range(n-1)]
shifts=np.array(shifts)
print(f'帧数(n={n}) 差分均值={d1.mean():.2f} 中位={np.median(d1):.2f}')
print(f'估计的全局水平位移: 非零帧占比={(np.abs(shifts)>0).mean()*100:.1f}%  位移绝对值均值={np.abs(shifts).mean():.2f}px(48宽)')
# 3) 关键假说：把"平移"补偿掉之后，剩余残差是不是只剩"作画变化"
res=[]
for i in range(n-1):
    a=X[i].reshape(H,W); b=X[i+1].reshape(H,W)
    s=int(shifts[i])
    if s>0: ra=a[:, s:]; rb=b[:, :W-s]
    elif s<0: ra=a[:, :W+s]; rb=b[:, -s:]
    else: ra=a; rb=b
    if ra.shape[1]<8: res.append(np.nan); continue
    res.append(np.abs(ra-rb).mean())
res=np.array(res)
print(f'运动补偿后残差: 均值={np.nanmean(res):.2f}  (原始 {d1.mean():.2f}, 降 {100*(1-np.nanmean(res)/d1.mean()):.1f}%)')
q=np.nanpercentile(res[~np.isnan(res)],[50,75,90,95,99])
print('残差分位 p50/p75/p90/p95/p99 =', [round(x,2) for x in q])
