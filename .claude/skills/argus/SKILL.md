---
name: argus
description: "Argus ETF 均线信号扫描器。扫描ETF池、查询单只ETF、早盘分析、盘后对比等。触发词：argus, ETF扫描, 均线信号, 买入信号, 持仓监控"
allowed-tools: Bash(*)
argument-hint: "[scan|code <CODE>|morning|compare|record ...]"
---

## Argus - ETF 均线信号扫描器

脚本路径: `/Users/bytedance/argus/scan.py`
Python路径: `/Users/bytedance/argus/.venv/bin/python`

基础命令: `/Users/bytedance/argus/.venv/bin/python /Users/bytedance/argus/scan.py`

## 根据用户意图选择参数

### 1. 扫描全部ETF
```bash
/Users/bytedance/argus/.venv/bin/python /Users/bytedance/argus/scan.py
```
可选参数:
- `--detail` 显示完整指标列
- `--refresh` 强制刷新缓存
- `--no-cache` 不使用缓存，全部实时拉取
- `--no-xhs` 不生成小红书日志(盘中模式)

### 2. 查询单只ETF分析信息
```bash
/Users/bytedance/argus/.venv/bin/python /Users/bytedance/argus/scan.py --code <CODE> --no-cache
```
- `<CODE>` 可以是ETF代码(如 `512480`)或联接基金代码(如 `004752`)，会自动反查对应ETF

### 3. 早盘分析(实时数据)
```bash
/Users/bytedance/argus/.venv/bin/python /Users/bytedance/argus/scan.py --morning
```

### 4. 盘后对比(与盘中快照对比)
```bash
/Users/bytedance/argus/.venv/bin/python /Users/bytedance/argus/scan.py --compare
```

### 5. 买入记录管理
脚本路径: `/Users/bytedance/argus/record.py`

记录买入:
```bash
/Users/bytedance/argus/.venv/bin/python /Users/bytedance/argus/record.py --buy <CODE> --date <YYYY-MM-DD> --time <HH:MM>
```

查看记录:
```bash
/Users/bytedance/argus/.venv/bin/python /Users/bytedance/argus/record.py --list
```

## 参数解析规则

根据 `$ARGUMENTS` 判断用户意图:
- 空 或 `scan` → 扫描全部
- `code <CODE>` 或 直接传入一个6位数字代码 → 查询单只ETF (`--code <CODE> --no-cache`)
- `morning` → 早盘分析
- `compare` → 盘后对比
- `detail` → 详细扫描
- `refresh` → 刷新缓存后扫描
- `record buy <CODE> <DATE> [TIME]` → 记录买入
- `record list` → 查看买入记录

执行对应命令并展示结果。
