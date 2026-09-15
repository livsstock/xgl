#!/usr/bin/env python3
"""
LIVS-Stock 每日财经数据采集脚本 v2.0
运行于 GitHub Actions，每交易日北京时间 8:00 自动采集
数据输出到 output/daily-finance-YYYY-MM-DD.json 和 stock.josn

v2.0 更新:
- 数据质量信号灯 (manifest.json)
- 重试机制 (每个数据源3次重试)
- 合规 UA
- 时区修正 (强制 Asia/Shanghai)
- watchlist 外置到 config/watchlist.json
- 历史对比 (turnover_change_pct)

数据源：腾讯财经、新浪财经、同花顺、CBOE 等公开接口
"""

import json
import re
import time
import os
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

# ==================== 配置 ====================
try:
    from zoneinfo import ZoneInfo
    TZ_CST = ZoneInfo("Asia/Shanghai")
except ImportError:
    TZ_CST = timezone(timedelta(hours=8))

UA = "LIVS-Stock-WorkBuddy/2.0 (research)"
OUTPUT_DIR = "output"
CONFIG_DIR = "config"
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds

# 加载外部 watchlist
def load_watchlist():
    watchlist_path = os.path.join(CONFIG_DIR, "watchlist.json")
    if os.path.exists(watchlist_path):
        with open(watchlist_path, "r", encoding="utf-8") as f:
            return json.load(f)
    # fallback: hardcoded
    return [
        {"code": "sh601398", "name": "工商银行"},
        {"code": "sh601288", "name": "农业银行"},
        {"code": "sh601012", "name": "隆基绿能"},
        {"code": "sh601958", "name": "金钼股份"},
        {"code": "sh600392", "name": "盛和资源"},
        {"code": "sz000725", "name": "京东方A"},
        {"code": "sh600111", "name": "北方稀土"},
        {"code": "sh600875", "name": "东方电气"},
        {"code": "sh000001", "name": "上证指数"},
        {"code": "sz399001", "name": "深证成指"},
        {"code": "sz399006", "name": "创业板指"},
    ]

WATCHLIST = load_watchlist()

INDICES = [
    {"code": "sh000001", "name": "上证指数"},
    {"code": "sz399001", "name": "深证成指"},
    {"code": "sz399006", "name": "创业板指"},
    {"code": "sh000300", "name": "沪深300"},
    {"code": "hkHSI", "name": "恒生指数"},
    {"code": "hkHSCEI", "name": "恒生科技"},
]

# ==================== 数据质量追踪 ====================
class DataQuality:
    def __init__(self):
        self.sources = {}
    
    def set_ok(self, source_name):
        self.sources[source_name] = "ok"
    
    def set_failed(self, source_name):
        self.sources[source_name] = "failed"
    
    def set_stale(self, source_name):
        self.sources[source_name] = "stale"
    
    @property
    def overall(self):
        if not self.sources:
            return "failed"
        values = list(self.sources.values())
        if all(v == "ok" for v in values):
            return "complete"
        critical_sources = ["tencent_quotes"]
        if any(self.sources.get(s) == "failed" for s in critical_sources):
            return "failed"
        failed_count = sum(1 for v in values if v == "failed")
        if failed_count >= len(values) // 2:
            return "failed"
        return "partial"
    
    def to_dict(self):
        return {
            "overall": self.overall,
            "sources": dict(self.sources),
        }

quality = DataQuality()

# ==================== 工具函数 ====================

