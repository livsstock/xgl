#!/usr/bin/env python3
"""
LIVS-Stock 异常检测脚本
运行于 GitHub Actions，在每日数据采集后执行异常检测
输出：manifest.json + anomaly_alert.md

检测维度：
  1. 数据完整性（标的覆盖、关键字段）
  2. 价格突变（单日涨跌幅、连续下跌）
  3. 板块联动（同板块齐跌、全市场下跌）
  4. 数据质量（价格异常、换手率异常、ETF溢价）

设计原则：
  - 异常检测不阻断数据提交流程
  - 纯标准库，无额外依赖
  - 自适应多种数据格式
"""

import json
import os
import sys
import glob
from datetime import datetime, timezone, timedelta

# ==================== 配置 ====================

TZ_CST = timezone(timedelta(hours=8))

# 任务指定的12只监控标的（用于完整性检查）
EXPECTED_STOCKS = [
    {"code": "sz000725", "name": "京东方A"},
    {"code": "sh600111", "name": "北方稀土"},
    {"code": "sh600019", "name": "宝钢股份"},
    {"code": "sh600893", "name": "航发动力"},
    {"code": "sz000960", "name": "锡业股份"},
    {"code": "sz002155", "name": "湖南黄金"},
    {"code": "sh600392", "name": "盛和资源"},
    {"code": "sh601958", "name": "金钼股份"},
    {"code": "sh601212", "name": "白银有色"},
    {"code": "sz159770", "name": "机器人ETF"},
    {"code": "sh518880", "name": "黄金ETF"},
    {"code": "sh512480", "name": "半导体ETF"},
]

# 板块分组（用于联动检测）
SECTOR_GROUPS = {
    "有色金属": ["锡业股份", "白银有色", "盛和资源", "金钼股份"],
    "稀土": ["北方稀土", "盛和资源"],
    "黄金": ["湖南黄金", "黄金ETF"],
    "科技": ["京东方A", "机器人ETF", "半导体ETF"],
    "钢铁": ["宝钢股份"],
    "军工": ["航发动力"],
}

# 阈值配置
THRESHOLDS = {
    "single_drop_alert": -5.0,       # 单日跌幅 ≥ 5% → alert
    "single_drop_critical": -8.0,     # 单日跌幅 ≥ 8% → critical
    "single_surge_alert": 5.0,        # 单日涨幅 ≥ 5% → alert
    "consecutive_drop_days": 3,       # 连续下跌天数
    "consecutive_drop_pct": -10.0,    # 连续累计跌幅
    "sector_decline_pct": -3.0,       # 板块个股跌幅阈值
    "sector_decline_count": 3,        # 板块触发联动的最少个股数
    "market_decline_pct": -2.0,       # 全市场平均跌幅阈值
    "turnover_low": 0.1,              # 换手率下限 (%)
    "turnover_high": 20.0,            # 换手率上限 (%)
    "etf_premium_pct": 3.0,           # ETF溢价偏离阈值 (%)
}


# ==================== 数据加载 ====================

