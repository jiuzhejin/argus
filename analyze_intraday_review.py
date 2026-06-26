from __future__ import annotations

import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parent
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".cache" / "matplotlib"))
os.environ.setdefault("XDG_CACHE_HOME", str(ROOT / ".cache"))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd


LOG_DIR = ROOT / "logs"
OUT_DIR = LOG_DIR / "intraday_review"
MERGED_OUT_DIR = LOG_DIR / "signal_review_merged"

STATUS_ORDER = [
    "★ 买入信号",
    "◆ 趋势持有",
    "▲ 接近支撑",
    "◇ 多头排列",
    "- 趋势完好",
    "✗ 趋势偏弱",
]
BUY_STATUSES = {"★ 买入信号", "◆ 趋势持有"}
WEAK_STATUS = "✗ 趋势偏弱"

STATUS_PATTERN = re.compile("|".join(re.escape(s) for s in sorted(STATUS_ORDER, key=len, reverse=True)))
HEADER_DATE_RE = re.compile(r"^---\s+(\d{4}-\d{2}-\d{2})\s")
HEADER_DATETIME_RE = re.compile(r"^---\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+---$")
ROW_RE = re.compile(r"^(\d{6})\s+(\S+)\s+([0-9.]+)")
STOP_RE = re.compile(r"🔴 止损\s+(.+?)\(")


@dataclass
class Event:
    code: str
    name: str
    date: pd.Timestamp
    event_type: str
    status: str
    price: float
    source: str
    log_kind: str = ""
    log_time: str = ""
    eval_label: str = ""
    ret_1d: float | None = None
    ret_3d: float | None = None
    ret_5d: float | None = None
    max_5d: float | None = None
    min_5d: float | None = None


def classify_log_kind(path: Path) -> str:
    if path.name.endswith("_intraday.log"):
        return "intraday"
    if path.name.endswith("_morning.log"):
        return "morning"
    return "close"


def parse_log_header(path: Path, text: str) -> tuple[pd.Timestamp, str]:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    match_dt = HEADER_DATETIME_RE.match(first_line)
    if match_dt:
        return pd.Timestamp(match_dt.group(1)), match_dt.group(2)

    match = HEADER_DATE_RE.match(first_line)
    if match:
        return pd.Timestamp(match.group(1)), ""

    stem = path.stem
    token = stem.replace("scan_", "").replace("_intraday", "")
    if len(token) == 8:
        return pd.Timestamp(token), ""
    if len(token) == 4:
        return pd.Timestamp(f"2026{token}"), ""
    raise ValueError(f"cannot infer date from {path.name}")


def parse_log(path: Path) -> tuple[list[dict], list[dict]]:
    text = path.read_text(encoding="utf-8")
    date, log_time = parse_log_header(path, text)
    log_kind = classify_log_kind(path)

    rows: list[dict] = []
    sell_rows: list[dict] = []
    pending_stop_name: str | None = None

    for raw in text.splitlines():
        line = raw.rstrip()

        row_match = ROW_RE.match(line)
        if row_match:
            code, name, price = row_match.groups()
            status_match = STATUS_PATTERN.search(line)
            if status_match:
                rows.append(
                    {
                        "date": date,
                        "log_time": log_time,
                        "log_kind": log_kind,
                        "code": code,
                        "name": name,
                        "price": float(price),
                        "status": status_match.group(0),
                        "source": path.name,
                    }
                )

        stop_match = STOP_RE.search(line)
        if stop_match:
            pending_stop_name = stop_match.group(1).strip()
            continue

        if pending_stop_name and "→" in line:
            statuses = [m.group(0) for m in STATUS_PATTERN.finditer(line)]
            if len(statuses) >= 2:
                sell_rows.append(
                    {
                        "date": date,
                        "log_time": log_time,
                        "log_kind": log_kind,
                        "name": pending_stop_name,
                        "from_status": statuses[0],
                        "to_status": statuses[1],
                        "source": path.name,
                    }
                )
            pending_stop_name = None

    return rows, sell_rows


