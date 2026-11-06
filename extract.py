import subprocess, sys, numpy as np
def frames(path, W=32, H=18, fps=None):
    vf = f"scale={W}:{H},format=gray"
    if fps: vf = f"fps={fps}," + vf
    p = subprocess.run(['/opt/homebrew/bin/ffmpeg','-v','error','-i',path,'-vf',vf,'-f','rawvideo','-pix_fmt','gray','-'],
                       capture_output=True)
    buf = p.stdout; n = len(buf)//(W*H)
    return np.frombuffer(buf[:n*W*H], dtype=np.uint8).reshape(n, W*H).astype(np.float32)
if __name__ == '__main__':
    X = frames(sys.argv[1])
    print(X.shape)
