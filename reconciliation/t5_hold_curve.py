# -*- coding: utf-8 -*-
"""逐日持有期曲线 —— 判断是平滑过渡还是T+5尖峰"""
import importlib.util
spec=importlib.util.spec_from_file_location('h','reconcile/backtest_hardgate.py')
h=importlib.util.module_from_spec(spec)
try: spec.loader.exec_module(h)
except SystemExit: pass
bars=h.load_kline("sh000001",1500)
feats=h.compute_features(bars)
for i,f in enumerate(feats):
    w=bars[max(0,i-19):i+1]
    lo=min(x['low'] for x in w); hi=max(x['high'] for x in w)
    f['pos20']=(f['close']-lo)/(hi-lo)*100 if hi>lo else 50.0
T5=lambda f: f['pos20']>=70 and (f['dev_ma20'] if f['dev_ma20'] is not None else -99)<1

# 固定事件集（用H=5的合并）
H0=5
pr0=[(i,feats[i]) for i in range(len(feats))]
idxs=sorted(i for i,f in pr0 if T5(f))
cl=[];cur=[idxs[0]]
for x in idxs[1:]:
    if x-cur[-1]<=3: cur.append(x)
    else: cl.append(cur);cur=[x]
cl.append(cur)
starts=[c[0] for c in cl]
print(f"固定 {len(starts)} 个事件起点，逐日看累计收益\n")
print(f"{'T+n':<6}{'事件数':>7}{'T5下跌率':>10}{'T5均收':>10}{'基线下跌率':>12}{'基线均收':>10}{'超额':>9}")
print("-"*72)
for H in range(1,13):
    rs=[(feats[s+H]['close']/feats[s]['close']-1)*100 for s in starts if s+H<len(feats)]
    ar=[(feats[i+H]['close']/feats[i]['close']-1)*100 for i in range(len(feats)) if i+H<len(feats)]
    d=sum(1 for r in rs if r<0)/len(rs)*100
    bd=sum(1 for r in ar if r<0)/len(ar)*100
    bar="█"*max(0,int((d-bd)))
    print(f"T+{H:<4}{len(rs):>7}{d:>9.1f}%{sum(rs)/len(rs):>9.2f}%{bd:>11.1f}%"
          f"{sum(ar)/len(ar):>9.2f}%{d-bd:>+8.1f}pt {bar}")
