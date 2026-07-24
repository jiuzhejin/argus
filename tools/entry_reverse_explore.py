#!/usr/bin/env python3
"""反向加仓判据探索(独立脚本，不改 scan.py)。

前一版 entry_guard_backtest 发现：对「▲ 接近支撑」信号，「逢跌/价<MA5」的点
后续 D+1~D+5 反而更好——说明这是左侧低吸信号，右侧追涨思路拧了。

本脚本据此反向探索：对所有「▲ 接近支撑」触发点(可选是否要求 MA5↑)，
收集多个候选特征，逐个评估「满足该条件 vs 不满足」两组的前瞻收益，
找出真正能筛出好低吸点的条件。

候选条件:
  A 逢跌      : 今日涨跌 < 0
  B 价在MA5下 : 现价 < MA5
  C 缩量      : 量比 < 1.0
  D 贴近MA50  : 距MA50 <= 2.0%
  组合: A&C(缩量回调), B&C, A&B 等

用法:
    .venv/bin/python tools/entry_reverse_explore.py [--years 2] [--require-ma5up]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scan import ETF_POOL, get_cache_path  # noqa: E402
from backtest_strategy import analyze_hist, _dist_value  # noqa: E402

_CLOSE: dict[str, pd.DataFrame] = {}


def _closes(symbol: str) -> pd.DataFrame:
    if symbol not in _CLOSE:
        df = pd.read_csv(get_cache_path(symbol)).sort_values("date").reset_index(drop=True)
        df["d"] = df["date"].astype(str).str[:10]
        _CLOSE[symbol] = df
    return _CLOSE[symbol]


def today_change(symbol: str, as_of: str) -> float | None:
    df = _closes(symbol)
    idx = df.index[df["d"] == as_of]
    if len(idx) == 0 or idx[0] == 0:
        return None
    i = idx[0]
    prev, cur = float(df["close"].iloc[i - 1]), float(df["close"].iloc[i])
    return (cur - prev) / prev * 100 if prev else None


def fwd_return(symbol: str, as_of: str, k: int) -> float | None:
    df = _closes(symbol)
    idx = df.index[df["d"] == as_of]
    if len(idx) == 0:
        return None
    i = idx[0]
    if i + k >= len(df):
        return None
    base, fut = float(df["close"].iloc[i]), float(df["close"].iloc[i + k])
    return (fut / base - 1) * 100 if base else None


def trading_dates() -> list[str]:
    df = pd.read_csv(get_cache_path("588000"))
    return sorted(df["date"].astype(str).str[:10].unique().tolist())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--start", type=str, default=None, help="起始日 YYYY-MM-DD (覆盖 --years)")
    ap.add_argument("--end", type=str, default=None, help="结束日 YYYY-MM-DD")
    ap.add_argument("--require-ma5up", action="store_true",
                    help="是否仍要求 MA5拐头↑ (默认放开,看纯接近支撑)")
    args = ap.parse_args()

    all_dates = trading_dates()
    if args.start:
        start = args.start
    else:
        end_ts = pd.Timestamp(all_dates[-1])
        start = (end_ts - pd.Timedelta(days=int(args.years * 365))).strftime("%Y-%m-%d")
    end = args.end or all_dates[-1]
    dates = [d for d in all_dates if start <= d <= end]
    print(f"回测区间: {dates[0]} -> {dates[-1]}  ({len(dates)} 交易日)")
    print(f"MA5↑过滤: {'开' if args.require_ma5up else '关(纯 ▲接近支撑)'}\n")

    rows = []  # 每个触发点: dict(特征 + fwd)
    for date in dates:
        for code, name in ETF_POOL:
            r = analyze_hist(code, name, as_of_date=date)
            if r.get("状态") != "▲ 接近支撑":
                continue
            if args.require_ma5up and r.get("MA5拐头") != "↑":
                continue
            price = float(r.get("现价", 0) or 0)
            ma5 = float(r.get("MA5", 0) or 0)
            vol = float(r.get("量比", 0) or 0)
            dist = _dist_value(r.get("距MA50", "0%"))
            chg = today_change(code, date)
            fwds = [fwd_return(code, date, k) for k in range(1, 6)]
            if chg is None or price <= 0 or ma5 <= 0 or any(v is None for v in fwds):
                continue
            rows.append({
                "跌": chg < 0, "价<MA5": price < ma5, "缩量": vol < 1.0,
                "贴MA50": dist <= 2.0,
                "d3": fwds[2], "d5": fwds[4],
            })

    df = pd.DataFrame(rows)
    n = len(df)
    print(f"总触发点: {n}\n")
    if n == 0:
        return

    print(f"{'条件':<22}{'样本':>5}{'D+3均值':>9}{'D+3胜率':>8}{'D+5均值':>9}{'D+5胜率':>8}")
    print("-" * 62)

    def show(label, mask):
        sub = df[mask]
        if len(sub) == 0:
            print(f"{label:<22}{'0':>5}")
            return
        print(f"{label:<20}{len(sub):>5}{sub['d3'].mean():>+9.2f}{(sub['d3']>0).mean():>8.0%}"
              f"{sub['d5'].mean():>+9.2f}{(sub['d5']>0).mean():>8.0%}")

    show("全部(基线)", df.index >= 0)
    show("A 逢跌", df["跌"])
    show("B 价<MA5", df["价<MA5"])
    show("C 缩量", df["缩量"])
    show("D 贴MA50(<=2%)", df["贴MA50"])
    print("-- 组合 --")
    show("A&C 缩量回调", df["跌"] & df["缩量"])
    show("B&C 价<MA5&缩量", df["价<MA5"] & df["缩量"])
    show("A&B 跌&价<MA5", df["跌"] & df["价<MA5"])
    show("A&D 跌&贴MA50", df["跌"] & df["贴MA50"])
    show("A&B&C 跌&价<MA5&缩量", df["跌"] & df["价<MA5"] & df["缩量"])
    print("\n解读: 找 D+3/D+5 均值与胜率都明显高于基线的条件，作为反向低吸判据候选。")


if __name__ == "__main__":
    main()
