#!/usr/bin/env python3
"""LIVS-Stock v2.1 每日财经数据采集脚本
GitHub Actions 每交易日 8:00 CST 自动采集
v2.1新增: 个股技术指标(MACD/超跌/趋势/操作级别) + stock_data输出
数据源: 腾讯财经+新浪+同花顺+CBOE"""
import json, re, time, os, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))

# ===== 2026年中国股市休市日历（节假日+周末）=====
# 需每年更新。格式: "YYYY-MM-DD"
CN_HOLIDAYS_2026 = {
    # 元旦
    "2026-01-01", "2026-01-02", "2026-01-03",
    # 春节
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22",
    # 清明节
    "2026-04-04", "2026-04-05", "2026-04-06",
    # 劳动节
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    # 端午节
    "2026-05-31", "2026-06-01", "2026-06-02",
    # 中秋节
    "2026-09-25", "2026-09-26", "2026-09-27",
    # 国庆节
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07",
}

def is_trading_day(d):
    """判断是否为A股交易日（排除周末和中国法定假日）"""
    if d.weekday() >= 5:  # 周六日
        return False
    ds = d.strftime("%Y-%m-%d")
    if ds in CN_HOLIDAYS_2026:
        return False
    return True

WATCHLIST = [
    # === 核心池6只（与WB对齐v2.1）===
    # 超跌轨4只
    {"code":"sh601899","name":"紫金矿业"},{"code":"sh600111","name":"北方稀土"},
    {"code":"sh603799","name":"华友钴业"},{"code":"sh601958","name":"金钼股份"},
    # 长效轨2只
    {"code":"sh512480","name":"半导体ETF"},{"code":"sz000858","name":"五粮液"},
    # === 底仓观察位（不参与评分，仅-15%止损管理）===
    {"code":"sh601398","name":"工商银行"},{"code":"sh601288","name":"农业银行"},
    # === 扩展监控 ===
    {"code":"sh600392","name":"盛和资源"},{"code":"sh600875","name":"东方电气"},
    {"code":"sz000725","name":"京东方A"},{"code":"sh518880","name":"黄金ETF"},
]
INDICES = [{"code":"sh000001","name":"上证指数"},{"code":"sz399001","name":"深证成指"},
           {"code":"sz399006","name":"创业板指"}]
OUT = "output"

def fetch(url, hdr=None, to=10):
    if hdr is None: hdr = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=to) as r: return r.read().decode("utf-8",errors="replace")
    except Exception as e: print(f"  [W] {url[:70]}.. -> {e}"); return None

def sf(v):
    if v is None or v=="" or v=="-": return None
    try: return float(v)
    except: return None

def trade_date():
    """获取最近交易日（排除周末和节假日）"""
    t = datetime.now(TZ).date()
    # 从昨天开始往前找最近的交易日
    d = t - timedelta(days=1)
    while not is_trading_day(d):
        d = d - timedelta(days=1)
    return d

def slp(): time.sleep(0.35)

