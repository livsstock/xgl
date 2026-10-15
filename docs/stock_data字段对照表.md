# stock_data 字段对照表（大海 → v2.1）

> 配套 `livsstock-deploy-v2.zip`
> 时间：2026/09/18
> 目的：大海的 `daily_forecast.py` 拿到 stock_data 后，知道每个字段在他预测模型里对应什么信号

---

## 一、一句话映射

```
v2.1 stock_data["stocks"][i]  →  你预测模型里的"一只标的"实例
v2.1 stock_data["stocks"][i]["technical"]  →  你之前自己算的 MACD/RSI 等指标
```

**以前**你得拉 26+ 天 K 线自己算 MACD，现在 WorkBuddy 直接给你**算好**塞进 `technical` 块里。

---

## 二、字段映射表（按你之前的预测思路）

| 你之前的输入 | v2.1 字段 | 计算方法 |
|---|---|---|
| MACD.DIF | `technical.macd.dif` | EMA(12) - EMA(26) |
| MACD.DEA | `technical.macd.dea` | EMA(9) of DIF |
| MACD.MACD 柱 | `technical.macd.macd` | (DIF - DEA) × 2 |
| 金叉/死叉信号 | `technical.macd.signal` | `"golden_cross"` / `"death_cross"` / `"neutral"` |
| RSI(6) | `technical.rsi6` | Wilder 平滑，0-100 |
| 10日累计涨幅 | `technical.change_10d_pct` | (今日收 - 10日前收) / 10日前收 × 100 |
| 量比 | `technical.volume_ratio` | 今日成交量 / 5日均量 |
| 换手率 | `technical.turnover_rate_pct` | 字段 38，单位 % |
| 现价 | `close` | 字段 3 |
| 昨收 | `pre_close` | 字段 4 |
| 开盘 | `open` | 字段 5 |
| 最高 | `high` | 字段 33 |
| 最低 | `low` | 字段 34 |
| 成交量(手) | `volume_hand` | 字段 6 |
| 成交额(万) | `turnover_wan` | 字段 37 |
| 成交额(亿) | `turnover_yi` | 字段 37 / 10000 |
| 涨跌幅 | `change_pct` | 字段 32 |

---

## 三、新增的两个评分字段（v2.1 独创）

| 字段 | 范围 | 含义 | 你可以怎么用 |
|---|---|---|---|
| `technical.oversold_score` | 0-100 | 超跌强度 | ≥60 → 强超跌候选 / ≥40 → 一般超跌 / <20 → 不超跌 |
| `technical.trend_score` | 0-100 | 趋势强度 | ≥70 → 强势 / 40-70 → 中性偏多 / <40 → 弱势 |

### 超跌评分构成（你能直接复用逻辑）

```
rsi6 < 20    → +40
rsi6 < 30    → +25
rsi6 < 40    → +10
10日跌 < -20% → +40
10日跌 < -15% → +30
10日跌 < -10% → +20
10日跌 < -5%  → +10
MACD 底背离   → +20
上限 100
```

### 趋势评分构成

```
MACD 金叉 → +30
MACD 死叉 → -20
收 > MA20  → +20
MA5 > MA10 → +15
收 > MA5   → +15
收 > MA10  → +10
上下限 [0, 100]
```

---

## 四、Python 对接示例

### 4.1 最小读取示例

```python
import json
import urllib.request

# 1. 读 manifest（你已经在做的）
manifest = json.loads(urllib.request.urlopen(
    "https://raw.githubusercontent.com/livsstock/xgl/data/daily/output/manifest.json"
).read())

# 3. 拿当日 stock_data
entry = manifest["entries"][0]
stock_url = entry["stock_data_raw_url"]
data = json.loads(urllib.request.urlopen(stock_url).read())

print(f"日期: {data['date']}, 标的数: {data['count']}")
```

### 4.2 批量评分示例

