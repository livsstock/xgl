# 管道修复说明 (2026-09-30)

## 1. daily_collect.py WATCHLIST 更新
**问题**：核心6股中的紫金矿业(sh601899)、华友钴业(sh603799)、五粮液(sz000858) 不在 WATCHLIST 中，导致自动采集的收盘数据文件缺失这3只。
**修复**：WATCHLIST 已按核心池6只+底仓观察位2只+扩展监控4只重新组织，确保每日采集覆盖全部核心标的。

## 2. 休市日跳过逻辑
**问题**：`trade_date()` 仅处理周末，不识别中国法定假日。9/25中秋节管道仍生成了预测文件。
**修复**：新增 `CN_HOLIDAYS_2026` 休市日历 + `is_trading_day()` 函数，`trade_date()` 改为从昨天往前找最近交易日，非交易日 main() 会提前退出。

## 3. 降级告警机制
**问题**：9/24-9/29 管道降级运行4个交易日，无告警。
**修复**：manifest 写入后增加降级检测，当宏观数据失败/降级、个股数据失败/降级、或存在收盘价为空的股票时，输出醒目的降级告警信息。

## 4. 洞1修复：market_environment.label 字段映射
**问题**：`align_core6.py` 的 `extract_market_direction()` 在解析WB数据时，寻找 `_market_environment.index.env_label` 字段，但WB实际数据结构中该字段不存在（只有 `chg_today_pct`、`r_value_kuramoto`），导致WB大盘方向始终走兜底逻辑。
**修复**：新增基于 `index.chg_today_pct` + `M_sse_v2p5` 综合判断逻辑：
- `chg_today_pct <= -0.5%` 或 `M_sse < 35` → bearish
- `chg_today_pct >= 0.5%` → bullish
- 其他 → neutral

**验证结果**（9/28数据）：
- WB 大盘方向：bearish（上证跌-1.62%，M_sse=43.6）✅
- 大海大盘方向：bearish（market_outlook: 偏空）✅
- 对齐结论：双模型一致看空，CRITICAL 级别，买入信号冻结 ✅

## 5. 9/28 金钼股份名称截断问题
**问题**：WB 指出 9/28 文件中"金钼股份"被写为"金股份"。
**说明**：此问题出在9/28降级模式下的生成逻辑。WATCHLIST 修复后，后续采集会使用正确名称"金钼股份"。9/28的降级文件属于历史产物，已在本地收盘数据中修正。
