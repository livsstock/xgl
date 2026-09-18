#!/usr/bin/env python3
"""
LIVS-Stock 每日财经数据采集脚本 v2.1
运行于 GitHub Actions，每交易日北京时间 8:00 自动采集

v2.1 更新:
- 新增个股技术指标计算（MACD/超跌评分/趋势评分/操作级别）
- 新增 stock_data_YYYYMMDD.json 输出，供预测引擎直接消费
- 标的池扩至 20 只（14股 + 3ETF + 3指数）
- manifest.json 双文件索引

数据源：腾讯财经、新浪财经、同花顺、CBOE 等公开接口
输出：output/daily-finance-YYYY-MM-DD.json（宏观）
      output/stock_data_YYYYMMDD.json（个股技术指标）
"""

import json
import re
import time
import os
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

# ==================== 配置 ====================
TZ_CST = timezone(timedelta(hours=8))
OUTPUT_DIR = "output"

WATCHLIST = [
    # === 防御组 (5) ===
    {"code": "sh601398", "name": "工商银行"},
    {"code": "sh601288", "name": "农业银行"},
    {"code": "sh600036", "name": "招商银行"},
    {"code": "sh601012", "name": "隆基绿能"},
    {"code": "sh601958", "name": "金钼股份"},
    # === 有色金属组 (5) ===
    {"code": "sh600111", "name": "北方稀土"},
    {"code": "sh600392", "name": "盛和资源"},
    {"code": "sh600893", "name": "航发动力"},
    {"code": "sz000960", "name": "锡业股份"},
    {"code": "sz002155", "name": "湖南黄金"},
    # === 成长组 (4) ===
    {"code": "sz000725", "name": "京东方A"},
    {"code": "sh600875", "name": "东方电气"},
    {"code": "sh600019", "name": "宝钢股份"},
    {"code": "sh601212", "name": "白银有色"},
    # === 主题ETF (3) ===
    {"code": "sz159770", "name": "机器人ETF"},
    {"code": "sh518880", "name": "黄金ETF"},
    {"code": "sh512480", "name": "半导体ETF"},
]

INDICES = [
    {"code": "sh000001", "name": "上证指数"},
    {"code": "sz399001", "name": "深证成指"},
    {"code": "sz399006", "name": "创业板指"},
    {"code": "sh000300", "name": "沪深300"},
]

# 指数代码集合（用于区分指数和个股）
INDEX_CODES = {item["code"] for item in INDICES}

# ==================== 工具函数 ====================

def fetch_url(url, headers=None, timeout=10):
    """通用HTTP请求"""
    if headers is None:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [WARN] fetch_url: {url[:60]}... -> {e}")
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
    if today.weekday() == 0:  # 周一 -> 上周五
        return today - timedelta(days=3)
    elif today.weekday() == 6:  # 周日 -> 上周五
        return today - timedelta(days=2)
    else:
        return today - timedelta(days=1)


# ==================== 行情数据采集 ====================

def fetch_a_shares():
    """通过腾讯行情接口获取A股/指数实时数据"""
    print("[1] 采集A股/指数行情...")
    all_codes = []
    for item in WATCHLIST + INDICES:
        c = item["code"]
        if c.startswith("sh") or c.startswith("sz"):
            all_codes.append(c)

    url = f"http://qt.gtimg.cn/q={','.join(all_codes)}"
    raw = fetch_url(url)
    if not raw:
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
        high = safe_float(fields[33]) if len(fields) > 33 else None
        low = safe_float(fields[34]) if len(fields) > 34 else None
        change_pct = safe_float(fields[32]) if fields[32] else (
            ((close - pre_close) / pre_close * 100) if close and pre_close else None
        )
        volume = safe_float(fields[36])  # 手
        turnover_amount = safe_float(fields[37])  # 万元
        # 换手率字段（部分股票有）
        turnover_rate = safe_float(fields[38]) if len(fields) > 38 else None

        results[code] = {
            "name": name,
            "close": close,
            "pre_close": pre_close,
            "high": high,
            "low": low,
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
            "volume": volume,
            "turnover_amount": turnover_amount,
            "turnover_rate": turnover_rate,
        }
    print(f"  获取 {len(results)} 条行情数据")
    return results