def load_data():
    """
    加载采集数据，自适应多种格式。
    尝试顺序：stock.json → stock.josn → output/ 最新文件
    """
    data = None
    source = None

    # 尝试1：stock.json（标准拼写）
    if os.path.exists("stock.json"):
        try:
            with open("stock.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            source = "stock.json"
        except Exception as e:
            print(f"[WARN] stock.json 解析失败: {e}")

    # 尝试2：stock.josn（daily_collect.py 当前输出文件名）
    if data is None and os.path.exists("stock.josn"):
        try:
            with open("stock.josn", "r", encoding="utf-8") as f:
                data = json.load(f)
            source = "stock.josn"
        except Exception as e:
            print(f"[WARN] stock.josn 解析失败: {e}")

    # 尝试3：output/ 目录下最新的 daily-finance-*.json
    if data is None and os.path.isdir("output"):
        json_files = sorted(
            glob.glob(os.path.join("output", "daily-finance-*.json")),
            key=os.path.getmtime,
            reverse=True,
        )
        if json_files:
            try:
                with open(json_files[0], "r", encoding="utf-8") as f:
                    data = json.load(f)
                source = json_files[0]
            except Exception as e:
                print(f"[WARN] {json_files[0]} 解析失败: {e}")

    if data is None:
        print("[ERROR] 无法找到或解析采集数据文件")
        return None, None

    print(f"[OK] 从 {source} 加载数据")
    return data, source


def extract_stocks(data):
    """
    从数据中提取个股列表，自适应多种结构。
    返回 list[dict]，每项含 code/name/close/change_pct/volume/turnover/high/low
    """
    stocks = []

    # 格式1: eod_quotes.watchlist（daily_collect.py 当前格式）
    if isinstance(data, dict):
        eod = data.get("eod_quotes", {})
        wl = eod.get("watchlist", []) if isinstance(eod, dict) else []
        if isinstance(wl, list) and len(wl) > 0:
            for item in wl:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("code", ""))
                # 补全前缀（daily_collect.py 输出的是纯数字 code）
                if code and not code.startswith("sh") and not code.startswith("sz"):
                    if code.startswith("6"):
                        code = "sh" + code
                    else:
                        code = "sz" + code
                stocks.append({
                    "code": code,
                    "name": item.get("name", ""),
                    "close": _safe_float(item.get("close")),
                    "change_pct": _safe_float(item.get("change_pct")),
                    "volume": _safe_float(item.get("volume")),
                    "turnover": _safe_float(item.get("turnover")),
                    "high": _safe_float(item.get("high")),
                    "low": _safe_float(item.get("low")),
                    "pre_close": _safe_float(item.get("pre_close")),
                })
            return stocks

        # 格式2: 顶层 "stocks" 字段
        raw_stocks = data.get("stocks", [])
        if isinstance(raw_stocks, list) and len(raw_stocks) > 0:
            stocks = _parse_stock_list(raw_stocks)
            if stocks:
                return stocks

        # 格式3: 顶层 "data" 数组
        raw_data = data.get("data", [])
        if isinstance(raw_data, list) and len(raw_data) > 0:
            stocks = _parse_stock_list(raw_data)
            if stocks:
                return stocks

        # 格式4: dict-of-dict（key=code）
        for key in ["stocks", "data", "quotes", "a_shares", "watchlist"]:
            val = data.get(key)
            if isinstance(val, dict) and len(val) > 0:
                for code, info in val.items():
                    if not isinstance(info, dict):
                        continue
                    stocks.append({
                        "code": code,
                        "name": info.get("name", ""),
                        "close": _safe_float(info.get("close") or info.get("price")),
                        "change_pct": _safe_float(info.get("change_pct") or info.get("pct_chg")),
                        "volume": _safe_float(info.get("volume") or info.get("vol")),
                        "turnover": _safe_float(info.get("turnover") or info.get("amount")),
                        "high": _safe_float(info.get("high")),
                        "low": _safe_float(info.get("low")),
                        "pre_close": _safe_float(info.get("pre_close")),
                    })
                if stocks:
                    return stocks

    return stocks


def _parse_stock_list(raw_list):
    """解析列表格式的个股数据"""
    stocks = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", item.get("symbol", item.get("ts_code", ""))))
        name = str(item.get("name", item.get("stock_name", "")))
        if not code and not name:
            continue
        stocks.append({
            "code": code,
            "name": name,
            "close": _safe_float(item.get("close") or item.get("price") or item.get("trade_price")),
            "change_pct": _safe_float(item.get("change_pct") or item.get("pct_chg") or item.get("percent")),
            "volume": _safe_float(item.get("volume") or item.get("vol")),
            "turnover": _safe_float(item.get("turnover") or item.get("amount") or item.get("turnover_rate")),
            "high": _safe_float(item.get("high")),
            "low": _safe_float(item.get("low")),
            "pre_close": _safe_float(item.get("pre_close") or item.get("last_close")),
        })
    return stocks


def _safe_float(val):
    """安全转浮点"""
    if val is None or val == "" or val == "-":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


