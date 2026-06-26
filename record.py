"""
Argus - 交易记录管理

记录买入/卖出操作，自动查询交易时的信号状态。

用法:
  .venv/bin/python record.py --buy 004432 --date 2026-05-14 --time 14:30 --amount 2000
  .venv/bin/python record.py --sell 004432 --date 2026-05-18 --time 14:30 --amount 1000
  .venv/bin/python record.py --list
"""

import argparse
import json
import re
from pathlib import Path

from scan import OTC_FUND, FUND_TO_ETF, ETF_NAME, STATUS_ORDER, analyze

LOG_DIR = Path(__file__).parent / "logs"

RECORDS_PATH = Path(__file__).parent / "logs" / "trade_records.json"
DCA_RECORDS_PATH = Path(__file__).parent / "logs" / "dca_records.json"
_OLD_PATH = Path(__file__).parent / "logs" / "buy_records.json"


def _get_amt_nav(r: dict) -> tuple:
    """从记录中取金额和净值，兼容新旧字段名，正确处理 0 值"""
    amt = r.get("金额") if r.get("金额") is not None else r.get("买入金额")
    nav = r.get("净值") if r.get("净值") is not None else r.get("买入净值")
    return amt, nav


_FIELD_RENAMES = {"买入金额": "金额", "买入净值": "净值", "买入时间": "时间"}


def _migrate_fields(records: list) -> list:
    """统一旧字段名到新字段名"""
    for r in records:
        r.setdefault("类型", "买入")
        for old, new in _FIELD_RENAMES.items():
            if old in r and new not in r:
                r[new] = r.pop(old)
    return records


def _backfill_nav(records: list) -> bool:
    """回填缺失净值和金额的卖出记录（有份额但缺净值，且日期已过）"""
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    changed = False
    for r in records:
        if r.get("类型") != "卖出":
            continue
        if not r.get("份额") or r.get("份额", 0) <= 0:
            continue
        if r.get("净值") and r.get("金额"):
            continue
        trade_date = (r.get("时间") or "")[:10]
        if not trade_date or trade_date >= today:
            continue
        fund_code = r.get("基金代码", r.get("ETF代码"))
        nav = _query_nav(fund_code, trade_date)
        if nav and nav > 0:
            r["净值"] = nav
            r["金额"] = round(r["份额"] * nav, 2)
            changed = True
            print(f"  📝 自动回填: {r.get('ETF名称')} {trade_date} 净值={nav} 金额={r['金额']:.0f}元")
    return changed


def _load_records(path: Path = RECORDS_PATH) -> list:
    # 自动迁移旧文件(仅正常交易文件)
    if path == RECORDS_PATH and not RECORDS_PATH.exists() and _OLD_PATH.exists():
        records = json.loads(_OLD_PATH.read_text(encoding="utf-8"))
        records = _migrate_fields(records)
        _save_records(records)
        _OLD_PATH.rename(_OLD_PATH.with_suffix(".json.bak"))
        print(f"  📦 已迁移 buy_records.json → trade_records.json ({len(records)} 条)")
    if path.exists():
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print(f"  ⚠ 交易记录文件损坏，返回空列表")
            return []
        if records and "买入金额" in records[0]:
            records = _migrate_fields(records)
            _save_records(records, path)
        # 自动回填缺失的卖出净值和金额
        if _backfill_nav(records):
            _save_records(records, path)
        return records
    return []


