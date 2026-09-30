# 对账数据目录

## 结构
```
reconciliation/
├── daily/          # 每日参考账（每天收盘后更新）
│   └── YYYY-MM-DD.json
├── weekly/         # 每周表决账（每周五收盘后汇总）
│   └── YYYY-WXX.json
└── README.md
```

## 分工
- **每日参考账**：收盘后对比当天预测vs实际，逐只打勾/打叉，记录分歧信号
- **每周表决账**：周五汇总本周数据，算胜率/分歧统计/置信度校准，输出权重调整建议

## 数据提供方
- WorkBuddy 预测 → `predictions/`
- 大海预测 → `predictions-dahai/`
- 实际数据 → `output/stock_data_YYYYMMDD.json`
- 对账结果 → 本目录