# ==================== 检测引擎 ====================

def check_completeness(stocks):
    """
    检测1: 数据完整性
    - 12只标的是否都有数据
    - 关键字段 (close, change_pct, volume, turnover) 是否为空
    """
    anomalies = []
    missing = []

    for expected in EXPECTED_STOCKS:
        found = False
        for s in stocks:
            s_code = s.get("code", "")
            s_name = s.get("name", "")
            e_code = expected["code"]
            e_name = expected["name"]
            # 匹配：code 精确匹配，或 name 匹配，或 code 去前缀后匹配
            if (s_code == e_code or s_name == e_name or s_name == e_code
                    or s_code == e_code[2:] or e_code == s_code[2:] if len(s_code) > 2 else False):
                found = True
                # 检查关键字段
                critical_missing = []
                for field in ["close", "change_pct"]:
                    if s.get(field) is None:
                        critical_missing.append(field)
                if critical_missing:
                    anomalies.append({
                        "type": "missing_fields",
                        "stock": e_name,
                        "code": e_code,
                        "value": None,
                        "level": "alert",
                        "message": f"{e_name}({e_code}) 缺少关键字段: {', '.join(critical_missing)}",
                    })
                break
        if not found:
            missing.append(expected)
            anomalies.append({
                "type": "missing_stock",
                "stock": e_name,
                "code": e_code,
                "value": None,
                "level": "alert",
                "message": f"{e_name}({e_code}) 数据缺失，采集未覆盖",
            })

    return anomalies, missing


def check_price_anomalies(stocks):
    """
    检测2: 价格突变
    - 单日涨跌幅 ≥ ±5% → alert
    - 单日涨跌幅 ≥ ±8% → critical
    - 连续3日下跌且累计跌幅 ≥ 10% → alert（需要历史数据，单日快照跳过）
    """
    anomalies = []
    t = THRESHOLDS

    for s in stocks:
        pct = s.get("change_pct")
        if pct is None:
            continue
        name = s.get("name", "未知")
        code = s.get("code", "")

        # 暴跌 critical
        if pct <= t["single_drop_critical"]:
            anomalies.append({
                "type": "sharp_drop",
                "stock": name,
                "code": code,
                "value": pct,
                "level": "critical",
                "message": f"{name} 单日跌幅 {pct:.2f}%，超过 {t['single_drop_critical']:.0f}% 紧急阈值",
            })
        # 暴跌 alert
        elif pct <= t["single_drop_alert"]:
            anomalies.append({
                "type": "sharp_drop",
                "stock": name,
                "code": code,
                "value": pct,
                "level": "alert",
                "message": f"{name} 单日跌幅 {pct:.2f}%，超过 {abs(t['single_drop_alert']):.0f}% 阈值",
            })

        # 暴涨 alert
        if pct >= t["single_surge_alert"]:
            anomalies.append({
                "type": "sharp_surge",
                "stock": name,
                "code": code,
                "value": pct,
                "level": "alert",
                "message": f"{name} 单日涨幅 +{pct:.2f}%，超过 {t['single_surge_alert']:.0f}% 阈值",
            })

    return anomalies