def _save_records(records: list, path: Path = RECORDS_PATH):
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _lookup_from_log(etf_code: str, date: str) -> dict | None:
    """从扫描日志中提取某只ETF当天的信号（比收盘数据回算更准确）"""
    mmdd = date[5:].replace("-", "")
    # 优先盘中日志，再看盘后日志
    for suffix in ["_intraday", ""]:
        path = LOG_DIR / f"scan_{mmdd}{suffix}.log"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if not line.strip().startswith(etf_code):
                continue
            # 提取状态
            status = None
            for s in STATUS_ORDER:
                if s in line:
                    status = s
                    break
            if not status:
                continue
            # 提取关键指标
            parts = line.split()
            result = {"状态": status, "来源": f"日志({path.name})"}
            # 现价是第3个字段(代码、名称之后)
            for p in parts:
                try:
                    price = float(p)
                    if 0.1 < price < 100:
                        result.setdefault("现价", price)
                        break
                except ValueError:
                    continue
            # 距MA50
            m = re.search(r'([+-][\d.]+%)', line)
            if m:
                result["距MA50"] = m.group(1)
            # 量比
            for p in parts:
                try:
                    v = float(p)
                    if 0.1 <= v <= 10 and v != result.get("现价"):
                        result["量比"] = v
                except ValueError:
                    continue
            # MA5拐头
            if "↑" in line:
                result["MA5拐头"] = "↑"
            elif "↓" in line:
                result["MA5拐头"] = "↓"
            # 信号评估
            m = re.search(r'(强|中|弱)\|([^\s]+)', line)
            if m:
                result["信号评估"] = f"{m.group(1)}|{m.group(2)}"
            return result
    return None


def _resolve_fund(fund_code: str):
    """解析基金代码，返回 (etf_code, etf_name, fund_label) 或 None"""
    etf_code = FUND_TO_ETF.get(fund_code)
    if not etf_code:
        if fund_code in ETF_NAME:
            etf_code = fund_code
        else:
            print(f"  ❌ 未找到基金代码 {fund_code} 对应的ETF")
            return None
    etf_name = ETF_NAME.get(etf_code, etf_code)
    if fund_code != etf_code:
        try:
            import akshare as ak
            info = ak.fund_individual_basic_info_xq(symbol=fund_code)
            fname = info[info["item"] == "基金名称"]["value"].values[0]
            fund_label = f"{fund_code} {fname}"
        except Exception:
            otc = OTC_FUND.get(etf_code)
            fund_label = f"{otc[0]} {otc[1]}" if otc else fund_code
    else:
        fund_label = f"{etf_code}(ETF直投)"
    return etf_code, etf_name, fund_label


def _query_signal(etf_code: str, etf_name: str, date: str) -> dict:
    """查询某ETF在指定日期的信号"""
    log_result = _lookup_from_log(etf_code, date)
    if log_result:
        print(f"  从 {log_result.pop('来源')} 提取 {etf_name} 信号")
        return log_result
    print(f"  无当日日志，用收盘数据回算 {etf_name} 在 {date} 的信号...")
    return analyze(etf_code, etf_name, as_of_date=date)


def _query_nav(fund_code: str, date: str) -> float | None:
    """查询基金在指定日期的净值"""
    try:
        import akshare as ak
        nav_df = ak.fund_open_fund_info_em(symbol=fund_code, indicator="单位净值走势")
        nav_row = nav_df[nav_df["净值日期"].astype(str) == date]
        if not nav_row.empty:
            return float(nav_row["单位净值"].values[0])
    except Exception:
        pass
    return None


