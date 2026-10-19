#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_dahai_format.py — WB 预测 → 大海 schema 适配器（v1.0，2026-10-16）

【背景】
大海 2026-10-16 发来《给WB-JSON格式要求.md》，要求 WB 推送到
  predictions/YYYY-MM-DD.json
且字段结构与他 predictions-dahai/YYYY-MM-DD_dahai.json 完全一致。

【本脚本做什么】
读 WB 内部格式 predictions/{date}.json（schema v2.5），
按大海 schema 输出 predictions/{date}.json 的"对齐版"，
字段映射见下方 MAP 表，关键补充：
  - market_outlook   ← WB market_direction + market_env + t1_prediction
  - index_summary    ← WB _tech(上证) / market_env
  - stock_calls[]    ← WB stock_calls[]（核心6只全覆盖）
  - summary          ← 由 action 计数汇总
  - iron_rules       ← WB 信号规则说明（与大海铁律对齐口径）
  - kelly            ← 【新增】统一凯利分级口径（老板 10/16 拍板）

【统一凯利分级口径】（与大海上轮达成一致的提案）
  tier 由「样本外验证 + FDR 多重检验」共同决定：
    A 级 = 过样本外 且 过 FDR          → 给仓位（半凯利）
    B 级 = 仅过样本外，未过 FDR        → 给仓位（半凯利 × 0.5 折减）
    C 级 = 仅训练段显著               → 不给仓位
    D 级 = 均不显著                   → 不给仓位
  硬约束：单标的 ≤20% / 总仓 ≤60% / 低于 2% 不操作

【用法】
  python3 export_dahai_format.py                      # 处理今天
  python3 export_dahai_format.py --date 2026-10-16    # 指定日期
  python3 export_dahai_format.py --date 2026-10-16 --push   # 生成并推送
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRED_DIR = os.path.join(BASE_DIR, "predictions")
sys.path.insert(0, os.path.join(BASE_DIR, "risk_report"))

# 核心6只（大海要求"覆盖核心6只标的"）
CORE6 = ["紫金矿业", "北方稀土", "华友钴业", "金钼股份", "半导体ETF国联安", "五粮液"]
CORE6_ALIAS = {"半导体ETF国联安": "半导体ETF", "半导体ETF": "半导体ETF国联安"}

# 方向映射：WB(up/down/neutral) → 大海(bullish/bearish/neutral)
DIR_MAP = {"up": "bullish", "down": "bearish", "neutral": "neutral",
           "bullish": "bullish", "bearish": "bearish"}


# 名称别名（对齐 facts 层 / 大海文件 / WB 三方命名）
NAME_ALIAS = {
    "半导体ETF国联安": "半导体ETF",
    "沪深300ETF": "沪深300",
    "中证500ETF": "中证500",
    "创业板ETF": "创业板指",
}

SECTOR_MAP = {
    "紫金矿业": "有色金属", "北方稀土": "有色金属", "华友钴业": "有色金属",
    "金钼股份": "有色金属", "锡业股份": "有色金属", "湖南黄金": "有色金属",
    "五粮液": "白酒消费", "贵州茅台": "白酒消费",
    "半导体ETF国联安": "半导体", "半导体ETF": "半导体",
    "工商银行": "银行", "农业银行": "银行", "宁波银行": "银行",
    "隆基绿能": "光伏", "中国神华": "煤炭", "宝钢股份": "钢铁",
    "航发动力": "军工", "东方电气": "电力设备",
}


def _macd_detail(call):
    """WB 侧 MACD 是简写(above/below/golden_cross)，转成大海的明细文本"""
    m = call.get("_tech", {}).get("macd", "unknown")
    return {"above": "DIF>DEA 多头", "below": "DIF<DEA 空头",
            "golden_cross": "MACD 金叉", "death_cross": "MACD 死叉",
            "diverging": "DIF<DEA 空头扩大"}.get(m, m)