def build_timeseries() -> tuple[pd.DataFrame, pd.DataFrame]:
    all_rows: list[dict] = []
    explicit_sells: list[dict] = []

    for path in sorted(LOG_DIR.glob("scan_*_intraday.log")):
        rows, sells = parse_log(path)
        all_rows.extend(rows)
        explicit_sells.extend(sells)

    df = pd.DataFrame(all_rows)
    if df.empty:
        raise RuntimeError("no intraday log rows found")

    df = df.sort_values(["date", "code"]).drop_duplicates(["date", "code"], keep="last").reset_index(drop=True)

    name_to_code = (
        df.sort_values(["name", "date"])
        .groupby("name", as_index=False)
        .last()[["name", "code"]]
        .set_index("name")["code"]
        .to_dict()
    )

    sell_df = pd.DataFrame(explicit_sells)
    if not sell_df.empty:
        sell_df["code"] = sell_df["name"].map(name_to_code)
        sell_df = sell_df.dropna(subset=["code"]).reset_index(drop=True)
    else:
        sell_df = pd.DataFrame(columns=["date", "name", "from_status", "to_status", "source", "code"])

    return df, sell_df


def _time_score(log_time: str) -> tuple[int, int]:
    if not log_time:
        return (5, 9999)
    hh, mm, *_ = [int(x) for x in log_time.split(":")]
    minutes = hh * 60 + mm
    if 14 * 60 + 40 <= minutes <= 14 * 60 + 50:
        return (0, abs(minutes - (14 * 60 + 45)))
    if 14 * 60 <= minutes < 15 * 60:
        return (1, abs(minutes - (14 * 60 + 45)))
    if 15 * 60 <= minutes <= 16 * 60:
        return (2, minutes - 15 * 60)
    if 9 * 60 + 30 <= minutes <= 11 * 60 + 30:
        return (3, minutes)
    return (4, minutes)


def merge_all_logs() -> tuple[pd.DataFrame, pd.DataFrame]:
    all_rows: list[dict] = []
    explicit_sells: list[dict] = []

    log_paths = sorted(LOG_DIR.glob("scan_*.log"))
    for path in log_paths:
        rows, sells = parse_log(path)
        all_rows.extend(rows)
        explicit_sells.extend(sells)

    df = pd.DataFrame(all_rows)
    if df.empty:
        raise RuntimeError("no scan log rows found")

    df["_priority"] = df["log_time"].apply(_time_score)
    df = (
        df.sort_values(["date", "code", "_priority", "source"])
        .drop_duplicates(["date", "code"], keep="first")
        .drop(columns=["_priority"])
        .reset_index(drop=True)
    )

    name_to_code = (
        df.sort_values(["name", "date"])
        .groupby("name", as_index=False)
        .last()[["name", "code"]]
        .set_index("name")["code"]
        .to_dict()
    )

    sell_df = pd.DataFrame(explicit_sells)
    if not sell_df.empty:
        sell_df["code"] = sell_df["name"].map(name_to_code)
        sell_df["_priority"] = sell_df["log_time"].apply(_time_score)
        sell_df = (
            sell_df.dropna(subset=["code"])
            .sort_values(["date", "code", "_priority", "source"])
            .drop_duplicates(["date", "code"], keep="first")
            .drop(columns=["_priority"])
            .reset_index(drop=True)
        )
    else:
        sell_df = pd.DataFrame(columns=["date", "log_time", "log_kind", "name", "from_status", "to_status", "source", "code"])

    return df, sell_df


def nth_future_return(prices: pd.Series, idx: int, days: int) -> float | None:
    future_idx = idx + days
    if future_idx >= len(prices):
        return None
    return prices.iloc[future_idx] / prices.iloc[idx] - 1


def window_extremes(prices: pd.Series, idx: int, days: int = 5) -> tuple[float | None, float | None]:
    future = prices.iloc[idx + 1 : idx + days + 1]
    if future.empty:
        return None, None
    base = prices.iloc[idx]
    return future.max() / base - 1, future.min() / base - 1


def classify_buy(ret_5d: float | None, max_5d: float | None) -> str:
    if ret_5d is None or max_5d is None:
        return "待观察"
    if ret_5d > 0:
        return "有效"
    if max_5d > 0:
        return "存疑"
    return "失效"


def classify_sell(ret_5d: float | None, min_5d: float | None) -> str:
    if ret_5d is None or min_5d is None:
        return "待观察"
    if ret_5d < 0:
        return "有效"
    if min_5d < 0:
        return "存疑"
    return "失效"


