import sys; sys.path.insert(0,'.')
from core import load
import numpy as np, cv2
CLIP='/Users/mac/Downloads/Kars on X_ _Preview clip of the _Mahoutsukai no Yoru_ anime film featuring Alice shown at KYOMAF 2026. https___t.co_QOvWLhyZ2u https___t.co_kl9EsfBrnl_ _ X.mp4'
MV='/Users/mac/Downloads/【乐正绫·言和原创】🌧️容我人间流浪，「暴雨梨花」敬上🌧️【气象万千】.mp4'

def dense_flow_residual(path, W=160, H=90, fps=24):
    import subprocess
    vf=f'fps={fps},scale={W}:{H},format=gray'
    p=subprocess.run(['/opt/homebrew/bin/ffmpeg','-v','error','-i',path,'-vf',vf,'-f','rawvideo','-pix_fmt','gray','-'],capture_output=True)
    b=p.stdout; n=len(b)//(W*H)
    F=np.frombuffer(b[:n*W*H],dtype=np.uint8).reshape(n,H,W)
    rows=[]
    for i in range(n-1):
        a=F[i].astype(np.float32); c=F[i+1].astype(np.float32)
        flow=cv2.calcOpticalFlowFarneback(a,c,None,0.5,3,15,3,5,1.2,0)
        warped=cv2.remap(c, (np.arange(W)[None,:]-flow[...,0]).astype(np.float32),
                            (np.arange(H)[:,None]-flow[...,1]).astype(np.float32), cv2.INTER_LINEAR)
        raw=np.abs(c-a).mean()
        mask=np.ones_like(flow[...,0],bool); mask[8:-8,8:-8]=True; mask[:8,:]=mask[-8:,:]=False; mask[:,:8]=mask[:,-8:]=False
        res=np.abs(warped-a)[mask].mean()
        rows.append((i, raw, res, float(np.abs(flow[...,0]).mean()), float(np.abs(flow[...,1]).mean())))
    return np.array(rows)

for name, path in [('番剧预告', CLIP), ('动画MV-前60s', MV)]:
    r=dense_flow_residual(path, fps=24)
    raw,res=r[:,1],r[:,2]
    print(f'\n=== {name} ({len(r)} 对帧, 24fps, 160x90) ===')
    print(f'原始差分   mean={raw.mean():.2f} p50={np.median(raw):.2f} p90={np.percentile(raw,90):.2f}')
    print(f'光流补偿后 mean={res.mean():.2f} p50={np.median(res):.2f} p90={np.percentile(res,90):.2f}')
    print(f'  补偿降幅：{100*(1-res.mean()/raw.mean()):.1f}%')
    # 关键：补偿后残差的高峰是不是只出现在"作画/镜头变化"
    thr=np.percentile(res,90)
    big=np.where(res>thr)[0]
    from collections import Counter
    gaps=np.diff(big)
    print(f'  补偿后残差>p90 的帧数={len(big)} ({len(big)/len(res)*100:.1f}%)，间隔众数={Counter(gaps.tolist()).most_common(3)}')
