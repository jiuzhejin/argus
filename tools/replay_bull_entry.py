#!/usr/bin/env python3
"""回放 □ 多头排列 入场规则（独立实现，避开 scan.fetch_hist 的实时增补）。

复用 .cache/{code}.csv，按每日 truncate 计算 MA/量比等指标，
判定入场条件，并统计 D+1..D+5 收益。
"""
import sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache"
sys.path.insert(0, str(ROOT))
from scan import ETF_POOL  # (code, name) list


def compute_status(df):
    """在 df 的最后一行时点上跑与 scan.py 一致的分类。返回 dict."""
    if len(df) < 140:
        return None
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    ma50 = close.rolling(50).mean()
    ma100 = close.rolling(100).mean()
    vol_avg20 = volume.shift(1).rolling(20).mean()

    c = close.iloc[-1]
    m5, m10, m20 = ma5.iloc[-1], ma10.iloc[-1], ma20.iloc[-1]
    m50, m100 = ma50.iloc[-1], ma100.iloc[-1]
    prev_close = close.iloc[-2]
    prev_ma50 = ma50.iloc[-2]
    dist_prev = (prev_close - prev_ma50) / prev_ma50 * 100 if pd.notna(prev_ma50) and prev_ma50 > 0 else 999
    vol = volume.iloc[-1]
    va20 = vol_avg20.iloc[-1]

    recent_low_5d = close.tail(5).min()
    recent_low_10d = close.tail(10).min()
    support_tested = recent_low_5d <= m50 * 1.015
    support_recent_10d = recent_low_10d <= m50 * 1.02
    ma5_up = ma5.iloc[-1] > ma5.iloc[-2] > ma5.iloc[-3]
    ma20_up = ma20.iloc[-1] > ma20.iloc[-2] > ma20.iloc[-3]
    vol_ratio = float(vol / va20) if pd.notna(va20) and va20 > 0 else 0
    dist_ma50_pct = (c - m50) / m50 * 100

    if abs(c - m5) / m5 > 0.3:
        return None

    above_long = c > m50 and c > m100
    above_mid = c > m50
    bull_align = c > m5 and c > m10 and c > m20 and above_long
    ma50_slope = (ma50.iloc[-1] - ma50.iloc[-10]) / ma50.iloc[-10] * 100

    # 突破提醒
    days_below = 0
    recent_closes = close.iloc[-6:-1]
    recent_ma50 = ma50.iloc[-6:-1]
    if len(recent_closes) >= 5 and len(recent_ma50) >= 5:
        days_below = int((recent_closes < recent_ma50).sum())

    buy_core = (
        c > m5 and c > m10 and above_long
        and 0.8 <= vol_ratio <= 2.5
        and ma5_up and support_tested
        and ma50_slope > 0
    )
    ENTRY_DIST_CAP = 4.5
    trend_ready = m20 > m50 and ma50_slope > 0.1
    executable_breakout = days_below >= 3 and above_long
    strict_buy = (
        buy_core and dist_ma50_pct < ENTRY_DIST_CAP
        and 0.85 <= vol_ratio <= 1.9
        and trend_ready
        and (m5 > m10 > m20 or executable_breakout)
    )
    trend_follow_ready = (
        buy_core and m20 > m50 > m100
        and 0.85 <= vol_ratio <= 1.8
        and ((m20/m50-1)*100 >= 0.5 or dist_ma50_pct <= 2.5)
    )
    early_reversal_follow = (
        above_mid and not above_long and support_tested
        and support_recent_10d and ma5_up and ma20_up
        and c > m5 and c > m10 and c > m20
        and m5 > m10 > m20 and 0.85 <= vol_ratio <= 1.8
        and dist_ma50_pct <= 6.5 and -1.8 < ma50_slope <= 0
        and c >= m100 * 0.995 and days_below >= 3
    )
    early_bull_follow = (
        bull_align and support_recent_10d and ma5_up and ma20_up
        and m5 > m10 > m20 and 0.85 <= vol_ratio <= 1.6
        and 4.5 <= dist_ma50_pct <= 6.5 and c <= m100 * 1.03
        and -1.2 < ma50_slope <= 0
    )
    learning_signal = (
        not above_long and c > m5 and c > m10 and c > m20
        and m5 > m10 > m20 and ma5_up and ma20_up
        and 0.8 <= vol_ratio <= 1.8
        and dist_ma50_pct <= 8.0 and c >= m50 * 0.94
    )
    near_support = above_long and dist_ma50_pct <= 5.0 and c < m20

    bull_entry_ok = (
        bull_align
        and dist_ma50_pct <= 3.0
        and 0.85 <= vol_ratio <= 1.9
        and ma5_up
        and support_tested
    )

    if strict_buy: status = "★ 低位确认"
    elif trend_follow_ready or early_reversal_follow or early_bull_follow: status = "◆ 趋势跟随"
    elif learning_signal: status = "◇ 转强初期"
    elif near_support: status = "▲ 接近支撑"
    elif bull_align: status = "□ 多头排列"
    elif above_long: status = "- 趋势完好"
    else: status = "✗ 趋势偏弱"

    return {
        "close": c, "dist": dist_ma50_pct, "vol_ratio": vol_ratio,
        "ma5_up": ma5_up, "support_tested": support_tested,
        "bull_entry": bull_entry_ok, "status": status,
        "ma50_slope": ma50_slope,
        "dist_prev": dist_prev,
    }


