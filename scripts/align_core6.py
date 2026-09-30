#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双模型对齐脚本：拉取 GitHub 上 WB 和大海的预测文件，逐只比对核心 6 只的方向，
一致才报买点，分歧只摆分歧。

核心原则（2026-09-30 更新）：
  大盘方向是一切操作的前提。双模型大盘一致看空 → 冻结一切买入信号，无例外。

用法：
  python3 align_core6.py [--date YYYY-MM-DD] [--push]

数据源：
  WB:   predictions/YYYY-MM-DD.json
  大海: predictions-dahai/YYYY-MM-DD_dahai.json

凭证：
  自动读取 .credentials/gh_tokens.env，也可通过环境变量覆盖
"""
import json
import base64
import subprocess
import argparse
import os
from datetime import datetime, date, timedelta

# ===== 2026年中国股市休市日历（节假日+周末）=====
# 需每年更新。格式: "YYYY-MM-DD"
CN_HOLIDAYS_2026 = {
    # 元旦
    "2026-01-01", "2026-01-02", "2026-01-03",
    # 春节（预估，需根据国务院通知更新）
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

def get_prev_trading_day(d):
    """获取指定日期之前的最近交易日"""
    d = d - timedelta(days=1)
    while not is_trading_day(d):
        d = d - timedelta(days=1)
    return d

def get_next_trading_day(d):
    """获取指定日期之后的最近交易日"""
    d = d + timedelta(days=1)
    while not is_trading_day(d):
        d = d + timedelta(days=1)
    return d

def get_trading_days_between(start, end):
    """获取两个日期之间的所有交易日（含首尾）"""
    days = []
    d = start
    while d <= end:
        if is_trading_day(d):
            days.append(d)
        d = d + timedelta(days=1)
    return days

def check_data_quality(dahai_data, wb_data, target_date_str):
    """
    校验双方数据质量：
    1. 确认数据日期与目标交易日一致
    2. 确认WB数据是收盘数据（非盘中快照）
    3. 确认大海数据基于收盘数据
    返回 (passed: bool, warnings: list[str])
    """
    warnings = []
    passed = True
    
    # 检查大海数据日期
    if dahai_data:
        dh_date = dahai_data.get("date", "")
        if dh_date != target_date_str:
            dh_basis = dahai_data.get("data_basis", "")
            if target_date_str not in str(dh_basis):
                warnings.append(f"⚠️ 大海预测日期({dh_date})与目标日期({target_date_str})不一致")
        dh_basis = str(dahai_data.get("data_basis", ""))
        if dh_basis and "收盘" not in dh_basis and "close" not in dh_basis.lower():
            warnings.append(f"⚠️ 大海数据可能非收盘数据: data_basis='{dh_basis}'")
    
    # 检查WB数据
    if wb_data:
        wb_basis = wb_data.get("data_basis", {})
        if isinstance(wb_basis, dict):
            as_of = wb_basis.get("as_of", "")
            note = wb_basis.get("note", "")
            if "盘中快照" in str(note) or "非收盘" in str(note):
                warnings.append(f"🔴 WB数据为盘中快照({as_of})，非收盘数据，对齐结果可能有偏差")
                passed = False
            elif as_of:
                try:
                    time_part = as_of.split(" ")[1] if " " in as_of else ""
                    hour = int(time_part.split(":")[0])
                    if hour < 15:
                        warnings.append(f"🔴 WB数据采集时间({as_of})早于收盘(15:00)，可能不是收盘数据")
                        passed = False
                except:
                    pass
        wb_pred = wb_data.get("predict_date", "")
        wb_target = wb_data.get("target_date", "")
        if wb_pred and wb_target and wb_pred != wb_target:
            warnings.append(f"⚠️ WB预测日期({wb_pred})与目标日期({wb_target})不一致")
    
    return passed, warnings

# 加载凭证
CRED_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".credentials", "gh_tokens.env")
if os.path.exists(CRED_FILE):
    with open(CRED_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

def ensure_gh_auth():
    """使用 classic token 确保 gh 已认证"""
    token = os.environ.get("GH_TOKEN_CLASSIC", "")
    if not token:
        raise RuntimeError("未找到 GH_TOKEN_CLASSIC，请检查 .credentials/gh_tokens.env")
    subprocess.run(
        ["bash", "-c", f"printf '%s' '{token}' | gh auth login --with-token 2>/dev/null"],
        check=True, capture_output=True
    )

# 核心 6 只代码（纯 6 位数字）
CORE6 = {
    "601899": "紫金矿业",
    "600111": "北方稀土",
    "603799": "华友钴业",
    "601958": "金钼股份",
    "512480": "半导体ETF",
    "000858": "五粮液",
}

def gh_api(path, method="GET", data=None):
    """调用 gh api，返回解析后的 JSON"""
    cmd = ["gh", "api", f"repos/livsstock/xgl/{path}"]
    if method != "GET":
        cmd += ["--method", method]
    if data:
        cmd += ["--input", "-"]
        r = subprocess.run(cmd, input=json.dumps(data), capture_output=True, text=True)
    else:
        r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh api failed ({path}): {r.stderr[:300]}")
    return json.loads(r.stdout) if r.stdout.strip() else None


def get_file_json(path):
    """读取仓库中 JSON 文件内容"""
    meta = gh_api(f"contents/{path}")
    raw = base64.b64decode(meta["content"]).decode("utf-8")
    return json.loads(raw)


def fetch_wb(date_str):
    """拉取 WB 的预测文件: predictions/YYYY-MM-DD.json"""
    try:
        return get_file_json(f"predictions/{date_str}.json")
    except Exception as e:
        print(f"  WB 文件拉取失败: {e}")
        return None


def fetch_dahai(date_str):
    """拉取大海的预测文件: predictions-dahai/YYYY-MM-DD_dahai.json"""
    try:
        return get_file_json(f"predictions-dahai/{date_str}_dahai.json")
    except Exception as e:
        print(f"  大海文件拉取失败: {e}")
        return None


def normalize_direction(d):
    """统一方向字段：中文/英文/bearish/bullish → 标准英文"""
    if not d:
        return "unknown"
    d_lower = d.lower().strip()
    mapping = {
        "偏多": "bullish", "多": "bullish", "涨": "bullish", "平偏涨": "bullish",
        "偏空": "bearish", "空": "bearish", "跌": "bearish",
        "中性": "neutral", "中": "neutral", "平": "neutral", "观望": "neutral",
        "up": "bullish", "down": "bearish", "neutral": "neutral",
        "bullish": "bullish", "bearish": "bearish",
        "hold": "neutral", "watch": "neutral",
        "试探": "buy_probe", "标准": "buy_standard",
        "重仓": "buy_heavy", "加仓": "buy_add",
        "buy": "bullish", "sell": "bearish",
    }
    return mapping.get(d_lower, d_lower)


def is_buy_signal(action):
    """判断是否为买入信号"""
    if not action:
        return False
    a = action.lower()
    return any(kw in a for kw in ["buy", "试探", "标准", "重仓", "加仓", "bullish"])


def extract_core6(data):
    """
    从预测数据中提取核心 6 只的方向。
    兼容两种 schema：
      - WB: stock_calls[] 或 predictions[]，code 为纯 6 位数字
      - 大海: stock_calls[]，code 为 sh601899 / sz000858 格式
    """
    directions = {}
    stock_list = data.get("stock_calls", data.get("predictions", []))

    for stock in stock_list:
        raw_code = stock.get("code", "")
        digits = "".join(c for c in str(raw_code) if c.isdigit())
        if len(digits) > 6:
            digits = digits[-6:]
        if digits not in CORE6:
            continue

        raw_action = stock.get("action", stock.get("direction", "unknown"))
        action = normalize_direction(raw_action)

        directions[digits] = {
            "name": CORE6[digits],
            "action": action,
            "raw_direction": stock.get("direction", raw_action),
            "reason": stock.get("reason", ""),
        }
    return directions


def extract_market_direction(data, source="wb"):
    """
    从预测数据中提取大盘方向判断（当期状态，非次日预测）。
    大盘风控是双模型对齐的最高优先级。
    
    搜索优先级（当期状态优先，避免误读次日预测为当期）：
    1. market_overview.direction — 大海当期市场判断
    2. market_outlook.direction — 兼容旧格式
    3. _market_environment — WB 大盘环境（含 env_label 当期状态）
    4. market_direction — 通用字段
    5. eod_quotes.indices 实际涨跌推算 — 基于真实收盘数据
    6. t1_prediction.sh_index.direction — 次日预测（最后兜底，标注为预测值）
    """
    # 通用：优先读当期状态字段
    for field in ["market_overview", "market_outlook", "market_direction"]:
        mo = data.get(field, {})
        if isinstance(mo, dict) and mo.get("direction"):
            direction = normalize_direction(mo.get("direction", ""))
            confidence = mo.get("confidence", None)
            reason = mo.get("reason", mo.get("notes", ""))
            return {"direction": direction, "confidence": confidence, "reason": reason, "source": field}
    
    # WB 特有：_market_environment（含下划线前缀）
    me = data.get("_market_environment", {})
    if isinstance(me, dict):
        # 优先读 index.env_label（当期环境标签）
        idx = me.get("index", {})
        if isinstance(idx, dict) and idx.get("env_label"):
            direction = normalize_direction(idx.get("env_label", ""))
            return {"direction": direction, "confidence": None,
                    "reason": f"环境标签:{idx.get('env_label')} M_sse={idx.get('M_score', '?')}",
                    "source": "_market_environment.index.env_label"}
        # 次选：当日预测方向（如 9_21_forecast）
        for key, val in me.items():
            if "forecast" in key and isinstance(val, dict) and val.get("direction"):
                direction = normalize_direction(val.get("direction", ""))
                return {"direction": direction, "confidence": None,
                        "reason": f"当日预测:{val.get('direction')} ({val.get('compare_to_close_ref', '')})",
                        "source": f"_market_environment.{key}"}
    
    # 从 eod_quotes.indices 实际涨跌推算当期大盘方向
    indices = data.get("eod_quotes", {}).get("indices", {})
    if indices:
        for idx_name in ["上证指数", "沪深300", "上证"]:
            if idx_name in indices:
                change = indices[idx_name].get("change_pct", 0)
                if isinstance(change, str):
                    try:
                        change = float(change.replace("%", ""))
                    except:
                        change = 0
                if change <= -0.5:
                    return {"direction": "bearish", "confidence": None, "reason": f"{idx_name}实际跌{change}%", "source": "eod_quotes"}
                elif change >= 0.5:
                    return {"direction": "bullish", "confidence": None, "reason": f"{idx_name}实际涨{change}%", "source": "eod_quotes"}
                else:
                    return {"direction": "neutral", "confidence": None, "reason": f"{idx_name}实际变动{change}%", "source": "eod_quotes"}
    
    # 最后兜底：t1_prediction（次日预测，非当期状态，需标注）
    if source == "wb":
        t1 = data.get("t1_prediction", {}).get("sh_index", {})
        if t1 and t1.get("direction"):
            direction = normalize_direction(t1.get("direction", ""))
            return {"direction": direction, "confidence": t1.get("confidence"),
                    "reason": f"[次日预测] {t1.get('reason', '')}", "source": "t1_prediction"}
    
    return {"direction": "unknown", "confidence": None, "reason": "无法从数据中提取大盘方向", "source": "none"}


def is_market_high_risk(wb_market, dh_market):
    """
    大盘风控门：判断大盘风险等级。
    返回 (is_high_risk: bool, level: str, detail: str)
    
    级别定义：
    - CRITICAL: 双方一致看空 → 冻结一切买入，无例外
    - HIGH: 一方看空 或 任一方方向未知(fail-closed) → 暂停超跌策略，仅允许止损/止盈
    - MODERATE: 双方中性 → 观望为主，等待明确信号
    - NORMAL: 双方均有明确方向且至少一方看多、无看空 → 可正常执行个股分析
    
    核心原则：宁可误拦、不可漏放（fail-closed）。
    unknown 方向不视为安全，而归入 HIGH。
    """
    wb_dir = wb_market.get("direction", "unknown")
    dh_dir = dh_market.get("direction", "unknown")

    both_bearish = wb_dir == "bearish" and dh_dir == "bearish"
    one_bearish = wb_dir == "bearish" or dh_dir == "bearish"
    both_neutral = wb_dir == "neutral" and dh_dir == "neutral"
    any_unknown = wb_dir == "unknown" or dh_dir == "unknown"
    both_bullish = wb_dir == "bullish" and dh_dir == "bullish"

    if both_bearish:
        return True, "CRITICAL", "🔴 双模型大盘一致看空 — 冻结一切买入信号，建议全面观望"
    elif one_bearish:
        return True, "HIGH", "🟠 一方大盘看空 — 暂停超跌策略，仅允许已有持仓止损/止盈操作"
    elif any_unknown:
        # fail-closed: 方向未知时视为高风险，宁可误拦不可漏放
        unknown_sources = []
        if wb_dir == "unknown":
            unknown_sources.append(f"WB(来源:{wb_market.get('source', '?')})")
        if dh_dir == "unknown":
            unknown_sources.append(f"大海(来源:{dh_market.get('source', '?')})")
        return True, "HIGH", f"⚠️ 大盘方向无法确认({', '.join(unknown_sources)}) — 宁可误拦，暂停买入操作"
    elif both_neutral:
        return False, "MODERATE", "🟡 双方大盘中性 — 观望为主，等待明确信号"
    else:
        return False, "NORMAL", "🟢 大盘风向偏多 — 可正常执行个股分析"


def extract_index_changes(wb_data):
    """
    从 WB 数据中提取主要指数涨跌幅。
    WB 格式: eod_quotes.indices -> {指数名: {close, change_pct}}
    """
    indices = wb_data.get("eod_quotes", {}).get("indices", {})
    result = {}
    for name, info in indices.items():
        result[name] = {
            "close": info.get("close"),
            "change_pct": info.get("change_pct", 0),
        }
    return result


def detect_oversold_info(wb_data, threshold=-2.0):
    """
    超跌信息提示（非买入触发器）。
    
    仅在大盘风控门通过后（非 CRITICAL / HIGH）才作为辅助参考。
    大盘下行时，超跌不等于见底，不做任何买入暗示。
    
    返回 (has_significant_drop, level, detail, index_data)
    """
    index_changes = extract_index_changes(wb_data)
    
    if not index_changes:
        return False, "NONE", "⚠️ 无指数涨跌幅数据", {}

    worst_index = min(index_changes.items(), key=lambda x: x[1]["change_pct"])
    worst_name = worst_index[0]
    worst_change = worst_index[1]["change_pct"]
    
    index_summary = {}
    for name, info in index_changes.items():
        index_summary[name] = {
            "close": info["close"],
            "change_pct": info["change_pct"],
        }
    
    if worst_change <= -3.0:
        detail = f"🔻 大盘大跌 — {worst_name}跌{worst_change:.2f}%，需密切关注系统性风险"
        return True, "MAJOR_DROP", detail, index_summary
    elif worst_change <= threshold:
        detail = f"📉 大盘显著下跌 — {worst_name}跌{worst_change:.2f}%，观望为主"
        return True, "SIGNIFICANT_DROP", detail, index_summary
    elif worst_change <= -1.0:
        detail = f"ℹ️ 大盘温和下跌 — {worst_name}跌{worst_change:.2f}%"
        return False, "MILD_DROP", detail, index_summary
    else:
        return False, "NONE", "✅ 大盘未出现显著下跌", index_summary


def classify_divergence(wb_dir_norm, dh_dir_norm):
    """
    对分歧进行分级：
    - 强分歧：一方看涨、一方看跌（方向对立）
    - 弱分歧：一方有明确方向、一方观望
    """
    bullish_set = {"bullish", "buy_probe", "buy_standard", "buy_heavy", "buy_add"}
    bearish_set = {"bearish"}
    neutral_set = {"neutral", "unknown"}
    
    wb_is_bull = wb_dir_norm in bullish_set
    wb_is_bear = wb_dir_norm in bearish_set
    dh_is_bull = dh_dir_norm in bullish_set
    dh_is_bear = dh_dir_norm in bearish_set
    
    if (wb_is_bull and dh_is_bear) or (wb_is_bear and dh_is_bull):
        return "强分歧", f"Wb:{wb_dir_norm}/大海:{dh_dir_norm} 方向对立"
    else:
        return "弱分歧", f"Wb:{wb_dir_norm}/大海:{dh_dir_norm}"


def align(wb_data, dahai_data, date_str):
    """
    比对两份预测，输出对齐结论。
    
    核心逻辑（2026-09-30 更新）：
    1. 大盘风控门（绝对开关）
       - CRITICAL（双方看空）→ 冻结一切买入，无任何例外
       - HIGH（一方看空）→ 暂停超跌策略，仅允许止损/止盈
    2. 个股分析（仅在大盘风控非 CRITICAL 时输出买入信号）
    3. 超跌信息（仅在大盘风控非 CRITICAL/HIGH 时作为辅助参考）
    """

    # ===== 第一层：大盘风控门（绝对开关，最高优先级）=====
    wb_market = extract_market_direction(wb_data, "wb")
    dh_market = extract_market_direction(dahai_data, "dahai")
    is_high_risk, risk_level, risk_detail = is_market_high_risk(wb_market, dh_market)
    
    # 大盘是否完全冻结买入
    market_freeze = (risk_level == "CRITICAL")
    # 大盘是否限制为仅止损止盈
    market_caution = (risk_level == "HIGH")

    # ===== 第二层：大盘指数信息（仅供参考）=====
    index_info = None
    oversold_info = None
    try:
        _, drop_level, drop_detail, index_data = detect_oversold_info(wb_data)
        oversold_info = {
            "level": drop_level,
            "detail": drop_detail,
            "index_data": index_data,
        }
    except Exception:
        pass

    # ===== 第三层：个股分析 =====
    wb_dirs = extract_core6(wb_data)
    dh_dirs = extract_core6(dahai_data)

    results = []
    aligned_buy = []
    divergence = []
    all_watch = True

    for code in CORE6:
        name = CORE6[code]
        wb = wb_dirs.get(code, {"action": "无数据", "raw_direction": "无"})
        dh = dh_dirs.get(code, {"action": "无数据", "raw_direction": "无"})

        wb_buy = is_buy_signal(wb.get("action", ""))
        dh_buy = is_buy_signal(dh.get("action", ""))

        # === 大盘冻结时：所有买入信号全部拦截 ===
        if market_freeze:
            if wb_buy or dh_buy:
                status = "🚫 大盘风控冻结（不允许买入）"
            elif not wb_buy and not dh_buy:
                wb_dir_norm = normalize_direction(wb.get("raw_direction", wb.get("action", "")))
                dh_dir_norm = normalize_direction(dh.get("raw_direction", dh.get("action", "")))
                if wb_dir_norm == dh_dir_norm:
                    status = "⚪ 一致不买（大盘冻结）"
                else:
                    div_level, div_detail = classify_divergence(wb_dir_norm, dh_dir_norm)
                    status = f"🔶 {div_level}(Wb:{wb_dir_norm}/大海:{dh_dir_norm})·大盘冻结"
                    divergence.append(f"{name}({code}): {div_detail}")
        
        # === 大盘谨慎时：仅允许止损/止盈，不开新仓 ===
        elif market_caution:
            if wb_buy and dh_buy:
                status = "⚠️ 大盘一方看空·买入信号暂不执行"
            elif wb_buy or dh_buy:
                status = "⚠️ 大盘一方看空·买入信号暂不执行"
            elif not wb_buy and not dh_buy:
                wb_dir_norm = normalize_direction(wb.get("raw_direction", wb.get("action", "")))
                dh_dir_norm = normalize_direction(dh.get("raw_direction", dh.get("action", "")))
                if wb_dir_norm == dh_dir_norm:
                    status = "⚪ 一致不买"
                else:
                    div_level, div_detail = classify_divergence(wb_dir_norm, dh_dir_norm)
                    status = f"🔶 {div_level}(Wb:{wb_dir_norm}/大海:{dh_dir_norm})"
                    divergence.append(f"{name}({code}): {div_detail}")
        
        # === 大盘正常时：正常个股分析 ===
        else:
            if wb_buy and dh_buy:
                status = "✅ 一致买入"
                aligned_buy.append(f"{name}({code}): WB={wb['action']}, 大海={dh['action']}")
                all_watch = False
            elif not wb_buy and not dh_buy:
                wb_dir_norm = normalize_direction(wb.get("raw_direction", wb.get("action", "")))
                dh_dir_norm = normalize_direction(dh.get("raw_direction", dh.get("action", "")))
                if wb_dir_norm == dh_dir_norm:
                    status = "⚪ 一致不买"
                else:
                    div_level, div_detail = classify_divergence(wb_dir_norm, dh_dir_norm)
                    status = f"🔶 {div_level}(Wb:{wb_dir_norm}/大海:{dh_dir_norm})"
                    divergence.append(f"{name}({code}): {div_detail}")
            else:
                status = "⚠️ 分歧"
                divergence.append(f"{name}({code}): WB={wb['action']}, 大海={dh['action']}")
                all_watch = False

        results.append({
            "code": code,
            "name": name,
            "wb_action": wb["action"],
            "wb_direction": wb.get("raw_direction", ""),
            "dahai_action": dh["action"],
            "dahai_direction": dh.get("raw_direction", ""),
            "status": status,
        })

    # ===== 总结论 =====
    if market_freeze:
        conclusion = "🔴 大盘风控冻结 — 双模型一致看空，一切买入信号冻结，全面观望"
    elif market_caution:
        conclusion = "🟠 大盘风险提示 — 一方看空，不开新仓，已有持仓仅执行止损/止盈"
    elif aligned_buy:
        conclusion = f"✅ {len(aligned_buy)}只一致买入信号"
    elif all_watch:
        conclusion = "⚪ 全部观望"
    elif divergence:
        conclusion = "🔶 有分歧，暂不操作"
    else:
        conclusion = "⚪ 一致不买"

    return {
        "date": date_str,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "market": {
            "wb": wb_market,
            "dahai": dh_market,
            "risk_level": risk_level,
            "risk_detail": risk_detail,
            "is_high_risk": is_high_risk,
            "market_freeze": market_freeze,
            "market_caution": market_caution,
            "oversold_info": oversold_info,
        },
        "results": results,
        "aligned_buy": aligned_buy,
        "divergence": divergence,
        "conclusion": conclusion,
    }


def push_alignment(result, date_str):
    """推送对齐结果到 GitHub"""
    content = json.dumps(result, ensure_ascii=False, indent=2)
    payload = {
        "message": f"[LIVS-Stock] 双模型对齐 {date_str}",
        "content": base64.b64encode(content.encode()).decode(),
    }
    try:
        meta = gh_api(f"contents/reconciliation/daily/{date_str}_align.json")
        payload["sha"] = meta["sha"]
    except:
        pass

    gh_api(f"contents/reconciliation/daily/{date_str}_align.json",
           method="PUT", data=payload)


def main():
    parser = argparse.ArgumentParser(description="双模型对齐：大海 vs WorkBuddy")
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"),
                        help="对齐日期，默认今天")
    parser.add_argument("--push", action="store_true", help="推送对齐结果到 GitHub")
    args = parser.parse_args()

    # 确保 gh 认证
    try:
        ensure_gh_auth()
    except Exception as e:
        print(f"⚠️ gh 认证失败: {e}")
        print("请确认 .credentials/gh_tokens.env 中的 token 有效")
        return

    # ===== 交易日检查 =====
    try:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    except ValueError:
        print(f"❌ 日期格式错误: {args.date}，请使用 YYYY-MM-DD 格式")
        return
    
    if not is_trading_day(target_date):
        print(f"⚠️ {args.date} 不是交易日（周末或节假日），跳过对齐")
        prev_td = get_prev_trading_day(target_date)
        print(f"   最近交易日: {prev_td.strftime('%Y-%m-%d')}")
        print(f"   用法: python3 align_core6.py --date {prev_td.strftime('%Y-%m-%d')}")
        return
    
    print(f"=== 双模型对齐 {args.date} ===")
    print(f"交易日确认: ✅ 是交易日\n")

    # 拉取两份预测
    print("拉取预测数据...")
    wb_data = fetch_wb(args.date)
    dh_data = fetch_dahai(args.date)

    if not wb_data and not dh_data:
        print(f"\n❌ 双方预测均未找到 ({args.date})")
        return
    if not wb_data:
        print(f"\n⚠️ WB 预测缺失 ({args.date})，仅显示大海侧")
    if not dh_data:
        print(f"\n⚠️ 大海预测缺失 ({args.date})，仅显示大海侧")

    # ===== 数据质量校验 =====
    if wb_data and dh_data:
        data_ok, warnings = check_data_quality(dh_data, wb_data, args.date)
        if warnings:
            print("\n--- 数据质量检查 ---")
            for w in warnings:
                print(f"  {w}")
            if not data_ok:
                print("\n  ⚠️ 数据质量未达标，对齐结果仅供参考")
                print("  建议：确保双方都使用当日收盘数据后再做正式对齐\n")
            else:
                print("  ✅ 数据质量检查通过\n")

    # 如果双方都有，执行对齐
    if wb_data and dh_data:
        result = align(wb_data, dh_data, args.date)

        # ===== 大盘风控门（最先输出，最醒目）=====
        mkt = result["market"]
        print(f"\n{'='*55}")
        print(f"  🔍 大盘风控门（最高优先级 · 绝对开关）")
        print(f"{'='*55}")
        wb_dir = mkt["wb"].get("direction", "unknown") if isinstance(mkt["wb"], dict) else mkt["wb"]
        dh_dir = mkt["dahai"].get("direction", "unknown") if isinstance(mkt["dahai"], dict) else mkt["dahai"]
        print(f"  WB   大盘方向: {wb_dir}")
        print(f"  大海 大盘方向: {dh_dir}")
        print(f"\n  {mkt['risk_detail']}")
        
        if mkt.get("market_freeze"):
            print(f"\n  ⛔ 风控等级: {mkt['risk_level']} — 一切买入信号冻结，无例外")
            print(f"  📌 已有持仓仅执行止损/止盈纪律，不开新仓")
        elif mkt.get("market_caution"):
            print(f"\n  ⚠️ 风控等级: {mkt['risk_level']} — 不开新仓，仅止损/止盈")
        print(f"{'='*55}\n")

        # ===== 大盘指数信息（仅供参考）=====
        if mkt.get("oversold_info"):
            oi = mkt["oversold_info"]
            print(f"{'='*55}")
            print(f"  📊 大盘指数参考")
            print(f"{'='*55}")
            if oi.get("index_data"):
                for idx_name, idx_info in oi["index_data"].items():
                    chg = idx_info.get("change_pct", 0)
                    close = idx_info.get("close", "N/A")
                    arrow = "🔻" if chg < 0 else "🔺" if chg > 0 else "➖"
                    print(f"    {arrow} {idx_name}: {close} ({chg:+.2f}%)")
            print(f"\n  {oi.get('detail', '')}")
            if mkt.get("market_freeze"):
                print(f"  ⚠️ 大盘冻结中，下跌信息仅供参考，不作为买入依据")
            elif mkt.get("market_caution"):
                print(f"  ⚠️ 大盘谨慎中，下跌信息仅供参考，不作为买入依据")
            print(f"{'='*55}\n")

        print(f"=== 个股对齐 ===")
        print("核心 6 只:")
        for r in result["results"]:
            print(f"  {r['name']:8s}({r['code']})")
            print(f"    WB:   {r['wb_action']:12s} ({r['wb_direction']})")
            print(f"    大海: {r['dahai_action']:12s} ({r['dahai_direction']})")
            print(f"    → {r['status']}")

        if result["aligned_buy"]:
            print(f"\n🟢 一致买入信号:")
            for b in result["aligned_buy"]:
                print(f"  • {b}")

        if result["divergence"]:
            print(f"\n⚠️ 分歧项:")
            for d in result["divergence"]:
                print(f"  • {d}")

        print(f"\n=== 总结论: {result['conclusion']} ===")

        # 推送
        if args.push:
            try:
                push_alignment(result, args.date)
                print(f"\n✅ 对齐结果已推送: reconciliation/daily/{args.date}_align.json")
            except Exception as e:
                print(f"\n⚠️ 推送失败: {e}")
    else:
        data = wb_data or dh_data
        source = "WB" if wb_data else "大海"
        dirs = extract_core6(data)
        print(f"\n=== {source} 侧预测 ({args.date}) ===")
        for code in CORE6:
            d = dirs.get(code)
            if d:
                print(f"  {d['name']:8s}({code}): {d['action']} ({d.get('raw_direction', '')})")
            else:
                print(f"  {CORE6[code]:8s}({code}): 无数据")
        print(f"\n⚠️ 无法对齐，缺少{'大海' if not dh_data else 'WB'}侧数据")


if __name__ == "__main__":
    main()
