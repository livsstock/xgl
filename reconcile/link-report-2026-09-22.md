# 链路命中率报告 · 2026-09-22T11:32:07

**健康度：60/100**
**判定：🟡 部分节点不通：人工补齐后再跑**

## 链路耗时
- wb_load_ms: 0 ms
- dahai_fetch_ms: 31088 ms
- actual_fetch_ms: 0 ms

## 协议层 5 节点诊断

| 节点 | 状态 | 说明 |
|---|---|---|
| WorkBuddy 本地预测 | OK | size=15.5KB, n_calls=26 |
| WorkBuddy 镜像（wb-2026-09-22.json）| DEPRECATED | 协议命名冗余 |
| 大海预测 | MISSING | n_stocks=—, has_market=— |
| 实际收盘数据 | OK | 上证实际 0.21% |

## 后续动作
- ⚠️ 阻塞项见上方表格，逐一排查后重跑
- 常见原因：大海还没推/推错分支/文件命名差