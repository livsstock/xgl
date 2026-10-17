# LIVS-Stock 工程重构需求清单（喂 Kimi K3 用）

> WB 起草 · 2026-10-17 · 回应大海《双模型现状自查》
> 定位：**模拟盘阶段，不实操**。以下按优先级排列，每项含"现状/目标/验收标准"。

---

## 背景速览（给 Kimi 的上下文）

LIVS-Stock 是双模型股票预测系统：
- **WB（WorkBuddy）**：Python 脚本群，跑在沙箱 `/workspace/finance_task/`，负责数据采集、技术指标、回测规则库、凯利仓位
- **大海（DaHai）**：外部模型，靠 GitHub 仓库 `livsstock/xgl` 交换 JSON（`predictions/` ↔ `predictions-dahai/`）
- 数据源：新浪日K（`money.finance.sina.com.cn`）、腾讯实时（`qt.gtimg.cn`，GBK编码）
- 每日流程：08:35 生成预测 → 推 GitHub → 16:10 风控报告 + 双方对账
- 已知痛点：脚本零散、指标计算不可靠、schema 半对齐、凯利 p 值无回测支撑

---

## 需求 1（P0）：技术指标自动计算链路

**现状（大海自查第1、2条，WB认账）**：
- RSI/MACD 有手填估算成分（大海 10/16 补数据时手填了 RSI 38/22/10）
- WB 侧虽有 `rebuild_history.py` 里的 RSI/MACD 实现，但**没与 daily_predict.py 统一**，三处脚本三套算法（daily_predict / rebuild_history / export_dahai_format）
- 外部技术分析接口不稳定（返回 null / 非JSON）

**目标**：单一模块 `lib/indicators.py`，全部指标本地计算，零外部技术接口依赖。

**具体要求**：
1. 实现：RSI(6/14)、MACD(12,26,9 含 DIF/DEA/柱值精确到小数)、MA(5/10/20/60)、量比、20日回撤/30日回撤、Kuramoto R（现有实现迁移）
2. 输入：统一 K 线结构体 `{day, open, high, low, close, volume}`
3. 数据源适配层：新浪日K + 腾讯实时双通道，GBK 解码，失败自动切换，全挂时明确报错（不许静默返回 None）
4. **单元测试**：用一段固定 60 根的 K 线数据做黄金样本，输出与通达信/同花顺手工核对过的期望值，误差 ≤ 0.01
5. 所有旧脚本（daily_predict.py / rebuild_history.py / export_dahai_format.py）改为 import 此模块，删掉各自的内联实现

**验收**：跑 26 只标的 + 5 个指数，零 null、零手填，指标值与黄金样本一致。

---

## 需求 2（P0）：统一 JSON Schema（根治"观望(看涨)"矛盾）

**现状（大海自查第3条，WB认账）**：
- WB 出现过 `action=hold, direction=up` 的自相矛盾
- 两方 action 词表已对齐（hold=无操作/watch=观察/buy=买入），但 direction 与 action 的**约束关系**没有 schema 校验
- 对齐表出现过"观望(看涨)"这种矛盾显示

**目标**：一份 JSON Schema（v3），双方共用，带校验器。

**具体要求**：
1. 文件：`schema/prediction_v3.schema.json`（JSON Schema draft-07）
2. 核心约束：
   - `direction ∈ {bullish, bearish, neutral}`
   - `action ∈ {hold, watch, buy, avoid}`
   - **一致性规则**：action=buy ⟹ direction=bullish；action=avoid ⟹ direction=bearish；hold/watch 无方向约束（watch 可以带看涨，表示"上涨候选但未触发"——但要单独起名 `watch_bull`/`watch_bear`，禁止用 watch+direction 组合产生歧义）
3. 校验器 `schema/validate.py`：推送前跑，不合规文件禁止推送（退出码 1）
4. 兼容：读取旧文件（v2.5 / 大海现有格式）自动升级到 v3
5. 26 只标的**全部必须出现**（解决大海自查第5条"名实不符"），核心6只带 `core_pool: true`

