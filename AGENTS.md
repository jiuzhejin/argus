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
- `--no-xhs`：盘中模式下不生成小红书日志

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
- `record buy <CODE> <DATE> [TIME]`：记录买入
- `record list`：查看买入记录
