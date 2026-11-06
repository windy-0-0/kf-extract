import sys; sys.path.insert(0,'.')
import numpy as np, cv2, subprocess
FF='/opt/homebrew/bin/ffmpeg'
def flows(path,W=160,H=90,fps=24):
    vf=f'fps={fps},scale={W}:{H},format=gray'
    p=subprocess.run([FF,'-v','error','-i',path,'-vf',vf,'-f','rawvideo','-pix_fmt','gray','-'],capture_output=True)
    b=p.stdout; n=len(b)//(W*H)
    return np.frombuffer(b[:n*W*H],dtype=np.uint8).reshape(n,H,W).astype(np.float32), W,H

def run(path,label,fps=24):
    F,W,H=flows(path,fps=fps)
    xs=np.arange(W)[None,:].repeat(H,0).astype(np.float32)
    ys=np.arange(H)[:,None].repeat(W,1).astype(np.float32)
    mask=np.zeros((H,W),bool); mask[6:-6,6:-6]=True
    rows=[]
    for i in range(len(F)-1):
        a=F[i]; c=F[i+1]
        fl=cv2.calcOpticalFlowFarneback(a,c,None,0.5,3,15,3,5,1.2,0)
        warp=cv2.remap(c, xs+fl[...,0], ys+fl[...,1], cv2.INTER_LINEAR)   # ← 已用锚点校正的方向
        raw=np.abs(c-a)[mask].mean()
        res=np.abs(warp-a)[mask].mean()
        mag=np.hypot(fl[...,0],fl[...,1])[mask].mean()
        rows.append((raw,res,mag))
    r=np.array(rows); raw,res,mag=r.T
    print(f'\n=== {label} ({len(r)} 对, {fps}fps) ===')
    print(f'原始差分        mean={raw.mean():.2f}  p50={np.median(raw):.2f} p90={np.percentile(raw,90):.2f}')
    print(f'光流补偿后残差  mean={res.mean():.2f}  p50={np.median(res):.2f} p90={np.percentile(res,90):.2f}')
    print(f'  降幅={100*(1-res.mean()/raw.mean()):.1f}%   平均光流幅值={mag.mean():.2f}px@160宽')
    return raw,res,mag

CLIP='/Users/mac/Downloads/Kars on X_ _Preview clip of the _Mahoutsukai no Yору_ anime film featuring Alice shown at KYOMAF 2026. https___t.co_QOvWLhyZ2u https___t.co_kl9EsfBrnl_ _ X.mp4'
CLIP='/Users/mac/Downloads/Kars on X_ _Preview clip of the _Mahoutsukai no Yoru_ anime film featuring Alice shown at KYOMAF 2026. https___t.co_QOvWLhyZ2u https___t.co_kl9EsfBrnl_ _ X.mp4'
MV='/Users/mac/Downloads/【乐正绫·言和原创】🌧️容我人间流浪，「暴雨梨花」敬上🌧️【气象万千】.mp4'
run(CLIP,'番剧预告',24)

raw,res,mag = run(MV,'动画MV 前 60s',24)
# 决定性判据：把"运动量"与"残差"分开，看残差高峰是否对应「作画替换」而非「快速运动」
q=np.percentile(res,90)
big=res>q
print(f'\n残差>p90 的帧中，光流幅值均值={mag[big].mean():.2f} vs 全局{mag.mean():.2f}')
print(f'  → 若残差高峰时运动也大，说明仍是"运动没补偿干净"，不是作画变化')
print(f'  相关系数 corr(残差, 运动) = {np.corrcoef(res, mag)[0,1]:.3f}')
