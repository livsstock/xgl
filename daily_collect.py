#!/usr/bin/env python3
"""
LIVS-Stock 每日财经数据采集脚本
运行于 GitHub Actions，每交易日北京时间 8:00 自动采集
数据输出到 output/daily-finance-YYYY-MM-DD.json 和 stock.josn

数据源：腾讯财经、新浪财经、同花顺、CBOE 等公开接口
"""

import json
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

# ==================== 配置 ====================
TZ_CST = timezone(timedelta(hours=8))
WATCHLIST = [
    {"code": "sh601398", "name": "工商银行"},
    {"code": "sh601288", "name": "农业银行"},
    {"code": "sh601012", "name": "隆基绿能"},
    {"code": "sh601958", "name": "金钼股份"},
    {"code": "sh600392", "name": "盛和资源"},
    {"code": "sz000725", "name": "京东方A"},
    {"code": "sh600111", "name": "北方稀土"},
    {"code": "sh600875", "name": "东方电气"},  # 航发动力替代
    {"code": "sh000001", "name": "上证指数"},
    {"code": "sz399001", "name": "深证成指"},
    {"code": "sz399006", "name": "创业板指"},
]
INDICES = [
    {"code": "sh000001", "name": "上证指数"},
    {"code": "sz399001", "name": "深证成指"},
    {"code": "sz399006", "name": "创业板指"},
    {"code": "sh000300", "name": "沪深300"},
    {"code": "hkHSI", "name": "恒生指数"},
    {"code": "hkHSCEI", "name": "恒生科技"},
]
OUTPUT_DIR = "output"

# ==================== 工具函数 ====================

def fetch_url(url, headers=None, timeout=10):
    """通用HTTP请求"""
    if headers is None:
        headers = {"User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [WARN] {url} -> {e}")
        return None

def safe_float(val):
    """安全转浮点数"""
    if val is None or val == "" or val == "-":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None

def get_trade_date():
    """获取上一个交易日（简化：跳过周末）"""
    today = datetime.now(TZ_CST).date()
    # 简单回退：如果今天周一，取上周五
    if today.weekday() == 0:
        return today - timedelta(days=3)
    elif today.weekday() == 6:
        return today - timedelta(days=2)
    else:
        return today - timedelta(days=1)

# ==================== 数据采集函数 ====================

def fetch_a_shares():
    """通过腾讯行情接口获取A股/指数数据"""
    print("[1] 采集A股/指数行情...")
    codes = []
    for item in WATCHLIST + INDICES:
        c = item["code"]
        if c.startswith("sh") or c.startswith("sz"):
            codes.append(c)

    url = f"http://qt.gtimg.cn/q={','.join(codes)}"
    raw = fetch_url(url)
    if not raw:
        return None

    results = {}
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # 格式: v_sh601398="1~工商银行~601398~8.22~..."
        m = re.match(r'v_(\w+)="(.+)"', line)
        if not m:
            continue
        code = m.group(1)
        fields = m.group(2).split("~")
        if len(fields) < 45:
            continue
        name = fields[1]
        close = safe_float(fields[3])
        pre_close = safe_float(fields[4])
        change_pct = safe_float(fields[32]) if fields[32] else (
            ((close - pre_close) / pre_close * 100) if close and pre_close else None
        )
        volume = safe_float(fields[36])  # 手
        turnover = safe_float(fields[37])  # 万元

        results[code] = {
            "name": name,
            "close": close,
            "pre_close": pre_close,
            "change_pct": round(change_pct, 2) if change_pct else None,
            "volume": volume,
            "turnover": turnover,
        }
    return results


def fetch_us_market():
    """获取美股收盘数据（通过新浪美股）"""
    print("[2] 采集美股行情...")
    tickers = {
        ".DJI": "djia",
        ".INX": "sp500",
        ".IXIC": "nasdaq",
        ".KFX": "china_golden_dragon",
    }
    results = {}
    for suffix, key in tickers.items():
        url = f"https://finance.sina.com.cn/stock/usstock/api/jsonp_v2.php/var%20_{suffix}=USStockService.getStockData?symbol={suffix}"
        raw = fetch_url(url, timeout=8)
        if not raw:
            results[key] = {"close": None, "change_pct": None}
            continue
        try:
            # 解析JSONP
            json_str = re.search(r'\((.*)\)', raw, re.DOTALL)
            if json_str:
                data = json.loads(json_str.group(1))
                price = safe_float(data.get("price"))
                change_pct = safe_float(data.get("change_percent"))
                results[key] = {"close": price, "change_pct": change_pct}
            else:
                results[key] = {"close": None, "change_pct": None}
        except Exception:
            results[key] = {"close": None, "change_pct": None}
    return results