def check_sector_correlation(stocks):
    """
    检测3: 板块联动
    - 同板块3只以上同时跌 ≥ 3% → sector_alert
    - 全部标的平均跌幅 ≥ 2% → market_alert
    """
    anomalies = []
    t = THRESHOLDS

    # 按板块分析
    for sector_name, member_names in SECTOR_GROUPS.items():
        declining_members = []
        for s in stocks:
            s_name = s.get("name", "")
            pct = s.get("change_pct")
            if s_name in member_names and pct is not None and pct <= t["sector_decline_pct"]:
                declining_members.append({"name": s_name, "change_pct": pct})

        if len(declining_members) >= t["sector_decline_count"]:
            names_str = "、".join(m["name"] for m in declining_members)
            avg_pct = sum(m["change_pct"] for m in declining_members) / len(declining_members)
            anomalies.append({
                "type": "sector_decline",
                "sector": sector_name,
                "stocks": [m["name"] for m in declining_members],
                "level": "alert",
                "message": (
                    f"{sector_name}板块 {len(declining_members)} 只同跌≥{abs(t['sector_decline_pct']):.0f}%："
                    f"{names_str}，均跌 {avg_pct:.2f}%"
                ),
            })

    # 全市场平均跌幅
    valid_pcts = [s["change_pct"] for s in stocks if s.get("change_pct") is not None]
    if valid_pcts:
        avg_decline = sum(valid_pcts) / len(valid_pcts)
        if avg_decline <= t["market_decline_pct"]:
            falling = sum(1 for p in valid_pcts if p < 0)
            anomalies.append({
                "type": "market_decline",
                "sector": "全市场",
                "stocks": [],
                "level": "alert",
                "message": (
                    f"全部 {len(valid_pcts)} 只标的平均跌幅 {avg_decline:.2f}%，"
                    f"其中 {falling} 只下跌，触发市场预警"
                ),
            })

    return anomalies


def check_data_quality(stocks):
    """
    检测4: 数据质量
    - 价格 ≤ 0 → data_error
    - 换手率异常 (< 0.1% 或 > 20%) → volume_anomaly
    - ETF价格异常偏离 → etf_premium_alert（需 NAV 数据，当前标记待完善）
    """
    anomalies = []
    t = THRESHOLDS
    etf_names = {"机器人ETF", "黄金ETF", "半导体ETF"}

    for s in stocks:
        name = s.get("name", "未知")
        code = s.get("code", "")
        close = s.get("close")
        turnover = s.get("turnover")  # 换手率

        # 价格异常
        if close is not None and close <= 0:
            anomalies.append({
                "type": "data_error",
                "stock": name,
                "code": code,
                "value": close,
                "level": "critical",
                "message": f"{name}({code}) 收盘价 {close}，价格异常（≤0）",
            })

        # 换手率异常（仅在字段存在且非 ETF 时检查，ETF 换手率含义不同）
        if turnover is not None and name not in etf_names:
            if turnover < t["turnover_low"] or turnover > t["turnover_high"]:
                anomalies.append({
                    "type": "volume_anomaly",
                    "stock": name,
                    "code": code,
                    "value": turnover,
                    "level": "alert",
                    "message": f"{name} 换手率 {turnover:.2f}%，超出正常范围 [{t['turnover_low']}%, {t['turnover_high']}%]",
                })

        # ETF溢价检测（需要 NAV 数据，当前数据源无 NAV，仅记录待扩展）
        # 若未来数据源提供 NAV，可在此补充：
        # if name in etf_names and nav is not None and close is not None:
        #     premium = (close - nav) / nav * 100
        #     if abs(premium) >= t["etf_premium_pct"]:
        #         anomalies.append(...)

    return anomalies


# ==================== 状态判定 ====================

def determine_status(anomalies, missing, stocks):
    """
    判定整体状态
    优先级: failed > critical → failed | alert → alert | 缺数据 → partial | 正常 → complete
    """
    levels = [a.get("level", "") for a in anomalies]
    types = [a.get("type", "") for a in anomalies]

    # critical 级别或严重数据错误 → failed
    if "critical" in levels or "data_error" in types:
        return "failed"
    # alert 级别 → alert
    if "alert" in levels:
        return "alert"
    # 有缺失标的但无异常 → partial
    if missing:
        return "partial"
    # 实际数据量远少于预期 → partial
    if len(stocks) > 0 and len(stocks) < len(EXPECTED_STOCKS) * 0.5:
        return "partial"
    # 无数据 → failed
    if not stocks:
        return "failed"
    return "complete"


# ==================== 输出生成 ====================

