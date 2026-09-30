# -*- coding: utf-8 -*-
"""验证：补上洞1/洞3后，9/21 能否被拦下"""
import json

def normalize_direction(d):
    if not d: return "unknown"
    dl = d.lower().strip()
    m = {"偏多":"bullish","多":"bullish","涨":"bullish",
         "偏空":"bearish","空":"bearish","跌":"bearish",
         "中性":"neutral","中":"neutral","平":"neutral","观望":"neutral",
         "up":"bullish","down":"bearish","neutral":"neutral",
         "bullish":"bullish","bearish":"bearish","hold":"neutral","watch":"neutral"}
    return m.get(dl, dl)

# ---- 修洞1：多路径读取 ----
def extract_fixed(data, source="wb"):
    paths = [
        ("market_outlook.direction", lambda d: d.get("market_outlook",{}).get("direction")),
        ("market_direction",         lambda d: d.get("market_direction")),
        ("market_environment.label", lambda d: d.get("market_environment",{}).get("label")),
        ("market_environment.direction", lambda d: d.get("market_environment",{}).get("direction")),
        ("t1_prediction.sh_index.direction", lambda d: d.get("t1_prediction",{}).get("sh_index",{}).get("direction")),
    ]
    for name, fn in paths:
        v = fn(data)
        if v:
            return normalize_direction(v), name
    return "unknown", None

# ---- 修洞3：unknown 改为 fail-closed ----
def gate_fixed(wb_dir, dh_dir):
    KNOWN = ("bullish","bearish","neutral")
    if wb_dir not in KNOWN or dh_dir not in KNOWN:
        return True, "UNKNOWN", "⚪ 方向读取失败 — fail-closed 拦下"
    if wb_dir == "bearish" and dh_dir == "bearish":
        return True, "CRITICAL", "🔴 双方看空 — 冻结"
    if wb_dir == "bearish" or dh_dir == "bearish":
        return True, "HIGH", "🟠 一方看空 — 暂停超跌"
    if wb_dir == "neutral" and dh_dir == "neutral":
        return False, "MODERATE", "🟡 双方中性"
    return False, "NORMAL", "🟢 偏多 — 放行"

print("="*68)
print("9/21 复现验证")
print("="*68)

wb = json.load(open('dist/predictions/wb-2026-09-21.json'))
dh = json.load(open('dist/predictions/dahai-2026-09-21.json'))

wb_d, wb_src = extract_fixed(wb, 'wb')
dh_d, dh_src = extract_fixed(dh, 'dahai')
print(f"\nWB   方向 = {wb_d:8} (来自 {wb_src})")
print(f"大海 方向 = {dh_d:8} (来自 {dh_src})")

print("\n--- 场景A: 只修洞1（能读到大海 label） ---")
hi, lvl, msg = gate_fixed(wb_d, dh_d)
print(f"判定: {lvl}  is_high_risk={hi}")
print(f"{msg}")
print(f"→ {'✅ 拦下' if hi else '❌ 仍放行 —— 因为双方都判偏多'}")

print("\n--- 场景B: 洞1未修（大海读不到） + 修洞3 fail-closed ---")
hi2, lvl2, msg2 = gate_fixed(wb_d, "unknown")
print(f"判定: {lvl2}  is_high_risk={hi2}")
print(f"→ {'✅ 拦下' if hi2 else '❌ 仍放行'}")

print("\n" + "="*68)
print("结论")
print("="*68)
print("""
洞1修复后，大海方向 = bullish("偏多")
  → 双方都是偏多 → NORMAL → 仍然放行

也就是说：**洞1和洞3都修好，9/21 依然拦不住。**
真正的问题是 9/21 双方方向判断本身就是"偏多"。
""")
