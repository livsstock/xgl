#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_predict.py — WorkBuddy 每日预测自动化（v1.0，2026-09-24 上线）

目的：解决"预测机制断了"的根本问题 —— 以前预测文件靠手工生成，漏 9/23。
      现在每天 11:30 / 14:00 / 16:00 三个时点自动跑，输出与 9/22 手工文件
      schema 完全对齐的 predictions/{date}.json。

触发方式（三选一）：
  - 手动：python3 daily_predict.py                          # 跑今天的预测
  - 手动：python3 daily_predict.py --date 2026-09-23        # 补跑某天
  - cron：见 docs/自动化部署说明.md（每日 11:30/14:00/16:00）

输出：
  - predictions/{YYYY-MM-DD}.json  主预测文件
  - predictions/{YYYY-MM-DD}_{HHMM}.json  时点快照（可选，--config 决定）
  - logs/daily_predict-{YYYY-MM-DD}.log  运行日志

预测逻辑（与 9/22 手写 schema 完全对齐）：
  1. 大盘环境（M_sse v2.5）：15日涨跌30% + MA20偏离20% + 板块宽度30% + Kuramoto R_value20%
  2. 大盘方向预测：用 M_sse 阈值判定（≥60 偏多, 40-60 中性, <40 偏空）
  3. 个股层：26 只观察池全覆盖 + 核心池 6 只重点判定
  4. 入场信号：量比 v2（动态阈值）/MACD 金叉/RSI6 v2（环境自适应）

依赖：仅用 urllib + 标准库（沙箱环境无 requests 依赖时也能跑）

设计原则：
  - 离线优先：单只股票失败不影响其他
  - 日志完整：每个数据点是否取到都打日志
  - 失败兜底：网络全挂时输出带 _partial=true 的骨架，方便对账识别
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Any

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = BASE_DIR
sys.path.insert(0, PROJECT_DIR)

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com/"}
TIMEOUT = 8
TODAY_STR = datetime.now().strftime("%Y-%m-%d")

# 阈值常量（v3 系列 —— 2026-10-20 按回测结果收紧）
# ---------------------------------------------------------------
# [依据] backtest/signal_stats.json（28标的 / 65,249根K线 / 近10年 / BH-FDR α=0.05）
#   S2 RSI6<15   T+5 58.9% / +1.05%（n=1209，FDR 通过）← 唯一强信号
#   S1 RSI6<25   T+5 55.2% / +0.54%（n=5572，FDR 通过）
#   S5 10日跌>10% T+5 56.0% / +0.87%（n=3078，FDR 通过）
#   S3 MACD金叉   T+5 50.0%（n=7737）→ 等于随机，只能作否决不能作入场
#   S6 量比≥1.5   T+5 50.4%（n=6811）→ 无效，降级为辅助
# v2 的 RSI6<30（中性档）落在 25~30 之间，回测边缘极薄，v3 全部收紧到 25/15。
RSI6_BUY_STRONG = 15   # 主买入（S2，胜率 58.9%）
RSI6_BUY_NORMAL = 25   # 试探买入（S1，胜率 55.2%）
RSI6_WATCH_ONLY = 30   # 仅观察，不给买入
VOL_RATIO_V2_BASE = 1.3
VOL_RATIO_V2_AMP_MULT = 5  # 阈值 = max(1.3, 大盘振幅% × 5)
RSI6_V2_BULL = 25   # 保留兼容（环境宽松不再放宽阈值，宽松本身就是降低胜率）
RSI6_V2_NEUT = 25
RSI6_V2_BEAR = 25
MSSE_BULL = 60
MSSE_BEAR = 40
MSSE_VERY_BEAR = 35  # 14:00 扫描用

# [2026-10-20 新增] 观察池名 → 事实层名。
# 事实层（facts_builder.UNIVERSE）用短名，观察池用全名，不做映射会静默
# fallback 到自算指标，造成 RSI 与事实层不一致（半导体 17.90 vs 7.91）。
FACTS_ALIAS = {
    "半导体ETF国联安": "半导体ETF",
    "沪深300ETF": "沪深300ETF",
    "中证500ETF": "中证500ETF",
    "创业板ETF": "创业板ETF",
    "黄金ETF": "黄金ETF",
    "机器人ETF": "机器人ETF",
}

# 仅在别 modules 未定义时兜底
NAME_ALIAS = FACTS_ALIAS

# 26 只观察池（与 config/watchlist.json 同步）
WATCHLIST = [
    ("601398", "sh601398", "工商银行"),
    ("601288", "sh601288", "农业银行"),
    ("601012", "sh601012", "隆基绿能"),
    ("000858", "sz000858", "五粮液"),
    ("600519", "sh600519", "贵州茅台"),
    ("601899", "sh601899", "紫金矿业"),
    ("603799", "sh603799", "华友钴业"),
    ("601088", "sh601088", "中国神华"),
    ("002142", "sz002142", "宁波银行"),
    ("510300", "sh510300", "沪深300ETF"),
    ("510500", "sh510500", "中证500ETF"),
    ("159915", "sz159915", "创业板ETF"),
    ("600019", "sh600019", "宝钢股份"),
    ("600893", "sh600893", "航发动力"),
    ("000960", "sz000960", "锡业股份"),
    ("002155", "sz002155", "湖南黄金"),
    ("601212", "sh601212", "白银有色"),
    ("562500", "sh562500", "机器人ETF"),
    ("518880", "sh518880", "黄金ETF"),
    ("512480", "sh512480", "半导体ETF国联安"),
    ("601958", "sh601958", "金钼股份"),
    ("000725", "sz000725", "京东方A"),
    ("600036", "sh600036", "招商银行"),
    ("600111", "sh600111", "北方稀土"),
    ("600875", "sh600875", "东方电气"),
    ("600392", "sh600392", "盛和资源"),
]
NAME_TO_TENCENT = {n: t for _, t, n in WATCHLIST}
NAME_TO_CODE = {n: c for c, _, n in WATCHLIST}