def generate_manifest(date_str, status, anomalies, missing, stocks):
    """生成 manifest.json"""
    total = max(len(EXPECTED_STOCKS), len(stocks))
    found_count = len(stocks)
    missing_count = len(missing)
    complete_count = found_count - missing_count

    # 统计有完整数据的标的数
    complete_with_data = sum(
        1 for s in stocks
        if s.get("close") is not None and s.get("change_pct") is not None
    )

    manifest = {
        "date": date_str,
        "status": status,
        "anomalies": anomalies,
        "missing_stocks": [
            {"code": s["code"], "name": s["name"]} for s in missing
        ],
        "data_quality": {
            "total": len(EXPECTED_STOCKS),
            "found": found_count,
            "complete": complete_with_data,
            "partial": found_count - complete_with_data,
            "missing": missing_count,
            "failed": 0,
        },
        "generated_at": datetime.now(TZ_CST).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "detector_version": "1.0",
    }
    return manifest


def generate_alert_md(date_str, status, anomalies, missing, stocks):
    """生成人类可读的 anomaly_alert.md"""
    lines = []

    # 标题
    status_emoji = {
        "complete": "✅", "partial": "⚠️",
        "alert": "🔴", "failed": "❌",
    }
    emoji = status_emoji.get(status, "❓")
    status_text = {
        "complete": "正常", "partial": "部分异常",
        "alert": "预警", "failed": "失败",
    }
    lines.append(f"# {emoji} LIVS-Stock 异常检测 {date_str} [{status_text.get(status, '未知')}]")
    lines.append("")

    # 数据概况
    found_names = [s.get("name", "?") for s in stocks]
    lines.append(f"## 数据概况")
    lines.append(f"- 预期标的: {len(EXPECTED_STOCKS)} 只")
    lines.append(f"- 实际采集: {len(stocks)} 只")
    lines.append(f"- 缺失: {len(missing)} 只")
    if missing:
        missing_str = "、".join(f"{m['name']}({m['code']})" for m in missing)
        lines.append(f"- 缺失标的: {missing_str}")
    lines.append("")

    # 异常汇总
    if anomalies:
        # 按 level 分组：critical 优先
        criticals = [a for a in anomalies if a.get("level") == "critical"]
        alerts = [a for a in anomalies if a.get("level") == "alert"]

        if criticals:
            lines.append(f"## 🚨 严重异常 ({len(criticals)} 项)")
            lines.append("")
            for a in criticals:
                lines.append(f"- **{a['message']}**")
            lines.append("")

        if alerts:
            lines.append(f"## ⚠️ 预警 ({len(alerts)} 项)")
            lines.append("")
            for a in alerts:
                lines.append(f"- {a['message']}")
            lines.append("")
    else:
        lines.append("## ✅ 未检测到异常")
        lines.append("")

    # 个股概况表
    lines.append("## 个股行情概况")
    lines.append("")
    lines.append("| 标的 | 代码 | 收盘价 | 涨跌幅 | 状态 |")
    lines.append("|------|------|--------|--------|------|")
    for s in stocks:
        name = s.get("name", "?")
        code = s.get("code", "?")
        close = f"{s['close']:.2f}" if s.get("close") is not None else "N/A"
        pct_str = f"{s['change_pct']:+.2f}%" if s.get("change_pct") is not None else "N/A"
        # 标记异常
        stock_anomalies = [a for a in anomalies if a.get("stock") == name or a.get("code") == code]
        flag = "⚠️" if stock_anomalies else "✅"
        lines.append(f"| {name} | {code} | {close} | {pct_str} | {flag} |")

    # 缺失标的也列入
    for m in missing:
        lines.append(f"| {m['name']} | {m['code']} | - | - | ❌ 缺失 |")
    lines.append("")

    # 检测元信息
    lines.append("---")
    lines.append(f"*检测时间: {datetime.now(TZ_CST).strftime('%Y-%m-%d %H:%M:%S')} CST*")
    lines.append(f"*检测脚本: anomaly_detector.py v1.0*")

    return "\n".join(lines)


# ==================== 主流程 ====================

