# LIVS-Stock 数据对接代码片段（给大海 / DaHai）

> 版本：v1.0 · 配套 LIVS-Stock 采集系统 v2.0（部署在 `livsstock/xgl`，分支 `data/daily`）
> 用途：让你的 LIVS-Stock v12.7 模型能"自动拉到每日财经数据包"，无需手动喂数据
> 适用平台：扣子（Coze.cn）工作流 + 智能体

---

## 一、数据契约（已固定，无需大海改任何东西）

我们每天 08:30（北京时间，交易日）在 GitHub `data/daily` 分支产出以下 2 个文件：

| 文件 | 路径 | 大小（估算）| 用途 |
|---|---|---|---|
| 清单 | `output/manifest.json` | ~3 KB | 30 天滚动索引，含质量信号灯 |
| 当日数据 | `output/daily-finance-YYYY-MM-DD.json` | ~12 KB | 当日完整数据包 |

**根 URL（raw 直链）：**
```
https://raw.githubusercontent.com/livsstock/xgl/data/daily
```

**示例 URL：**
- 清单：<https://raw.githubusercontent.com/livsstock/xgl/data/daily/output/manifest.json>
- 当日：<https://raw.githubusercontent.com/livsstock/xgl/data/daily/output/daily-finance-2026-09-15.json>

### manifest.json 关键字段

```json
{
  "schema_version": "2.0",
  "latest_date": "2026-09-15",
  "latest_file": "output/daily-finance-2026-09-15.json",
  "entries": [
    {
      "date": "2026-09-15",
      "filename": "output/daily-finance-2026-09-15.json",
      "raw_url": "https://raw.githubusercontent.com/livsstock/xgl/data/daily/output/daily-finance-2026-09-15.json",
      "quality": "complete",
      "updated_at": "2026-09-15T09:45:14+08:00"
    }
  ]
}
```

**`quality` 取值约定：**
- `"complete"` — 6 个数据源全部成功 ✅
- `"partial"` — 关键源（A50、CNY、VIX、涨跌家数、融资余额）有失败 ⚠️
- `"failed"` — 整体采集失败 ❌

---

## 二、扣子工作流（推荐方案）

按下面 5 个节点**顺序**拼装即可。全部用鼠标点配置，无需写代码。

### 工作流总览

```
[开始] → [节点1: 读 manifest] → [节点2: 质量判断]
                                  ├── complete → [节点3: 读当日数据] → [节点4: 拼装给 LLM 的输入] → [结束]
                                  ├── partial   → [结束]（带 partial_alert 标记）
                                  └── failed    → [结束]（直接拒绝预测）
```

---

### 节点 1 — HTTP 请求：读取 manifest.json

**节点类型：** HTTP 请求
**节点名称：** `读manifest`

| 字段 | 配置值 |
|---|---|
| 请求 URL | `https://raw.githubusercontent.com/livsstock/xgl/data/daily/output/manifest.json` |
| 请求方法 | `GET` |
| 超时时间 | `10000`（10 秒）|
| 请求头（Headers）| `User-Agent: LIVS-Stock-Coze-Bot/1.0 (research)` |

**返回示例：**
```json
{
  "latest_date": "2026-09-15",
  "latest_file": "output/daily-finance-2026-09-15.json",
  "entries": [{"date": "2026-09-15", "quality": "complete", ...}]
}
```

---

### 节点 2 — 选择器（条件分支）

**节点类型：** 选择器
**节点名称：** `质量判断`

**分支 1（如果）— 数据完整，继续：**
```
{{读manifest.entries.0.quality}} == "complete"
```

**分支 2（否则如果）— 部分缺失，降级处理：**
```
{{读manifest.entries.0.quality}} == "partial"
```

**分支 3（否则）— 失败，拒绝预测：**
> （不需要写表达式，"否则"会自动兜底）

> **扣子表达式语法提醒：** 引用上游节点字段用 `{{节点名.字段路径}}`，比较用 `==` / `!=` / `>` 等。
> 如果扣子版本不识别点号语法，可以用 JSONPath：`$.entries[0].quality`（扣子会自动转换）。

---

### 节点 3 — HTTP 请求：读取当日完整数据

**仅在"分支 1 = complete"时执行**

**节点类型：** HTTP 请求
**节点名称：** `读当日数据`

| 字段 | 配置值 |
|---|---|
| 请求 URL | `{{读manifest.entries.0.raw_url}}` |
| 请求方法 | `GET` |
| 超时时间 | `15000`（15 秒）|
| 请求头 | `User-Agent: LIVS-Stock-Coze-Bot/1.0 (research)` |