# 核心池 v2.2：4 超跌 + 2 长效
CORE_POOL = {
    "oversold_rebound": ["紫金矿业", "北方稀土", "华友钴业", "金钼股份"],
    "slow_bull": ["半导体ETF国联安", "五粮液"],
}

# 板块映射（板块宽度计算用，简单按业务归类）
SECTOR_MAP = {
    "资源周期": ["紫金矿业", "北方稀土", "华友钴业", "金钼股份", "湖南黄金",
              "锡业股份", "白银有色", "黄金ETF", "中国神华", "宝钢股份"],
    "新能源": ["隆基绿能", "机器人ETF"],
    "防御": ["工商银行", "农业银行", "招商银行", "宁波银行", "五粮液", "贵州茅台"],
    "成长科技": ["半导体ETF国联安", "京东方A", "东方电气", "航发动力"],
    "宽基": ["沪深300ETF", "中证500ETF", "创业板ETF"],
    "稀土": ["北方稀土", "盛和资源"],
}


def http_get(url: str, encoding: str = "utf-8") -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read().decode(encoding, errors="replace")
    except (urllib.error.URLError, TimeoutError, Exception) as e:
        return None


def log(msg: str, log_path: str = None):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def qt_quote(tencent_code: str) -> Optional[Dict]:
    """腾讯实时盘口。返回 dict 或 None。"""
    raw = http_get(f"https://qt.gtimg.cn/q={tencent_code}", encoding="gbk")
    if not raw:
        return None
    m = re.search(r'v_\w+="([^"]*)"', raw)
    if not m:
        return None
    p = m.group(1).split("~")
    if len(p) < 40:
        return None
    try:
        return {
            "name": p[1],
            "price": float(p[3]),
            "prev_close": float(p[4]),
            "open": float(p[5]),
            "volume_hand": float(p[6]) if p[6] else 0.0,
            "change_pct": float(p[32]) if p[32] else 0.0,
            "high": float(p[33]) if p[33] else 0.0,
            "low": float(p[34]) if p[34] else 0.0,
            "timestamp": p[30],
        }
    except (ValueError, IndexError):
        return None


def fetch_kline(tencent_code: str, days: int = 70) -> List[Dict]:
    """腾讯 qfq 日K。盘中最后一根为今日实时bar。"""
    url = (f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
           f"?param={tencent_code},day,,,{days},qfq")
    raw = http_get(url)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    node = data.get("data", {}).get(tencent_code, {})
    k = node.get("qfqday") or node.get("day") or []
    out = []
    for r in k:
        if len(r) < 6:
            continue
        try:
            out.append({
                "date": r[0],
                "open": float(r[1]),
                "close": float(r[2]),
                "high": float(r[3]),
                "low": float(r[4]),
                "volume": float(r[5]),
            })
        except (ValueError, IndexError):
            continue
    return out