def replay():
    lookback = 45
    events = []
    for code, name in ETF_POOL:
        csv = CACHE / f"{code}.csv"
        if not csv.exists(): continue
        df = pd.read_csv(csv)
        df = df.sort_values("date").reset_index(drop=True)
        if len(df) < 150: continue
        dates = df["date"].astype(str).str[:10].tolist()
        # 只对最后 lookback 天做入场评估，且给 D+5 留缓冲
        n = len(df)
        start = max(140, n - lookback - 6)
        prev_status = None
        for i in range(start, n - 5):
            sub = df.iloc[:i+1]
            r = compute_status(sub)
            if r is None:
                prev_status = None
                continue
            cur = r["status"]
            if prev_status is not None and prev_status != "□ 多头排列" and cur == "□ 多头排列":
                base = r["close"]
                fut = []
                for j in range(1, 6):
                    if i+j < n:
                        fc = float(df.iloc[i+j]["close"])
                        fut.append((dates[i+j], (fc/base-1)*100))
                events.append({
                    "code": code, "name": name, "date": dates[i],
                    "prev": prev_status, "cur": cur,
                    "dist": r["dist"], "vol": r["vol_ratio"],
                    "slope": r["ma50_slope"],
                    "ma5_up": "↑" if r["ma5_up"] else "↓",
                    "support": "是" if r["support_tested"] else "否",
                    "entry": "是" if r["bull_entry"] else "否",
                    "future": fut,
                })
            prev_status = cur

    events.sort(key=lambda e: (e["date"], e["code"]))

    print(f"{'代码':>6} {'名称':10s} {'转入日':>10} {'前状态':11s} {'距MA50':>7} {'量比':>5} {'斜率':>6} {'MA5':>3} {'回踩':>3} {'入场':>3} | D+1..D+5 (%)  | 结论")
    print("-"*145)
    stat = {"picks_good": 0, "picks_bad": 0, "skip_good": 0, "skip_bad": 0}
    for e in events:
        fut_txt = " ".join([f"{r:+5.1f}" for _, r in e["future"]])
        d3 = e["future"][2][1] if len(e["future"]) >= 3 else None
        verdict = "?" if d3 is None else ("√好" if d3 >= -1.0 else "×差")
        if e["entry"] == "是":
            stat["picks_good" if verdict == "√好" else "picks_bad"] += 1
        else:
            stat["skip_good" if verdict == "√好" else "skip_bad"] += 1
        print(f"{e['code']:>6} {e['name'][:10]:10s} {e['date']:>10} {e['prev'][:11]:11s} {e['dist']:>+6.1f}%  {e['vol']:>5.2f} {e['slope']:>+5.2f} {e['ma5_up']:>3} {e['support']:>3} {e['entry']:>3} | {fut_txt}  | {verdict}")

    total = len(events)
    print()
    print(f"[入场=是] 命中 {stat['picks_good']}（D+3≥-1%）  错买 {stat['picks_bad']}（D+3<-1%）")
    print(f"[入场=否] 漏 {stat['skip_good']}（D+3≥-1%）  躲开 {stat['skip_bad']}（D+3<-1%）")
    picks = stat['picks_good'] + stat['picks_bad']
    if picks:
        print(f"入场准确率 = {stat['picks_good']}/{picks} = {stat['picks_good']/picks:.0%}")
    misses = stat['skip_good']
    if total:
        print(f"漏抓率 = {misses}/{total} = {misses/total:.0%}")


if __name__ == "__main__":
    replay()
