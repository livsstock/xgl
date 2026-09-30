# -*- coding: utf-8 -*-
"""持有期敏感性 + 合并阈值稳健性 —— 交叉验证请求里我自己先算的项"""
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

print("="*78); print("一、持有期敏感性（T5 vs 基线）"); print("="*78)
print(f"{'持有期':<10}{'事件':>6}{'T5下跌率':>10}{'T5均收':>10}{'基线下跌率':>12}{'基线均收':>10}{'超额':>9}")
print("-"*78)
for H in (1,3,5,10,20):
    pr=[(i,feats[i],(feats[i+H]['close']/feats[i]['close']-1)*100)
        for i in range(len(feats)) if i+H<len(feats)]
    idxs=sorted(i for i,f,r in pr if T5(f))
    if not idxs: continue
    cl=[];cur=[idxs[0]]
    for x in idxs[1:]:
        if x-cur[-1]<=3: cur.append(x)
        else: cl.append(cur);cur=[x]
    cl.append(cur)
    rs=[(feats[c[0]+H]['close']/feats[c[0]]['close']-1)*100 for c in cl if c[0]+H<len(feats)]
    ar=[p[2] for p in pr]
    d=sum(1 for r in rs if r<0)/len(rs)*100
    bd=sum(1 for r in ar if r<0)/len(ar)*100
    print(f"T+{H:<8}{len(rs):>6}{d:>9.1f}%{sum(rs)/len(rs):>9.2f}%{bd:>11.1f}%"
          f"{sum(ar)/len(ar):>9.2f}%{d-bd:>+8.1f}pt")

print()
print("="*78); print("二、独立事件合并阈值稳健性（H=5）"); print("="*78)
H=5
pr=[(i,feats[i],(feats[i+H]['close']/feats[i]['close']-1)*100)
    for i in range(len(feats)) if i+H<len(feats)]
idxs=sorted(i for i,f,r in pr if T5(f))
print(f"{'合并阈值':<12}{'事件数':>8}{'下跌率':>10}{'均收':>10}{'vs基线':>12}")
for gap in (1,2,3,5,10):
    cl=[];cur=[idxs[0]]
    for x in idxs[1:]:
        if x-cur[-1]<=gap: cur.append(x)
        else: cl.append(cur);cur=[x]
    cl.append(cur)
    rs=[(feats[c[0]+H]['close']/feats[c[0]]['close']-1)*100 for c in cl if c[0]+H<len(feats)]
    d=sum(1 for r in rs if r<0)/len(rs)*100
    ar=[p[2] for p in pr]
    bd=sum(1 for r in ar if r<0)/len(ar)*100
    print(f"gap<={gap:<8}{len(rs):>8}{d:>9.1f}%{sum(rs)/len(rs):>9.2f}%{d-bd:>+11.1f}pt")

print()
print("="*78); print("三、去掉 2020 年"); print("="*78)
for lo_y in ("2020","2021"):
    cl2=[]
    sub=[p for p in pr if p[1]['date'][:4]>=lo_y]
    idxs2=sorted(i for i,f,r in sub if T5(f))
    if not idxs2: continue
    cl=[];cur=[idxs2[0]]
    for x in idxs2[1:]:
        if x-cur[-1]<=3: cur.append(x)
        else: cl.append(cur);cur=[x]
    cl.append(cur)
    rs=[(feats[c[0]+H]['close']/feats[c[0]]['close']-1)*100 for c in cl if c[0]+H<len(feats)]
    ar=[p[2] for p in sub]
    print(f"  {lo_y}年起: {len(rs)} 事件  下跌率 {sum(1 for r in rs if r<0)/len(rs)*100:.1f}%  "
          f"均收 {sum(rs)/len(rs):+.2f}%   基线下跌率 {sum(1 for r in ar if r<0)/len(ar)*100:.1f}%")

print()
print("="*78); print("四、pos 65/70/75 精确复核（H=5）"); print("="*78)
for pv in (65,70,75):
    def t(f,pv=pv): return f['pos20']>=pv and (f['dev_ma20'] if f['dev_ma20'] is not None else -99)<1
    idxs=sorted(i for i,f,r in pr if t(f))
    cl=[];cur=[idxs[0]]
    for x in idxs[1:]:
        if x-cur[-1]<=3: cur.append(x)
        else: cl.append(cur);cur=[x]
    cl.append(cur)
    rs=[(feats[c[0]+H]['close']/feats[c[0]]['close']-1)*100 for c in cl if c[0]+H<len(feats)]
    ar=[p[2] for p in pr]
    bd=sum(1 for r in ar if r<0)/len(ar)*100
    d=sum(1 for r in rs if r<0)/len(rs)*100
    print(f"  pos>={pv}: {len(rs):3}事件  下跌率 {d:.1f}%  均收 {sum(rs)/len(rs):+.2f}%  超额 {d-bd:+.1f}pt")