def _record_trade(trade_type: str, fund_code: str, date: str, time_str: str, amount: float = None, shares: float = None, dca: bool = False):
    """记录一笔买入或卖出操作。dca=True 时写入独立的定投记录文件。"""
    resolved = _resolve_fund(fund_code)
    if not resolved:
        return
    etf_code, etf_name, fund_label = resolved

    path = DCA_RECORDS_PATH if dca else RECORDS_PATH
    records = _load_records(path)

    # 卖出时检查是否有持仓
    if trade_type in ("卖出", "清仓"):
        has_holding = any(
            r.get("基金代码", r.get("ETF代码")) == fund_code
            or r.get("ETF代码") == etf_code
            for r in records if r.get("类型", "买入") == "买入"
        )
        if not has_holding:
            print(f"  ⚠️  未找到 {fund_code} 的买入记录，仍然记录{trade_type}")

    result = _query_signal(etf_code, etf_name, date)
    # 清仓不需要金额和净值
    if trade_type == "清仓":
        nav = None
        amount = None
    else:
        nav = _query_nav(fund_code, date) if fund_code != etf_code else None

    record = {
        "类型": trade_type,
        "时间": f"{date} {time_str}",
        "联接基金": fund_label,
        "基金代码": fund_code,
        "ETF代码": etf_code,
        "ETF名称": etf_name,
        "当时信号": result.get("状态", "未知"),
        "距MA50": result.get("距MA50"),
        "量比": result.get("量比"),
        "MA5拐头": result.get("MA5拐头"),
    }
    if trade_type != "清仓":
        record["金额"] = amount
        record["净值"] = nav
        if shares is not None:
            record["份额"] = shares
    if result.get("信号评估"):
        record["信号评估"] = result["信号评估"]

    records.append(record)
    _save_records(records, path)

    label = {"买入": "买入", "卖出": "卖出", "清仓": "清仓"}.get(trade_type, trade_type)
    kind = "定投" if dca else "交易"
    print(f"\n  📝 {kind}·{label}记录已保存")
    if trade_type != "清仓" and nav is None:
        print(f"  ⚠️  净值暂未获取到，后续请回填（影响持仓监控和盈亏计算）")
    print(f"  {'─'*40}")
    print(f"  联接基金: {fund_label}")
    print(f"  对应ETF:  {etf_code} {etf_name}")
    print(f"  {label}时间: {date} {time_str}")
    if trade_type != "清仓":
        print(f"  {label}金额: {f'{amount:.0f}元' if amount else '-'}")
        if shares is not None:
            print(f"  {label}份额: {shares:.2f}份")
        print(f"  {label}净值: {nav or '-'}")
    print(f"  当时信号: {result.get('状态', '-')}")
    print(f"  距MA50:   {result.get('距MA50', '-')}")
    print(f"  量比:     {result.get('量比', '-')}")
    print(f"  MA5拐头:  {result.get('MA5拐头', '-')}")
    if result.get("信号评估"):
        print(f"  信号评估: {result['信号评估']}")


def record_buy(fund_code: str, date: str, time_str: str, amount: float = None, dca: bool = False):
    """记录一笔买入操作"""
    _record_trade("买入", fund_code, date, time_str, amount, dca=dca)


def record_sell(fund_code: str, date: str, time_str: str, amount: float = None, shares: float = None, dca: bool = False):
    """记录一笔卖出操作"""
    _record_trade("卖出", fund_code, date, time_str, amount, shares=shares, dca=dca)


def record_clear(fund_code: str, date: str, time_str: str, dca: bool = False):
    """记录清仓操作（全部卖出，无需金额）"""
    _record_trade("清仓", fund_code, date, time_str, dca=dca)


