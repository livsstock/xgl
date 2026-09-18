# LIVS-Stock 异常检测系统说明文档

## 概述

异常检测系统集成在 GitHub Actions 每日数据采集中，在 `daily_collect.py` 采集完成后自动执行，对采集数据进行四维质量检查。

**核心原则**：异常检测不阻断数据提交流程，即使检测脚本出错也会继续 commit。

## 文件清单

| 文件 | 说明 |
|------|------|
| `scripts/anomaly_detector.py` | 异常检测主脚本 |
| `scripts/daily-collect.yml` | GitHub Actions 工作流（已集成异常检测步骤） |
| `scripts/daily_collect.py` | 每日数据采集脚本（原有，未修改） |

## 检测维度

### 1. 数据完整性（completeness）

- **标的覆盖**：检查 12 只监控标的是否都有数据
- **关键字段**：检查 `close`（收盘价）和 `change_pct`（涨跌幅）是否存在

**12 只监控标的**：

| 板块 | 标的 |
|------|------|
| 有色金属 | 锡业股份(sz000960)、白银有色(sh601212)、盛和资源(sh600392)、金钼股份(sh601958) |
| 稀土 | 北方稀土(sh600111) |
| 黄金 | 湖南黄金(sz002155)、黄金ETF(sh518880) |
| 科技 | 京东方A(sz000725)、机器人ETF(sz159770)、半导体ETF(sh512480) |
| 钢铁 | 宝钢股份(sh600019) |
| 军工 | 航发动力(sh600893) |

### 2. 价格突变（price_anomaly）

| 条件 | 级别 | 说明 |
|------|------|------|
| 单日跌幅 ≥ 8% | 🔴 critical | 紧急预警 |
| 单日跌幅 ≥ 5% | ⚠️ alert | 异常预警 |
| 单日涨幅 ≥ 5% | ⚠️ alert | 异常预警 |
| 连续3日下跌且累计 ≥ 10% | ⚠️ alert | 需历史数据支持 |

### 3. 板块联动（sector_correlation）

| 条件 | 级别 |
|------|------|
| 同板块 ≥ 3只同时跌 ≥ 3% | ⚠️ alert |
| 全部12只平均跌幅 ≥ 2% | ⚠️ market_alert |

### 4. 数据质量（data_quality）

| 条件 | 级别 |
|------|------|
| 价格 ≤ 0 | 🔴 data_error |
| 换手率 < 0.1% 或 > 20% | ⚠️ volume_anomaly |
| ETF溢价偏离 NAV ≥ 3% | ⚠️ etf_premium_alert（待数据源支持） |

## 输出文件

### manifest.json（机器可读）

```json
{
  "date": "2026-09-18",
  "status": "complete|partial|alert|failed",
  "anomalies": [...],
  "missing_stocks": [...],
  "data_quality": {
    "total": 12,
    "found": 11,
    "complete": 10,
    "partial": 1,
    "missing": 1,
    "failed": 0
  },
  "generated_at": "2026-09-18T08:01:30+08:00"
}
```

### anomaly_alert.md（人类可读）

包含：
- 检测状态标题（✅/⚠️/🔴/❌）
- 数据概况
- 异常汇总（按严重级别分组）
- 个股行情表格

## 状态判定逻辑

```
优先级从高到低：
1. failed  → 存在 critical 级别异常 或 数据错误
2. alert   → 存在 alert 级别异常
3. partial → 有缺失标的但无异常
4. complete → 一切正常
```

## 大海侧读取逻辑

大海在读取 `stock.json` 数据时，应同时检查 `manifest.json`：

| manifest.status | 大海行为 |
|----------------|---------|
| `complete` | 正常流程，按盘前9:00执行分析 |
| `alert` | **立即触发紧急分析**，不等盘前时间 |
| `failed` | 通知用户数据采集中断，检查 GitHub Actions 日志 |
| `partial` | 正常流程，但标注缺失标的 |

### 建议的大海报脚本逻辑

```python
import json, os

def check_data_status():
    """检查最新采集数据状态"""
    if not os.path.exists("manifest.json"):
        return "unknown", "manifest.json 不存在"

    with open("manifest.json", "r", encoding="utf-8") as f:
        manifest = json.load(f)

    status = manifest.get("status", "unknown")
    anomalies = manifest.get("anomalies", [])
    missing = manifest.get("missing_stocks", [])

    if status == "failed":
        error = manifest.get("error", "未知原因")
        return "failed", f"数据采集失败: {error}"

    if status == "alert":
        criticals = [a for a in anomalies if a.get("level") == "critical"]
        alerts = [a for a in anomalies if a.get("level") == "alert"]
        msg_parts = []
        if criticals:
            msg_parts.append(f"🔴 严重异常 {len(criticals)} 项")
        if alerts:
            msg_parts.append(f"⚠️ 预警 {len(alerts)} 项")
        return "alert", " | ".join(msg_parts)

    if status == "partial" and missing:
        names = ", ".join(m.get("name", "?") for m in missing)
        return "partial", f"缺失标的: {names}"

    return "complete", "数据完整，无异常"
```

## 自适应设计

脚本支持多种数据格式：

1. **文件查找**：`stock.json` → `stock.josn` → `output/daily-finance-*.json`
2. **数据提取**：
   - `eod_quotes.watchlist` 数组（当前 daily_collect.py 格式）
   - 顶层 `stocks` 数组
   - `data` 数组
   - dict-of-dict 格式（code → info）

## 工作流变更说明

### 修改点

1. 在 "Run collection script" 后新增 "Anomaly Detection" 步骤
2. "Commit and push results" 步骤增强：
   - `git add` 增加 `manifest.json` 和 `anomaly_alert.md`
   - 根据异常状态生成不同的 commit message
   - `git commit` 添加 `|| true` 防止无变更时失败

### 兼容性

- 异常检测失败不会阻断 CI（脚本以 `sys.exit(0)` 兜底）
- `manifest.json` 和 `anomaly_alert.md` 是新增文件，不影响原有数据流
- `git commit || true` 确保无变更时不报错

## 后续扩展

1. **连续下跌检测**：需要接入历史数据（如读取最近3天的 output 文件）
2. **ETF溢价检测**：需要数据源提供 NAV（净值）数据
3. **北向资金异动**：东财2024/8起停披，若未来恢复可添加
4. **自动通知**：可对接 webhook 推送告警到飞书/微信
5. **股票池同步**：当 daily_collect.py 更新 WATCHLIST 后，同步更新 EXPECTED_STOCKS

## 注意事项

- **文件名差异**：daily_collect.py 输出 `stock.josn`（拼写错误），检测脚本同时兼容 `stock.json` 和 `stock.josn`
- **标的池差异**：当前 daily_collect.py 的 WATCHLIST 包含 11 只标的（含银行、指数），与异常检测的 12 只监控标的不同。当 WATCHLIST 更新后，两侧将自动对齐
- **纯标准库**：脚本不依赖任何第三方 Python 包，仅需 Python 3.8+