def calc_ma(closes: List[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def calc_rsi(closes: List[float], n: int = 6) -> Optional[float]:
    """Wilder RSI（真实递归平滑版）。

    ⚠️ 2026-09-29 修复：此前实现只取最近 n 期做简单平均，虽注释写"Wilder RSI"
    但并非 Wilder 算法，导致 RSI6 系统性偏低。
    实例：紫金矿业 9/29 简单平均=7.5，真实 Wilder=20.0，相差 13 点，
    直接跨越 30 阈值线两侧，造成虚假 buy 信号。

    Wilder 算法：首个均值用简单平均，此后递归：
        avg = (prev_avg * (n - 1) + current) / n
    这保留了超卖的累积性，不会因窗口内出现一根小阳线就跳到高位。
    """
    if len(closes) < n + 1:
        return None

    # 至少需要 n+1 个点算首个均值
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    # 首个均值：前 n 期的简单平均
    avg_gain = sum(max(d, 0) for d in diffs[:n]) / n
    avg_loss = sum(max(-d, 0) for d in diffs[:n]) / n

    # Wilder 递归平滑后续所有期
    for d in diffs[n:]:
        avg_gain = (avg_gain * (n - 1) + max(d, 0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-d, 0)) / n

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_macd(closes: List[float]) -> Tuple[str, Optional[float], Optional[float]]:
    """返回 (state, dif, dea)。state ∈ {golden_cross, death_cross, above, below}。

    口径 A（事件）：DIF 今日上穿 DEA → golden_cross；
             DIF 今日下穿 DEA → death_cross。
    口径 B（状态）：当前 DIF > DEA → above；否则 below。
    """
    if len(closes) < 35:
        return "below", None, None
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    if not ema12 or not ema26:
        return "below", None, None
    dif_series = [a - b for a, b in zip(ema12[-len(ema26):], ema26)]
    dea_series = _ema(dif_series, 9)
    if not dea_series or len(dea_series) < 2:
        return "below", None, None
    dif_today, dif_yest = dif_series[-1], dif_series[-2]
    dea_today, dea_yest = dea_series[-1], dea_series[-2]
    state = "above" if dif_today > dea_today else "below"
    if dif_yest <= dea_yest and dif_today > dea_today:
        state = "golden_cross"
    elif dif_yest >= dea_yest and dif_today < dea_today:
        state = "death_cross"
    return state, round(dif_today, 4), round(dea_today, 4)


def _ema(series: List[float], n: int) -> List[float]:
    if len(series) < n:
        return []
    k = 2 / (n + 1)
    out = [sum(series[:n]) / n]
    for x in series[n:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def calc_volume_ratio(klines: List[Dict]) -> Tuple[Optional[float], Optional[float]]:
    """返回 (vol_ratio, v2_threshold)。
    vol_ratio = 今日累计成交量 / (近5日均量 × 当日已交易时长占比)
    v2_threshold = max(1.3, 大盘振幅% × 5) — 注：本函数只用 vol_ratio，振幅在调用方传
    """
    if len(klines) < 6:
        return None, None
    today = klines[-1]
    last5 = klines[-6:-1]
    if not last5:
        return None, None
    avg5 = sum(k["volume"] for k in last5) / len(last5)
    if avg5 <= 0:
        return None, None
    # 时间归一化
    elapsed = elapsed_trade_fraction()
    if elapsed <= 0:
        return None, None
    expected_now = avg5 * elapsed
    if expected_now <= 0:
        return None, None
    return round(today["volume"] / expected_now, 2), None


def elapsed_trade_fraction(now: Optional[datetime] = None) -> float:
    """当前时刻占全天交易时长比例（9:30-11:30 + 13:00-15:00 = 240min）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return 0.0
    t = now.hour * 60 + now.minute
    morning = max(0, min(t, 11 * 60 + 30) - 9 * 60 - 30)
    afternoon = max(0, min(t, 15 * 60) - 13 * 60)
    elapsed = morning + afternoon
    return max(0.01, min(elapsed / 240.0, 1.0))


def stock_signal(
    name: str,
    code: str,
    tencent: str,
    market_amp_pct: float,
    m_sse: float,
    log_path: str,
    facts: Optional[Dict] = None,
) -> Optional[Dict]:
    """单只股票信号生成。返回 dict 或 None（数据全失败）。

    [2026-10-20 重构] 优先级：facts 事实层 > 自行拉K线计算。
    事实层存在时，所有指标直接读 facts（与大海同源、同时点），
    不再自己算——这是"事实唯一"架构的落地。
    """
    # ---------- 优先读事实层 ----------
    if facts:
        f = facts.get(name)
        if not f:
            # [2026-10-20 修复] 观察池名与事实层名不一致时必须别名回退，
            # 否则会静默 fallback 到自算指标（半导体ETF国联安 RSI 17.90 vs 事实层 7.91），
            # 这正是"同一份 facts"没落地的典型漏洞。
            alt = FACTS_ALIAS.get(name) or NAME_ALIAS.get(name)
            if alt:
                f = facts.get(alt)
        if f:
            rsi6 = f.get("rsi6")
            macd_state = (f.get("macd") or {}).get("state")
            ma20_dev = f.get("ma20_dev")
            vol_ratio = f.get("vol_ratio")
            dd30 = f.get("dd30")
            today_price = f.get("close")
            today_chg = f.get("chg_pct") or 0.0
            ten_d = f.get("ten_day_chg")
            return _decide(name, code, today_price, today_chg, ma20_dev,
                           rsi6, macd_state, vol_ratio, dd30,
                           market_amp_pct, m_sse,
                           source="facts", ten_day_chg=ten_d)

    # ---------- 回退：自行拉K线 ----------
    quote = qt_quote(tencent)
    klines = fetch_kline(tencent, 70)
    if not klines:
        return None
    closes = [r["close"] for r in klines]
    today_price = quote["price"] if quote else klines[-1]["close"]
    today_chg = quote["change_pct"] if quote else 0.0

    ma20 = calc_ma(closes, 20)
    ma20_dev = round((today_price / ma20 - 1) * 100, 2) if ma20 else None
    rsi6 = round(calc_rsi(closes, 6), 1) if calc_rsi(closes, 6) else None
    macd_state, dif, dea = calc_macd(closes)
    vol_ratio, _ = calc_volume_ratio(klines)
    dd30 = round((today_price / max(closes[-31:-1]) - 1) * 100, 2) if len(closes) >= 31 else None

    return _decide(name, code, today_price, today_chg, ma20_dev,
                   rsi6, macd_state, vol_ratio, dd30,
                   market_amp_pct, m_sse, source="self", ten_day_chg=None)


def _decide(name, code, today_price, today_chg, ma20_dev, rsi6,
            macd_state, vol_ratio, dd30, market_amp_pct, m_sse,
            source="self", ten_day_chg=None):
    """信号判定逻辑（数据来源无关，facts 与自算共用同一套规则）。

    [v3.0 2026-10-20 按回测重构 —— 与大海 10/20 对齐]
    回测（28标的/65,249根/近10年/BH-FDR α=0.05）给出的硬结论：
      · 唯一强信号是 RSI6<15（T+5 58.9%）与 RSI6<25（55.2%）、10日跌>10%（56.0%）
      · MACD金叉 T+5 仅 50.0%、量比≥1.5 仅 50.4% —— 都是随机，不能当入场券
    所以：
      · RSI 分档：<15 → buy（强）；15~25 → buy_probe（试探）；25~30 → watch；>30 不触发
      · MACD金叉 / 量比：只作"加分项/否决项"，单独出现不给买入
      · 大盘环境不再放宽 RSI 阈值（放宽 = 稀释胜率），只影响方向与置信度
    """

    # 动态量比阈值（仅作辅助打分，不再是入场门票）
    vol_threshold = round(max(VOL_RATIO_V2_BASE, market_amp_pct * VOL_RATIO_V2_AMP_MULT), 2)

    sig_vol = vol_ratio is not None and vol_ratio >= vol_threshold
    sig_macd = macd_state == "golden_cross"
    macd_bad = macd_state in ("death_cross", "below")

    # ---- RSI 分档（唯一有统计效力的入场依据）----
    rsi_strong = rsi6 is not None and rsi6 < RSI6_BUY_STRONG      # <15  S2
    rsi_normal = rsi6 is not None and rsi6 < RSI6_BUY_NORMAL      # <25  S1
    rsi_edge = rsi6 is not None and rsi6 < RSI6_WATCH_ONLY         # <30  边缘，只观察
    sig_rsi = rsi_strong or rsi_normal
    any_signal = sig_rsi or sig_vol or sig_macd

    # 方向判定：基于 chg_today + ma20_dev
    if today_chg > 0 or (ma20_dev is not None and ma20_dev > 0):
        direction = "up"
    elif today_chg < 0 or (ma20_dev is not None and ma20_dev < 0):
        direction = "down"
    else:
        direction = "neutral"

    # ---- action 判定 ----
    is_core = (name in CORE_POOL["oversold_rebound"] or
               name in CORE_POOL["slow_bull"])

    if rsi_strong:
        base_action = "buy"              # S2 强信号
    elif rsi_normal:
        base_action = "buy_probe"        # S1 中等信号 → 试探仓
    elif sig_macd and rsi_edge:
        base_action = "watch"            # 组合 S8 样本仅 68，不足以上仓，只观察
    elif rsi_edge:
        base_action = "watch"            # 25~30 边缘
    elif sig_macd or sig_vol:
        base_action = "watch"            # 只有 MACD/量比 → 观察，不给买
    else:
        base_action = "hold"

    if name in CORE_POOL["oversold_rebound"]:
        track_rule = "短线轨 +5%止盈/-5%止损/10日"
        signal_type = "oversold_rebound"
        action = base_action if base_action in ("buy", "buy_probe") else "watch"
    elif name in CORE_POOL["slow_bull"]:
        track_rule = "长效轨 -15%硬止损/20日"
        signal_type = "slow_bull"
        action = "hold" if base_action in ("hold", "watch") else base_action
    else:
        track_rule = "watchlist 观察"
        signal_type = "watchlist"
        action = base_action

    # ---- 否决层：MACD 空头加速 + 无量，压掉 RSI 边缘信号 ----
    veto = []
    if macd_bad and rsi_edge and not rsi_normal:
        veto.append("RSI仅边缘(<30)且MACD空头，否决买入")
        if action in ("buy", "buy_probe"):
            action = "watch"

    reason_parts = []
    if rsi_strong:
        reason_parts.append(f"RSI6={rsi6}<{RSI6_BUY_STRONG}（T+5胜率58.9%）")
    elif rsi_normal:
        reason_parts.append(f"RSI6={rsi6}<{RSI6_BUY_NORMAL}（T+5胜率55.2%）")
    elif rsi_edge:
        reason_parts.append(f"RSI6={rsi6}<{RSI6_WATCH_ONLY}（边缘，胜率不足，仅观察）")
    if sig_macd:
        reason_parts.append("MACD金叉（胜率50.0%，仅辅助）")
    if sig_vol:
        reason_parts.append(f"量比{vol_ratio}≥{vol_threshold}（胜率50.4%，仅辅助）")
    if not reason_parts:
        reason_parts.append(f"回撤{dd30}%" if dd30 is not None else "无信号")
    reason_parts += veto

    fired = rsi_strong or rsi_normal

    # [2026-10-20] 大海 validate_opinion.py R1 只认 buy/sell/hold/watch。
    # 内部保留 buy_probe（试探仓）细分 → 放在 action_internal；
    # 对外的 action 必须是规范值，否则他明天 14:00 扫描会直接判 R1 失败。
    action_canonical = ("buy" if action.startswith("buy")
                        else "sell" if action == "sell"
                        else "watch" if action == "watch"
                        else "hold")

    return {
        "code": code,
        "name": name,
        "action": action_canonical,
        "action_internal": action,
        "direction": direction,
        "reason": "触发：" + "，".join(reason_parts) if fired else "未触发：" + "，".join(reason_parts),
        "signal_type": signal_type,
        "core_pool": is_core,
        "track_rule": track_rule,
        "_tech": {
            "price": today_price,
            "chg_today": today_chg,
            "ma20_dev": ma20_dev,
            "vol_ratio": vol_ratio,
            "rsi6": rsi6,
            "macd": macd_state,
            "dd30": dd30,
        },
        "_signal_detail": {
            "rsi_strong": rsi_strong, "rsi_normal": rsi_normal, "rsi_edge": rsi_edge,
            "macd_golden": sig_macd, "vol_hit": sig_vol,
            "veto": veto,
            "rule_version": "v3.0-20261020",
        },
        # [2026-10-20] 大海 validate_opinion.py R3 要求：action=buy 必须有 evidence，
        # 且 R4-R7 会用 evidence 里的 close/rsi6/ten_day_chg/macd_signal 与 facts 比对。
        # 我们直接把自己用的指标原样写进去（本来就来自 facts，天然一致）。
        "evidence": {
            "close": today_price,
            "rsi6": rsi6,
            "ten_day_chg": ten_day_chg,
            "macd_signal": macd_state,
            "vol_ratio": vol_ratio,
            "ma20_dev": ma20_dev,
            "source": "facts" if source == "facts" else "self-computed",
        },
        # [2026-10-20] 大海 validate_opinion.py R2 要求 kelly_grade ∈ A/B/C/D 且必须有值。
        # 分级与 export_dahai_format.kelly_for_call 同口径（A=RSI<15, B=RSI<25, C=其它买入, D=非买）。
        "kelly_grade": ("A" if (rsi6 is not None and rsi6 < 15 and macd_state != "death_cross")
                        else "B" if (rsi6 is not None and rsi6 < 25)
                        else "C" if action.startswith("buy")
                        else "D"),
        "_data_source": source,
    }


def calc_market_env() -> Tuple[Dict, float, float, Optional[float]]:
    """大盘环境：上证 000001。
    返回 (env_dict, m_sse_v25, raw_sse_v1, kuramoto_r)。
    M_sse v2.5: 0.3×R_15 + 0.2×R_MA20dev + 0.3×breadth + 0.2×R_kuramoto
    breadth = (上涨家数 / 总家数) - 0.5 范围 [-0.5, 0.5]
    kuramoto_r ≈ 用 26 只 watchlist 同步性近似
    """
    sh_quote = qt_quote("sh000001")
    sh_klines = fetch_kline("sh000001", 70)
    if not sh_klines:
        return {
            "sh_quote_failed": True,
            "M_sse_v1": None,
            "M_sse_v2p5": None,
        }, 50.0, 50.0, None

    closes = [r["close"] for r in sh_klines]
    today = sh_quote["price"] if sh_quote else closes[-1]
    chg_today = sh_quote["change_pct"] if sh_quote else 0.0
    amp_today = 0.0
    if sh_quote and sh_quote["high"] and sh_quote["low"] and sh_quote["prev_close"]:
        amp_today = (sh_quote["high"] - sh_quote["low"]) / sh_quote["prev_close"] * 100
    # 近 5 日均振幅（不含今日）— 用于 vol_ratio v2 阈值，v1 公式原意
    if len(sh_klines) >= 6:
        recent5 = sh_klines[-6:-1]
        amp_5d_avg = sum(
            (k["high"] - k["low"]) / k["open"] * 100 for k in recent5
        ) / len(recent5)
    else:
        amp_5d_avg = amp_today

    # R_15：15日涨跌幅（百分点 → 0-100 标尺）
    r15_raw = (closes[-1] / closes[-16] - 1) * 100 if len(closes) >= 16 else 0.0
    r15 = max(0, min(100, 50 + r15_raw * 5))  # ±10% 映射 0-100

    # R_MA20dev
    ma20 = calc_ma(closes, 20)
    ma20_dev_pct = ((today / ma20 - 1) * 100) if ma20 else 0.0
    r_ma20dev = max(0, min(100, 50 + ma20_dev_pct * 5))

    # breadth：用 26 只观察池近似（上涨家数占比）
    up_count = 0
    valid_count = 0
    phase_data = []
    for code, tencent, name in WATCHLIST:
        q = qt_quote(tencent)
        if q and q["change_pct"] is not None:
            valid_count += 1
            if q["change_pct"] > 0:
                up_count += 1
            phase_data.append(q["change_pct"])
    breadth = ((up_count / valid_count) - 0.5) if valid_count else 0.0
    breadth_score = max(0, min(100, (breadth + 0.5) * 100))

    # Kuramoto R：相位一致性简化（用涨跌幅的相干性近似）
    if phase_data and len(phase_data) >= 5:
        import math
        # 把涨跌幅映射到相位
        phases = [math.atan2(p, 10) for p in phase_data]  # 涨→相位接近 +π/2
        # R = |Σ exp(i·θ)| / N
        cos_sum = sum(math.cos(p) for p in phases)
        sin_sum = sum(math.sin(p) for p in phases)
        r_kura = math.sqrt(cos_sum**2 + sin_sum**2) / len(phases)
    else:
        r_kura = 0.0
    kuramoto_score = max(0, min(100, r_kura * 100))

    m_sse_v25 = (0.3 * r15 + 0.2 * r_ma20dev +
                 0.3 * breadth_score + 0.2 * kuramoto_score)

    # v1（兼容对比）
    m_sse_v1 = 0.6 * r15 + 0.4 * r_ma20dev

    env = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "scope": "上证指数 sh000001",
        "index": {
            "close": round(today, 2),
            "chg_today_pct": round(chg_today, 2),
            "amplitude_pct": round(amp_today, 2),
            "r_15": round(r15_raw, 2),
            "ma20_dev": round(ma20_dev_pct, 2),
            "breadth": round(breadth, 2),
            "r_value_kuramoto": round(r_kura, 2),
        },
        "breadth_detail": {
            "up_count": up_count,
            "valid_count": valid_count,
            "up_ratio": round(up_count / valid_count, 2) if valid_count else None,
        },
        "amplitude_5d_avg_pct": round(amp_5d_avg, 2),
        "M_sse_v1": round(m_sse_v1, 1),
        "M_sse_v2p5": round(m_sse_v25, 1),
        "M_sse_version": "v2.5",
    }

    return env, m_sse_v25, m_sse_v1, r_kura

    return env, m_sse_v25, m_sse_v1, r_kura


def predict_market_direction(m_sse: float, env: Dict) -> Dict:
    """大盘方向预测：基于 M_sse + 当日实时涨跌。"""
    chg = env.get("index", {}).get("chg_today_pct", 0)
    if m_sse >= MSSE_BULL and chg >= 0:
        direction = "up"
        center = 0.5
        rng = [0.1, 1.5]
    elif m_sse >= MSSE_BULL:
        direction = "up"
        center = 0.3
        rng = [-0.3, 1.0]
    elif m_sse >= MSSE_BEAR:
        direction = "neutral"
        center = 0.0
        rng = [-0.5, 0.5]
    elif m_sse >= MSSE_VERY_BEAR:
        direction = "down"
        center = -0.3
        rng = [-1.0, 0.3]
    else:
        direction = "down"
        center = -0.8
        rng = [-1.5, -0.3]

    if chg < -1:
        direction = "down"  # 当日已经大跌，强制偏空
    elif chg > 1.5 and direction == "down":
        direction = "neutral"  # 当日已经大涨，修正

    confidence = round(min(0.85, 0.4 + abs(m_sse - 50) / 100), 2)

    return {
        "direction": direction,
        "range_pct": rng,
        "center_pct": center,
        "close_ref": env.get("index", {}).get("close"),
        "confidence": confidence,
    }


def _normalize_facts_pkg(pkg):
    """[2026-10-20] 事实包双格式归一。
    v1（facts_builder.py）: {"stocks":[{code:"sh601899", close, chg_pct, ...}], "indices":[...],
                            "quality_gate":"PASS", "data_date":...}
    v2（大海 testbuild_facts.py）: {"instruments":{"601899":{code:"601899", change_pct, ...}},
                            "overall_quality":"complete", "market":{...}}
    统一输出：{"stocks":[{统一字段}], "indices":[...], "quality_gate":"PASS/FAIL", ...}
    WB 侧所有指标都从这里取，格式差异不该影响预测逻辑。
    """
    if not isinstance(pkg, dict):
        return None

    # ---- 已是 v1 ----
    if isinstance(pkg.get("stocks"), list) and pkg["stocks"]:
        out = dict(pkg)
        for s in out["stocks"]:
            s.setdefault("chg_pct", s.get("change_pct"))
        for i in out.get("indices") or []:
            i.setdefault("chg_pct", i.get("change_pct"))
        return out

    # ---- v2 ----
    inst = pkg.get("instruments")
    if not isinstance(inst, dict):
        return None
    stocks, indices = [], []
    for _code, s in inst.items():
        item = dict(s)
        item["chg_pct"] = item.get("change_pct",
                                   item.get("chg_pct"))
        item.setdefault("ma20_dev",
                        round((item["close"] / item["ma20"] - 1) * 100, 2)
                        if item.get("ma20") else None)
        item.setdefault("dd30", None)  # v2 未提供，predict 侧容忍 None
        item.setdefault("rsi6", item.get("rsi6"))
        item.setdefault("vol_ratio", item.get("vol_ratio"))
        if item.get("type") == "index":
            indices.append(item)
        else:
            stocks.append(item)
    gate = "PASS" if pkg.get("overall_quality") == "complete" else "FAIL"
    return {
        "date": pkg.get("date"),
        "data_date": pkg.get("date"),
        "generated_at": pkg.get("generated_at"),
        "quality_gate": gate,
        "gate_failures": pkg.get("quality_errors") or [],
        "_is_stale": False,
        "_caliber": pkg.get("indicator_spec"),
        "_schema_version": pkg.get("schema_version", "facts-v2.0"),
        "indices": indices,
        "stocks": stocks,
    }


def load_facts(target_date, allow_t1=False, log_path=None):
    """[2026-10-20 落地] 事实层优先：能读包就读包，读不到才现建。
    返回 (facts_map, facts_meta)：
      facts_map = {name: stock_dict}（含 indices，名称为"上证指数"等）
      facts_meta = {"date":..., "data_date":..., "quality_gate":..., "is_stale":...}
    这一步是"事实唯一"架构的入口——所有个股指标都从这里取，不再自算。
    """
    def _lg(m):
        if log_path:
            log(m, log_path)

    path = os.path.join(PROJECT_DIR, "facts", f"{target_date}.json")
    pkg = None
    if os.path.exists(path):
        try:
            raw = json.load(open(path, encoding="utf-8"))
            pkg = _normalize_facts_pkg(raw)
            if pkg is None:
                _lg("[facts] 已有包格式无法识别，改为现建")
            else:
                _lg(f"[facts] 命中已有事实包 {path} "
                    f"（{raw.get('schema_version', 'facts-v1')} → 归一）")
        except Exception as e:
            _lg(f"[facts] 读取失败({e})，改为现建")
    if pkg is None:
        try:
            import facts_builder
            raw = facts_builder.build(target_date, allow_t1=allow_t1)
            pkg = _normalize_facts_pkg(raw)
            os.makedirs(os.path.join(PROJECT_DIR, "facts"), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
            _lg(f"[facts] 已现建事实包 facts/{target_date}.json "
                f"({raw.get('schema_version', 'facts-v1')})")
        except Exception as e:
            _lg(f"[facts][ERR] 构建事实包失败: {e} —— 将回退到自算指标")
            return {}, {}

    fmap = {}
    for it in (pkg.get("stocks") or []) + (pkg.get("indices") or []):
        fmap[it.get("name")] = it
    meta = {
        "date": pkg.get("date"),
        "data_date": pkg.get("data_date") or pkg.get("date"),
        "quality_gate": pkg.get("quality_gate"),
        "is_stale": bool(pkg.get("_is_stale")),
        "stale_note": pkg.get("_stale_note"),
        "n": len(fmap),
        "caliber": pkg.get("_caliber"),
    }
    _lg(f"[facts] 装载 {meta['n']} 只 | 质量门={meta['quality_gate']} | "
        f"数据日={meta['data_date']} | stale={meta['is_stale']}")
    return fmap, meta


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=TODAY_STR,
                        help="预测日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--config", action="store_true",
                        help="同时生成时点快照 predictions/{date}_{HHMM}.json")
    parser.add_argument("--no-push", dest="no_push", action="store_true",
                        help="只生成不推送（补跑历史日期时用，避免污染远端）")
    parser.add_argument("--allow-t1", dest="allow_t1", action="store_true",
                        help="允许事实包使用最近已收盘日数据（收盘前跑用）")
    args = parser.parse_args()

    target_date = args.date
    log_path = os.path.join(PROJECT_DIR, "logs",
                            f"daily_predict-{target_date}.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    log(f"=== daily_predict.py 启动 target={target_date} ===", log_path)

    # 0. 事实层（唯一数据源）—— 必须在一切计算之前
    log("装载事实层 facts/ ...", log_path)
    facts_map, facts_meta = load_facts(target_date,
                                       allow_t1=args.allow_t1, log_path=log_path)
    use_facts = bool(facts_map) and facts_meta.get("quality_gate") != "FAIL"

    # 1. 大盘环境
    log("计算大盘环境 (M_sse v2.5)...", log_path)
    env, m_sse_v25, m_sse_v1, kura_r = calc_market_env()
    if env.get("sh_quote_failed"):
        log("[WARN] 上证盘口+K线全失败，M_sse 兜底为 50.0（中性）", log_path)
    else:
        log(f"M_sse v2.5 = {m_sse_v25:.1f} (v1 对比 {m_sse_v1:.1f})", log_path)
        log(f"breadth = {env['breadth_detail']}", log_path)

    # 把 m_sse 注入 env 顶层，避免丢字段
    env["_m_sse_used"] = round(m_sse_v25, 1)
    env["_m_sse_v1_compare"] = round(m_sse_v1, 1)
    env["_kuramoto_r"] = round(kura_r, 4) if kura_r is not None else None

    market_amp = env.get("amplitude_5d_avg_pct", env.get("index", {}).get("amplitude_pct", 0.5))
    vol_threshold_global = round(max(VOL_RATIO_V2_BASE, market_amp * VOL_RATIO_V2_AMP_MULT), 2)
    log(f"全局量比阈值 v2 = {vol_threshold_global} (大盘振幅 {market_amp}%)", log_path)

    # 2. 大盘方向
    market_pred = predict_market_direction(m_sse_v25, env)
    log(f"大盘方向预测: {market_pred['direction']} "
        f"(range {market_pred['range_pct']}, center {market_pred['center_pct']}, "
        f"conf {market_pred['confidence']})", log_path)

    # 3. 个股预测（指标一律取自事实层）
    log(f"开始 {len(WATCHLIST)} 只观察池预测... (数据源: "
        f"{'facts 事实层' if use_facts else '自算回退'})", log_path)
    stock_calls = []
    failed = []
    from_facts = 0
    for code, tencent, name in WATCHLIST:
        try:
            r = stock_signal(name, code, tencent, market_amp, m_sse_v25, log_path,
                             facts=facts_map if use_facts else None)
            if r:
                stock_calls.append(r)
                if r.get("_data_source") == "facts":
                    from_facts += 1
            else:
                failed.append(name)
        except Exception as e:
            log(f"[ERR] {name}: {e}", log_path)
            failed.append(name)
        if not use_facts:
            time.sleep(0.05)  # 回退模式下才需要限流（facts 模式无网络请求）
    log(f"完成 {len(stock_calls)}/{len(WATCHLIST)} 只（其中 {from_facts} 只取自事实层），"
        f"失败 {len(failed)}：{failed}", log_path)

    # 4. 决策摘要
    buy_list = [s["name"] for s in stock_calls if s["action"] == "buy"]
    watch_list = [s["name"] for s in stock_calls if s["action"] == "watch"]
    hold_list = [s["name"] for s in stock_calls if s["action"] == "hold"]
    log(f"buy={buy_list}, watch={watch_list}, hold 数量={len(hold_list)}", log_path)

    # 5. 组装输出（与 9/22 手工文件 schema 完全对齐）
    output = {
        "schema_version": "2.5",
        "predictor": "WorkBuddy",
        "predict_date": target_date,
        # [2026-10-20] 大海 validate_opinion.py / reconcile_v2.py 用 opinion["date"]
        # 取日期来匹配 facts；WB 原生只有 predict_date → 他的报告显示"日期: None"。
        # 补一个同义字段，零成本对齐。
        "date": target_date,
        "based_on_facts": f"facts/{target_date}.json",
        "target_date": target_date,
        # ---- 顶层平铺字段：对账专用（2026-09-29 新增，见《字段映射统一方案》）
        # 无需下钻嵌套结构即可读取，双方共用同一路径
        "market_direction": market_pred["direction"],
        "market_confidence": market_pred["confidence"],
        "_生成方式": f"daily_predict.py v1.0 自动生成 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "data_basis": {
            # [2026-10-20 修正] 数据源与 as_of 一律以事实层为准，
            # 不再写"当前时刻"——否则会出现"用 10/16 的数、标 10/19 的时间"
            "source": (facts_meta.get("caliber", {}).get("source")
                       if use_facts else "腾讯 qt.gtimg.cn 实时盘口 + newfqkline qfq 日K"),
            "as_of": facts_meta.get("data_date") or datetime.now().strftime("%Y-%m-%d %H:%M"),
            "facts_date": facts_meta.get("date") if use_facts else None,
            "facts_quality_gate": facts_meta.get("quality_gate") if use_facts else None,
            "facts_stale": facts_meta.get("is_stale") if use_facts else None,
            "facts_source": "shared" if use_facts else "self-computed",
            "note": ("收盘定格" if (datetime.now().hour >= 15 or use_facts)
                     else "盘中快照，非收盘定格"),
            "partial": len(failed) > 0,
            "failed_count": len(failed),
            "failed_stocks": failed,
        },
        "t1_prediction": {
            "sh_index": market_pred,
            "confidence": market_pred["confidence"],
            "_v25_note": f"M_sse v2.5 = {m_sse_v25:.1f}（v1 对比 {m_sse_v1:.1f}）",
        },
        "midterm_trend": {
            "horizon": "5-10个交易日",
            "sh_index": {
                "direction": "bullish" if m_sse_v25 >= MSSE_BULL else (
                    "bearish" if m_sse_v25 < MSSE_BEAR else "neutral"),
                "view": f"M_sse={m_sse_v25:.1f}，{'偏多' if m_sse_v25 >= MSSE_BULL else '中性' if m_sse_v25 >= MSSE_BEAR else '偏空'}",
                "key_levels": {
                    "support": [],
                    "resistance": [],
                },
            },
            "confidence": market_pred["confidence"],
        },
        "stock_calls": stock_calls,
        "reconciliation_hooks": {
            "t1_direction": market_pred["direction"],
            "t1_range": str(market_pred["range_pct"]),
            "stock_direction": "26只全覆盖，核心池6只重点",
            "divergence_check": f"对比大海 {target_date} 预测",
        },
        "core_pool": {
            "_说明": "核心池 v2.2：4超跌 + 2长效",
            "oversold_rebound": CORE_POOL["oversold_rebound"],
            "slow_bull": CORE_POOL["slow_bull"],
        },
        # [兼容层 2026-10-16] 顶层 market_env —— 大海侧对账直接读这个键，
        # 之前只写 _market_environment 导致他读到 null（"很多数据都是空的"根因之一）
        "market_env": {
            "M_sse": round(m_sse_v25, 2),
            "M_sse_v1": round(m_sse_v1, 2),
            "thresholds": {"bull": MSSE_BULL, "bear": MSSE_BEAR},
            "breadth": (env.get("index") or {}).get("breadth"),
            "breadth_detail": env.get("breadth_detail"),
            "kuramoto_r": (round(kura_r, 4) if kura_r is not None else None),
            "vol_ratio_threshold": round(vol_threshold_global, 2),
            "market_amp_pct": market_amp,
        },
        "_market_environment": env,
        "_param_versions": {
            "M_sse": "v2.5",
            "vol_ratio": f"v2 (动态阈值 {vol_threshold_global} = max(1.3, 大盘振幅{market_amp}%×5))",
            "rsi6": f"v2 (环境自适应阈值 {RSI6_V2_NEUT if m_sse_v25 >= MSSE_BEAR else RSI6_V2_BEAR}, M_sse={m_sse_v25:.1f})",
        },
        "_link_protocol": {
            "version": "v1.1",
            "files": {
                "self": f"predictions/{target_date}.json",
                "dahai_expected": f"predictions-dahai/{target_date}_dahai.json",
            },
            "git_path": "https://github.com/livsstock/xgl (branch: data/daily)",
        },
    }

    # 6. 写入文件
    pred_dir = os.path.join(PROJECT_DIR, "predictions")
    os.makedirs(pred_dir, exist_ok=True)
    out_path = os.path.join(pred_dir, f"{target_date}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    log(f"已写入 {out_path} ({os.path.getsize(out_path)} bytes)", log_path)

    # [2026-10-20 修正] 推送分支必须是 main！事实包/大海文件都在 main 上，
    # 推到 data/daily 等于大海永远看不到（他的对账脚本读 main）。
    if not getattr(args, "no_push", False):
        import push_api
        rel = os.path.relpath(out_path, PROJECT_DIR).replace(os.sep, "/")
        log(f"[推送] {rel} → main: {push_api.put_file(out_path, rel)}", log_path)
        # 事实包一并发出去，保证双方读同一份 facts
        frel = f"facts/{target_date}.json"
        fpath = os.path.join(PROJECT_DIR, frel)
        if os.path.exists(fpath):
            log(f"[推送] {frel} → main: {push_api.put_file(fpath, frel)}", log_path)


    if args.config:
        snapshot_path = os.path.join(
            pred_dir, f"{target_date}_{datetime.now().strftime('%H%M')}.json")
        with open(snapshot_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        log(f"已写入时点快照 {snapshot_path}", log_path)

    # 7. 控制台摘要
    print()
    print("=" * 60)
    print(f"WB 预测摘要 {target_date}")
    print("=" * 60)
    print(f"大盘: {market_pred['direction']} "
          f"{market_pred['range_pct']} 中心 {market_pred['center_pct']}% "
          f"(conf {market_pred['confidence']})")
    print(f"M_sse v2.5 = {m_sse_v25:.1f} (v1 = {m_sse_v1:.1f})")
    print(f"数据源: {'facts 事实层' if use_facts else '自算回退'} | "
          f"数据日 {facts_meta.get('data_date')} | 取自事实层 {from_facts} 只")
    print(f"核心池建议:")
    for s in stock_calls:
        if s["core_pool"]:
            print(f"  {s['name']:6s} [{s['action']:5s}] {s['direction']:6s} | "
                  f"{s['reason']}")
    print(f"观察池 buy 信号: {buy_list}")
    print(f"观察池 watch 信号: {len(watch_list)} 只")
    print(f"数据失败: {len(failed)} 只 {failed if 'failed' in dir() else ''}")
    print(f"详细: {out_path}")
    print("=" * 60)

    # 退出码：事实层不可用或失败太多时返回非零（让 cron 能感知）
    if len(failed) >= 10:
        log(f"[FAIL] 失败 {len(failed)}/{len(WATCHLIST)} 超过阈值，退出码 2", log_path)
        return 2
    if not use_facts and from_facts == 0:
        log("[FAIL] 全部指标为自算（facts 层不可用），退出码 3", log_path)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())