def build_events(df: pd.DataFrame, explicit_sell_df: pd.DataFrame) -> pd.DataFrame:
    events: list[Event] = []

    explicit_sell_dates = {
        (row.code, row.date): row.source for row in explicit_sell_df.itertuples()
    }

    for code, g in df.groupby("code", sort=True):
        g = g.sort_values("date").reset_index(drop=True)
        prices = g["price"]
        statuses = g["status"]

        has_buy_before = False
        sell_recorded_since_last_buy = False
        for idx, row in g.iterrows():
            prev_status = statuses.iloc[idx - 1] if idx > 0 else None
            status = row["status"]
            date = row["date"]
            price = float(row["price"])
            name = row["name"]

            if status in BUY_STATUSES and prev_status not in BUY_STATUSES:
                max_5d, min_5d = window_extremes(prices, idx, 5)
                ret_1d = nth_future_return(prices, idx, 1)
                ret_3d = nth_future_return(prices, idx, 3)
                ret_5d = nth_future_return(prices, idx, 5)
                events.append(
                    Event(
                        code=code,
                        name=name,
                        date=date,
                        event_type="buy",
                        status=status,
                        price=price,
                        source=row["source"],
                        log_kind=row.get("log_kind", ""),
                        log_time=row.get("log_time", ""),
                        eval_label=classify_buy(ret_5d, max_5d),
                        ret_1d=ret_1d,
                        ret_3d=ret_3d,
                        ret_5d=ret_5d,
                        max_5d=max_5d,
                        min_5d=min_5d,
                    )
                )
                has_buy_before = True
                sell_recorded_since_last_buy = False

            explicit_source = explicit_sell_dates.get((code, date))
            if explicit_source and has_buy_before and not sell_recorded_since_last_buy:
                max_5d, min_5d = window_extremes(prices, idx, 5)
                ret_1d = nth_future_return(prices, idx, 1)
                ret_3d = nth_future_return(prices, idx, 3)
                ret_5d = nth_future_return(prices, idx, 5)
                events.append(
                    Event(
                        code=code,
                        name=name,
                        date=date,
                        event_type="sell",
                        status=status,
                        price=price,
                        source=explicit_source,
                        log_kind=row.get("log_kind", ""),
                        log_time=row.get("log_time", ""),
                        eval_label=classify_sell(ret_5d, min_5d),
                        ret_1d=ret_1d,
                        ret_3d=ret_3d,
                        ret_5d=ret_5d,
                        max_5d=max_5d,
                        min_5d=min_5d,
                    )
                )
                sell_recorded_since_last_buy = True
                continue

            if (
                has_buy_before
                and not sell_recorded_since_last_buy
                and idx > 0
                and prev_status != WEAK_STATUS
                and status == WEAK_STATUS
            ):
                max_5d, min_5d = window_extremes(prices, idx, 5)
                ret_1d = nth_future_return(prices, idx, 1)
                ret_3d = nth_future_return(prices, idx, 3)
                ret_5d = nth_future_return(prices, idx, 5)
                events.append(
                    Event(
                        code=code,
                        name=name,
                        date=date,
                        event_type="sell_proxy",
                        status=status,
                        price=price,
                        source=row["source"],
                        log_kind=row.get("log_kind", ""),
                        log_time=row.get("log_time", ""),
                        eval_label=classify_sell(ret_5d, min_5d),
                        ret_1d=ret_1d,
                        ret_3d=ret_3d,
                        ret_5d=ret_5d,
                        max_5d=max_5d,
                        min_5d=min_5d,
                    )
                )
                sell_recorded_since_last_buy = True

    events_df = pd.DataFrame([e.__dict__ for e in events])
    if events_df.empty:
        return pd.DataFrame(
            columns=["code", "name", "date", "event_type", "status", "price", "source", "eval_label", "ret_1d", "ret_3d", "ret_5d", "max_5d", "min_5d"]
        )
    return events_df.sort_values(["date", "code", "event_type"]).reset_index(drop=True)


def pct_text(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value * 100:+.1f}%"