# ===== K线获取 =====
def fetch_kline(code, days=35):
    """腾讯日K前复权 -> list[{date,open,close,high,low,volume}]"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq"
    raw = fetch(url, to=12)
    if not raw: return None
    try:
        d = json.loads(raw).get("data",{}).get(code,{})
        kl = d.get("qfqday") or d.get("day") or []
        res = []
        for k in kl:
            if len(k)>=6:
                res.append({"date":k[0],"open":sf(k[1]),"close":sf(k[2]),
                            "high":sf(k[3]),"low":sf(k[4]),"volume":sf(k[5])})
        return res if res else None
    except Exception as e: print(f"  [W] K线解析{code}: {e}"); return None

# ===== 技术指标计算 =====
def calc_ema(vals, p):
    if not vals or len(vals)<p: return []
    m = 2.0/(p+1); ema = [sum(vals[:p])/p]
    for i in range(p,len(vals)): ema.append(vals[i]*m + ema[-1]*(1-m))
    return ema

def calc_macd(kl):
    if not kl or len(kl)<26: return None,None,None
    c = [k["close"] for k in kl if k["close"] is not None]
    if len(c)<26: return None,None,None
    e12,e26 = calc_ema(c,12), calc_ema(c,26)
    off = len(e12)-len(e26); e12a = e12[off:]
    if len(e12a)<9 or len(e26)<9: return None,None,None
    dif = [a-b for a,b in zip(e12a,e26)]
    dea = calc_ema(dif,9); doff=len(dif)-len(dea); difa=dif[doff:]
    if not dea: return None,None,None
    return round(difa[-1],3), round(dea[-1],3), round(2*(difa[-1]-dea[-1]),3)

def macd_signal(kl):
    """判定MACD信号: 金叉/金叉增强/零轴企稳/企稳初期/死叉/死叉恶化"""
    if not kl or len(kl)<26: return None
    c = [k["close"] for k in kl if k["close"] is not None]
    if len(c)<26: return None
    e12,e26 = calc_ema(c,12), calc_ema(c,26)
    e12a = e12[len(e12)-len(e26):]
    if len(e12a)<9 or len(e26)<9: return None
    dif = [a-b for a,b in zip(e12a,e26)]; dea = calc_ema(dif,9)
    difa = dif[len(dif)-len(dea):]
    if len(difa)<3 or len(dea)<3: return None
    dp,dn = difa[-2],difa[-1]; ep,en = dea[-2],dea[-1]
    mp,mn = 2*(dp-ep), 2*(dn-en)
    if dp<=ep and dn>en: return "金叉增强" if dn>0 else "金叉"
    if dp>=ep and dn<en: return "死叉恶化" if mn<mp and mp<0 else "死叉"
    if dn>en and mn>0 and mn<mp: return "零轴企稳" if dn<0 else "金叉增强"
    if dn<en and mn<0 and abs(mn)<abs(mp): return "企稳初期"
    if dn<en and mn<0 and abs(mn)>abs(mp): return "死叉恶化"
    if dn>en and mn>0 and abs(mn)>abs(mp): return "金叉增强"
    return None

def ten_day_chg(kl):
    if not kl or len(kl)<11: return None,None,None
    tc = kl[-1]["close"]; ri = max(0,len(kl)-11)
    rc,rd = kl[ri]["close"],kl[ri]["date"]
    if tc and rc and rc!=0: return round((tc-rc)/rc*100,2),rd,rc
    return None,None,None

def decline_pct(kl):
    if not kl or len(kl)<2: return None
    hs = [k["high"] for k in kl[-20:] if k["high"] is not None]
    tc = kl[-1]["close"]
    if hs and tc and max(hs)!=0: return round((tc-max(hs))/max(hs)*100,2)
    return None

def oversold_score(tdc):
    if tdc is None: return None
    return round(min(100,max(0, tdc*-5)))

def trend_score(kl, msig):
    if not kl or len(kl)<10: return None
    c = [k["close"] for k in kl if k["close"] is not None]
    if len(c)<10: return None
    s = 50
    ma5 = sum(c[-5:])/5; ma10 = sum(c[-10:])/10
    ma20 = sum(c[-20:])/20 if len(c)>=20 else c[-1]
    s += 15 if ma5>ma10 else -10
    s += 10 if ma10>ma20 else -10
    adj = {"金叉增强":20,"金叉":15,"零轴企稳":10,"企稳初期":5,"死叉":-10,"死叉恶化":-20}
    if msig in adj: s += adj[msig]
    return min(100,max(0,s))

def det_level(dp, msig):
    """操作级别: 标准仓/轻仓试/观察池/MACD否决/暂不关注"""
    if dp is None and msig is None: return "暂不关注"
    if msig=="死叉恶化" and (dp is None or dp>-7): return "MACD否决"
    if dp is not None:
        if dp<=-10: return "MACD否决" if msig=="死叉恶化" else "标准仓"
        if dp<=-7: return "轻仓试"
        if msig in ("金叉","金叉增强","零轴企稳","企稳初期"): return "观察池"
        return "暂不关注"
    return "观察池" if msig in ("金叉","金叉增强") else "暂不关注"

# ===== 宏观数据采集(v2.0保留) =====
def fetch_a_shares():
    print("[1] A股/指数行情...")
    codes = [i["code"] for i in WATCHLIST+INDICES if i["code"][:2] in ("sh","sz")]
    raw = fetch(f"http://qt.gtimg.cn/q={','.join(codes)}")
    if not raw: return None
    res = {}
    for ln in raw.strip().split("\n"):
        ln = ln.strip()
        m = re.match(r'v_(\w+)="(.+)"', ln)
        if not m: continue
        f = m.group(2).split("~")
        if len(f)<45: continue
        cl,pc = sf(f[3]),sf(f[4])
        cp = sf(f[32]) if f[32] else ((cl-pc)/pc*100 if cl and pc else None)
        res[m.group(1)] = {"name":f[1],"close":cl,"pre_close":pc,
            "change_pct":round(cp,2) if cp is not None else None,
            "volume":sf(f[36]),"turnover":sf(f[38]) if len(f)>38 else None,
            "high":sf(f[33]) if len(f)>33 else None,
            "low":sf(f[34]) if len(f)>34 else None}
    return res

def fetch_us():
    print("[2] 美股行情...")
    r = {}
    for sym,key in {".DJI":"djia",".INX":"sp500",".IXIC":"nasdaq",".KFX":"china_golden_dragon"}.items():
        url = f"https://finance.sina.com.cn/stock/usstock/api/jsonp_v2.php/var%20_{sym}=USStockService.getStockData?symbol={sym}"
        raw = fetch(url, to=8)
        if not raw: r[key]={"close":None,"change_pct":None}; continue
        try:
            j = re.search(r'\((.*)\)',raw,re.DOTALL)
            if j:
                d = json.loads(j.group(1))
                r[key] = {"close":sf(d.get("price")),"change_pct":sf(d.get("change_percent"))}
            else: r[key]={"close":None,"change_pct":None}
        except: r[key]={"close":None,"change_pct":None}
    return r

def fetch_vix():
    print("[3] VIX...")
    raw = fetch("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv", to=8)
    if not raw: return None
    ls = raw.strip().split("\n")
    if len(ls)<2: return None
    return sf(ls[-1].split(",")[4])

def fetch_a50():
    print("[4] A50期货...")
    raw = fetch("https://hq.sinajs.cn/list=hf_FTCHINACNY",
                hdr={"User-Agent":"Mozilla/5.0","Referer":"https://finance.sina.com.cn"}, to=8)
    if not raw: return None
    m = re.search(r'"(.+)"', raw)
    if m:
        f = m.group(1).split(",")
        if len(f)>=5:
            c,p = sf(f[3]),sf(f[2])
            return round((c-p)/p*100,2) if c and p else None
    return None

def fetch_usd():
    print("[5] 汇率...")
    raw = fetch("http://qt.gtimg.cn/q=usDXY", to=8)
    if not raw: return None
    m = re.search(r'"(.+)"', raw)
    return sf(m.group(1).split("~")[3]) if m else None

def fetch_margin():
    print("[6] 融资余额...")
    raw = fetch("https://data.10jqka.com.cn/market/rzrqgg/", to=10)
    if not raw: return None
    m = re.search(r'融资余额[：:]\s*([\d,.]+)', raw)
    return sf(m.group(1).replace(",","")) if m else None

def fetch_commod():
    print("[7] 大宗商品...")
    hdr = {"User-Agent":"Mozilla/5.0","Referer":"https://finance.sina.com.cn"}
    r = {}
    for code,info in {"hf_CL":"wti_crude","hf_GC":"gold","hf_HG":"copper"}.items():
        raw = fetch(f"https://hq.sinajs.cn/list={code}", hdr=hdr, to=8)
        if not raw: r[info]={"price":None,"change_pct":None}; continue
        m = re.search(r'"(.+)"', raw)
        if m:
            f = m.group(1).split(",")
            r[info] = {"price":sf(f[1]) if len(f)>1 else None,
                       "change_pct":sf(f[4]) if len(f)>4 else None}
        else: r[info]={"price":None,"change_pct":None}
    return r

def fetch_sector():
    print("[8] 板块数据...")
    raw = fetch("http://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php", to=8)
    if not raw: return {"top5":[],"bottom5":[],"hot_topics":[]}
    secs = []
    for ln in raw.strip().split("\n"):
        m = re.match(r'var\s+hq_str_(\w+)="(.+)"', ln.strip())
        if m:
            f = m.group(2).split(",")
            if len(f)>=4: secs.append({"name":f[0],"change_pct":sf(f[3]) if len(f)>3 else None})
    secs.sort(key=lambda x: x.get("change_pct") or -999, reverse=True)
    return {"top5":secs[:5],"bottom5":secs[-5:],"hot_topics":[]}

def fetch_sentiment(ad):
    print("[9] 市场情绪...")
    to = 0
    if ad:
        for c in ("sh000001","sz399001"):
            if c in ad and ad[c].get("turnover"): to += ad[c]["turnover"]
    return {"limit_up_count":None,"limit_down_count":None,
            "total_turnover_billion":round(to/10000,1) if to else None,
            "turnover_change_pct":None,"advance_count":None,"decline_count":None}

# ===== v2.1核心: 个股技术指标采集 =====
def collect_stocks(ad):
    print("\n[10] 个股K线与技术指标...")
    stocks, idx_info = [], None
    # 上证指数
    if ad and "sh000001" in ad:
        ix = ad["sh000001"]
        idx_info = {"code":"sh000001","name":ix.get("name","上证指数"),
                    "close":ix.get("close"),"change_percent":ix.get("change_pct"),
                    "ten_day_change":None}
    print("  -> 上证指数K线...")
    ikl = fetch_kline("sh000001")
    if ikl and idx_info: idx_info["ten_day_change"] = ten_day_chg(ikl)[0]
    slp()
    for i,it in enumerate(WATCHLIST):
        cd,nm = it["code"],it["name"]
        print(f"  [{i+1}/{len(WATCHLIST)}] {nm}({cd})...")
        b = (ad or {}).get(cd, {})
        klines = fetch_kline(cd); slp()
        dif,dea,mb = calc_macd(klines) if klines else (None,None,None)
        ms = macd_signal(klines) if klines else None
        tdc,rd,rc = ten_day_chg(klines) if klines else (None,None,None)
        dp = decline_pct(klines) if klines else None
        os_ = oversold_score(tdc); ts = trend_score(klines,ms); lv = det_level(dp,ms)
        stocks.append({"code":cd,"name":nm,"close":b.get("close"),"change_pct":b.get("change_pct"),
            "prev_close":b.get("pre_close"),"turnover":b.get("turnover"),
            "high":b.get("high"),"low":b.get("low"),
            "decline_pct":dp,"ref_date":rd,"ref_close":rc,
            "dif":dif,"dea":dea,"macd":mb,"macd_signal":ms,"roe_ttm":None,
            "oversold_score":os_,"trend_score":ts,"level":lv})
        mk = f"★{lv}" if lv in ("标准仓","轻仓试") else lv
        print(f"    close={b.get('close')} chg={b.get('change_pct')}% dp={dp}% MACD={ms} -> {mk}")
    return stocks, idx_info

# ===== 主流程 =====
def main():
    td = trade_date(); ds = td.strftime("%Y-%m-%d"); rd = datetime.now(TZ).strftime("%Y-%m-%d")
    
    # ===== 交易日检查 =====
    today = datetime.now(TZ).date()
    if not is_trading_day(today):
        print(f"⚠️ 今天 {today.strftime('%Y-%m-%d')} 不是交易日（周末或节假日），跳过采集")
        print(f"   将采集最近交易日 {ds} 的数据")
    
    print(f"=== LIVS-Stock v2.1 {ds} ===")
    print(f"采集时间: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} CST\n")

    # 宏观
    ad = fetch_a_shares(); us = fetch_us(); vx = fetch_vix(); a5 = fetch_a50()
    uc = fetch_usd(); mg = fetch_margin(); co = fetch_commod(); se = fetch_sector()
    sn = fetch_sentiment(ad)

    # 个股技术指标
    stocks, idx = collect_stocks(ad)

    # 宏观输出(v2.0格式)
    macro = {"date":ds,"collector":"daily_collect.py v2.1",
        "updated_at":datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "data_notes":["腾讯财经+新浪+同花顺+CBOE","v2.1+个股技术指标","北向资金:东财停披填null"],
        "macro":{"us_market":us or {},"a50_futures":{"change_pct":a5},
                 "usd_cny_offshore":uc,"vix":vx},
        "capital_flow":{"northbound":{"net_inflow_yesterday":None,"net_inflow_5d_cum":None,"top_add":[]},
                        "margin_balance_change":mg},
        "market_sentiment":sn,"sector":se,"watchlist_news":[],"policy":[],"breaking_news":[],
        "commodities":co,"eod_quotes":{"indices":{},"watchlist":[]}}
    if ad:
        for it in INDICES:
            c = it["code"]
            if c in ad: macro["eod_quotes"]["indices"][it["name"]]={"close":ad[c]["close"],"change_pct":ad[c]["change_pct"]}
        for it in WATCHLIST:
            c = it["code"]
            if c in ad: macro["eod_quotes"]["watchlist"].append({"code":c[2:],"name":ad[c]["name"],
                "close":ad[c]["close"],"change_pct":ad[c]["change_pct"]})

    # 个股技术指标输出(v2.1)
    sc = sum(1 for s in stocks if s["level"]=="标准仓")
    lc = sum(1 for s in stocks if s["level"]=="轻仓试")
    wc = sum(1 for s in stocks if s["level"]=="观察池")
    rc = sum(1 for s in stocks if s["level"]=="MACD否决")
    cp = []
    if sc: cp.append(f"标准仓{sc}只({','.join(s['name'] for s in stocks if s['level']=='标准仓')})")
    if lc: cp.append(f"轻仓试{lc}只({','.join(s['name'] for s in stocks if s['level']=='轻仓试')})")
    if wc: cp.append(f"观察池{wc}只")
    if rc: cp.append(f"MACD否决{rc}只")
    conc = "；".join(cp) if cp else "当前无明确操作标的"
    ic = idx.get("change_percent") if idx else None
    reason = (f"大盘{'下跌' if ic and ic<0 else '上涨' if ic and ic>0 else '平盘'}"
              f"{abs(ic) if ic else 0}%，" if ic else "") + f"{len(stocks)}只标的中{sc+lc}只达到建仓条件"

    stock_out = {"date":ds,"report_date":rd,"index":idx or {},"stocks":stocks,
                 "conclusion":conc,"reason":reason}

    # 写文件
    os.makedirs(OUT, exist_ok=True)
    mf = f"daily-finance-{ds}.json"
    with open(os.path.join(OUT,mf),"w",encoding="utf-8") as f: json.dump(macro,f,ensure_ascii=False,indent=2)
    print(f"\n[OK] {OUT}/{mf}")

    sf_name = f"stock_data_{td.strftime('%Y%m%d')}.json"
    with open(os.path.join(OUT,sf_name),"w",encoding="utf-8") as f: json.dump(stock_out,f,ensure_ascii=False,indent=2)
    print(f"[OK] {OUT}/{sf_name}")

    with open("stock.josn","w",encoding="utf-8") as f: json.dump(macro,f,ensure_ascii=False,indent=2)
    print("[OK] stock.josn")

    # manifest
    nc = sum(1 for s in stocks if s["close"] is None)
    nm = sum(1 for s in stocks if s["dif"] is None)
    notes = []
    if nc: notes.append(f"{nc}只收盘价为空")
    if nm: notes.append(f"{nm}只MACD不足")
    man = {"version":"2.1","last_updated":datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "trade_date":ds,"files":{
            "macro":{"filename":mf,"path":f"{OUT}/{mf}","status":"complete" if ad else "degraded"},
            "stock_data":{"filename":sf_name,"path":f"{OUT}/{sf_name}","status":"complete" if stocks else "degraded","stock_count":len(stocks)}},
        "quality":{"macro_status":"failed" if nc>len(stocks)*0.5 else ("complete" if ad else "degraded"),
                   "stock_data_status":"failed" if nm>len(stocks)*0.5 else ("complete" if stocks else "degraded"),
                   "kline_source":"tencent_qfq","notes":notes},
        "watchlist_count":len(WATCHLIST),"index_count":len(INDICES)}
    with open(os.path.join(OUT,"manifest.json"),"w",encoding="utf-8") as f: json.dump(man,f,ensure_ascii=False,indent=2)
    print(f"[OK] {OUT}/manifest.json")

    # ===== 降级告警 =====
    degraded = False
    alert_msgs = []
    if man["quality"]["macro_status"] == "failed":
        degraded = True
        alert_msgs.append("🔴 宏观数据采集失败！超过50%个股收盘价为空")
    elif man["quality"]["macro_status"] == "degraded":
        degraded = True
        alert_msgs.append("🟠 宏观数据降级：部分数据源不可用")
    if man["quality"]["stock_data_status"] == "failed":
        degraded = True
        alert_msgs.append("🔴 个股技术指标采集失败！超过50%股票MACD数据不足")
    elif man["quality"]["stock_data_status"] == "degraded":
        degraded = True
        alert_msgs.append("🟠 个股数据降级：部分K线数据缺失")
    if nc and nc > 0:
        degraded = True
        alert_msgs.append(f"⚠️ {nc}只股票收盘价为空（数据源可能异常）")
    if notes:
        for n in notes:
            alert_msgs.append(f"  · {n}")
    
    if degraded:
        print(f"\n{'!'*55}")
        print(f"  🚨 降级告警 — 管道数据质量异常！")
        print(f"{'!'*55}")
        for msg in alert_msgs:
            print(f"  {msg}")
        print(f"  请检查: 1)数据源是否可用 2)网络是否正常 3)WATCHLIST代码是否正确")
        print(f"{'!'*55}")

    # 摘要
    print(f"\n{'='*50}\n=== v2.1 采集摘要 {ds} ===\n{'='*50}")
    print(f"宏观: {'OK' if ad else 'FAIL'} | 个股: {len(stocks)}只")
    if idx: print(f"上证: {idx.get('close')} ({idx.get('change_percent')}%)")
    print(f"\n标准仓:{sc} 轻仓试:{lc} 观察池:{wc} MACD否决:{rc} 暂不关注:{len(stocks)-sc-lc-wc-rc}")
    if sc or lc:
        print("\n--- 重点关注 ---")
        for s in stocks:
            if s["level"] in ("标准仓","轻仓试"):
                print(f"  {s['name']}: close={s['close']} dp={s['decline_pct']}% MACD={s['macd_signal']} OS={s['oversold_score']} -> ★{s['level']}")
    if us:
        print("\n--- 外盘 ---")
        for k,v in us.items(): print(f"  {k}: {v}")
    print(f"  VIX:{vx} A50:{a5} 汇率:{uc}")
    if co:
        print("\n--- 商品 ---")
        for k,v in co.items(): print(f"  {k}: {v}")
    print(f"\n{'='*50}\n=== v2.1 采集完成 ===\n{'='*50}")

if __name__=="__main__":
    main()