def fetch_vix():
    """获取VIX指数（CBOE）"""
    print("[3] 采集VIX...")
    url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
    raw = fetch_url(url, timeout=8)
    if not raw:
        return None
    lines = raw.strip().split("\n")
    if len(lines) < 2:
        return None
    last = lines[-1].split(",")
    if len(last) >= 5:
        return safe_float(last[4])  # Close
    return None


def fetch_a50():
    """获取富时中国A50期货（新浪财经）"""
    print("[4] 采集A50期货...")
    url = "https://hq.sinajs.cn/list=hf_FTCHINACNY"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
    raw = fetch_url(url, headers=headers, timeout=8)
    if not raw:
        return None
    try:
        m = re.search(r'"(.+)"', raw)
        if m:
            fields = m.group(1).split(",")
            if len(fields) >= 5:
                close = safe_float(fields[3])
                pre_close = safe_float(fields[2])
                change_pct = ((close - pre_close) / pre_close * 100) if close and pre_close else None
                return round(change_pct, 2) if change_pct else None
    except Exception:
        pass
    return None


def fetch_usd_cny():
    """获取美元兑人民币汇率"""
    print("[5] 采集汇率...")
    url = "http://qt.gtimg.cn/q=usDXY"
    raw = fetch_url(url, timeout=8)
    if not raw:
        return None
    try:
        m = re.search(r'"(.+)"', raw)
        if m:
            fields = m.group(1).split("~")
            return safe_float(fields[3])
    except Exception:
        pass
    return None


def fetch_margin_balance():
    """获取融资余额（同花顺数据中心）"""
    print("[6] 采集融资余额...")
    url = "https://data.10jqka.com.cn/market/rzrqgg/"
    raw = fetch_url(url, timeout=10)
    if not raw:
        return None
    try:
        # 同花顺页面有融资余额表格
        m = re.search(r'融资余额[：:]\s*([\d,.]+)', raw)
        if m:
            val = m.group(1).replace(",", "")
            return safe_float(val)
    except Exception:
        pass
    return None