def _ten_day(facts_map, name, tech):
    """10日涨跌幅：以 facts 事实层为唯一来源（双方必须同源）。"""
    f = facts_map.get(name) or facts_map.get(NAME_ALIAS.get(name, name))
    if f and f.get("ten_day_chg") is not None:
        return f["ten_day_chg"]
    return tech.get("ten_day_chg")   # 兜底：Tech 里的同名字段


def load_facts(date):
    """读当日事实包，构建 name -> fact 映射。缺包则返回空字典（不阻断）。"""
    p = os.path.join(BASE_DIR, "facts", f"{date}.json")
    if not os.path.exists(p):
        return {}
    j = json.load(open(p, encoding="utf-8"))
    return {s["name"]: s for s in j.get("stocks", []) + j.get("indices", [])}


def load_kelly():
    try:
        import kelly
        return kelly
    except Exception:
        return None


def kelly_for_call(call, kelly_mod):
    """
    对单只标的给出统一凯利分级口径。
    返回 dict 或 None（C/D 级不给仓位）。
    """
    if kelly_mod is None:
        return None
    t = call.get("_tech", {})
    action = call.get("action", "hold")
    rsi6 = t.get("rsi6")
    dd30 = t.get("dd30", 0.0)
    chg = t.get("chg_today", 0.0)
    macd = t.get("macd", "above")

    # --- 信号分级（与 risk_report 的 A/B/C/D 同口径）---
    # A：RSI6 极端超卖 + MACD 未恶化（近似"过样本外+过FDR"的强信号）
    # B：RSI6 超卖 或 MACD 金叉，但另一项不满足
    # C/D：无有效信号
    tier = None
    p = 0.0

    if action == "buy":
        # WB 已判 buy → 至少 B 级；若 RSI6<25 且 macd != 恶化 → 提 A
        if rsi6 is not None and rsi6 < 25 and macd in ("above", "golden_cross"):
            tier = "A"
        elif rsi6 is not None and rsi6 < 30:
            tier = "B"
        elif macd == "golden_cross":
            tier = "B"
        else:
            tier = "C"

    if tier is None:
        return {"tier": None, "f_final": 0.0, "below_min": True,
                "note": "非买入信号，不给仓位"}

    # 胜率估计：A 级用样本外胜率经验值，B 级折减
    # 注意：tier_mult（A=1.0 / B=0.5）已在 kelly.py 内生效，
    # 这里 extra_mult 不再重复惩罚 B 级，避免双重折减（口径简洁）
    if tier == "A":
        p = 0.60
        extra = 1.0
    elif tier == "B":
        p = 0.54
        extra = 1.0
    else:
        return {"tier": tier, "f_final": 0.0, "below_min": True,
                "note": "C 级信号，不给仓位"}

    b = 1.5  # 赔率：凯利 b=1.5（对应赔率 1.5:1）
    # [口径修正 2026-10-16] drift_pt 是「规则 edge 衰减幅度(pt)」，量级 0~12，
    # 不是股价回撤百分比。之前误用 abs(dd30)=13.65pt → 落入 >10pt 档被清零。
    # 这里按信号强度估计 edge 衰减：A 级衰减小(2pt)，B 级中等(4pt)。
    drift = 2.0 if tier == "A" else 4.0
    k = kelly_mod.from_stats(p, b * 100, 100, tier=tier,
                             drift_pt=drift, extra_mult=extra)
    k["tier"] = tier
    return k


