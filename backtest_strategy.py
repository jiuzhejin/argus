from __future__ import annotations

from pathlib import Path

import pandas as pd

from scan import ETF_POOL, get_cache_path


ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
OUT_DIR = LOG_DIR / "strategy_backtest"

BUY_STATUS = "★ 低位确认"
FOLLOW_STATUS = "◆ 趋势跟随"
LEARN_STATUS = "◇ 转强初期"
WEAK_STATUS = "✗ 趋势偏弱"
TP_STATUSES = {"◆ 趋势跟随", "□ 多头排列", "- 趋势完好", "▲ 接近支撑"}
# 当前 scan.py 新增了“转强初期”，但它不是确认型买点。
# 回测里给它更小的权重，只用于估算“先买一点看看”的收益弹性。
BUY_WEIGHTS = {BUY_STATUS: 1.0, FOLLOW_STATUS: 0.5, LEARN_STATUS: 0.25}


def load_scan_dates() -> list[str]:
    merged = LOG_DIR / "signal_review_merged" / "daily_status.csv"
    if merged.exists():
        df = pd.read_csv(merged)
        dates = sorted(pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").unique().tolist())
        if dates:
            return dates

    dates = set()
    for path in LOG_DIR.glob("scan_*.log"):
        try:
            first = path.read_text(encoding="utf-8").splitlines()[0]
        except Exception:
            continue
        if first.startswith("--- "):
            parts = first.split()
            if len(parts) >= 2:
                dates.add(parts[1])
    return sorted(dates)


def _dist_value(dist: str) -> float:
    try:
        return float(str(dist).replace("%", "").replace("+", ""))
    except (TypeError, ValueError):
        return 0.0


def is_take_profit_watch(row: dict) -> bool:
    status = row.get("状态", "")
    if status not in TP_STATUSES:
        return False
    dist_val = _dist_value(row.get("距MA50", "0%"))
    return dist_val >= 8


def take_profit_reason(row: dict) -> str | None:
    status = row.get("状态", "")
    if status not in TP_STATUSES:
        return None
    price = float(row.get("现价", 0) or 0)
    ma5 = float(row.get("MA5", 0) or 0)
    ma10 = float(row.get("MA10", 0) or 0)
    dist_val = _dist_value(row.get("距MA50", "0%"))
    soft = (
        dist_val >= 8
        and ma5 > 0
        and price < ma5
        and row.get("MA5拐头") == "↓"
    )
    hard = (
        dist_val >= 10
        and ma10 > 0
        and price < ma10
        and status in {"□ 多头排列", "- 趋势完好", "▲ 接近支撑"}
    )
    if hard:
        return "take_profit_hard"
    if soft:
        return "take_profit_soft"
    return None


def should_take_profit(row: dict) -> bool:
    return take_profit_reason(row) is not None


def load_hist(symbol: str) -> pd.DataFrame:
    path = get_cache_path(symbol)
    if not path.exists():
        raise RuntimeError(f"missing cache for {symbol}: {path}")
    df = pd.read_csv(path)
    if "date" not in df.columns:
        raise RuntimeError(f"cache missing date column for {symbol}")
    df = df.sort_values("date").reset_index(drop=True)
    return df


def analyze_hist(symbol: str, name: str, as_of_date: str) -> dict:
    df = load_hist(symbol)
    df = df[df["date"].astype(str).str[:10] <= as_of_date].tail(250).copy()
    if len(df) < 140:
        return {"代码": symbol, "名称": name, "状态": "⚠ 数据不足"}

    close = df["close"].astype(float)
    volume = df["volume"].astype(float)

    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    ma50 = close.rolling(50).mean()
    ma100 = close.rolling(100).mean()
    vol_avg20 = volume.rolling(20).mean()

    c = close.iloc[-1]
    m5, m10, m20 = ma5.iloc[-1], ma10.iloc[-1], ma20.iloc[-1]
    m50, m100 = ma50.iloc[-1], ma100.iloc[-1]
    vol = volume.iloc[-1]
    va20 = vol_avg20.iloc[-1]

    recent_low_5d = close.tail(5).min()
    recent_low_10d = close.tail(10).min()
    support_tested = recent_low_5d <= m50 * 1.015
    support_recent_10d = recent_low_10d <= m50 * 1.02
    ma5_up = ma5.iloc[-1] > ma5.iloc[-2] and ma5.iloc[-2] > ma5.iloc[-3]
    ma20_up = ma20.iloc[-1] > ma20.iloc[-2] and ma20.iloc[-2] > ma20.iloc[-3]
    vol_ratio = round(vol / va20, 2) if pd.notna(va20) and va20 > 0 else 0
    dist_ma50_pct = round((c - m50) / m50 * 100, 2)

    if abs(c - m5) / m5 > 0.3:
        return {"代码": symbol, "名称": name, "状态": "⚠ 数据异常(价格偏离MA5超30%)"}

    above_long = c > m50 and c > m100
    above_mid = c > m50
    bull_align = c > m5 and c > m10 and c > m20 and above_long
    ma50_slope = (ma50.iloc[-1] - ma50.iloc[-10]) / ma50.iloc[-10] * 100

    buy_core = (
        c > m5 and c > m10
        and above_long
        and 0.8 <= vol_ratio <= 2.5
        and ma5_up
        and support_tested
        and ma50_slope > 0
    )

    breakout = ""
    days_below = 0
    recent_closes = close.iloc[-6:-1]
    recent_ma50 = ma50.iloc[-6:-1]
    if len(recent_closes) >= 5 and len(recent_ma50) >= 5:
        days_below = int((recent_closes < recent_ma50).sum())
        if above_long and days_below >= 3:
            breakout = f"⬆ 突破({days_below}/5日在MA50下)"

    entry_dist_cap = 4.5
    trend_ready = m20 > m50 and ma50_slope > 0.1
    executable_breakout = breakout != ""
    strict_buy = (
        buy_core
        and dist_ma50_pct < entry_dist_cap
        and 0.85 <= vol_ratio <= 1.9
        and trend_ready
        and (m5 > m10 > m20 or executable_breakout)
    )
    trend_follow_ready = (
        buy_core
        and m20 > m50 > m100
        and 0.85 <= vol_ratio <= 1.8
        and (
            (m20 / m50 - 1) * 100 >= 0.5
            or dist_ma50_pct <= 2.5
        )
    )
    early_reversal_follow = (
        above_mid
        and not above_long
        and support_tested
        and support_recent_10d
        and ma5_up
        and ma20_up
        and c > m5 and c > m10 and c > m20
        and m5 > m10 > m20
        and 0.85 <= vol_ratio <= 1.8
        and dist_ma50_pct <= 6.5
        and -1.8 < ma50_slope <= 0
        and c >= m100 * 0.995
        and days_below >= 3
    )
    early_bull_follow = (
        bull_align
        and support_recent_10d
        and ma5_up
        and ma20_up
        and m5 > m10 > m20
        and 0.85 <= vol_ratio <= 1.6
        and 4.5 <= dist_ma50_pct <= 6.5
        and c <= m100 * 1.03
        and -1.2 < ma50_slope <= 0
    )
    early_reversal_watch = (
        above_mid
        and not above_long
        and support_recent_10d
        and ma5_up
        and ma20_up
        and c > m5 and c > m10
        and m5 > m10 > m20
        and 0.8 <= vol_ratio <= 1.8
        and dist_ma50_pct <= 5.0
        and -1.8 < ma50_slope <= 0
    )
    learning_signal = (
        not above_long
        and c > m5 and c > m10 and c > m20
        and m5 > m10 > m20
        and ma5_up
        and ma20_up
        and 0.8 <= vol_ratio <= 1.8
        and dist_ma50_pct <= 8.0
        and c >= m50 * 0.94
    )
    near_support = above_long and dist_ma50_pct <= 5.0 and c < m20

    if strict_buy:
        status = "★ 低位确认"
    elif trend_follow_ready or early_reversal_follow or early_bull_follow:
        status = "◆ 趋势跟随"
    elif learning_signal:
        status = "◇ 转强初期"
    elif early_reversal_watch:
        status = "- 趋势完好"
    elif near_support:
        status = "▲ 接近支撑"
    elif bull_align:
        status = "□ 多头排列"
    elif above_long:
        status = "- 趋势完好"
    else:
        status = "✗ 趋势偏弱"

    return {
        "代码": symbol,
        "名称": name,
        "现价": round(c, 3),
        "MA5": round(m5, 3),
        "MA10": round(m10, 3),
        "MA20": round(m20, 3),
        "MA50": round(m50, 3),
        "MA100": round(m100, 3),
        "距MA50": f"{dist_ma50_pct:+.1f}%",
        "量比": vol_ratio,
        "回踩MA50": "是" if support_tested else "否",
        "MA5拐头": "↑" if ma5_up else "↓",
        "状态": status,
    }


def run_backtest() -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = load_scan_dates()
    if not dates:
        raise RuntimeError("no scan dates found")

    history_rows: list[dict] = []
    for date in dates:
        for code, name in ETF_POOL:
            row = analyze_hist(code, name, as_of_date=date)
            row["日期"] = date
            history_rows.append(row)

    history = pd.DataFrame(history_rows)
    history = history.sort_values(["日期", "代码"]).reset_index(drop=True)

    trades: list[dict] = []
    open_positions: dict[str, dict] = {}

    for date in dates:
        day = history[history["日期"] == date].copy()
        row_map = {r["代码"]: r for _, r in day.iterrows()}

        # First process exits based on the current day's structure.
        for code in list(open_positions.keys()):
            pos = open_positions[code]
            row = row_map.get(code)
            if row is None:
                continue

            status = row.get("状态", "")
            exit_reason = ""
            if status == WEAK_STATUS:
                exit_reason = "stop_loss"
            elif should_take_profit(row):
                exit_reason = take_profit_reason(row)

            if exit_reason:
                trades.append(
                    {
                        "代码": code,
                        "名称": pos["名称"],
                        "买入日期": pos["买入日期"],
                        "买入价": pos["买入价"],
                        "买入状态": pos["买入状态"],
                        "仓位权重": pos["仓位权重"],
                        "卖出日期": date,
                        "卖出价": row.get("现价"),
                        "卖出原因": exit_reason,
                        "卖出状态": status,
                        "止盈观察区": is_take_profit_watch(row),
                        "持有天数": (pd.Timestamp(date) - pd.Timestamp(pos["买入日期"])).days,
                        "收益率": float(row.get("现价")) / pos["买入价"] - 1,
                        "加权收益率": (float(row.get("现价")) / pos["买入价"] - 1) * pos["仓位权重"],
                        "已平仓": True,
                    }
                )
                del open_positions[code]

        # Then open new positions if today's signal is a fresh executable buy.
        for _, row in day.iterrows():
            code = row["代码"]
            signal = row.get("状态")
            if signal not in BUY_WEIGHTS:
                continue
            if code in open_positions:
                continue

            prev = history[(history["代码"] == code) & (history["日期"] < date)].tail(1)
            prev_status = prev.iloc[0]["状态"] if not prev.empty else None
            if prev_status == signal:
                continue

            open_positions[code] = {
                "名称": row["名称"],
                "买入日期": date,
                "买入价": float(row["现价"]),
                "买入状态": signal,
                "仓位权重": BUY_WEIGHTS[signal],
            }

    latest_date = dates[-1]
    latest_rows = history[history["日期"] == latest_date].set_index("代码")
    for code, pos in open_positions.items():
        row = latest_rows.loc[code]
        trades.append(
            {
                "代码": code,
                "名称": pos["名称"],
                "买入日期": pos["买入日期"],
                "买入价": pos["买入价"],
                "买入状态": pos["买入状态"],
                "仓位权重": pos["仓位权重"],
                "卖出日期": latest_date,
                "卖出价": float(row["现价"]),
                "卖出原因": "mark_to_market",
                "卖出状态": row["状态"],
                "止盈观察区": is_take_profit_watch(row),
                "持有天数": (pd.Timestamp(latest_date) - pd.Timestamp(pos["买入日期"])).days,
                "收益率": float(row["现价"]) / pos["买入价"] - 1,
                "加权收益率": (float(row["现价"]) / pos["买入价"] - 1) * pos["仓位权重"],
                "已平仓": False,
            }
        )

    trades_df = pd.DataFrame(trades)
    if trades_df.empty:
        trades_df = pd.DataFrame(
            columns=[
                "代码", "名称", "买入日期", "买入价", "买入状态", "仓位权重",
                "卖出日期", "卖出价", "卖出原因", "卖出状态",
                "止盈观察区", "持有天数", "收益率", "加权收益率", "已平仓",
            ]
        )
    else:
        trades_df = trades_df.sort_values(["买入日期", "代码"]).reset_index(drop=True)
    return history, trades_df


def write_report(history: pd.DataFrame, trades: pd.DataFrame) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    history.to_csv(OUT_DIR / "history.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_DIR / "trades.csv", index=False, encoding="utf-8-sig")

    closed = trades[trades["已平仓"]].copy()
    open_ = trades[~trades["已平仓"]].copy()

    summary = {
        "trade_count": len(trades),
        "closed_count": len(closed),
        "open_count": len(open_),
        "open_watch_count": int(open_["止盈观察区"].fillna(False).sum()) if not open_.empty else 0,
        "closed_win_rate": float((closed["收益率"] > 0).mean()) if not closed.empty else None,
        "closed_avg_return": float(closed["收益率"].mean()) if not closed.empty else None,
        "closed_weighted_return": float(closed["加权收益率"].sum() / closed["仓位权重"].sum()) if not closed.empty and closed["仓位权重"].sum() > 0 else None,
        "open_avg_return": float(open_["收益率"].mean()) if not open_.empty else None,
        "open_weighted_return": float(open_["加权收益率"].sum() / open_["仓位权重"].sum()) if not open_.empty and open_["仓位权重"].sum() > 0 else None,
        "all_avg_return": float(trades["收益率"].mean()) if not trades.empty else None,
        "all_weighted_return": float(trades["加权收益率"].sum() / trades["仓位权重"].sum()) if not trades.empty and trades["仓位权重"].sum() > 0 else None,
    }

    month_df = trades.copy()
    month_df["买入月份"] = pd.to_datetime(month_df["买入日期"]).dt.strftime("%Y-%m")
    by_month = (
        month_df.groupby("买入月份", as_index=False)
        .agg(
            交易数=("代码", "size"),
            已平仓=("已平仓", "sum"),
            平均收益率=("收益率", "mean"),
            加权收益率=("加权收益率", "sum"),
            总仓位=("仓位权重", "sum"),
            胜率=("收益率", lambda s: (s > 0).mean()),
        )
    )
    if not by_month.empty:
        by_month["仓位加权平均收益率"] = by_month["加权收益率"] / by_month["总仓位"]

    lines = [
        "# Strategy Backtest",
        "",
        "- Buy rule: current `scan.py` opens on `★ 低位确认` (1.0x), `◆ 趋势跟随` (0.5x), and `◇ 转强初期` (0.25x estimate)",
        "- Exit rule: first `✗ 趋势偏弱` or first structural `take_profit` trigger",
        "- Open trades are marked to market with the latest available scan date",
        "",
        "## Summary",
        "",
        f"- Total trades: {summary['trade_count']}",
        f"- Closed trades: {summary['closed_count']}",
        f"- Open trades: {summary['open_count']}",
        f"- Closed win rate: {summary['closed_win_rate']:.1%}" if summary["closed_win_rate"] is not None else "- Closed win rate: -",
        f"- Closed avg return: {summary['closed_avg_return']:.1%}" if summary["closed_avg_return"] is not None else "- Closed avg return: -",
        f"- Closed weighted return: {summary['closed_weighted_return']:.1%}" if summary["closed_weighted_return"] is not None else "- Closed weighted return: -",
        f"- Open avg mark-to-market: {summary['open_avg_return']:.1%}" if summary["open_avg_return"] is not None else "- Open avg mark-to-market: -",
        f"- Open weighted mark-to-market: {summary['open_weighted_return']:.1%}" if summary["open_weighted_return"] is not None else "- Open weighted mark-to-market: -",
        f"- Open trades in take-profit watch zone: {summary['open_watch_count']}",
        f"- All trades avg return: {summary['all_avg_return']:.1%}" if summary["all_avg_return"] is not None else "- All trades avg return: -",
        f"- All trades weighted return: {summary['all_weighted_return']:.1%}" if summary["all_weighted_return"] is not None else "- All trades weighted return: -",
        "",
        "## By Month",
        "",
        by_month.to_markdown(index=False),
        "",
        "## Trades",
        "",
        trades.to_markdown(index=False),
        "",
    ]

    report_path = OUT_DIR / "report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def main() -> None:
    history, trades = run_backtest()
    report_path = write_report(history, trades)

    closed = trades[trades["已平仓"]]
    open_ = trades[~trades["已平仓"]]

    print(f"wrote {OUT_DIR}")
    print(f"report: {report_path}")
    print(f"total trades: {len(trades)}")
    print(f"closed trades: {len(closed)}")
    print(f"open trades: {len(open_)}")
    if not closed.empty:
        print(f"closed win rate: {(closed['收益率'] > 0).mean():.4f}")
        print(f"closed avg return: {closed['收益率'].mean():.6f}")
        if closed["仓位权重"].sum() > 0:
            print(f"closed weighted return: {closed['加权收益率'].sum() / closed['仓位权重'].sum():.6f}")
    if not open_.empty:
        print(f"open avg mtm: {open_['收益率'].mean():.6f}")
        if open_["仓位权重"].sum() > 0:
            print(f"open weighted mtm: {open_['加权收益率'].sum() / open_['仓位权重'].sum():.6f}")
    if not trades.empty:
        print(f"all avg return: {trades['收益率'].mean():.6f}")
        if trades["仓位权重"].sum() > 0:
            print(f"all weighted return: {trades['加权收益率'].sum() / trades['仓位权重'].sum():.6f}")


if __name__ == "__main__":
    main()
