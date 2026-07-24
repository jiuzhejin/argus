#!/usr/bin/env python3
"""止损规则 A/B 回测实验（独立脚本，不改动 scan.py）。

目的：验证"止损是不是太容易触发 / 刚买就割 / 联接C 来回折腾吃赎回费"。

设计原则
--------
- 信号引擎完全复用 backtest_strategy.analyze_hist —— 买入判定、状态分类三方一致，
  只有【平仓逻辑】不同，保证 A/B 干净对照。
- 日期驱动改为按 .cache/{code}.csv 的真实交易日逐日推进（默认最近 2 年），
  不再依赖残缺的 signal_review_merged/daily_status.csv（那个卡在 2026-06-26）。
- 三种平仓模型跑同一批买入信号：
    naive     : 复现现有 backtest_strategy.py —— 一变"趋势偏弱"就无条件卖。
    faithful  : 对齐实盘 scan.py:1081-1099 的 🔴/🟠 三闸门分级，只有确认破位(🔴)才卖。
    variantC  : faithful + C组合(持有保护期 + 成本止损线)。
- 全部计入 C 类联接基金赎回费：持有 < HOLD_FEE_DAYS 个自然日收 REDEEM_FEE。
  这是"来回折腾"的核心摩擦成本，naive/faithful/variantC 用同一口径扣费。

用法
----
    .venv/bin/python tools/stoploss_backtest.py                 # 默认近2年
    .venv/bin/python tools/stoploss_backtest.py --years 1
    .venv/bin/python tools/stoploss_backtest.py --start 2024-07-01
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scan import ETF_POOL, _STATUS_RANK, get_cache_path  # noqa: E402
from backtest_strategy import analyze_hist, _dist_value  # noqa: E402

WEAK_STATUS = "✗ 趋势偏弱"
BUY_WEIGHTS = {"★ 低位确认": 1.0, "◆ 趋势跟随": 0.5, "◇ 转强初期": 0.25}
TP_STATUSES = {"◆ 趋势跟随", "□ 多头排列", "- 趋势完好", "▲ 接近支撑"}

# ---- 成本 / C组合 参数（可调）----
REDEEM_FEE = 0.015        # C类联接基金持有<HOLD_FEE_DAYS天的惩罚性赎回费
HOLD_FEE_DAYS = 7         # 自然日；<7天赎回收 1.5%，>=7天 C类通常免
PROTECT_DAYS = 5          # C组合：买入后 N 个自然日内不触发硬止损(除非放量破位)
COST_STOP_PCT = -4.0      # C组合：相对买入价亏损超过该阈值(%)才算成本止损


def trading_dates(min_rows: int = 140) -> list[str]:
    """用 588000(数据最全的宽基)缓存的交易日做全局时间轴。"""
    df = pd.read_csv(get_cache_path("588000"))
    return sorted(df["date"].astype(str).str[:10].unique().tolist())


_CHG_CACHE: dict[str, dict[str, float]] = {}


def _load_chg_map(symbol: str) -> dict[str, float]:
    """预计算某标的 date->当日涨跌% 映射（收盘 vs 前收），只读一次盘。"""
    if symbol in _CHG_CACHE:
        return _CHG_CACHE[symbol]
    df = pd.read_csv(get_cache_path(symbol)).sort_values("date")
    close = df["close"].astype(float)
    chg = close.pct_change() * 100
    dkey = df["date"].astype(str).str[:10]
    m = {d: round(v, 2) if pd.notna(v) else 0.0 for d, v in zip(dkey, chg)}
    _CHG_CACHE[symbol] = m
    return m


def today_change(symbol: str, as_of_date: str) -> float:
    """as_of_date 当日涨跌%（收盘 vs 前收），对齐 scan.py 的 today_chg。"""
    return _load_chg_map(symbol).get(as_of_date, 0.0)


# ---------- 平仓判定：三种模型 ----------

def _is_take_profit(row: dict) -> str | None:
    """止盈判定，三个模型共用（对齐 scan.py take_profit_soft/hard）。"""
    status = row.get("状态", "")
    if status not in TP_STATUSES:
        return None
    price = float(row.get("现价", 0) or 0)
    ma5 = float(row.get("MA5", 0) or 0)
    ma10 = float(row.get("MA10", 0) or 0)
    dist = _dist_value(row.get("距MA50", "0%"))
    if dist >= 10 and ma10 > 0 and price < ma10 and status in {"□ 多头排列", "- 趋势完好", "▲ 接近支撑"}:
        return "take_profit_hard"
    if dist >= 8 and ma5 > 0 and price < ma5 and row.get("MA5拐头") == "↓":
        return "take_profit_soft"
    return None


def exit_naive(row: dict, pos: dict, hold_days: int, today_chg: float) -> str | None:
    """现有 backtest_strategy.py 的模型：转弱即卖。"""
    if row.get("状态") == WEAK_STATUS:
        return "stop_loss"
    return _is_take_profit(row)


def _confirmed_breakdown(row: dict, today_chg: float) -> bool:
    """对齐 scan.py:1088-1092 的 confirmed_breakdown 三闸门。"""
    status = row.get("状态", "")
    dist = _dist_value(row.get("距MA50", "0%"))
    vol = float(row.get("量比", 0) or 0)
    return (
        status not in _STATUS_RANK
        or dist <= -3
        or (dist < 0 and vol >= 1.5 and today_chg < 0)
    )


def exit_faithful(row: dict, pos: dict, hold_days: int, today_chg: float) -> str | None:
    """忠实还原实盘：转弱后只有确认破位(🔴)才卖，🟠观察不卖。"""
    tp = _is_take_profit(row)
    if tp:
        return tp
    if row.get("状态") == WEAK_STATUS:
        if _confirmed_breakdown(row, today_chg):
            return "stop_loss_hard"   # 🔴
        return None                    # 🟠 观察，不卖
    return None


def exit_variant_c(row: dict, pos: dict, hold_days: int, today_chg: float,
                   protect_days: int = PROTECT_DAYS, cost_stop: float = COST_STOP_PCT) -> str | None:
    """C组合 = faithful + 持有保护期 + 成本止损线。参数可注入以便网格扫描。"""
    tp = _is_take_profit(row)
    if tp:
        return tp
    if row.get("状态") != WEAK_STATUS:
        return None

    hard = _confirmed_breakdown(row, today_chg)
    if not hard:
        return None  # 只是浅破/缩量 → 🟠 观察

    # 已确认破位。C组合再加两道人性化闸门：
    price = float(row.get("现价", 0) or 0)
    pnl_pct = (price / pos["买入价"] - 1) * 100 if pos["买入价"] else 0.0
    vol = float(row.get("量比", 0) or 0)
    放量破位 = vol >= 1.5 and today_chg < 0

    # 闸门1：持有保护期内，除非放量破位，否则不硬砍(给方向时间 + 避开赎回费窗口)
    if hold_days < protect_days and not 放量破位:
        return None
    # 闸门2：还没跌破成本止损线，且非放量破位 → 继续观察(位置弱但没亏到该割)
    if pnl_pct > cost_stop and not 放量破位:
        return None
    return "stop_loss_C"


EXIT_MODELS = {
    "naive": exit_naive,
    "faithful": exit_faithful,
    "variantC": exit_variant_c,
}


# ---------- 回测主循环 ----------

def run_model(name: str, history: pd.DataFrame, dates: list[str], exit_fn=None) -> pd.DataFrame:
    if exit_fn is None:
        exit_fn = EXIT_MODELS[name]
    trades: list[dict] = []
    open_pos: dict[str, dict] = {}

    for date in dates:
        day = history[history["日期"] == date]
        row_map = {r["代码"]: r for _, r in day.iterrows()}

        # 先处理卖出
        for code in list(open_pos.keys()):
            row = row_map.get(code)
            if row is None:
                continue
            pos = open_pos[code]
            hold_days = (pd.Timestamp(date) - pd.Timestamp(pos["买入日期"])).days
            tchg = today_change(code, date)
            reason = exit_fn(row, pos, hold_days, tchg)
            if reason:
                sell = float(row.get("现价"))
                gross = sell / pos["买入价"] - 1
                fee = REDEEM_FEE if hold_days < HOLD_FEE_DAYS else 0.0
                trades.append({
                    "代码": code, "名称": pos["名称"],
                    "买入日期": pos["买入日期"], "买入价": pos["买入价"],
                    "买入状态": pos["买入状态"], "仓位权重": pos["仓位权重"],
                    "卖出日期": date, "卖出价": sell, "卖出原因": reason,
                    "卖出状态": row.get("状态"), "持有天数": hold_days,
                    "毛收益率": gross, "赎回费": fee, "净收益率": gross - fee,
                    "已平仓": True,
                })
                del open_pos[code]

        # 再开新仓（信号刚触发，非连续）
        for _, row in day.iterrows():
            code = row["代码"]
            sig = row.get("状态")
            if sig not in BUY_WEIGHTS or code in open_pos:
                continue
            prev = history[(history["代码"] == code) & (history["日期"] < date)].tail(1)
            if not prev.empty and prev.iloc[0]["状态"] == sig:
                continue
            open_pos[code] = {
                "名称": row["名称"], "买入日期": date, "买入价": float(row["现价"]),
                "买入状态": sig, "仓位权重": BUY_WEIGHTS[sig],
            }

    # 期末盯市
    last = dates[-1]
    last_rows = history[history["日期"] == last].set_index("代码")
    for code, pos in open_pos.items():
        if code not in last_rows.index:
            continue
        row = last_rows.loc[code]
        sell = float(row["现价"])
        gross = sell / pos["买入价"] - 1
        hold_days = (pd.Timestamp(last) - pd.Timestamp(pos["买入日期"])).days
        trades.append({
            "代码": code, "名称": pos["名称"],
            "买入日期": pos["买入日期"], "买入价": pos["买入价"],
            "买入状态": pos["买入状态"], "仓位权重": pos["仓位权重"],
            "卖出日期": last, "卖出价": sell, "卖出原因": "mark_to_market",
            "卖出状态": row["状态"], "持有天数": hold_days,
            "毛收益率": gross, "赎回费": 0.0, "净收益率": gross,
            "已平仓": False,
        })
    return pd.DataFrame(trades)


def summarize(name: str, tr: pd.DataFrame) -> dict:
    if tr.empty:
        return {"模型": name, "交易数": 0}
    closed = tr[tr["已平仓"]]
    stop_closed = closed[closed["卖出原因"].astype(str).str.startswith("stop")]
    quick = closed[closed["持有天数"] < HOLD_FEE_DAYS]
    return {
        "模型": name,
        "交易数": len(tr),
        "平仓数": len(closed),
        "止损平仓数": len(stop_closed),
        "<7天割肉笔数": len(quick),
        "赎回费总损耗%": round(tr["赎回费"].sum() * 100, 2),
        "平均持有天数": round(closed["持有天数"].mean(), 1) if len(closed) else None,
        "平仓胜率": round((closed["净收益率"] > 0).mean(), 3) if len(closed) else None,
        "平仓净均收益%": round(closed["净收益率"].mean() * 100, 2) if len(closed) else None,
        "全体净均收益%": round(tr["净收益率"].mean() * 100, 2),
        "全体加权净收益%": round((tr["净收益率"] * tr["仓位权重"]).sum() / tr["仓位权重"].sum() * 100, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--start", type=str, default=None)
    ap.add_argument("--grid", action="store_true", help="对C组合参数做网格敏感性扫描")
    args = ap.parse_args()

    all_dates = trading_dates()
    if args.start:
        start = args.start
    else:
        end = pd.Timestamp(all_dates[-1])
        start = (end - pd.Timedelta(days=int(args.years * 365))).strftime("%Y-%m-%d")
    dates = [d for d in all_dates if d >= start]
    print(f"回测区间: {dates[0]} -> {dates[-1]}  ({len(dates)} 个交易日)")

    # 预计算每日每标的信号（所有模型/参数组共享，只算一次）
    print("计算历史信号中 ...")
    rows = []
    for date in dates:
        for code, nm in ETF_POOL:
            r = analyze_hist(code, nm, as_of_date=date)
            r["日期"] = date
            rows.append(r)
    history = pd.DataFrame(rows).sort_values(["日期", "代码"]).reset_index(drop=True)

    out_dir = ROOT / "logs" / "stoploss_backtest"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.grid:
        import functools
        protect_grid = [0, 3, 5, 7, 10]
        cost_grid = [-3.0, -4.0, -5.0, -6.0]
        # 基准锚点：faithful 相当于 protect=0 且 cost=+inf（不加成本闸门）
        base = summarize("faithful", run_model("faithful", history, dates))
        grid_rows = []
        for pd_ in protect_grid:
            for cs in cost_grid:
                fn = functools.partial(exit_variant_c, protect_days=pd_, cost_stop=cs)
                tr = run_model("variantC", history, dates, exit_fn=fn)
                s = summarize(f"P{pd_}/C{cs}", tr)
                s["保护期"] = pd_
                s["成本线"] = cs
                grid_rows.append(s)
        grid = pd.DataFrame(grid_rows)
        grid.to_csv(out_dir / "grid.csv", index=False, encoding="utf-8-sig")
        print("\n" + "=" * 78)
        print(f"C组合参数网格 (对照 faithful 基线: 加权净收益 {base['全体加权净收益%']}%, "
              f"<7天割肉 {base['<7天割肉笔数']}笔, 赎回费 {base['赎回费总损耗%']}%)")
        print("=" * 78)
        show = grid[["模型", "交易数", "<7天割肉笔数", "赎回费总损耗%",
                     "平均持有天数", "平仓胜率", "全体加权净收益%"]]
        print(show.to_string(index=False))
        best = grid.loc[grid["全体加权净收益%"].idxmax()]
        print(f"\n最优(按加权净收益): 保护期{int(best['保护期'])}天 / 成本线{best['成本线']}% "
              f"→ 加权净收益 {best['全体加权净收益%']}%, 胜率 {best['平仓胜率']}, "
              f"<7天割肉 {int(best['<7天割肉笔数'])}笔")
        print(f"\n网格明细已写入: {out_dir / 'grid.csv'}")
        return

    summaries = []
    for name in ("naive", "faithful", "variantC"):
        tr = run_model(name, history, dates)
        tr.to_csv(out_dir / f"trades_{name}.csv", index=False, encoding="utf-8-sig")
        summaries.append(summarize(name, tr))

    summ = pd.DataFrame(summaries)
    summ.to_csv(out_dir / "summary.csv", index=False, encoding="utf-8-sig")
    print("\n" + "=" * 70)
    print(f"参数: 赎回费{REDEEM_FEE:.1%}(<{HOLD_FEE_DAYS}天) | 保护期{PROTECT_DAYS}天 | 成本止损{COST_STOP_PCT}%")
    print("=" * 70)
    print(summ.to_string(index=False))
    print(f"\n明细已写入: {out_dir}")


if __name__ == "__main__":
    main()