```python
def composite_signal(stock):
    """组合信号：大海可以替换成自己的策略"""
    tech = stock["technical"]
    if not tech:
        return "NO_DATA"
    
    # 强买入：趋势强势 + MACD 金叉
    if tech["trend_score"] >= 70 and tech["macd"]["signal"] == "golden_cross":
        return "STRONG_BUY"
    
    # 超跌买入：超跌评分高 + RSI 极低
    if tech["oversold_score"] >= 60 and tech["rsi6"] < 25:
        return "OVERSOLD_BUY"
    
    # 卖出：趋势弱 + MACD 死叉
    if tech["trend_score"] <= 30 and tech["macd"]["signal"] == "death_cross":
        return "SELL"
    
    return "HOLD"


for stock in data["stocks"]:
    sig = composite_signal(stock)
    print(f"{stock['code']} {stock['name']:<8} "
          f"收={stock['close']:.2f} 涨跌={stock['change_pct']:>5.2f}%  "
          f"MACD信号={stock['technical']['macd']['signal']:<13} "
          f"趋势分={stock['trend_score']:>3} 超跌分={stock['oversold_score']:>3} "
          f"→ {sig}")
```

### 4.3 MACD 完整数据（如果你要自定义计算）

```python
# 你想用 MACD 柱状图的扩张/收缩做更复杂的判断
def macd_momentum(tech):
    """MACD 动能：当前柱 vs 前一根柱"""
    # 注意：v2.1 只输出当前 MACD 值
    # 如果要历史柱，你需要拉 K 线（这跟 v2.0 之前的逻辑一样）
    return tech["macd"]["macd"]  # 单值
```

如果需要**完整 MACD 历史序列**，告诉我，可以加进 v2.2（用新浪的 K 线增量缓存到 `data/daily/cache/klines/`）。

---

## 五、扣子智能体的字段路径（你的 Coze 工作流里用）

```json
{
  "manifest.entries[0].stock_data_raw_url": "读当日 stock_data 的 URL",
  "stocks[].code": "标的代码",
  "stocks[].name": "标的名称",
  "stocks[].close": "收盘价",
  "stocks[].change_pct": "涨跌幅",
  "stocks[].technical.macd.dif": "MACD DIF",
  "stocks[].technical.macd.dea": "MACD DEA",
  "stocks[].technical.macd.signal": "MACD 信号",
  "stocks[].technical.rsi6": "RSI(6)",
  "stocks[].technical.change_10d_pct": "10日累计涨跌",
  "stocks[].technical.volume_ratio": "量比",
  "stocks[].technical.oversold_score": "超跌评分",
  "stocks[].technical.trend_score": "趋势评分"
}
```

---

## 六、字段缺失处理（异常情况）

| 字段缺失场景 | 你的处理 |
|---|---|
| `technical` 整块缺失 | 数据源失败 → 跳过该股，标 `"NO_TECH"` |
| `technical.macd.signal` 是 `null` | 视为 `neutral` |
| `rsi6` 是 `null` | 用 `change_10d_pct` 替代判断超跌 |
| `volume_ratio` 是 `null` | 用 `close > pre_close * 1.05` 当作"放量" |

v2.1 在以下情况会输出 None（已实测）：
- 拉不到 35 天 K 线（连续 2 次失败）→ 整股 `technical` 为 None
- 该股停牌超过 1 天 → K 线连续收盘价相同，MACD 计算可能 NaN

---

## 七、9/22 第一天拿到数据后建议的验证步骤

1. **拉 9/18 数据（周六测试用）**：`stock_data_20260918.json`
2. **跑一遍你现有的 forecast 逻辑**：看 MACD 信号是否跟你之前手算一致
3. **对比趋势评分 vs 你的判断**：v2.1 的 trend_score 是综合 5 个因子的简化版，不一定 100% 匹配你的权重，可以微调
4. **对比超跌评分**：v2.1 的 oversold_score 阈值是经验值，可调

---

## 八、对比 v2.0（让大海看差异）

| 维度 | v2.0 (旧) | v2.1 (新) |
|---|---|---|
| MACD | 你自己算 | WorkBuddy 算好给你 |
| 标的数 | 11 只（你手算范围）| 20 只（WorkBuddy 算 + 你 forecast）|
| K 线 | 你拉 | WorkBuddy 拉（新浪 35 天）|
| 评分维度 | 0 个 | 2 个（超跌 + 趋势）|
| 你的 forecast 改动 | 拉数据 + 算指标 | 只读 `technical.*` |

**结论：你的 forecast 脚本从"算指标 + 出信号"简化为"读指标 + 出信号"。**

---

*文档结束 · v2.1 · 测试通过 · 待阿牛哥 push GitHub*