"""量化「一张拼图里到底有多少真正不同的信息」——
   对每个格子算它与同图其它格子的最小 L1 距离，近=冗余。"""
import subprocess, numpy as np, json
FF='/opt/homebrew/bin/ffmpeg'
def load(path, W=32, H=18, fps=None):
    vf=(f'fps={fps},' if fps else '')+f'scale={W}:{H},format=gray'
    p=subprocess.run([FF,'-v','error','-i',path,'-vf',vf,'-f','rawvideo','-pix_fmt','gray','-'],capture_output=True)
    b=p.stdout; n=len(b)//(W*H)
    return np.frombuffer(b[:n*W*H],dtype=np.uint8).reshape(n,W*H).astype(np.float32)
