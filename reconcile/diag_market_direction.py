#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
洞1 字段映射诊断脚本 —— 用真实仓库数据，判定 extract_market_direction() 的每个搜索源是否命中。

用法：
    python3 reconcile/diag_market_direction.py

结论导向：
    如果 6 个搜索源全部 miss，说明函数恒返回 unknown，
    洞3 的 fail-closed 会把它一律拦成 HIGH —— 等于方向判断能力被废掉。
"""
import json
import glob
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# align_core6.py 里声明的 6 个搜索源（按优先级）
SEARCH_SOURCES = [
    ("market_overview.direction", ["market_overview", "direction"]),
    ("market_outlook.direction", ["market_outlook", "direction"]),
    ("_market_environment.index.env_label", ["_market_environment", "index", "env_label"]),
    ("_market_environment.index.chg_today_pct", ["_market_environment", "index", "chg_today_pct"]),
    ("market_direction", ["market_direction"]),
    ("eod_quotes.indices", ["eod_quotes", "indices"]),
    ("t1_prediction.sh_index.direction", ["t1_prediction", "sh_index", "direction"]),
]


def walk_get(obj, path):
    """按路径取值，任何一级缺失返回 (False, None)"""
    cur = obj
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return False, None
        cur = cur[k]
    return True, cur


def diag_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return None, str(e)
    hits = {}
    for name, p in SEARCH_SOURCES:
        ok, val = walk_get(data, p)
        hits[name] = (ok, val)
    return hits, None


def main():
    files = sorted(glob.glob(os.path.join(REPO, "output", "stock_data_*.json")))
    if not files:
        print("未找到 output/stock_data_*.json")
        return 1

    print("=" * 78)
    print("洞1 字段映射诊断 —— 对照真实 WB 数据文件")
    print("=" * 78)

    overall = {name: 0 for name, _ in SEARCH_SOURCES}
    top_keys_seen = set()

    for f in files:
        hits, err = diag_file(f)
        date = os.path.basename(f).replace("stock_data_", "").replace(".json", "")
        if err:
            print(f"{date}  读取失败: {err}")
            continue
        with open(f, encoding="utf-8") as fh:
            top_keys_seen.update(json.load(fh).keys())
        got = [name for name, (ok, _) in hits.items() if ok]
        for name in got:
            overall[name] += 1
        mark = "✅ 命中" if got else "❌ 全部 miss → 返回 unknown"
        print(f"{date}  {mark}" + (f"  命中: {got}" if got else ""))

    print()
    print("-" * 78)
    print("各搜索源命中次数（共 %d 个文件）:" % len(files))
    for name, _ in SEARCH_SOURCES:
        c = overall[name]
        flag = "✅" if c > 0 else "❌"
        print(f"  {flag}  {name:<45} {c}/{len(files)}")

    print()
    print("-" * 78)
    print("WB 数据文件实际拥有的顶层字段:")
    for k in sorted(top_keys_seen):
        print(f"  · {k}")

    print()
    print("=" * 78)
    total_hits = sum(overall.values())
    if total_hits == 0:
        print("结论：所有搜索源全部 miss —— extract_market_direction() 恒返回 unknown。")
        print("      → 洞3 fail-closed 会把 WB 侧一律判为 HIGH，方向判断能力实际失效。")
    else:
        print(f"结论：共 {total_hits} 次命中。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