def plot_one(code: str, g: pd.DataFrame, event_df: pd.DataFrame) -> Path:
    name = g["name"].iloc[0]
    fig, ax = plt.subplots(figsize=(13, 4.8))
    ax.plot(g["date"], g["price"], color="#2f5d8a", linewidth=1.8, label="intraday price")

    buy_df = event_df[event_df["event_type"] == "buy"]
    sell_df = event_df[event_df["event_type"].isin(["sell", "sell_proxy"])]

    if not buy_df.empty:
        ax.scatter(buy_df["date"], buy_df["price"], marker="^", s=110, color="#1a9850", label="buy")
        for row in buy_df.itertuples():
            ax.annotate(
                f"B {row.date.strftime('%m-%d')}",
                (row.date, row.price),
                xytext=(0, 10),
                textcoords="offset points",
                ha="center",
                fontsize=8,
                color="#1a9850",
            )

    if not sell_df.empty:
        colors = ["#d73027" if t == "sell" else "#fc8d59" for t in sell_df["event_type"]]
        ax.scatter(sell_df["date"], sell_df["price"], marker="v", s=110, color=colors, label="sell")
        for row in sell_df.itertuples():
            tag = "S" if row.event_type == "sell" else "P"
            ax.annotate(
                f"{tag} {row.date.strftime('%m-%d')}",
                (row.date, row.price),
                xytext=(0, -16),
                textcoords="offset points",
                ha="center",
                fontsize=8,
                color="#d73027" if row.event_type == "sell" else "#fc8d59",
            )

    ax.set_title(f"{name} {code}")
    ax.set_xlabel("date")
    ax.set_ylabel("price")
    ax.grid(alpha=0.25, linestyle="--")
    ax.legend(loc="best")

    recent_buy = buy_df.tail(1)
    recent_sell = sell_df.tail(1)
    summary_bits = []
    if not recent_buy.empty:
        row = recent_buy.iloc[0]
        summary_bits.append(f"last buy {row['date'].strftime('%m-%d')} {row['eval_label']} 5d {pct_text(row['ret_5d'])}")
    if not recent_sell.empty:
        row = recent_sell.iloc[0]
        summary_bits.append(f"last sell {row['date'].strftime('%m-%d')} {row['eval_label']} 5d {pct_text(row['ret_5d'])}")
    if summary_bits:
        ax.text(
            0.01,
            0.02,
            " | ".join(summary_bits),
            transform=ax.transAxes,
            fontsize=9,
            bbox={"boxstyle": "round,pad=0.25", "facecolor": "white", "alpha": 0.85, "edgecolor": "#cccccc"},
        )

    fig.autofmt_xdate()
    fig.tight_layout()
    out = OUT_DIR / f"{code}.png"
    fig.savefig(out, dpi=180, bbox_inches="tight")
    plt.close(fig)
    return out


def plot_overview(df: pd.DataFrame, events_df: pd.DataFrame) -> Path:
    codes = list(df.sort_values("code")["code"].drop_duplicates())
    cols = 4
    rows = math.ceil(len(codes) / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(18, rows * 2.8), sharex=False)
    axes_list = list(axes.flatten())

    for ax, code in zip(axes_list, codes):
        g = df[df["code"] == code].sort_values("date")
        ev = events_df[events_df["code"] == code]
        ax.plot(g["date"], g["price"], color="#2f5d8a", linewidth=1.2)

        buy_df = ev[ev["event_type"] == "buy"]
        sell_df = ev[ev["event_type"].isin(["sell", "sell_proxy"])]
        if not buy_df.empty:
            ax.scatter(buy_df["date"], buy_df["price"], marker="^", s=30, color="#1a9850")
        if not sell_df.empty:
            colors = ["#d73027" if t == "sell" else "#fc8d59" for t in sell_df["event_type"]]
            ax.scatter(sell_df["date"], sell_df["price"], marker="v", s=30, color=colors)

        ax.set_title(f"{g['name'].iloc[0]} {code}", fontsize=9)
        ax.grid(alpha=0.15, linestyle="--")
        ax.tick_params(axis="x", labelrotation=45, labelsize=7)
        ax.tick_params(axis="y", labelsize=7)

    for ax in axes_list[len(codes) :]:
        ax.axis("off")

    fig.suptitle("Intraday ETF review: green=buy, red=explicit sell, orange=proxy sell", fontsize=14)
    fig.tight_layout()
    out = OUT_DIR / "all_etfs_overview.png"
    fig.savefig(out, dpi=180, bbox_inches="tight")
    plt.close(fig)
    return out


