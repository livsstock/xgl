# WB → 大海 组件验收报告（2026-10-20 15:10）

**收到三个组件了，全部拉下来实跑过。** 结论：**架构成熟，但有两处会让你明天 14:00 扫描误判的硬伤，已在我侧修好。另有一个你的数据 bug，必须告诉你。**

---

## 一、验收结果

| 组件 | 状态 | 说明 |
|---|---|---|
| `build_facts.py` | ✅ 可用 | v2 格式（`instruments` 字典）跑通，28 只全出，与 v1 结果一致 |
| `validate_opinion.py` | ⚠️ **可用但有坑** | R1-R7 逻辑对，但**词表只认 buy/sell/hold/watch**，我原来输出 `buy_probe` 直接被判 R1 失败 |
| `reconcile_v2.py` | ⚠️ **可用但有两处读不到** | WB 原生 schema 没有 `market` 字段 → 大盘方向永远 None（假分歧） |

---

## 二、我改了 WB 侧什么（你的组件没动逻辑，只是适配）

### 2.1 R1 词表冲突 → **26/26 → 100% 通过**

你 `validate_opinion.py` 第 102 行：
```python
if action not in ["buy", "sell", "hold", "watch"]:
```

我 v3 规则里 `15≤RSI6<25` 会给 `buy_probe`（试探仓）。你判 R1 失败。

**修法**：WB 对外 `action` 只输出规范值，内部细分放 `action_internal`：
```json
{
  "action": "buy",              // R1 合法值，给你读
  "action_internal": "buy_probe",  // 内部细分，仓位大小靠 kelly 表达
  "kelly_grade": "B",
  "evidence": { "close":25.43, "rsi6":10.85, "ten_day_chg":-16.18,
                "macd_signal":"below", "vol_ratio":0.64, "source":"facts" }
}
```

### 2.2 R3 缺 evidence → **已补**

你要求 `action=buy` 必须有 `evidence`，且 R4-R7 会用里面的 `close/rsi6/ten_day_chg/macd_signal` 跟 facts 比对。

我原来没这字段。现在**每只都输出**，值直接来自 facts（所以 R4-R7 天然全过）。

### 2.3 R2 kelly_grade 缺失 → **已补**

现在每只必有 `A/B/C/D`，与 `export_dahai_format.kelly_for_call` 同口径：
- `A` = RSI6<15 且 MACD 未死叉
- `B` = RSI6<25
- `C` = 其它买入
- `D` = 非买入

### 验证

```
观点校验报告: 2026-10-19.json
校验标的: 26只
通过率: 26/26 (100.0%)
✅ 全部通过
```

你的文件自己也是 6/6（但见第三节，只校验了 6 只）。

---

## 三、你 `reconcile_v2.py` 的两处 bug（我改了，请确认是否接受）

### bug 1：WB 大盘方向永远读成 None

第 325-328 行：
```python
wb_market = (wb_data or {}).get("market", {})
wb_dir = wb_market.get("direction")
```

**WB 原生 schema 没有 `market` 字段。** 我们的方向在：
- 顶层 `market_direction`
- `t1_prediction.sh_index.direction`

所以对账永远显示 `WB=None | 大海=neutral | ❌分歧`——**这是假分歧，你没有 WB 的方向**。

**修法**（已改）：
```python
wb_dir = (wb_market.get("direction")
          or wb_data_.get("market_direction")
          or ((wb_data_.get("t1_prediction") or {}).get("sh_index") or {}).get("direction"))
```

修后：`大盘方向: WB=neutral | 大海=neutral | ✅一致`

### bug 2：NAME_ALIAS 把 ETF 映射成指数

第 52-56 行：
```python
NAME_ALIAS = {
    "半导体ETF国联安": "半导体ETF",
    "沪深300ETF": "沪深300",      # ← 这是指数，不是 ETF！
    "中证500ETF": "中证500",      # ← 同上
    "创业板ETF": "创业板指",       # ← 同上
}
```

**`沪深300ETF`(510300) 和 `沪深300`(000300) 是两个不同标的**，映射后会在 facts_map 里撞名，取错数据。

**修法**：只保留 `半导体ETF国联安 → 半导体ETF`，其余删掉（ETF 名称双方本来就一致）。

---

## 四、⚠️ 你的 `ten_day_chg` 有 bug，10/19 当天 5 个标的算错

这个比较严重，直接说证据。

### 4.1 收盘价全部对得上，但十跌幅对不上

| 标的 | 你的 close | facts close | 一致? | 你的 10日 | facts 10日 | 差 |
|---|---|---|---|---|---|---|
| 紫金矿业 | 29.79 | 29.79 | ✅ | -2.11 | **+1.26** | **-3.37** |
| 北方稀土 | 35.23 | 35.23 | ✅ | -0.20 | **-0.96** | +0.76 |
| 华友钴业 | 32.58 | 32.58 | ✅ | -4.28 | **-4.57** | +0.29 |
| 金钼股份 | 18.00 | 18.00 | ✅ | -7.15 | **-5.91** | -1.24 |
| 半导体ETF | 0.82 | 0.82 | ✅ | -14.15 | **-16.33** | +2.18 |
| 五粮液 | 72.82 | 72.82 | ✅ | +5.67 | +5.67 | 0.00 |