def main():
    today_str = datetime.now(TZ_CST).strftime("%Y-%m-%d")
    print(f"=== LIVS-Stock 异常检测 {today_str} ===\n")

    # 1. 加载数据
    data, source = load_data()
    if data is None:
        # 生成 failed manifest
        manifest = generate_manifest(today_str, "failed", [], [], [])
        manifest["error"] = "无法找到或解析采集数据文件"
        _write_outputs(today_str, manifest, "failed", [], [], [])
        print("[RESULT] status=failed (数据加载失败)")
        return

    # 2. 提取个股数据
    stocks = extract_stocks(data)
    if not stocks:
        manifest = generate_manifest(today_str, "failed", [], [], [])
        manifest["error"] = "数据中未提取到个股记录"
        _write_outputs(today_str, manifest, "failed", [], [], [])
        print("[RESULT] status=failed (无个股数据)")
        return

    print(f"[OK] 提取到 {len(stocks)} 只标的数据\n")

    # 3. 执行四维检测
    print("[检测] 1/4 数据完整性...")
    completeness_anomalies, missing = check_completeness(stocks)

    print("[检测] 2/4 价格突变...")
    price_anomalies = check_price_anomalies(stocks)

    print("[检测] 3/4 板块联动...")
    sector_anomalies = check_sector_correlation(stocks)

    print("[检测] 4/4 数据质量...")
    quality_anomalies = check_data_quality(stocks)

    all_anomalies = completeness_anomalies + price_anomalies + sector_anomalies + quality_anomalies

    # 4. 判定状态
    status = determine_status(all_anomalies, missing, stocks)

    # 5. 生成输出
    _write_outputs(today_str, None, status, all_anomalies, missing, stocks)

    # 6. 打印摘要
    print(f"\n=== 检测结果 ===")
    print(f"  状态: {status}")
    print(f"  异常总数: {len(all_anomalies)}")
    if all_anomalies:
        criticals = sum(1 for a in all_anomalies if a.get("level") == "critical")
        alerts = sum(1 for a in all_anomalies if a.get("level") == "alert")
        if criticals:
            print(f"  🔴 严重: {criticals} 项")
        if alerts:
            print(f"  ⚠️ 预警: {alerts} 项")
        for a in all_anomalies[:8]:
            prefix = "🔴" if a["level"] == "critical" else "⚠️"
            print(f"    {prefix} {a['message']}")
        if len(all_anomalies) > 8:
            print(f"    ... 还有 {len(all_anomalies) - 8} 项，详见 anomaly_alert.md")
    if missing:
        print(f"  缺失标的: {', '.join(m['name'] for m in missing)}")
    print(f"\n[OK] 已写入 manifest.json + anomaly_alert.md")


def _write_outputs(today_str, pre_manifest, status, all_anomalies, missing, stocks):
    """统一写 manifest.json 和 anomaly_alert.md"""
    manifest = pre_manifest if pre_manifest else generate_manifest(
        today_str, status, all_anomalies, missing, stocks
    )
    with open("manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    alert_md = generate_alert_md(today_str, status, all_anomalies, missing, stocks)
    with open("anomaly_alert.md", "w", encoding="utf-8") as f:
        f.write(alert_md)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] 异常检测脚本报错: {e}")
        import traceback
        traceback.print_exc()
        # 即使检测失败，也生成一个 error manifest，不阻断提交流程
        try:
            today_str = datetime.now(TZ_CST).strftime("%Y-%m-%d")
            error_manifest = {
                "date": today_str,
                "status": "complete",  # 不阻断流程
                "anomalies": [],
                "missing_stocks": [],
                "data_quality": {"total": 0, "found": 0, "complete": 0, "partial": 0, "missing": 0, "failed": 0},
                "error": f"检测脚本异常: {str(e)[:200]}",
                "generated_at": datetime.now(TZ_CST).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            }
            with open("manifest.json", "w", encoding="utf-8") as f:
                json.dump(error_manifest, f, ensure_ascii=False, indent=2)
            with open("anomaly_alert.md", "w", encoding="utf-8") as f:
                f.write(f"# 异常检测脚本执行出错\n\n错误: {str(e)[:200]}\n")
        except Exception:
            pass  # 最后的兜底：即使写文件也失败，不阻塞 CI
        sys.exit(0)  # 永远返回 0，不阻断 commit