def summarize_events(events_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    def count_label(frame: pd.DataFrame, label: str) -> int:
        return int((frame["eval_label"] == label).sum())

    per_etf_rows = []
    for code, g in events_df.groupby("code", sort=True):
        name = g["name"].iloc[0]
        buy = g[g["event_type"] == "buy"]
        sell = g[g["event_type"].isin(["sell", "sell_proxy"])]
        per_etf_rows.append(
            {
                "code": code,
                "name": name,
                "buy_count": len(buy),
                "buy_valid": count_label(buy, "有效"),
                "buy_mixed": count_label(buy, "存疑"),
                "buy_fail": count_label(buy, "失效"),
                "buy_pending": count_label(buy, "待观察"),
                "buy_avg_5d": buy["ret_5d"].dropna().mean() if not buy.empty else None,
                "buy_best_5d": buy["max_5d"].dropna().max() if not buy.empty else None,
                "sell_count": len(sell),
                "sell_valid": count_label(sell, "有效"),
                "sell_mixed": count_label(sell, "存疑"),
                "sell_fail": count_label(sell, "失效"),
                "sell_pending": count_label(sell, "待观察"),
                "sell_avg_5d": sell["ret_5d"].dropna().mean() if not sell.empty else None,
                "sell_best_avoid": (-sell["min_5d"].dropna()).max() if not sell.empty and not sell["min_5d"].dropna().empty else None,
            }
        )

    per_etf_df = pd.DataFrame(per_etf_rows).sort_values(["buy_count", "sell_count", "code"], ascending=[False, False, True])

    summary_rows = []
    for event_type, label in [("buy", "buy"), ("sell", "sell")]:
        frame = events_df[events_df["event_type"].isin([event_type] if event_type == "buy" else ["sell", "sell_proxy"])]
        summary_rows.append(
            {
                "event_type": label,
                "count": len(frame),
                "valid": count_label(frame, "有效"),
                "mixed": count_label(frame, "存疑"),
                "fail": count_label(frame, "失效"),
                "pending": count_label(frame, "待观察"),
                "avg_1d": frame["ret_1d"].dropna().mean(),
                "avg_3d": frame["ret_3d"].dropna().mean(),
                "avg_5d": frame["ret_5d"].dropna().mean(),
            }
        )
    summary_df = pd.DataFrame(summary_rows)

    return summary_df, per_etf_df


def format_pct_column(frame: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    out = frame.copy()
    for col in cols:
        if col in out.columns:
            out[col] = out[col].apply(pct_text)
    return out


def write_report(df: pd.DataFrame, events_df: pd.DataFrame, summary_df: pd.DataFrame, per_etf_df: pd.DataFrame) -> Path:
    start = df["date"].min().strftime("%Y-%m-%d")
    end = df["date"].max().strftime("%Y-%m-%d")
    total_logs = int(df["date"].nunique())
    total_etfs = int(df["code"].nunique())

    buy_events = events_df[events_df["event_type"] == "buy"].copy()
    sell_events = events_df[events_df["event_type"].isin(["sell", "sell_proxy"])].copy()

    top_buy = buy_events.sort_values("ret_5d", ascending=False).head(8)
    weak_buy = buy_events.sort_values("ret_5d", ascending=True).head(8)
    best_sell = sell_events.sort_values("ret_5d", ascending=True).head(8)
    weak_sell = sell_events.sort_values("ret_5d", ascending=False).head(8)

    lines: list[str] = []
    lines.append("# Intraday signal review")
    lines.append("")
    lines.append(f"- Coverage: `{start}` to `{end}`, `{total_logs}` trading snapshots, `{total_etfs}` ETFs")
    lines.append("- Buy event: first day entering `★ 买入信号` or `◆ 趋势持有`")
    lines.append("- Sell event: explicit `🔴 止损`; if absent after a buy, first day falling into `✗ 趋势偏弱` is marked as proxy sell")
    lines.append("- Evaluation rule:")
    lines.append("  - Buy `有效`: 5-day return > 0; `存疑`: 5-day return <= 0 but max gain within 5 days > 0; `失效`: max gain within 5 days <= 0")
    lines.append("  - Sell `有效`: 5-day return < 0; `存疑`: 5-day return >= 0 but min return within 5 days < 0; `失效`: min return within 5 days >= 0")
    lines.append("")
    lines.append("## Overall")
    lines.append("")
    lines.append(format_pct_column(summary_df, ["avg_1d", "avg_3d", "avg_5d"]).to_markdown(index=False))
    lines.append("")
    lines.append("## Per ETF")
    lines.append("")
    lines.append(
        format_pct_column(
            per_etf_df[
                [
                    "code",
                    "name",
                    "buy_count",
                    "buy_valid",
                    "buy_mixed",
                    "buy_fail",
                    "buy_pending",
                    "buy_avg_5d",
                    "sell_count",
                    "sell_valid",
                    "sell_mixed",
                    "sell_fail",
                    "sell_pending",
                    "sell_avg_5d",
                ]
            ],
            ["buy_avg_5d", "sell_avg_5d"],
        ).to_markdown(index=False)
    )
    lines.append("")

    def append_table(title: str, frame: pd.DataFrame, cols: list[str]) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if frame.empty:
            lines.append("_None_")
        else:
            lines.append(format_pct_column(frame[cols], [c for c in cols if c.startswith("ret_") or c.endswith("_5d") or c in {"max_5d", "min_5d"}]).to_markdown(index=False))
        lines.append("")

    append_table("Best buy signals", top_buy, ["date", "code", "name", "status", "price", "eval_label", "ret_1d", "ret_3d", "ret_5d", "max_5d", "min_5d"])
    append_table("Weak buy signals", weak_buy, ["date", "code", "name", "status", "price", "eval_label", "ret_1d", "ret_3d", "ret_5d", "max_5d", "min_5d"])
    append_table("Best sell signals", best_sell, ["date", "code", "name", "event_type", "price", "eval_label", "ret_1d", "ret_3d", "ret_5d", "min_5d"])
    append_table("Weak sell signals", weak_sell, ["date", "code", "name", "event_type", "price", "eval_label", "ret_1d", "ret_3d", "ret_5d", "min_5d"])

    out = OUT_DIR / "report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def configure_matplotlib() -> None:
    plt.rcParams["font.sans-serif"] = [
        "PingFang SC",
        "Heiti TC",
        "Arial Unicode MS",
        "Noto Sans CJK SC",
        "SimHei",
        "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    configure_matplotlib()

    df, explicit_sell_df = build_timeseries()
    events_df = build_events(df, explicit_sell_df)
    summary_df, per_etf_df = summarize_events(events_df)

    df.to_csv(OUT_DIR / "daily_status.csv", index=False, encoding="utf-8-sig")
    events_df.to_csv(OUT_DIR / "events.csv", index=False, encoding="utf-8-sig")
    summary_df.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    per_etf_df.to_csv(OUT_DIR / "per_etf_summary.csv", index=False, encoding="utf-8-sig")

    for code, g in df.groupby("code", sort=True):
        plot_one(code, g.sort_values("date"), events_df[events_df["code"] == code].copy())
    plot_overview(df, events_df)
    report_path = write_report(df, events_df, summary_df, per_etf_df)

    print(f"wrote {OUT_DIR}")
    print(f"report: {report_path}")
    print(f"events: {len(events_df)}")
    print(summary_df.to_string(index=False))


def main_merged() -> None:
    MERGED_OUT_DIR.mkdir(parents=True, exist_ok=True)
    configure_matplotlib()

    df, explicit_sell_df = merge_all_logs()
    events_df = build_events(df, explicit_sell_df)
    summary_df, per_etf_df = summarize_events(events_df)

    df.to_csv(MERGED_OUT_DIR / "daily_status.csv", index=False, encoding="utf-8-sig")
    events_df.to_csv(MERGED_OUT_DIR / "events.csv", index=False, encoding="utf-8-sig")
    summary_df.to_csv(MERGED_OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    per_etf_df.to_csv(MERGED_OUT_DIR / "per_etf_summary.csv", index=False, encoding="utf-8-sig")

    global OUT_DIR
    old_out_dir = OUT_DIR
    OUT_DIR = MERGED_OUT_DIR
    for code, g in df.groupby("code", sort=True):
        plot_one(code, g.sort_values("date"), events_df[events_df["code"] == code].copy())
    plot_overview(df, events_df)
    report_path = write_report(df, events_df, summary_df, per_etf_df)
    OUT_DIR = old_out_dir

    print(f"wrote {MERGED_OUT_DIR}")
    print(f"report: {report_path}")
    print(f"events: {len(events_df)}")
    print(summary_df.to_string(index=False))


if __name__ == "__main__":
    if "--merged" in sys.argv:
        main_merged()
    else:
        main()
