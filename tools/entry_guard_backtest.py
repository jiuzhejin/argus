#!/usr/bin/env python3
"""加仓判据加固验证（独立脚本，不改 scan.py）。

针对 check_holdings 的「▲ 接近支撑 + MA5拐头↑ → 🟢 加仓」分支(scan.py:1212)，
验证两道加固闸门是否「挡住坏点、不误伤好点」：
    闸门1 当日不明显下跌: 今日涨跌 > DIP_GUARD_PCT (默认 -1.5%)
    闸门2 价格未被短均线压制: 现价 >= MA5

方法：复用 backtest_strategy.analyze_hist 的信号，扫近2年所有交易日，
找出每个「▲ 接近支撑 + MA5↑」触发点，按是否通过闸门分为 kept / blocked 两组，
统计各组 D+1~D+5 的前瞻收益(用 ETF 缓存收盘价)。
若 blocked 组后续表现明显差于 kept 组，说明闸门挡对了。

用法:
    .venv/bin/python tools/entry_guard_backtest.py [--years 2] [--dip -1.5]
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


def trading_dates() -> list[str]:
    df = pd.read_csv(get_cache_path("588000"))
    return sorted(df["date"].astype(str).str[:10].unique().tolist())


_CLOSE_CACHE: dict[str, pd.DataFrame] = {}


def _closes(symbol: str) -> pd.DataFrame:
    if symbol not in _CLOSE_CACHE:
        df = pd.read_csv(get_cache_path(symbol)).sort_values("date").reset_index(drop=True)
        df["d"] = df["date"].astype(str).str[:10]
        _CLOSE_CACHE[symbol] = df
    return _CLOSE_CACHE[symbol]


def today_change(symbol: str, as_of: str) -> float | None:
    df = _closes(symbol)
    idx = df.index[df["d"] == as_of]
    if len(idx) == 0 or idx[0] == 0:
        return None
    i = idx[0]
    prev, cur = float(df["close"].iloc[i - 1]), float(df["close"].iloc[i])
    return (cur - prev) / prev * 100 if prev else None


def fwd_return(symbol: str, as_of: str, k: int) -> float | None:
    """D+k 相对 as_of 收盘的前瞻收益%。"""
    df = _closes(symbol)
    idx = df.index[df["d"] == as_of]
    if len(idx) == 0:
        return None
    i = idx[0]
    if i + k >= len(df):
        return None
    base, fut = float(df["close"].iloc[i]), float(df["close"].iloc[i + k])
    return (fut / base - 1) * 100 if base else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--dip", type=float, default=-1.5, help="当日跌幅闸门(百分比)")
    args = ap.parse_args()

    all_dates = trading_dates()
    end = pd.Timestamp(all_dates[-1])
    start = (end - pd.Timedelta(days=int(args.years * 365))).strftime("%Y-%m-%d")
    dates = [d for d in all_dates if d >= start]
    print(f"回测区间: {dates[0]} -> {dates[-1]}  ({len(dates)} 交易日)")
    print(f"闸门参数: 当日跌幅 > {args.dip}%  且  现价 >= MA5\n")

    kept, blocked = [], []   # 每项: D+1..D+5 收益列表
    blocked_reasons = {"当日下跌": 0, "价<MA5": 0, "两者都触发": 0}

    for date in dates:
        for code, name in ETF_POOL:
            r = analyze_hist(code, name, as_of_date=date)
            if r.get("状态") != "▲ 接近支撑" or r.get("MA5拐头") != "↑":
                continue
            price = float(r.get("现价", 0) or 0)
            ma5 = float(r.get("MA5", 0) or 0)
            chg = today_change(code, date)
            if chg is None or price <= 0 or ma5 <= 0:
                continue

            dip_fail = chg <= args.dip
            ma5_fail = price < ma5
            fwds = [fwd_return(code, date, k) for k in range(1, 6)]
            if any(v is None for v in fwds):
                continue

            if dip_fail or ma5_fail:
                blocked.append(fwds)
                if dip_fail and ma5_fail:
                    blocked_reasons["两者都触发"] += 1
                elif dip_fail:
                    blocked_reasons["当日下跌"] += 1
                else:
                    blocked_reasons["价<MA5"] += 1
            else:
                kept.append(fwds)

    def summ(group, label):
        n = len(group)
        if n == 0:
            print(f"  {label}: 0 个触发点")
            return
        arr = pd.DataFrame(group, columns=[f"D+{k}" for k in range(1, 6)])
        means = arr.mean()
        winr = (arr > 0).mean()
        print(f"  {label}: {n} 个触发点")
        for k in range(1, 6):
            c = f"D+{k}"
            print(f"    {c}: 平均收益 {means[c]:+.2f}%   胜率 {winr[c]:.0%}")

    print("=" * 56)
    print("原判据(接近支撑+MA5↑)全部触发点，按加固闸门分两组:")
    print("=" * 56)
    total = len(kept) + len(blocked)
    print(f"总触发 {total} 次 | 保留(加固后仍加仓) {len(kept)} | 拦下(降级持有) {len(blocked)}")
    print(f"拦下原因分布: {blocked_reasons}\n")
    summ(kept, "✅ KEPT  (通过闸门, 仍喊加仓)")
    print()
    summ(blocked, "🛑 BLOCKED(被闸门拦下, 降级持有)")
    print()
    print("解读: 若 BLOCKED 组 D+1~D+5 明显差于 KEPT 组(收益更低/胜率更低)，")
    print("      说明闸门挡住的确实是坏点，加固有效且未误伤。")


if __name__ == "__main__":
    main()
