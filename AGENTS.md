# Argus Agent Guide

本仓库是 Argus ETF 均线信号扫描器。

## 环境

- 主扫描脚本：`/Users/bytedance/argus/scan.py`
- 记录管理脚本：`/Users/bytedance/argus/record.py`
- 推荐 Python：`/Users/bytedance/argus/.venv/bin/python`

基础命令：

```bash
/Users/bytedance/argus/.venv/bin/python /Users/bytedance/argus/scan.py
```

## 常用操作

### 扫描全部 ETF

```bash
/Users/bytedance/argus/.venv/bin/python /Users/bytedance/argus/scan.py
```

可选参数：

- `--refresh`：强制刷新缓存
- `--llm`：调用 etf-agent CLI 做二次判断（默认关闭）

### 查询单只 ETF

```bash
/Users/bytedance/argus/.venv/bin/python /Users/bytedance/argus/scan.py --code <CODE>
```

`<CODE>` 可以是 ETF 代码，例如 `512480`；也可以是联接基金代码，例如 `004752`。

### 早盘分析

```bash
/Users/bytedance/argus/.venv/bin/python /Users/bytedance/argus/scan.py --morning
```

### 管理买入记录

记录买入：

```bash
/Users/bytedance/argus/.venv/bin/python /Users/bytedance/argus/record.py --buy <CODE> --date <YYYY-MM-DD> --time <HH:MM>
```

查看记录：

```bash
/Users/bytedance/argus/.venv/bin/python /Users/bytedance/argus/record.py --list
```

## 意图映射

常见用户意图与命令的对应关系：

- `scan` 或空输入：扫描全部 ETF
- `code <CODE>` 或直接输入 6 位代码：执行单只 ETF 分析，并带 `--code <CODE>`
- `morning`：执行早盘分析
- `refresh`：扫描时带 `--refresh`
- `llm`：扫描时带 `--llm`（etf-agent 二次判断）
- `record buy <CODE> <DATE> [TIME]`：记录买入
- `record list`：查看买入记录

## 策略改动流程（硬性要求：先回测验证，再改实盘）

任何涉及买入/加仓/止损/止盈判据的策略逻辑改动（主要在 `scan.py` 的 `check_holdings` 和 `analyze`），**必须先用历史数据回测验证有效，才允许落地到 `scan.py`**。不允许凭直觉或"看着合理"直接改实盘判据。

标准流程：

1. **定位根因**：读代码 + 用实际历史数据验证问题机制（不臆测）。
2. **出方案（此时零代码改动）**：先讲清假设，不碰 `scan.py`。
3. **回测验证**：在 `tools/` 下写独立验证脚本（复用 `backtest_strategy.analyze_hist` 作信号引擎，保证与实盘同源），用近 2 年历史数据检验方案。**回测有否决权**——数据不支持就推翻方案，不硬上。
4. **样本外验证**：用多段独立、基本不重叠的时间窗（如逐年切 3 段）确认结论方向一致，排除过拟合/运气。近期一段好不算数。
5. **落地**：确认有效后才改 `scan.py`，同步加参数常量、补 `test_scan.py` 用例、实跑 `scan.py` 端到端验证。
6. **留档**：验证脚本保留在 `tools/` 并纳入版本控制，作为该判据参数的可复现依据。

已有先例：止损 C 组合 P7/C-5（`tools/stoploss_backtest.py`）、加仓缩量回踩（`tools/entry_reverse_explore.py`，其中 `entry_guard_backtest.py` 记录了一次被回测证伪、随即纠正方向的过程）。

诚实边界：回测是"信号层面"验证（历史 K + analyze_hist），未建模盘中活价/滑点/真实成交时点；证明的是"该判据在历史上更优"，非"未来每次都对"。小样本/弱市结论需注明保留。

## 持仓止损 / 加仓逻辑

持仓监控(`check_holdings`)在信号转「✗ 趋势偏弱」且确认破位时才提示止损，并有两道保护闸门(经回测+样本外验证 P7/C-5)：

- **持有保护期**：买入后 7 个自然日内不触发硬止损(与联接C类惩罚性赎回费窗口对齐)，除非放量破位。
- **成本止损线**：相对买入成本浮亏未破 -5% 不硬止损。成本基准用买入日 ETF 缓存收盘价(非联接净值)。
- **放量破位例外**：量比≥1.5 且当日下跌属急跌，任何时候都放行 🔴 止损，不受闸门保护。

被闸门拦下的降级为 🟠 止损观察。分批买入按最后一笔买入的日期/成本计算。
参数常量见 `scan.py` 的 `HOLD_PROTECT_DAYS` / `COST_STOP_PCT`；回测脚本见 `tools/stoploss_backtest.py`。

加仓提示只在「缩量回踩低吸」形态触发(经回测+3段样本外验证)：

- 「▲ 接近支撑 + MA5↑」时，仅当 **量比 < 1(缩量)** 且 **现价 < MA5(回踩)** 才提示 🟢 加仓，其余降级 🔵 持有观察。
- 该组合 D+5 胜率显著高于「仅 MA5↑」，且要求价<MA5 天然规避「MA5 窗口滚动假拐头」误判。
- 参数常量 `ADD_VOL_RATIO_MAX`；回测脚本见 `tools/entry_reverse_explore.py`。