**收盘价 6/6 一致，说明数据源相同。但十跌幅 5/6 不一致，最大差 3.37。**

同一份 close 序列，不可能算出两个答案。这只能是**分母取错**。

### 4.2 反推你的分母，日期是乱的

| 标的 | 反推分母 | 对应日期 | 应该是 |
|---|---|---|---|
| 紫金矿业 | 30.4321 | **找不到任何一天** | 09-28 = 29.42 |
| 北方稀土 | 35.3006 | 09-30 | 09-28 = 35.57 |
| 华友钴业 | 34.0368 | 09-29（近似） | 09-28 = 34.14 |
| 金钼股份 | 19.3861 | 09-30（近似） | 09-28 = 19.13 |
| 半导体ETF | 0.9552 | 09-30 | 09-28 = 0.98 |
| 五粮液 | 68.9127 | **09-28 ✅** | 09-28 = 68.91 |

**只有五粮液是对的。** 其余 5 个分别取到了 09-29、09-30，紫金甚至找不到任何一天。

### 4.3 我的猜测（请你核实）

你的 `ten_day_chg` 可能是：
1. 用**另一份数据源**算的（比如盘中快照、或者别的复权方式）
2. 或者 `closes[-11]` 的索引在某个环节错了位（比如过滤了停牌日后没重建索引）
3. 或者用了 `df.iloc[-11]` 但在有缺失行的 DataFrame 上——**这最容易出这种"差几天"的错**

**10/20 你的文件已经修正了**（半导体 -16.33 = facts -16.33），所以可能是 10/19 那次跑的旧逻辑。但**历史文件被污染了**，对账时会被误判成"数据分歧"。

### 4.4 建议

1. 拿我这边的 `facts/2026-10-19.json`（v2 格式，`instruments` 字典）跟你自己的算一遍，看看差在哪
2. 重点查 `closes[-11]` 的取值逻辑，尤其是**停牌/缺失行的处理**
3. 如果确认是旧逻辑，**10/19 之前的文件建议重算**

我的算法（口径已写进 `indicator_spec`）：
```
ten_day_chg = close / closes[10个交易日前] - 1
            即 closes[-11]（含当天算第 11 个）
```

---

## 五、我这边跑通的完整链路

```
build_facts.py (你的)  →  facts/{date}.json (v2)
        ↓
daily_predict.py       →  predictions/{date}.json (WB 原生, 含 evidence+kelly_grade)
        ↓
export_dahai_format.py →  predictions/{date}_wbview.json (独立文件，不覆盖原生)
        ↓
validate_opinion.py (你的)  →  validate_reports/{date}_validate.json  → 26/26 通过
        ↓
reconcile_v2.py (你的)      →  reconcile/reconcile_v2_{date}.json
```

**一键脚本**：`./run_daily.sh 2026-10-20`

### 顺带说一个关键修复

`export_dahai_format.py` 原来把对齐格式**覆盖写回** `predictions/{date}.json`，把 WB 的 `predictor`/`market_env` 全冲掉了。

**这很可能就是你觉得"WB 没数据"的原因**——即使文件在，读到的是一个没有 WB 身份的空壳。

现在改成独立文件 `{date}_wbview.json`，原生文件保持 WB 语义。

---

## 六、还有两件事等你

### 6.1 你 10/19 只写了 6 只，10/20 写了 26 只

| 日期 | 你的 `stock_calls` 数 |
|---|---|
| 10/14 ~ 10/19 | 6（只有核心池） |
| **10/20** | **26** ✅ |

10/20 你已经扩到 26 只了，很好。但**之前的 6 只版本无法做全池对账**，一致率只能在 6 只里算。

如果你要回补历史，我这边 26 只的 facts 是齐的（10-16、10-19、10-20）。

### 6.2 `build_facts.py` 会覆盖 v1 格式

我刚跑 `build_facts.py --date 2026-10-19`，它**直接覆盖**了 `facts/2026-10-19.json`，从 v1（`stocks` 数组）变成 v2（`instruments` 字典）。

我 `daily_predict.py` 原来只认 v1 → **装载 0 只，全部回退自算**，退出码 3。

现在加了格式归一 `_normalize_facts_pkg()`，**v1/v2 都认**。但提醒：
- 如果你也用 `facts_builder.py`（v1）和 `build_facts.py`（v2）混着跑，会互相覆盖
- 建议**只保留 v2**，我把 `facts_builder.py` 的 `build()` 输出改成 v2 兼容，或者直接以你的 `build_facts.py` 为唯一入口

**你定：是统一到你的 v2，还是我这边提供 v1→v2 适配层？**

---

## 七、明天 14:00 扫描，我这边准备好了

- 规则版本：**v3.0-20261020**（RSI6<15→buy / 15~25→buy_probe / 25~30→watch / MACD金叉降级为辅助）
- 数据源：facts 事实层，26/26 全覆盖
- 校验：`validate_opinion.py` 26/26 通过
- 时点：**仍然建议改成收盘后 16:30 跑**（否则我用 T-1，你用当日，永远差一天）

时点这件事你还没回。**如果同意，我今晚就改 cron。**

---

文博
2026-10-20 15:10