def build_dahai_schema(wb, ks=None, facts_map=None):
    """把 WB schema v2.5 → 大海 schema（facts_map 提供事实层真值）"""
    facts_map = facts_map or {}
    date = wb.get("predict_date")
    env = wb.get("_market_environment", {}) or {}
    idx = env.get("index", {}) or {}
    t1 = wb.get("t1_prediction", {}) or {}
    sh = t1.get("sh_index", {}) or {}

    direction = wb.get("market_direction", "neutral")
    conf_raw = wb.get("market_confidence", 0.5)
    # 大海用 0-100 整数表示置信度
    conf100 = int(round(conf_raw * 100))

    out = {
        "date": date,
        "predict_date": wb.get("target_date", date),
        "model_version": "WB-v2.5+" + (wb.get("_生成方式", "").split("@")[0].strip()[-8:] if wb.get("_生成方式") else ""),
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "data_basis": wb.get("data_basis", {}).get("as_of", date) + "收盘数据",
        "type": "T+1_evening",
        "market_outlook": {
            "direction": DIR_MAP.get(direction, "neutral"),
            "confidence": conf100,
            "sse_close": sh.get("close_ref"),
            "sse_change_pct": idx.get("chg_today_pct"),
            "reason": (f"M_sse v2.5={env.get('M_sse_v2p5')}"
                       f"（v1={env.get('M_sse_v1')}），"
                       f"市场宽度={env.get('breadth_detail', {}).get('up_ratio')}，"
                       f"Kuramoto R={idx.get('r_value_kuramoto')}。"
                       f"阈值区间[{sh.get('range_pct', [None, None])[0]}, "
                       f"{sh.get('range_pct', [None, None])[1]}]%，"
                       f"中心{sh.get('center_pct')}%。"),
            "risk_level": ("HIGH" if abs(idx.get("chg_today_pct") or 0) > 2
                           else "MODERATE" if abs(idx.get("chg_today_pct") or 0) > 1
                           else "LOW"),
            "market_gate": ("pass" if (idx.get("chg_today_pct") or 0) > -2 else "fail"),
            "market_gate_detail": f"上证当日{idx.get('chg_today_pct')}%"
                                  f"（门限 -2%），10日R_15={idx.get('r_15')}",
        },
        "index_summary": {
            "close": sh.get("close_ref"),
            "change_percent": idx.get("chg_today_pct"),
            "ten_day_change": idx.get("r_15"),
            "amplitude_pct": idx.get("amplitude_pct"),
            "ma20_dev_pct": idx.get("ma20_dev"),
        },
        "stock_calls": [],
        "summary": {},
        "iron_rules": {
            "rule_1_env": f"M_sse v2.5={env.get('M_sse_v2p5')}，"
                          f"{'偏多' if (env.get('M_sse_v2p5') or 0) >= 60 else '中性' if (env.get('M_sse_v2p5') or 0) >= 40 else '偏空'}",
            "rule_2_rsi": f"RSI6 自适应阈值（动态量比阈值 {env.get('vol_ratio_threshold')}）",
            "rule_3_macd": "MACD 恶化一票否决（与大海铁律3同口径）",
            "rule_4_gate": "上证涨跌 > -2% 才有买入资格",
            "rule_5_entry": "量比≥动态阈值 / MACD金叉 / RSI6<自适应阈值 三选一",
            "rule_6_dual": "双模型方向一致且均为买入信号才建议买",
        },
    }

    # --- stock_calls 映射 ---
    for c in wb.get("stock_calls", []):
        name = c.get("name")
        is_core = c.get("core_pool", False) or name in CORE6 or name in CORE6_ALIAS
        t = c.get("_tech", {}) or {}
        k = kelly_for_call(c, ks) if is_core else None
        item = {
            "code": c.get("code"),
            "name": name,
            "direction": DIR_MAP.get(c.get("direction", "neutral"), "neutral"),
            "confidence": conf100 if is_core else int(round(conf_raw * 70)),
            "action": c.get("action", "hold"),
            "close": t.get("price"),
            "change_pct": t.get("chg_today"),
            # [修正 2026-10-19] 此前把 dd30(30日回撤) 误写进 ten_day_chg，
            # 导致紫金矿业填 -13.65 而实际 10 日仅 -0.3%（差 13.35pt）。
            # 现在优先从 facts 事实层取真值，取不到才用 _tech.dd30 并标注。
            "ten_day_chg": _ten_day(facts_map, name, t),
            "kelly_grade": (k.get("tier") if k and k.get("tier")
                            else ("D" if c.get("action") in ("hold", "watch", "avoid")
                                  else "C")),
            "rsi6": t.get("rsi6"),
            "macd_signal": t.get("macd"),
            "macd_detail": _macd_detail(c),
            "ma20_dev": t.get("ma20_dev"),
            "vol_ratio": t.get("vol_ratio"),
            "trend": c.get("direction", "neutral"),
            "reason": c.get("reason"),
            "sector": SECTOR_MAP.get(name, "其他"),
            "core_pool": bool(is_core),
        }
        if k is not None:
            item["kelly"] = {
                "tier": k.get("tier"),
                "f_raw": round(k.get("f_raw", 0), 4),
                "f_final": round(k.get("f_final", 0), 4),
                "f_final_pct": round(k.get("f_final", 0) * 100, 2),
                "below_min": k.get("below_min", True),
                "note": k.get("note", ""),
                "caliber": "A=全凯利×半凯利; B=全凯利×半凯利×0.5; C/D=不给仓位",
                "hard_limits": {"max_single": 0.20, "max_total": 0.60, "min_bet": 0.02},
            }
        out["stock_calls"].append(item)

    # --- summary 汇总 ---
    from collections import Counter
    cnt = Counter(c["action"] for c in out["stock_calls"])
    core_calls = [c for c in out["stock_calls"] if c.get("core_pool")]
    buy_core = [c["name"] for c in core_calls if c["action"] == "buy"]
    out["summary"] = {
        "hold": cnt.get("hold", 0),
        "watch": cnt.get("watch", 0),
        "buy": cnt.get("buy", 0),
        "avoid": cnt.get("avoid", 0),
        "buy_probe": len([c for c in core_calls if c["action"] == "buy"]),   # 试探仓（B级）
        "buy_standard": 0,                                                    # 标准仓（A级，需双方一致）
        "core_count": len(core_calls),
        "core_buy": buy_core,
        "overall": (f"WB 侧：大盘 {direction}（M_sse={env.get('M_sse_v2p5')}）；"
                    f"核心池 {len(core_calls)} 只，买入信号 {buy_core if buy_core else '无'}。"),
        "key_signals": [
            f"{c['name']} [{c['action']}] {c.get('reason','')}"
            for c in core_calls
        ],
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--push", action="store_true", help="生成后推送到 GitHub")
    args = ap.parse_args()

    src = os.path.join(PRED_DIR, f"{args.date}.json")
    if not os.path.exists(src):
        print(f"[ERR] 源文件不存在：{src}")
        print("      请先跑：python3 daily_predict.py --date " + args.date)
        return 1

    wb = json.load(open(src, encoding="utf-8"))
    ks = load_kelly()
    facts_map = load_facts(args.date)
    if not facts_map:
        print("[WARN] 无事实包，ten_day_chg 退回 Tech 值")
    out = build_dahai_schema(wb, ks, facts_map)

    dst = os.path.join(PRED_DIR, f"{args.date}.json")
    json.dump(out, open(dst, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"[OK] 已生成对齐版 → {dst} ({os.path.getsize(dst)} bytes)")
    print(f"     核心池 {out['summary']['core_count']} 只，"
          f"买入 {out['summary']['core_buy']}")
    for c in out["stock_calls"]:
        if c.get("core_pool") and "kelly" in c:
            kk = c["kelly"]
            print(f"     {c['name']:10s} tier={kk['tier']} "
                  f"凯利={kk['f_final_pct']}% {'(低于门槛)' if kk['below_min'] else ''}")

    if args.push:
        r = subprocess.run([sys.executable, os.path.join(BASE_DIR, "push_api.py"),
                            f"predictions/{args.date}.json"],
                           capture_output=True, text=True, timeout=90)
        print(r.stdout.strip().splitlines()[-1] if r.stdout else r.stderr[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