**验收**：校验器能抓出历史上所有矛盾样本（用 9/21~10/16 的文件做回归测试）。

---

## 需求 3（P1）：历史回测引擎（给凯利 p 值真实依据）

**现状（大海自查第4条，WB认账）**：
- 凯利 A/B/C/D 分级的 p 值（0.60/0.54）是经验估计，**无回测统计支撑**，有拍脑袋成分
- 已有资产：`backtest/` 下有指数规则回测（800根日K、样本外+FDR），但只覆盖股指、且口径与个股不统一

**目标**：`backtest/engine_v2.py`，统一回测个股+股指，输出各信号档位的真实胜率/赔率。

**具体要求**：
1. 数据：26 只标的 + 5 个指数，各取 ≥1500 根日K（新浪接口分批拉，缓存到本地 `data/kline/`，避免重复请求）
2. 信号定义：把 WB 实际使用的信号编码成可回测规则（RSI6<30 超卖、MACD金叉、量比≥1.3、20日回撤≥10%等，与 daily_predict 判定逻辑同源——从需求1的模块 import）
3. 产出统计：每个信号 → 胜率 p、平均盈亏比 b、样本数 n，按"信号后 T+1/T+3/T+5"三个窗口分别统计
4. **训练/验证分段**：前 70% 训练、后 30% 验证，Benjamini-Hochberg FDR 只对训练段做一次校正（沿用现有 `index_rules.py` 口径）
5. 输出直接喂凯利：`kelly_params.json`，格式 `{"signal_id": {"p": x, "b": y, "n": z, "tier": "A|B|C|D"}}`，替换现有硬编码的 0.60/0.54
6. 结果落盘 + 生成 markdown 摘要

**验收**：凯利模块从 `kelly_params.json` 读 p/b，不再有任何硬编码胜率；跑完全程 ≤ 30 分钟。

---

## 需求 4（P2）：工程结构收敛

**现状**：`/workspace/finance_task/` 下 20+ 零散脚本，职责重叠（push_wb.py / fix_push_predictions.py / push_api.py 三个推送器并存）。

**目标**：收敛为清晰结构：

```
finance_task/
├── lib/            # indicators.py, data_source.py, kelly.py, schema 校验
├── predictions/    # 输出（不改）
├── backtest/       # 回测引擎 + 规则库
├── reconcile/      # 对账
├── docs/           # 文档（不改）
└── daily_predict.py  # 主入口（保留文件名，cron 任务依赖它）
```

**要求**：
1. 废弃脚本移入 `legacy/`，不删除（留审计痕迹）
2. push 只保留 push_api.py 一条通道
3. `daily_predict.py` 的命令行接口保持不变（`--date` / `--config` / `--no-push`），自动化任务的 prompt 不用改

**验收**：周一 08:35 自动任务照常跑通，无需人工干预。

---

## 明确不做的事（防止 K3 过度发挥）

1. ❌ 不重写凯利模型本身（kelly.py 三层结构老板已拍板，只换 p/b 数据来源）
2. ❌ 不改 GitHub 仓库结构（predictions/ ↔ predictions-dahai/ 是大海侧对账的既定路径）
3. ❌ 不引入数据库/消息队列等重依赖（沙箱环境，标准库+requests 为限）
4. ❌ 不动 16:10 的两个既有自动化任务
5. ❌ 网络层失败重试逻辑不许静默吞错——宁可报错，不许空文件

---

## 优先级与依赖关系

```
需求1（指标链路）──┬──> 需求2（Schema）   可并行
                  └──> 需求3（回测引擎）  依赖需求1
需求4（结构收敛）    最后做，防止前面返工
```

## 给大海的两个确认点

1. v3 schema 里 `watch_bull / watch_bear` 的命名你是否认可？还是你有更好的方案？
2. 回测窗口 T+1/T+3/T+5 与你的持仓周期假设是否一致？如需 T+10 一并加。
