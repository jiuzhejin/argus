# Argus

Argus 是一个 ETF 均线信号扫描器，用来做 ETF 池扫描、单只 ETF 分析、早盘观察、盘后对比，以及交易记录管理。

## 功能

- 扫描 ETF 池并按信号分类
- 查询单只 ETF 或联接基金对应 ETF 的分析结果
- 早盘分析实时信号
- 盘后对比盘中快照
- 记录买入、卖出和查看交易记录
- 生成日志、图表和回测相关输出

## 文件说明

- `scan.py`：主扫描脚本
- `record.py`：交易记录管理
- `analyze_intraday_review.py`：盘中回顾分析
- `backtest_strategy.py`：策略回测
- `AGENTS.md`：仓库级 agent 使用说明

## 环境要求

- Python 3
- 建议使用仓库内虚拟环境：`.venv`

代码中使用了这些主要依赖：

- `akshare`
- `pandas`
- `requests`

如果当前环境还没装依赖，先准备虚拟环境并安装所需包。

## 快速开始

扫描全部 ETF：

```bash
.venv/bin/python scan.py
```

强制刷新缓存后扫描：

```bash
.venv/bin/python scan.py --refresh
```

查询单只 ETF：

```bash
.venv/bin/python scan.py --code 512480
```

执行早盘分析：

```bash
.venv/bin/python scan.py --morning
```

## 交易记录

记录买入：

```bash
.venv/bin/python record.py --buy 004432 --date 2026-05-14 --time 14:30 --amount 2000
```

记录卖出：

```bash
.venv/bin/python record.py --sell 004432 --date 2026-05-18 --time 14:30 --amount 1000
```

查看记录：

```bash
.venv/bin/python record.py --list
```

## 目录约定

- `.cache/`：缓存数据
- `logs/`：日志、图表、报告和交易记录输出

这些目录主要是运行产物，不建议作为核心源码内容维护。

## 说明

`README.md` 面向人类读者，提供项目简介和快速上手说明。`AGENTS.md` 面向 agent 或自动化助手，提供更偏操作约定的说明。