def show_records(dca: bool = False):
    """展示所有交易记录，按基金汇总持仓，计算持仓盈亏和已实现盈亏"""
    records = _load_records(DCA_RECORDS_PATH if dca else RECORDS_PATH)
    if not records:
        print("  暂无定投记录" if dca else "  暂无交易记录")
        return

    # 查询各基金最新净值
    import akshare as ak
    fund_codes = list({r.get("基金代码", r["ETF代码"]) for r in records})
    current_navs = {}
    for fc in fund_codes:
        try:
            nav_df = ak.fund_open_fund_info_em(symbol=fc, indicator="单位净值走势")
            if not nav_df.empty:
                current_navs[fc] = {
                    "净值": float(nav_df["单位净值"].iloc[-1]),
                    "日期": str(nav_df["净值日期"].iloc[-1]),
                }
        except Exception:
            pass

    # 按基金代码汇总
    from collections import OrderedDict
    holdings = OrderedDict()
    for r in records:
        fc = r.get("基金代码", r["ETF代码"])
        if fc not in holdings:
            holdings[fc] = {
                "records": [],
                "name": r.get("联接基金", r.get("ETF名称", fc)),
                "etf": f"{r['ETF代码']} {r['ETF名称']}",
                "buy_amount": 0,
                "buy_shares": 0,
                "sell_amount": 0,
                "sell_shares": 0,
            }
        holdings[fc]["records"].append(r)
        typ = r.get("类型", "买入")
        if typ == "清仓":
            # 清仓：全部归零，后续买入从零开始统计
            holdings[fc]["buy_amount"] = 0
            holdings[fc]["buy_shares"] = 0
            holdings[fc]["sell_amount"] = 0
            holdings[fc]["sell_shares"] = 0
            holdings[fc].setdefault("cleared", True)
            continue
        amt, nav = _get_amt_nav(r)
        # 优先使用直接记录的份额（卖出按份额操作时）
        direct_shares = r.get("份额")
        if direct_shares is not None and direct_shares > 0:
            shares = direct_shares
        elif amt and amt > 0:
            if nav and nav > 0:
                shares = amt / nav
            else:
                shares = amt
                holdings[fc].setdefault("nav_missing", True)
        else:
            shares = 0
        if shares > 0:
            if typ == "卖出":
                holdings[fc]["sell_amount"] += amt or 0
                holdings[fc]["sell_shares"] += shares
            else:
                holdings[fc]["buy_amount"] += amt or 0
                holdings[fc]["buy_shares"] += shares

    # 统计
    n_buys = sum(1 for r in records if r.get("类型", "买入") == "买入")
    n_sells = sum(1 for r in records if r.get("类型") in ("卖出", "清仓"))
    print(f"\n{'='*60}")
    title = "定投汇总" if dca else "持仓汇总"
    print(f"  📊 {title} ({len(holdings)} 只基金, {n_buys} 笔买入, {n_sells} 笔卖出)")
    print(f"{'='*60}")

    grand_total_cost = 0
    grand_total_value = 0
    grand_realized = 0

    for fc, h in holdings.items():
        cur = current_navs.get(fc)
        buy_amt = h["buy_amount"]
        buy_shares = h["buy_shares"]
        sell_amt = h["sell_amount"]
        sell_shares = h["sell_shares"]
        net_shares = buy_shares - sell_shares
        # 加权买入成本
        avg_cost = buy_amt / buy_shares if buy_shares > 0 else 0

        print(f"\n  ▸ {h['name']}  ({h['etf']})")

        if buy_amt > 0 and buy_shares > 0:
            # 已实现盈亏: 卖出收回金额 - 卖出份额 * 加权成本
            realized = sell_amt - sell_shares * avg_cost if sell_shares > 0 else 0
            grand_realized += realized

            net_cost = net_shares * avg_cost  # 剩余持仓成本
            n_buy = sum(1 for r in h["records"] if r.get("类型", "买入") == "买入")
            n_sell = sum(1 for r in h["records"] if r.get("类型") in ("卖出", "清仓"))
            txn_label = f"{n_buy}买"
            if n_sell > 0:
                txn_label += f"+{n_sell}卖"

            print(f"    投入: {buy_amt:.0f}元 ({txn_label})  加权成本: {avg_cost:.4f}")

            if sell_amt > 0:
                r_sign = "+" if realized >= 0 else ""
                print(f"    已卖出: {sell_amt:.0f}元  已实现盈亏: {r_sign}{realized:.0f}元")

            if net_shares > 1:  # 还有持仓(忽略<1份的舍入误差)
                if cur:
                    current_value = net_shares * cur["净值"]
                    unrealized = current_value - net_cost
                    unrealized_pct = unrealized / net_cost * 100 if net_cost > 0 else 0
                    grand_total_cost += net_cost
                    grand_total_value += current_value
                    sign = "+" if unrealized >= 0 else ""
                    print(f"    最新净值: {cur['净值']}({cur['日期']})")
                    print(f"    剩余市值: {current_value:.0f}元  浮动盈亏: {sign}{unrealized:.0f}元 ({unrealized_pct:+.2f}%)")
                else:
                    print(f"    最新净值: 查询失败")
            elif sell_shares > 0:
                print(f"    ✅ 已清仓")
        else:
            n = len(h["records"])
            print(f"    共 {n} 笔 (无金额记录，无法计算)")
            if cur:
                print(f"    最新净值: {cur['净值']}({cur['日期']})")

        # 明细
        for r in h["records"]:
            typ = r.get("类型", "买入")
            date = r.get("时间") or r.get("买入时间", "")
            amt, nav = _get_amt_nav(r)
            signal = r.get("当时信号", "-")
            tag = "🟢买" if typ == "买入" else "🔴清" if typ == "清仓" else "🔴卖"
            amt_s = f"{amt:.0f}元" if amt else "-"
            nav_s = f"{nav}" if nav else "-"
            print(f"      {tag} {date}  {amt_s}  净值{nav_s}  {signal}")

    # 总计
    print(f"\n{'─'*60}")
    if grand_total_cost > 0:
        grand_unrealized = grand_total_value - grand_total_cost
        grand_pct = grand_unrealized / grand_total_cost * 100
        sign = "+" if grand_unrealized >= 0 else ""
        print(f"  💰 持仓: 成本 {grand_total_cost:.0f}元 → 市值 {grand_total_value:.0f}元  浮动 {sign}{grand_unrealized:.0f}元 ({grand_pct:+.2f}%)")
    if grand_realized != 0:
        r_sign = "+" if grand_realized >= 0 else ""
        print(f"  💰 已实现: {r_sign}{grand_realized:.0f}元")
    if grand_total_cost > 0 or grand_realized != 0:
        total_pnl = (grand_total_value - grand_total_cost) + grand_realized
        t_sign = "+" if total_pnl >= 0 else ""
        print(f"  💰 综合盈亏: {t_sign}{total_pnl:.0f}元")
    print(f"{'─'*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Argus 交易记录管理")
    parser.add_argument("--buy", metavar="CODE", help="记录买入(联接基金或ETF代码)")
    parser.add_argument("--sell", metavar="CODE", help="记录卖出(联接基金或ETF代码)")
    parser.add_argument("--clear", metavar="CODE", help="记录清仓(全部卖出，无需金额)")
    parser.add_argument("--date", help="交易日期(YYYY-MM-DD)")
    parser.add_argument("--time", dest="trade_time", default="15:00", help="交易时间(HH:MM，默认15:00)")
    parser.add_argument("--amount", type=float, help="交易金额(元)")
    parser.add_argument("--shares", type=float, help="交易份额(卖出时按份额记录)")
    parser.add_argument("--list", dest="show", action="store_true", help="查看交易记录")
    parser.add_argument("--dca", action="store_true", help="操作定投记录(独立于正常交易，写/读 dca_records.json)")
    args = parser.parse_args()

    if args.show:
        show_records(dca=args.dca)
    elif args.buy:
        if not args.date:
            print("  ❌ 请指定买入日期: --date YYYY-MM-DD")
        else:
            record_buy(args.buy, args.date, args.trade_time, args.amount, dca=args.dca)
    elif args.sell:
        if not args.date:
            print("  ❌ 请指定卖出日期: --date YYYY-MM-DD")
        else:
            record_sell(args.sell, args.date, args.trade_time, args.amount, shares=args.shares, dca=args.dca)
    elif args.clear:
        if not args.date:
            print("  ❌ 请指定清仓日期: --date YYYY-MM-DD")
        else:
            record_clear(args.clear, args.date, args.trade_time, dca=args.dca)
    else:
        parser.print_help()