def fetch_url(url, headers=None, timeout=10):
    """通用HTTP请求，带重试机制"""
    if headers is None:
        headers = {"User-Agent": UA}
    else:
        headers.setdefault("User-Agent", UA)
    
    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  [WARN] {url} -> attempt {attempt}/{MAX_RETRIES} -> {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
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
    if today.weekday() == 0:
        return today - timedelta(days=3)
    elif today.weekday() == 6:
        return today - timedelta(days=2)
    else:
        return today - timedelta(days=1)

def get_yesterday_str(trade_date):
    """获取前一交易日日期字符串，用于加载历史数据"""
    prev = trade_date - timedelta(days=1)
    # 跳过周末
    if prev.weekday() == 5:  # Saturday
        prev -= timedelta(days=1)
    elif prev.weekday() == 6:  # Sunday
        prev -= timedelta(days=2)
    return prev.strftime("%Y-%m-%d")

def load_previous_data(date_str):
    """加载前一交易日数据用于对比"""
    prev_path = os.path.join(OUTPUT_DIR, f"daily-finance-{date_str}.json")
    if os.path.exists(prev_path):
        try:
            with open(prev_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None

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
        quality.set_failed("tencent_quotes")
        return None

    results = {}
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
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
    
    if results:
        quality.set_ok("tencent_quotes")
    else:
        quality.set_failed("tencent_quotes")
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
    has_success = False
    for suffix, key in tickers.items():
        url = f"https://finance.sina.com.cn/stock/usstock/api/jsonp_v2.php/var%20_{suffix}=USStockService.getStockData?symbol={suffix}"
        raw = fetch_url(url, timeout=8)
        if not raw:
            results[key] = {"close": None, "change_pct": None}
            continue
        try:
            json_str = re.search(r'\((.*)\)', raw, re.DOTALL)
            if json_str:
                data = json.loads(json_str.group(1))
                price = safe_float(data.get("price"))
                change_pct = safe_float(data.get("change_percent"))
                results[key] = {"close": price, "change_pct": change_pct}
                has_success = True
            else:
                results[key] = {"close": None, "change_pct": None}
        except Exception:
            results[key] = {"close": None, "change_pct": None}
    
    if has_success:
        quality.set_ok("sina_fx_futures")
    else:
        quality.set_failed("sina_fx_futures")
    return results


def fetch_vix():
    """获取VIX指数（CBOE）"""
    print("[3] 采集VIX...")
    url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
    raw = fetch_url(url, timeout=8)
    if not raw:
        quality.set_failed("cboe_vix")
        return None
    lines = raw.strip().split("\n")
    if len(lines) < 2:
        quality.set_failed("cboe_vix")
        return None
    last = lines[-1].split(",")
    if len(last) >= 5:
        val = safe_float(last[4])  # Close
        if val is not None:
            quality.set_ok("cboe_vix")
            return val
    quality.set_failed("cboe_vix")
    return None


def fetch_a50():
    """获取富时中国A50期货（新浪财经）"""
    print("[4] 采集A50期货...")
    url = "https://hq.sinajs.cn/list=hf_FTCHINACNY"
    headers = {"User-Agent": UA, "Referer": "https://finance.sina.com.cn"}
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
        quality.set_failed("ths_margin")
        return None
    try:
        m = re.search(r'融资余额[：:]\s*([\d,.]+)', raw)
        if m:
            val = m.group(1).replace(",", "")
            result = safe_float(val)
            if result is not None:
                quality.set_ok("ths_margin")
                return result
    except Exception:
        pass
    quality.set_failed("ths_margin")
    return None


def fetch_commodities():
    """获取大宗商品（WTI原油、黄金、铜）"""
    print("[7] 采集大宗商品...")
    tickers = {
        "hf_CL": {"name": "wti_crude", "unit": 1},
        "hf_GC": {"name": "gold", "unit": 1},
        "hf_HG": {"name": "copper", "unit": 1},
    }
    headers = {"User-Agent": UA, "Referer": "https://finance.sina.com.cn"}
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
                    price = safe_float(fields[1])
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
    sector_url = "http://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
    sector_raw = fetch_url(sector_url, timeout=8)
    if not sector_raw:
        quality.set_failed("sina_sectors")
        return {"top5": [], "bottom5": [], "hot_topics": []}

    sectors = []
    try:
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
    result = {
        "top5": sectors[:5] if sectors else [],
        "bottom5": sectors[-5:] if sectors else [],
        "hot_topics": [],
    }
    if sectors:
        quality.set_ok("sina_sectors")
    else:
        quality.set_failed("sina_sectors")
    return result


def fetch_market_sentiment(a_data, prev_data):
    """从A股数据推算市场情绪，含历史对比"""
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

    turnover_billion = round(total_turnover / 10000, 1) if total_turnover else None

    # 计算成交额变化（与前一交易日对比）
    turnover_change_pct = None
    if prev_data and prev_data.get("market_sentiment", {}).get("total_turnover_billion"):
        prev_turnover = prev_data["market_sentiment"]["total_turnover_billion"]
        if turnover_billion and prev_turnover and prev_turnover > 0:
            turnover_change_pct = round((turnover_billion - prev_turnover) / prev_turnover * 100, 2)

    return {
        "limit_up_count": None,
        "limit_down_count": None,
        "total_turnover_billion": turnover_billion,
        "turnover_change_pct": turnover_change_pct,
        "advance_count": None,
        "decline_count": None,
    }


# ==================== 主流程 ====================

def main():
    trade_date = get_trade_date()
    date_str = trade_date.strftime("%Y-%m-%d")
    print(f"=== LIVS-Stock v2.0 每日采集 {date_str} ===")
    print(f"采集时间: {datetime.now(TZ_CST).strftime('%Y-%m-%d %H:%M:%S')} CST\n")

    # 加载前一交易日数据用于对比
    prev_date_str = get_yesterday_str(trade_date)
    prev_data = load_previous_data(prev_date_str)
    if prev_data:
        print(f"[INFO] 已加载前一交易日数据: {prev_date_str}")
    else:
        print(f"[INFO] 未找到前一交易日数据: {prev_date_str}")

    # 1. 采集所有数据
    a_data = fetch_a_shares()
    us_data = fetch_us_market()
    vix = fetch_vix()
    a50 = fetch_a50()
    usd_cny = fetch_usd_cny()
    margin = fetch_margin_balance()
    commodities = fetch_commodities()
    sector = fetch_sector_data()
    sentiment = fetch_market_sentiment(a_data, prev_data)

    # 2. 组装输出
    output = {
        "date": date_str,
        "collector": "LIVS-Stock-WorkBuddy/2.0",
        "updated_at": datetime.now(TZ_CST).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "data_notes": [
            "自动采集：腾讯财经+新浪+同花顺+CBOE公开接口",
            "北向资金：东财2024/8起停披，填null",
            "v2.0: 含数据质量信号灯、重试机制、历史对比",
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
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 输出数据文件
    output_filename = f"daily-finance-{date_str}.json"
    output_path = os.path.join(OUTPUT_DIR, output_filename)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n[OK] 写入 {output_path}")

    # 兼容旧路径
    stock_path = "stock.josn"
    with open(stock_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"[OK] 写入 {stock_path}")

    # 4. 输出 manifest.json（数据质量信号灯）
    manifest = {
        "date": date_str,
        "filename": f"output/daily-finance-{date_str}.json",
        "raw_url": f"https://raw.githubusercontent.com/livsstock/xgl/data/daily/output/daily-finance-{date_str}.json",
        "quality": quality.overall,
        "updated_at": datetime.now(TZ_CST).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
    }
    manifest_path = os.path.join(OUTPUT_DIR, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"[OK] 写入 {manifest_path} (quality: {quality.overall})")

    # 5. 摘要
    print(f"\n=== 采集摘要 ===")
    print(f"日期: {date_str}")
    print(f"数据质量: {quality.overall}")
    for src, status in quality.sources.items():
        icon = "✓" if status == "ok" else "✗"
        print(f"  {icon} {src}: {status}")
    if us_data:
        for k, v in us_data.items():
            print(f"  {k}: {v}")
    print(f"  VIX: {vix}")
    print(f"  A50: {a50}")
    print(f"  汇率: {usd_cny}")
    if commodities:
        for k, v in commodities.items():
            print(f"  {k}: {v}")
    if turnover_change_pct := sentiment.get("turnover_change_pct"):
        print(f"  成交额变化: {turnover_change_pct}%")

    print("\n=== 采集完成 ===")
    
    # 输出质量警告供 CI 日志使用
    if quality.overall == "partial":
        print("::warning::Data quality is PARTIAL - some sources failed")
    elif quality.overall == "failed":
        print("::error::Data quality is FAILED - critical sources unavailable")


if __name__ == "__main__":
    main()
