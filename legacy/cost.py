"""成本口径：关键帧 + net token 的换算。DSH 里 800x450 一张 ≈233 token（见 see-now README，
   按 14px patch / 42px 网格：rows*(cols+1)+2）。这里按同一公式换算多档分辨率。"""
def tok(w,h,cell=42):
    import math
    cols=math.ceil(w/cell); rows=math.ceil(h/cell)
    return rows*(cols+1)+2
for name,(w,h) in [('800x450',(800,450)),('1066x600',(1066,600)),('1280x720',(1280,720)),('1600x900',(1600,900))]:
    print(f'{name:10} ≈{tok(w,h):>4} token/张')