def fetch_commodities():
    """获取大宗商品（WTI原油、黄金、铜）"""
    print("[7] 采集大宗商品...")
    tickers = {
        "hf_CL": {"name": "wti_crude", "unit": 1},
        "hf_GC": {"name": "gold", "unit": 1},
        "hf_HG": {"name": "copper", "unit": 1},
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
    results = {}
    for code, info in tickers.items():
        url = f"https://hq.sinajs.cn/list={code}"
        raw = fetch_url(url, headers=headers, timeout=8)
        if not raw:
            results[info["name"]] = {"price": None, "change_pct": None}
            continue
        try:
            m = re.search(r'"(.+)"', raw)
            if m:
                fields = m.group(1).split(",")
                if len(fields) >= 5:
                    price = safe_float(fields[1])  # 最新价
                    # change_pct = 涨跌幅
                    change_pct = safe_float(fields[4]) if len(fields) > 4 else None
                    results[info["name"]] = {"price": price, "change_pct": change_pct}
                else:
                    results[info["name"]] = {"price": None, "change_pct": None}
            else:
                results[info["name"]] = {"price": None, "change_pct": None}
        except Exception:
            results[info["name"]] = {"price": None, "change_pct": None}
    return results


def fetch_sector_data():
    """获取板块涨跌排行（腾讯财经）"""
    print("[8] 采集板块数据...")
    url = "http://qt.gtimg.cn/q=sh000001,sz399001"
    raw = fetch_url(url, timeout=8)
    # 简化版：板块数据需要新浪行业接口
    sector_url = "http://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
    sector_raw = fetch_url(sector_url, timeout=8)
    if not sector_raw:
        return {"top5": [], "bottom5": [], "hot_topics": []}

    sectors = []
    try:
        # 解析行业板块涨跌幅
        lines = sector_raw.strip().split("\n")
        for line in lines:
            line = line.strip()
            if not line:
                continue
            m = re.match(r'var\s+hq_str_(\w+)="(.+)"', line)
            if m:
                fields = m.group(2).split(",")
                if len(fields) >= 4:
                    name = fields[0]
                    change_pct = safe_float(fields[3]) if len(fields) > 3 else None
                    sectors.append({"name": name, "change_pct": change_pct})
    except Exception:
        pass

    sectors.sort(key=lambda x: x.get("change_pct") or -999, reverse=True)
    return {
        "top5": sectors[:5] if sectors else [],
        "bottom5": sectors[-5:] if sectors else [],
        "hot_topics": [],
    }


def fetch_market_sentiment(a_data):
    """从A股数据推算市场情绪"""
    print("[9] 计算市场情绪...")
    if not a_data:
        return {
            "limit_up_count": None,
            "limit_down_count": None,
            "total_turnover_billion": None,
            "turnover_change_pct": None,
            "advance_count": None,
            "decline_count": None,
        }

    total_turnover = 0
    for code, info in a_data.items():
        if code in ("sh000001", "sz399001"):
            if info.get("turnover"):
                total_turnover += info["turnover"]

    return {
        "limit_up_count": None,
        "limit_down_count": None,
        "total_turnover_billion": round(total_turnover / 10000, 1) if total_turnover else None,
        "turnover_change_pct": None,
        "advance_count": None,
        "decline_count": None,
    }


# ==================== 主流程 ====================

def main():
    trade_date = get_trade_date()
    date_str = trade_date.strftime("%Y-%m-%d")
    print(f"=== LIVS-Stock 每日采集 {date_str} ===")
    print(f"采集时间: {datetime.now(TZ_CST).strftime('%Y-%m-%d %H:%M:%S')} CST\n")

    # 1. 采集所有数据
    a_data = fetch_a_shares()
    us_data = fetch_us_market()
    vix = fetch_vix()
    a50 = fetch_a50()
    usd_cny = fetch_usd_cny()
    margin = fetch_margin_balance()
    commodities = fetch_commodities()
    sector = fetch_sector_data()
    sentiment = fetch_market_sentiment(a_data)

    # 2. 组装输出
    output = {
        "date": date_str,
        "collector": "daily_collect.py (GitHub Actions)",
        "updated_at": datetime.now(TZ_CST).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "data_notes": [
            "自动采集：腾讯财经+新浪+同花顺+CBOE公开接口",
            "北向资金：东财2024/8起停披，填null",
        ],
        "macro": {
            "us_market": us_data if us_data else {},
            "a50_futures": {"change_pct": a50},
            "usd_cny_offshore": usd_cny,
            "vix": vix,
        },
        "capital_flow": {
            "northbound": {
                "net_inflow_yesterday": None,
                "net_inflow_5d_cum": None,
                "top_add": [],
            },
            "margin_balance_change": margin,
        },
        "market_sentiment": sentiment,
        "sector": sector,
        "watchlist_news": [],
        "policy": [],
        "breaking_news": [],
        "commodities": commodities,
        "eod_quotes": {
            "indices": {},
            "watchlist": [],
        },
    }

    # 填充指数
    if a_data:
        for item in INDICES:
            code = item["code"]
            if code in a_data:
                d = a_data[code]
                output["eod_quotes"]["indices"][item["name"]] = {
                    "close": d["close"],
                    "change_pct": d["change_pct"],
                }

        # 填充标的池
        for item in WATCHLIST:
            code = item["code"]
            if code in a_data:
                d = a_data[code]
                output["eod_quotes"]["watchlist"].append({
                    "code": code[2:],
                    "name": d["name"],
                    "close": d["close"],
                    "change_pct": d["change_pct"],
                })

    # 3. 输出文件
    import os
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 输出到 output/ 目录
    output_filename = f"daily-finance-{date_str}.json"
    output_path = os.path.join(OUTPUT_DIR, output_filename)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n[OK] 写入 {output_path}")

    # 同时输出到根目录 stock.josn（兼容旧路径）
    stock_path = "stock.josn"
    with open(stock_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"[OK] 写入 {stock_path}")

    # 4. 摘要
    print(f"\n=== 采集摘要 ===")
    print(f"日期: {date_str}")
    if us_data:
        for k, v in us_data.items():
            print(f"  {k}: {v}")
    print(f"  VIX: {vix}")
    print(f"  A50: {a50}")
    print(f"  汇率: {usd_cny}")
    if commodities:
        for k, v in commodities.items():
            print(f"  {k}: {v}")

    print("\n=== 采集完成 ===")


if __name__ == "__main__":
    main()