def fetch_kline_tencent(code, days=35):
    """通过腾讯财经获取日K线数据（前复权）"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq"
    raw = fetch_url(url, timeout=12)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        # 腾讯K线数据路径: data -> {code} -> day/qfqday
        stock_key = code
        day_data = None
        if "data" in data and stock_key in data["data"]:
            stock_data = data["data"][stock_key]
            # 优先用前复权数据
            day_data = stock_data.get("qfqday") or stock_data.get("day")
        if not day_data:
            return None

        klines = []
        for item in day_data:
            if len(item) >= 6:
                klines.append({
                    "date": item[0],
                    "open": safe_float(item[1]),
                    "close": safe_float(item[2]),
                    "high": safe_float(item[3]),
                    "low": safe_float(item[4]),
                    "volume": safe_float(item[5]),
                })
        return klines
    except Exception as e:
        print(f"  [WARN] kline parse {code}: {e}")
        return None


def fetch_all_klines(codes, days=35):
    """批量获取K线数据"""
    print(f"[1.1] 采集K线数据 ({len(codes)}只)...")
    all_klines = {}
    for i, code in enumerate(codes):
        klines = fetch_kline_tencent(code, days)
        if klines:
            all_klines[code] = klines
            print(f"  {code}: {len(klines)} 条K线")
        else:
            print(f"  {code}: K线获取失败")
        if i < len(codes) - 1:
            time.sleep(0.3)
    print(f"  K线获取完成: {len(all_klines)}/{len(codes)} 成功")
    return all_klines


# ==================== 技术指标计算 ====================

def calc_ema(values, period):
    """计算EMA"""
    if not values or len(values) < period:
        return []
    multiplier = 2.0 / (period + 1)
    ema = [sum(values[:period]) / period]  # 初始SMA
    for i in range(period, len(values)):
        ema.append(values[i] * multiplier + ema[-1] * (1 - multiplier))
    return ema


def calc_macd(closes, short=12, long=26, signal=9):
    """
    计算MACD指标
    返回: (dif, dea, macd_bar) 最新值，以及前一日值 (dif_prev, dea_prev, macd_bar_prev)
    """
    if not closes or len(closes) < long + signal:
        return None, None, None, None, None, None

    ema_short = calc_ema(closes, short)
    ema_long = calc_ema(closes, long)

    # DIF = EMA_short - EMA_long（对齐长度）
    offset = len(ema_short) - len(ema_long)
    dif_list = [ema_short[offset + i] - ema_long[i] for i in range(len(ema_long))]

    if len(dif_list) < signal:
        return None, None, None, None, None, None

    # DEA = EMA(DIF, signal)
    dea_list = calc_ema(dif_list, signal)

    # 对齐DIF和DEA
    dif_offset = len(dif_list) - len(dea_list)
    dif_aligned = dif_list[dif_offset:]

    if len(dif_aligned) < 2 or len(dea_list) < 2:
        return None, None, None, None, None, None

    # 最新值
    dif = round(dif_aligned[-1], 4)
    dea = round(dea_list[-1], 4)
    macd_bar = round(2 * (dif - dea), 4)

    # 前一日值
    dif_prev = round(dif_aligned[-2], 4)
    dea_prev = round(dea_list[-2], 4)
    macd_bar_prev = round(2 * (dif_prev - dea_prev), 4)

    return dif, dea, macd_bar, dif_prev, dea_prev, macd_bar_prev


def determine_macd_signal(dif, dea, macd_bar, dif_prev, dea_prev, macd_bar_prev):
    """
    判定MACD信号状态
    返回: 金叉/金叉增强/零轴企稳/企稳初期/死叉/死叉恶化/未知
    """
    if any(v is None for v in [dif, dea, macd_bar, dif_prev, dea_prev, macd_bar_prev]):
        return "未知"

    # 判断金叉/死叉
    prev_spread = dif_prev - dea_prev
    curr_spread = dif - dea

    is_golden_cross = prev_spread <= 0 and curr_spread > 0
    is_death_cross = prev_spread >= 0 and curr_spread < 0

    if is_golden_cross:
        # 金叉 + 放量确认（MACD柱明显增大）
        if macd_bar > macd_bar_prev * 1.2 and macd_bar > 0:
            return "金叉增强"
        return "金叉"

    if is_death_cross:
        return "死叉"

    # 非交叉状态
    if curr_spread < 0:
        # DIF在DEA下方（空头区域）
        if macd_bar > macd_bar_prev:
            # 绿柱缩短，企稳迹象
            if abs(dif) < 0.3:  # 接近零轴
                return "零轴企稳"
            return "企稳初期"
        else:
            # 绿柱扩大或持平
            if abs(macd_bar) > abs(macd_bar_prev) * 1.1:
                return "死叉恶化"
            return "死叉"
    else:
        # DIF在DEA上方（多头区域）
        if macd_bar > macd_bar_prev:
            if abs(dif) < 0.3:
                return "零轴企稳"
            return "金叉增强"
        else:
            return "金叉"


def calc_ten_day_change(klines):
    """计算10日涨跌幅"""
    if not klines or len(klines) < 11:
        return None, None, None
    today_close = klines[-1]["close"]
    ref = klines[-11]  # 10个交易日前
    ref_close = ref["close"]
    ref_date = ref["date"]
    if ref_close and ref_close > 0:
        chg = round((today_close - ref_close) / ref_close * 100, 2)
        return chg, ref_close, ref_date
    return None, None, None


def calc_decline_from_high(klines, window=20):
    """计算距近N日高点的跌幅"""
    if not klines:
        return None
    recent = klines[-window:] if len(klines) >= window else klines
    highs = [k.get("high") or k["close"] for k in recent]
    max_high = max(highs) if highs else None
    if max_high and max_high > 0:
        current = klines[-1]["close"]
        return round((current - max_high) / max_high * 100, 2)
    return None


def calc_oversold_score(ten_day_chg, change_pct):
    """超跌评分 0-100"""
    if ten_day_chg is None:
        return 50  # 无数据时中性

    if ten_day_chg <= -15:
        score = 95
    elif ten_day_chg <= -10:
        score = 75
    elif ten_day_chg <= -7:
        score = 60
    elif ten_day_chg <= -5:
        score = 45
    elif ten_day_chg <= -3:
        score = 30
    elif ten_day_chg <= 0:
        score = 20
    elif ten_day_chg <= 3:
        score = 10
    elif ten_day_chg <= 7:
        score = 5
    else:
        score = 0

    # 当日涨跌修正
    if change_pct is not None:
        if change_pct > 4:
            score = max(0, score - 15)
        elif change_pct < -4:
            score = min(100, score + 15)

    return score


def calc_trend_score(klines, macd_signal, dif, dea):
    """趋势评分 0-100"""
    score = 50  # 基础分

    if not klines or len(klines) < 5:
        return score

    # 近5日涨跌
    closes = [k["close"] for k in klines[-5:] if k.get("close")]
    if len(closes) >= 2:
        short_trend = (closes[-1] - closes[0]) / closes[0] * 100
        if short_trend > 5:
            score += 20
        elif short_trend > 2:
            score += 10
        elif short_trend < -5:
            score -= 20
        elif short_trend < -2:
            score -= 10

    # MACD方向修正
    if macd_signal in ("金叉", "金叉增强"):
        score += 15
    elif macd_signal in ("死叉", "死叉恶化"):
        score -= 15
    elif macd_signal == "零轴企稳":
        score += 5
    elif macd_signal == "企稳初期":
        score += 3

    # DIF/DEA位置修正
    if dif is not None and dea is not None:
        if dif > 0 and dea > 0:
            score += 5  # 多头区域
        elif dif < 0 and dea < 0:
            score -= 5  # 空头区域

    return max(0, min(100, score))


def determine_level(decline_pct, macd_signal, oversold_score):
    """操作级别判定"""
    # MACD否决条件
    if macd_signal in ("死叉恶化",) and (decline_pct is None or decline_pct > -10):
        return "MACD否决"

    # 超跌标准仓
    if decline_pct is not None and decline_pct <= -10 and macd_signal not in ("死叉恶化",):
        return "标准仓"

    # 轻仓试
    if decline_pct is not None and -10 < decline_pct <= -7:
        if macd_signal not in ("死叉恶化",):
            return "轻仓试"

    # 观察池
    if oversold_score >= 30 or macd_signal in ("金叉", "金叉增强", "零轴企稳", "企稳初期"):
        return "观察池"

    return "暂不关注"


# ==================== 宏观数据采集（保持v2.0逻辑） ====================

def fetch_us_market():
    """获取美股收盘数据"""
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
    """获取VIX指数"""
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
        return safe_float(last[4])
    return None


def fetch_a50():
    """获取富时中国A50期货"""
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
    """获取融资余额"""
    print("[6] 采集融资余额...")
    url = "https://data.10jqka.com.cn/market/rzrqgg/"
    raw = fetch_url(url, timeout=10)
    if not raw:
        return None
    try:
        m = re.search(r'融资余额[：:]\s*([\d,.]+)', raw)
        if m:
            val = m.group(1).replace(",", "")
            return safe_float(val)
    except Exception:
        pass
    return None


def fetch_commodities():
    """获取大宗商品"""
    print("[7] 采集大宗商品...")
    tickers = {
        "hf_CL": {"name": "wti_crude"},
        "hf_GC": {"name": "gold"},
        "hf_HG": {"name": "copper"},
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
    """获取板块涨跌排行"""
    print("[8] 采集板块数据...")
    sector_url = "http://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
    sector_raw = fetch_url(sector_url, timeout=8)
    if not sector_raw:
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
            "limit_up_count": None, "limit_down_count": None,
            "total_turnover_billion": None, "turnover_change_pct": None,
            "advance_count": None, "decline_count": None,
        }
    total_turnover = 0
    for code, info in a_data.items():
        if code in ("sh000001", "sz399001"):
            if info.get("turnover_amount"):
                total_turnover += info["turnover_amount"]
    return {
        "limit_up_count": None, "limit_down_count": None,
        "total_turnover_billion": round(total_turnover / 10000, 1) if total_turnover else None,
        "turnover_change_pct": None,
        "advance_count": None, "decline_count": None,
    }


# ==================== stock_data 生成 ====================

def generate_stock_data(trade_date, a_data, all_klines):
    """
    生成 stock_data_YYYYMMDD.json 格式数据
    供 daily_forecast.py 直接消费
    """
    print("[10] 生成 stock_data...")
    date_str = trade_date.strftime("%Y-%m-%d")
    date_compact = trade_date.strftime("%Y%m%d")

    # 下一个交易日作为 report_date
    report_date = trade_date + timedelta(days=1)
    while report_date.weekday() >= 5:  # 跳过周末
        report_date += timedelta(days=1)

    # 构建上证指数数据
    index_data = {}
    sh_index = a_data.get("sh000001", {}) if a_data else {}
    sh_klines = all_klines.get("sh000001", [])

    ten_day_chg_idx, ref_close_idx, ref_date_idx = calc_ten_day_change(sh_klines)

    index_data = {
        "code": "sh000001",
        "name": "上证指数",
        "close": sh_index.get("close"),
        "change_percent": sh_index.get("change_pct"),
        "ten_day_change": ten_day_chg_idx,
    }

    # 构建个股数据
    stocks_list = []
    for item in WATCHLIST:
        code = item["code"]
        name = item["name"]

        # 从实时行情获取基础数据
        quote = a_data.get(code, {}) if a_data else {}
        close = quote.get("close")
        change_pct = quote.get("change_pct")
        pre_close = quote.get("pre_close")
        high = quote.get("high")
        low = quote.get("low")
        turnover_rate = quote.get("turnover_rate")

        # 从K线数据计算技术指标
        klines = all_klines.get(code, [])

        # MACD
        closes = [k["close"] for k in klines if k.get("close")] if klines else []
        dif, dea, macd_bar, dif_prev, dea_prev, macd_bar_prev = calc_macd(closes)
        macd_signal = determine_macd_signal(dif, dea, macd_bar, dif_prev, dea_prev, macd_bar_prev)

        # 10日跌幅
        ten_day_chg, ref_close, ref_date = calc_ten_day_change(klines)

        # 距高点跌幅
        decline_pct = calc_decline_from_high(klines, window=20)

        # 评分
        oversold_score = calc_oversold_score(ten_day_chg, change_pct)
        trend_score = calc_trend_score(klines, macd_signal, dif, dea)
        level = determine_level(decline_pct, macd_signal, oversold_score)

        stock_entry = {
            "code": code,
            "name": name,
            "close": close,
            "change_pct": change_pct,
            "prev_close": pre_close,
            "turnover": turnover_rate,
            "high": high,
            "low": low,
            "decline_pct": decline_pct,
            "ref_date": ref_date,
            "ref_close": ref_close,
            "dif": dif,
            "dea": dea,
            "macd": macd_bar,
            "macd_signal": macd_signal,
            "roe_ttm": None,  # v2.2 再加
            "oversold_score": oversold_score,
            "trend_score": trend_score,
            "level": level,
        }
        stocks_list.append(stock_entry)

    # 组装完整输出
    stock_data = {
        "date": date_str,
        "report_date": report_date.strftime("%Y-%m-%d"),
        "schema_version": "2.1",
        "index": index_data,
        "stocks": stocks_list,
        "conclusion": "待预测引擎生成",
        "reason": "数据由采集脚本生成，预测结论由daily_forecast.py生成",
    }

    print(f"  生成 {len(stocks_list)} 只标的数据")
    has_macd = sum(1 for s in stocks_list if s["dif"] is not None)
    print(f"  MACD可用: {has_macd}/{len(stocks_list)}")
    has_tenday = sum(1 for s in stocks_list if s["ten_day_chg"] is not None)
    print(f"  10日跌幅可用: {has_tenday}/{len(stocks_list)}")

    return stock_data


# ==================== manifest 管理 ====================

def update_manifest(date_str, quality_overall, sources_status, stock_data_filename=None):
    """更新 manifest.json"""
    manifest_path = os.path.join(OUTPUT_DIR, "manifest.json")
    manifest = {"schema_version": "2.0", "entries": []}

    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
        except Exception:
            pass

    entry = {
        "date": date_str,
        "filename": f"{OUTPUT_DIR}/daily-finance-{date_str}.json",
        "raw_url": f"https://raw.githubusercontent.com/livsstock/xgl/data/daily/{OUTPUT_DIR}/daily-finance-{date_str}.json",
        "quality": quality_overall,
        "updated_at": datetime.now(TZ_CST).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "sources": sources_status,
    }
    if stock_data_filename:
        entry["stock_data_filename"] = stock_data_filename
        entry["stock_data_raw_url"] = f"https://raw.githubusercontent.com/livsstock/xgl/data/daily/{OUTPUT_DIR}/{stock_data_filename}"

    # 保留最近30天
    manifest["entries"] = [e for e in manifest.get("entries", []) if e.get("date") != date_str]
    manifest["entries"].append(entry)
    manifest["entries"] = sorted(manifest["entries"], key=lambda x: x.get("date", ""))[-30:]
    manifest["latest_date"] = manifest["entries"][-1]["date"] if manifest["entries"] else ""
    manifest["latest_file"] = manifest["entries"][-1].get("filename", "") if manifest["entries"] else ""

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"[OK] 更新 manifest.json (最新: {manifest['latest_date']})")


# ==================== 主流程 ====================

def main():
    trade_date = get_trade_date()
    date_str = trade_date.strftime("%Y-%m-%d")
    date_compact = trade_date.strftime("%Y%m%d")
    print(f"=== LIVS-Stock 每日采集 v2.1 {date_str} ===")
    print(f"采集时间: {datetime.now(TZ_CST).strftime('%Y-%m-%d %H:%M:%S')} CST\n")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # === 第一阶段：宏观数据采集 ===
    a_data = fetch_a_shares()
    us_data = fetch_us_market()
    vix = fetch_vix()
    a50 = fetch_a50()
    usd_cny = fetch_usd_cny()
    margin = fetch_margin_balance()
    commodities = fetch_commodities()
    sector = fetch_sector_data()
    sentiment = fetch_market_sentiment(a_data)

    # === 第二阶段：K线数据与技术指标 ===
    stock_codes = [item["code"] for item in WATCHLIST]
    index_codes = [item["code"] for item in INDICES]
    all_kline_codes = stock_codes + index_codes
    all_klines = fetch_all_klines(all_kline_codes, days=35)

    # === 第三阶段：数据质量评估 ===
    sources_status = {
        "tencent_quotes": "ok" if a_data else "failed",
        "sina_fx_futures": "ok" if (us_data or a50) else "failed",
        "cboe_vix": "ok" if vix else "failed",
        "a50_futures": "ok" if a50 else "failed",
        "usd_cny": "ok" if usd_cny else "failed",
        "ths_margin": "ok" if margin else "failed",
        "sina_sectors": "ok" if sector.get("top5") else "failed",
        "kline_data": "ok" if len(all_klines) >= len(stock_codes) * 0.8 else "partial",
    }

    # 判断整体质量
    ok_count = sum(1 for v in sources_status.values() if v == "ok")
    total_count = len(sources_status)
    if ok_count >= total_count - 1:
        quality_overall = "complete"
    elif ok_count >= total_count // 2:
        quality_overall = "partial"
    else:
        quality_overall = "failed"

    # A股行情数据失败才算整体failed
    if not a_data:
        quality_overall = "failed"

    # === 第四阶段：组装 daily-finance 输出 ===
    finance_output = {
        "schema_version": "2.1",
        "date": date_str,
        "collector": "LIVS-Stock-WorkBuddy/2.1",
        "generated_at": datetime.now(TZ_CST).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "data_quality": {
            "overall": quality_overall,
            "sources": sources_status,
        },
        "data_notes": [
            "自动采集：腾讯财经+新浪+同花顺+CBOE公开接口",
            "北向资金：东财2024/8起停披，填null",
            "v2.1: 新增个股技术指标计算",
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
                finance_output["eod_quotes"]["indices"][item["name"]] = {
                    "close": d["close"],
                    "change_pct": d["change_pct"],
                }

        # 填充标的池
        for item in WATCHLIST:
            code = item["code"]
            if code in a_data:
                d = a_data[code]
                finance_output["eod_quotes"]["watchlist"].append({
                    "code": code[2:],
                    "name": d["name"],
                    "close": d["close"],
                    "change_pct": d["change_pct"],
                })

    # === 第五阶段：生成 stock_data 输出 ===
    stock_data_output = generate_stock_data(trade_date, a_data, all_klines)
    stock_data_filename = f"stock_data_{date_compact}.json"

    # === 第六阶段：写入文件 ===
    finance_filename = f"daily-finance-{date_str}.json"

    finance_path = os.path.join(OUTPUT_DIR, finance_filename)
    with open(finance_path, "w", encoding="utf-8") as f:
        json.dump(finance_output, f, ensure_ascii=False, indent=2)
    print(f"\n[OK] 写入 {finance_path}")

    stock_data_path = os.path.join(OUTPUT_DIR, stock_data_filename)
    with open(stock_data_path, "w", encoding="utf-8") as f:
        json.dump(stock_data_output, f, ensure_ascii=False, indent=2)
    print(f"[OK] 写入 {stock_data_path}")

    # 兼容旧路径
    stock_compat_path = "stock.josn"
    with open(stock_compat_path, "w", encoding="utf-8") as f:
        json.dump(finance_output, f, ensure_ascii=False, indent=2)
    print(f"[OK] 写入 {stock_compat_path} (兼容)")

    # 更新 manifest
    update_manifest(date_str, quality_overall, sources_status, stock_data_filename)

    # === 摘要 ===
    print(f"\n=== 采集摘要 v2.1 ===")
    print(f"日期: {date_str}")
    print(f"数据质量: {quality_overall}")
    for k, v in sources_status.items():
        status_icon = "✅" if v == "ok" else "⚠️"
        print(f"  {status_icon} {k}: {v}")
    print(f"\n产出文件:")
    print(f"  {finance_filename} ({os.path.getsize(finance_path)} bytes)")
    print(f"  {stock_data_filename} ({os.path.getsize(stock_data_path)} bytes)")
    print(f"\n=== 采集完成 ===")


if __name__ == "__main__":
    main()