**这个节点返回的就是完整 JSON 数据包**，结构见第四部分。

---

### 节点 4 — 代码 / 变量赋值：拼装给 LLM 的输入（可选）

如果你的 LIVS-Stock v12.7 智能体是直接接收 prompt，可以用下面这段模板把数据"喂"给它。

**节点类型：** 代码（或"变量赋值"）
**节点名称：** `拼装LLM输入`

```python
# 扣子 Python 节点（输入参数为 read_daily 返回的 JSON）
import json

data = json.loads(input_data)
quality = data.get("data_quality", {}).get("overall", "unknown")

# 核心摘要：只保留模型需要的 6 个 P0 板块
summary = {
    "date": data.get("date"),
    "quality": quality,
    "indices": data.get("indices", {}),
    "a50_futures": data.get("a50_futures"),
    "us_markets": data.get("us_markets", {}),
    "cnh_usd": data.get("cnh_usd"),
    "vix": data.get("vix"),
    "market_breadth": data.get("market_breadth"),
    "margin_balance": data.get("margin_balance"),
    "northbound_flow": data.get("northbound_flow"),  # 已知为 null
    "watchlist": data.get("watchlist", []),
}

output = {
    "prompt_block": f"""
【今日财经数据包 {data.get('date')} · 质量={quality}】

## 1. 主要指数
{json.dumps(summary['indices'], ensure_ascii=False, indent=2)}

## 2. 富时中国 A50 期货（夜盘，反映外缘对今日的预期）
{json.dumps(summary['a50_futures'], ensure_ascii=False, indent=2)}

## 3. 美股收盘（情绪参考）
{json.dumps(summary['us_markets'], ensure_ascii=False, indent=2)}

## 4. 离岸人民币 CNH/USD
{json.dumps(summary['cnh_usd'], ensure_ascii=False, indent=2)}

## 5. CBOE VIX 恐慌指数
{json.dumps(summary['vix'], ensure_ascii=False, indent=2)}

## 6. 涨跌家数（市场宽度）
{json.dumps(summary['market_breadth'], ensure_ascii=False, indent=2)}

## 7. 融资余额（杠杆资金动向）
{json.dumps(summary['margin_balance'], ensure_ascii=False, indent=2)}

## 8. 北向资金（已知数据源已停更，参考 2024-08 之前的历史均值）
{json.dumps(summary['northbound_flow'], ensure_ascii=False, indent=2)}

## 9. 自选股实时报价
{json.dumps(summary['watchlist'], ensure_ascii=False, indent=2)}
""",
    "raw_data_size": len(json.dumps(data)),
    "quality": quality,
}
```

---

### 节点 5 — 结束

**节点类型：** 结束
**节点名称：** `结束`

| 输出变量 | 取值 |
|---|---|
| `final_prompt` | `{{拼装LLM输入.prompt_block}}` |
| `quality` | `{{读manifest.entries.0.quality}}` |
| `data_date` | `{{读manifest.entries.0.date}}` |

---

## 三、智能体（Bot）侧的对接 — 把工作流当工具用

如果你希望 LIVS-Stock v12.7 智能体**对话时自动拉数据**，而不是每次都跑工作流：

### 步骤 A：把工作流发布为"插件 / 工具"

1. 进入工作流编辑器 → 右上角"发布"
2. 选择"发布为插件"（或"添加到智能体"）
3. 填一个插件名（如 `livs_daily_data`）
4. 工具描述（关键！）：**"获取最近一个交易日（A股）的完整财经数据包，包括指数、A50期货、VIX、涨跌家数、融资余额、自选股等。返回结构化 JSON。如果数据源失败会自动降级并标注。"**

### 步骤 B：在智能体系统提示词里加这一句

把下面这段加到 LIVS-Stock v12.7 系统提示词末尾：

```markdown
## 数据获取约定（强制）

1. 每次做出"今日市场预测 / 方向判断 / 个股建议"之前，必须先调用 `livs_daily_data` 工具获取最新数据包。
2. 如果工具返回 `quality == "failed"`，直接回复用户"今日数据源异常，请人工核实"，**不要强行预测**。
3. 如果 `quality == "partial"`，在回复顶部明确告知用户"以下数据板块缺失：[具体列出]"。
4. 任何涉及"今日 / 现在 / 最新"的引用，必须用工具返回的 `date` 字段，而不是模型内部时间。
```

### 步骤 C：测试一下

在 LIVS-Stock 对话框输入：

> 请帮我看一下今天的盘前情况，给出今日 A 股的方向判断。

