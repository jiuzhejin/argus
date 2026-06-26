# Strategy Backtest

- Buy rule: current `scan.py` opens on `★ 买入信号` (1.0x) and `◆ 趋势持有` (0.5x)
- Exit rule: first `✗ 趋势偏弱` or first structural `take_profit` trigger
- Open trades are marked to market with the latest available scan date

## Summary

- Total trades: 8
- Closed trades: 2
- Open trades: 6
- Closed win rate: 0.0%
- Closed avg return: -5.9%
- Closed weighted return: -5.9%
- Open avg mark-to-market: 3.8%
- Open weighted mark-to-market: 3.6%
- Open trades in take-profit watch zone: 2
- All trades avg return: 1.4%
- All trades weighted return: 1.5%

## By Month

| 买入月份    |   交易数 |   已平仓 |     平均收益率 |     加权收益率 |   总仓位 |   胜率 |   仓位加权平均收益率 |
|:--------|------:|------:|----------:|----------:|------:|-----:|------------:|
| 2026-06 |     8 |     2 | 0.0137628 | 0.0662229 |   4.5 | 0.75 |   0.0147162 |

## Trades

|     代码 | 名称        | 买入日期       |   买入价 | 买入状态   |   仓位权重 | 卖出日期       |   卖出价 | 卖出原因           | 卖出状态   | 止盈观察区   |   持有天数 |         收益率 |       加权收益率 | 已平仓   |
|-------:|:----------|:-----------|------:|:-------|-------:|:-----------|------:|:---------------|:-------|:--------|-------:|------------:|------------:|:------|
| 512800 | 银行ETF     | 2026-06-12 | 0.814 | ◆ 趋势持有 |    0.5 | 2026-06-17 | 0.785 | stop_loss      | ✗ 趋势偏弱 | False   |      5 | -0.0356265  | -0.0178133  | True  |
| 562800 | 稀有金属ETF   | 2026-06-12 | 1.097 | ◆ 趋势持有 |    0.5 | 2026-06-26 | 1.172 | mark_to_market | - 趋势完好 | False   |     14 |  0.0683683  |  0.0341841  | False |
| 510300 | 沪深300ETF  | 2026-06-16 | 4.91  | ◆ 趋势持有 |    0.5 | 2026-06-26 | 5.048 | mark_to_market | ◇ 多头排列 | False   |     10 |  0.0281059  |  0.014053   | False |
| 512100 | 中证1000ETF | 2026-06-16 | 3.491 | ★ 买入信号 |    1   | 2026-06-26 | 3.569 | mark_to_market | ◇ 多头排列 | False   |     10 |  0.0223432  |  0.0223432  | False |
| 515070 | 人工智能ETF   | 2026-06-17 | 2.541 | ◆ 趋势持有 |    0.5 | 2026-06-26 | 2.758 | mark_to_market | ◇ 多头排列 | True    |      9 |  0.0853994  |  0.0426997  | False |
| 510500 | 中证500ETF  | 2026-06-22 | 9.007 | ◆ 趋势持有 |    0.5 | 2026-06-26 | 9.085 | mark_to_market | ◇ 多头排列 | False   |      4 |  0.00865993 |  0.00432997 | False |
| 512400 | 有色金属ETF   | 2026-06-22 | 2.155 | ◆ 趋势持有 |    0.5 | 2026-06-23 | 1.976 | stop_loss      | ✗ 趋势偏弱 | False   |      1 | -0.0830626  | -0.0415313  | True  |
| 512880 | 证券ETF     | 2026-06-22 | 1.131 | ◆ 趋势持有 |    0.5 | 2026-06-26 | 1.149 | mark_to_market | ◆ 趋势持有 | True    |      4 |  0.0159151  |  0.00795756 | False |
