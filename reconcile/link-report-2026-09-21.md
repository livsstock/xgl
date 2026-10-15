# 链路命中率报告 · 2026-09-21T10:03:54

**健康度：80/100**
**判定：🟢 协议层通：可跑 16:10 收盘对账**

## 链路耗时
- wb_load_ms: 0 ms
- dahai_fetch_ms: 5700 ms
- actual_fetch_ms: 8056 ms

## 协议层 5 节点诊断

| 节点 | 状态 | 说明 |
|---|---|---|
| WorkBuddy 本地预测 | OK | size=21.2KB, n_calls=26 |
| WorkBuddy 镜像（wb-2026-09-21.json）| DEPRECATED | 协议命名冗余 |
| 大海预测 | OK | n_stocks=17, has_market=True |
| 实际收盘数据 | MISSING | 上证实际 None% |
| 标的代码交集 | 17只 | WB=26, DH=17, 仅WB=9, 仅DH=0 |

## 后续动作
- ✅ 16:10 可正常跑 `python3 reconcile.py`
- ✅ 链路层绿灯，下一步观察语义层（双方预测方向/动作是否一致）