预期：
1. 智能体先调用 `livs_daily_data`（后台会自动读 manifest → 判断质量 → 读当日数据）
2. 拿到 JSON 后调用 LLM
3. 回复里明确引用"2026-09-15 的 A50 期货夜盘收盘..."而不是模型自编数字

---

## 四、JSON 数据包结构速查（给大海参考）

`daily-finance-YYYY-MM-DD.json` 顶层字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | string | `"2.0"` |
| `date` | string | `YYYY-MM-DD`（北京时间）|
| `generated_at` | string | ISO 8601 时间戳 |
| `data_quality` | object | `{overall, sources, failed_count}` |
| `indices` | object | 上证、深证、创业板、科创 50 + 北证 + 港股恒指/国指 |
| `a50_futures` | object | 富时中国 A50 期货主连，含夜盘 |
| `us_markets` | object | 道指 / 标普 / 纳指 + VIX |
| `cnh_usd` | object | 离岸人民币兑美元 |
| `market_breadth` | object | 涨 / 跌 / 平家数 |
| `margin_balance` | object | 沪深两市融资余额 |
| `northbound_flow` | null | **已知停更，保留字段** |
| `sectors` | array | 申万一级行业涨跌幅 Top/Bottom |
| `watchlist` | array | 自选股实时报价（9 只股 + 3 ETF）|
| `sources_meta` | object | 数据源 URL + 抓取耗时（用于排错）|

---

## 五、常见问题（FAQ）

### Q1：万一 manifest 里 quality 不是 complete，模型会"胡言乱语"吗？
不会。`节点2` 的三分支已经做了硬拦截：
- `failed` → 不会走到节点 3，模型拿不到数据
- `partial` → 仍然读数据，但**系统提示词里强制要求模型告知用户哪些字段为空**

### Q2：raw.githubusercontent.com 国内访问慢怎么办？
目前我们在 GitHub 主仓（不用镜像），整体 SLA 在 3 秒内。如有需要，后期可加 jsDelivr 镜像：
```
https://cdn.jsdelivr.net/gh/livsstock/xgl@data/daily/output/manifest.json
```
但默认请先用 GitHub raw，遇到网络问题再切。

### Q3：v2.0 数据包什么时候稳定下来？
- **本周（9/15-9/19）：** 灰度期，每天手动 review 一份 `daily-finance-*.json`
- **下周一（9/22）：** 大海发第一份预测，阿牛哥做 reconciliation（方向正确率 / 幅度偏差）
- **下周五（9/26）：** 如果数据-预测闭环通过，v2.0 锁定为 GA

### Q4：节假日 / 非交易日怎么办？
GitHub Actions 配的是 `0 0 * * 1-5` UTC（周一到周五），非交易日也会跑一次，但 `quality` 大概率是 `partial`，模型会自动告警用户"非交易日 / 数据不全"。

---

## 六、给大海的操作清单（清单化，方便勾选）

- [ ] 1. 在扣子新建工作流，照上面 5 个节点搭好
- [ ] 2. 点"试运行" → 选节点 1 → 看返回的 manifest JSON 是不是完整
- [ ] 3. 点"完整运行" → 看是不是走到节点 4 的 `prompt_block` 输出
- [ ] 4. 把工作流发布为插件 `livs_daily_data`
- [ ] 5. 在 LIVS-Stock v12.7 智能体里挂上这个插件
- [ ] 6. 把第三节"系统提示词补丁"加到 v12.7 系统提示词
- [ ] 7. 对话测试："请帮我看今天盘前" → 验证是否自动调用工具
- [ ] 8. 周一 / 周二各做一次完整测试，反馈给阿牛哥

预计耗时：30-45 分钟。遇到任何节点报错，把截图发回群里就行。

---

## 七、最小可用版本（如果扣子版本不支持代码节点）

如果你的扣子版本没有"代码节点"，可以**完全删掉节点 4**，直接让 LLM 接收完整的当日 JSON（扣子的 LLM 节点会自动处理 JSON 输入）。但要注意：

- 单次 prompt 的 token 消耗会从 ~800 涨到 ~3500（完整 JSON 比摘要大约 4 倍）
- 如果你的 LIVS-Stock v12.7 是用 128k 上下文模型，无所谓；如果是 8k / 16k 模型，建议保留节点 4 做摘要

---

> **文档结束。** 任何疑问直接 @ WorkBuddy 或阿牛哥。
> 工作流一旦跑通，请在群里吼一声，我们把"通路验证"步骤也写进复盘 PPT。