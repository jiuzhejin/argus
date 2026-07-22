"""scan.py 核心纯函数与持仓聚合逻辑的单元测试。

纯 stdlib unittest，不引入 pytest。跑法:
    .venv/bin/python -m unittest test_scan.py -v

覆盖:
    _add_advice   持仓感知加仓判据(5%/10% 分档 + 未持仓空串)
    _parse_dist   距MA50 字符串解析与异常回退
    _load_holdings / _held_codes  净份额聚合、清仓排除、份额回退
    check_holdings  关键分支(接近支撑加仓 / 趋势偏弱止损分档)
    save_cache / is_cache_fresh  缓存只存历史K(剔除当日行)、按日期判新鲜
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pandas as pd

import scan


class TestAddAdvice(unittest.TestCase):
    """_add_advice: 已持仓时按距MA50位置分档，未持仓走原文案(空串)。"""

    def test_not_held_returns_empty(self):
        # 未持仓一律空串，无论位置高低
        self.assertEqual(scan._add_advice(3, False), "")
        self.assertEqual(scan._add_advice(7, False), "")
        self.assertEqual(scan._add_advice(50, False), "")
        self.assertEqual(scan._add_advice(-5, False), "")

    def test_held_low(self):
        # 距MA50 < 5% → 低位可补
        adv = scan._add_advice(3, True)
        self.assertIn("[低位]", adv)
        self.assertIn("可再补", adv)

    def test_held_chase(self):
        # 5% <= 距MA50 < 10% → 追涨慎加
        adv = scan._add_advice(7, True)
        self.assertIn("[追涨]", adv)
        self.assertIn("慎加", adv)

    def test_held_high(self):
        # 距MA50 >= 10% → 高位不加
        adv = scan._add_advice(12, True)
        self.assertIn("[高位博弈]", adv)
        self.assertIn("不建议再加", adv)

    def test_boundary_5_is_chase(self):
        # 恰好 5.0 落入追涨档(< 5 为低位)
        self.assertIn("[追涨]", scan._add_advice(5.0, True))

    def test_boundary_10_is_high(self):
        # 恰好 10.0 落入高位档(< 10 为追涨)
        self.assertIn("[高位博弈]", scan._add_advice(10.0, True))

    def test_held_negative_dist_is_low(self):
        # 已跌破MA50(负值)属最低位，归低位档
        self.assertIn("[低位]", scan._add_advice(-2, True))

    def test_tiers_align_with_check_holdings(self):
        # 分档阈值须与 check_holdings 的 5/10 tier 语义一致
        self.assertNotEqual(scan._add_advice(4.99, True), scan._add_advice(5.01, True))
        self.assertNotEqual(scan._add_advice(9.99, True), scan._add_advice(10.01, True))


class TestParseDist(unittest.TestCase):
    """_parse_dist: 距MA50 字符串 → float，异常回退 0.0。"""

    def test_positive(self):
        self.assertEqual(scan._parse_dist("+0.4%"), 0.4)

    def test_negative(self):
        self.assertEqual(scan._parse_dist("-2.2%"), -2.2)

    def test_zero(self):
        self.assertEqual(scan._parse_dist("+0%"), 0.0)

    def test_no_percent_sign(self):
        self.assertEqual(scan._parse_dist("3.5"), 3.5)

    def test_numeric_input(self):
        # 传入非字符串也应被 str() 兜住
        self.assertEqual(scan._parse_dist(5), 5.0)

    def test_garbage_returns_zero(self):
        self.assertEqual(scan._parse_dist("N/A"), 0.0)
        self.assertEqual(scan._parse_dist(""), 0.0)
        self.assertEqual(scan._parse_dist(None), 0.0)

    def test_return_type_is_float(self):
        self.assertIsInstance(scan._parse_dist("+1%"), float)


def _rec(code, name="测试ETF", fund="000000", typ="买入",
         amount=None, nav=None, shares=None):
    """构造一条交易记录(字段与 trade_records.json 对齐)。"""
    r = {"ETF代码": code, "ETF名称": name, "联接基金": fund, "类型": typ}
    if amount is not None:
        r["金额"] = amount
    if nav is not None:
        r["净值"] = nav
    if shares is not None:
        r["份额"] = shares
    return r


class TestLoadHoldings(unittest.TestCase):
    """_load_holdings / _held_codes: 净份额聚合、清仓排除、份额回退。

    通过 patch record._load_records 注入合成记录，并让 RECORDS_PATH.exists() 为真，
    避免读真实文件/触发联网回填。
    """

    def _run(self, records):
        fake_path = mock.Mock()
        fake_path.exists.return_value = True
        with mock.patch.object(scan, "RECORDS_PATH", fake_path), \
             mock.patch("record._load_records", return_value=records):
            return scan._load_holdings()

    def test_no_file_returns_empty(self):
        fake_path = mock.Mock()
        fake_path.exists.return_value = False
        with mock.patch.object(scan, "RECORDS_PATH", fake_path):
            self.assertEqual(scan._load_holdings(), {})

    def test_empty_records(self):
        self.assertEqual(self._run([]), {})

    def test_single_buy_by_shares(self):
        holdings = self._run([_rec("510300", shares=1000)])
        self.assertIn("510300", holdings)

    def test_single_buy_by_amount_and_nav(self):
        # 金额/净值反推份额 = 5000/5 = 1000 > 1，算持仓
        holdings = self._run([_rec("510300", amount=5000, nav=5.0)])
        self.assertIn("510300", holdings)

    def test_amount_only_fallback(self):
        # 净值缺失时用金额兜底为份额，避免漏持仓
        holdings = self._run([_rec("510300", amount=5000)])
        self.assertIn("510300", holdings)

    def test_liquidated_excluded(self):
        # 买入后清仓 → 净份额归零，不算持仓
        holdings = self._run([
            _rec("510300", shares=1000),
            _rec("510300", typ="清仓"),
        ])
        self.assertNotIn("510300", holdings)

    def test_sell_partial_still_held(self):
        holdings = self._run([
            _rec("510300", shares=1000),
            _rec("510300", typ="卖出", shares=400),
        ])
        self.assertIn("510300", holdings)  # 剩 600 份

    def test_sell_all_not_held(self):
        holdings = self._run([
            _rec("510300", shares=1000),
            _rec("510300", typ="卖出", shares=1000),
        ])
        self.assertNotIn("510300", holdings)  # 剩 0 份

    def test_sub_one_share_ignored(self):
        # 净份额 <= 1 视为舍入噪声，不算持仓
        holdings = self._run([_rec("510300", shares=0.5)])
        self.assertNotIn("510300", holdings)

    def test_holdings_keeps_last_buy_record(self):
        # holdings 存最近一笔买入记录
        holdings = self._run([
            _rec("510300", shares=1000, name="旧名"),
            _rec("510300", shares=500, name="新名"),
        ])
        self.assertEqual(holdings["510300"]["ETF名称"], "新名")

    def test_multiple_codes(self):
        holdings = self._run([
            _rec("510300", shares=1000),
            _rec("562500", shares=2000),
        ])
        self.assertEqual(set(holdings), {"510300", "562500"})


class TestHeldCodes(unittest.TestCase):
    """_held_codes: 返回字符串代码集合。"""

    def test_returns_str_set(self):
        with mock.patch.object(scan, "_load_holdings",
                               return_value={510300: {}, "562500": {}}):
            codes = scan._held_codes()
        self.assertEqual(codes, {"510300", "562500"})
        self.assertTrue(all(isinstance(c, str) for c in codes))

    def test_empty(self):
        with mock.patch.object(scan, "_load_holdings", return_value={}):
            self.assertEqual(scan._held_codes(), set())


def _df_row(code, status, dist, name="测试ETF", vol=1.0, ma5turn="↑",
            price=1.0, ma5=0.9, ma10=0.9, today_chg=0.0):
    """构造 check_holdings 需要的最小 df 行。"""
    return {
        "代码": code, "名称": name, "状态": status, "距MA50": dist,
        "量比": vol, "MA5拐头": ma5turn, "现价": price, "MA5": ma5, "MA10": ma10,
        "今日涨跌": today_chg,
    }


class TestCheckHoldings(unittest.TestCase):
    """check_holdings 关键分支:接近支撑加仓 / 趋势偏弱止损分档。"""

    def _run(self, records, rows):
        df = pd.DataFrame(rows)
        fake_path = mock.Mock()
        fake_path.exists.return_value = True
        with mock.patch.object(scan, "RECORDS_PATH", fake_path), \
             mock.patch("record._load_records", return_value=records):
            return scan.check_holdings(df)

    def test_no_holdings_empty(self):
        fake_path = mock.Mock()
        fake_path.exists.return_value = False
        with mock.patch.object(scan, "RECORDS_PATH", fake_path):
            self.assertEqual(scan.check_holdings(pd.DataFrame()), [])

    def test_support_ma5_up_is_add(self):
        # 接近支撑 + MA5拐头↑ + 低位 → 🟢 加仓
        alerts = self._run(
            [_rec("510300", shares=1000)],
            [_df_row("510300", "▲ 接近支撑", "+0.4%", ma5turn="↑")],
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["级别"], "🟢 加仓")

    def test_support_ma5_down_is_hold(self):
        # 接近支撑 + MA5拐头↓ → 🔵 持有观望(不喊补，也不喊止损)
        alerts = self._run(
            [_rec("510300", shares=1000)],
            [_df_row("510300", "▲ 接近支撑", "+0.4%", ma5turn="↓")],
        )
        self.assertEqual(alerts[0]["级别"], "🔵 持有")

    def test_weak_shallow_is_stoploss_watch(self):
        # 趋势偏弱 + 浅破(-2.2%) + 缩量 → 🟠 止损观察(先观察)
        alerts = self._run(
            [_rec("562500", shares=1000)],
            [_df_row("562500", "✗ 趋势偏弱", "-2.2%", vol=1.0, ma5turn="↓")],
        )
        self.assertEqual(alerts[0]["级别"], "🟠 止损观察")

    def test_weak_deep_break_is_stoploss(self):
        # 趋势偏弱 + 明显跌破(-5%) → 🔴 止损
        alerts = self._run(
            [_rec("562500", shares=1000)],
            [_df_row("562500", "✗ 趋势偏弱", "-5.0%", vol=1.0)],
        )
        self.assertEqual(alerts[0]["级别"], "🔴 止损")

    def test_weak_volume_break_is_stoploss(self):
        # 趋势偏弱 + 放量(量比>=1.5) + 今日下跌 → 🔴 止损
        alerts = self._run(
            [_rec("562500", shares=1000)],
            [_df_row("562500", "✗ 趋势偏弱", "-1.0%", vol=1.8, today_chg=-1.5)],
        )
        self.assertEqual(alerts[0]["级别"], "🔴 止损")

    def test_weak_volume_rebound_is_stoploss_watch(self):
        # 趋势偏弱 + 放量 + 浅破 + 今日上涨(放量反弹回踩，非破位) → 🟠 止损观察
        # 复现 510300 场景：现价在 MA50 下方、量比>=1.5，但今天是大涨反弹，不应喊止损
        alerts = self._run(
            [_rec("510300", shares=1000)],
            [_df_row("510300", "✗ 趋势偏弱", "-2.0%", vol=1.6, today_chg=2.9)],
        )
        self.assertEqual(alerts[0]["级别"], "🟠 止损观察")

    def test_row_missing_skipped(self):
        # 持仓票不在当日扫描 df 中 → 跳过，不报错
        alerts = self._run(
            [_rec("999999", shares=1000)],
            [_df_row("510300", "▲ 接近支撑", "+0.4%")],
        )
        self.assertEqual(alerts, [])


class TestCriticalWatchEval(unittest.TestCase):
    """critical_watch_eval: 门槛全过 且 4项确认恰好差1项 → ◈ 临界观察。

    门槛项由调用方(analyze)判定后以 gate_ok 传入；这里只验判定与缺项文案。
    确认项顺序: 均线理顺 / MA5拐头 / MA20拐头 / 放量确认。
    """

    def test_miss_volume_is_critical(self):
        # 结构全齐、只差放量确认(农业场景) → 临界，缺项=量能
        is_crit, miss = scan.critical_watch_eval(
            True, ma_aligned=True, ma5_up=True, ma20_up=True, volume_ok=False)
        self.assertTrue(is_crit)
        self.assertEqual(miss, "量能未确认")

    def test_miss_ma20_turn_is_critical(self):
        # 只差 MA20 拐头(电力/绿电场景) → 临界，缺项=MA20
        is_crit, miss = scan.critical_watch_eval(
            True, ma_aligned=True, ma5_up=True, ma20_up=False, volume_ok=True)
        self.assertTrue(is_crit)
        self.assertEqual(miss, "MA20未拐头↑")

    def test_all_confirms_is_not_critical(self):
        # 4项确认全满足 → 不是临界(交由 analyze 判为 ◇ 转强初期)
        is_crit, miss = scan.critical_watch_eval(
            True, ma_aligned=True, ma5_up=True, ma20_up=True, volume_ok=True)
        self.assertFalse(is_crit)
        self.assertEqual(miss, "")

    def test_miss_two_confirms_is_not_critical(self):
        # 差 2 项确认 → 仍偏弱，不进临界
        is_crit, miss = scan.critical_watch_eval(
            True, ma_aligned=True, ma5_up=False, ma20_up=False, volume_ok=True)
        self.assertFalse(is_crit)
        self.assertEqual(miss, "")

    def test_gate_fail_is_never_critical(self):
        # 门槛不过(如跌太深) → 无论确认多好都不是临界
        is_crit, miss = scan.critical_watch_eval(
            False, ma_aligned=True, ma5_up=True, ma20_up=True, volume_ok=False)
        self.assertFalse(is_crit)
        self.assertEqual(miss, "")

    def test_confirm_labels_length(self):
        # 缺项标签数须与确认项数一致(增删确认项时防止错位)
        self.assertEqual(len(scan.CRIT_CONFIRM_LABELS), 4)


class TestCriticalWatchInStatusOrder(unittest.TestCase):
    """◈ 临界观察须已登记进状态枚举，且排在完好与偏弱之间。"""

    def test_in_status_order(self):
        self.assertIn("◈ 临界观察", scan.STATUS_ORDER)

    def test_between_good_and_weak(self):
        order = scan.STATUS_ORDER
        self.assertLess(order.index("- 趋势完好"), order.index("◈ 临界观察"))
        self.assertLess(order.index("◈ 临界观察"), order.index("✗ 趋势偏弱"))

    def test_in_status_rank(self):
        # 进入 rank 表后，持仓票转临界观察不会被 check_holdings 当异常保守止损
        self.assertIn("◈ 临界观察", scan._STATUS_RANK)


class TestCriticalWatchHoldingNotStopLoss(unittest.TestCase):
    """持仓票转 ◈ 临界观察时，不应触发止损(它比偏弱强，是在恢复)。"""

    def _run(self, records, rows):
        df = pd.DataFrame(rows)
        fake_path = mock.Mock()
        fake_path.exists.return_value = True
        with mock.patch.object(scan, "RECORDS_PATH", fake_path), \
             mock.patch("record._load_records", return_value=records):
            return scan.check_holdings(df)

    def test_critical_watch_not_stoploss(self):
        # 距MA50 浅负、缩量，状态=临界观察 → 不喊 🔴 止损
        alerts = self._run(
            [_rec("159611", shares=1000)],
            [_df_row("159611", "◈ 临界观察", "-3.0%", vol=1.0, ma5turn="↑")],
        )
        levels = [a["级别"] for a in alerts]
        self.assertNotIn("🔴 止损", levels)


class TestCache(unittest.TestCase):
    """save_cache 只存历史K(剔除当日行); is_cache_fresh 按日期(mtime)判新鲜。

    历史K收盘后不再变，当日价永远走实时源现抓，因此当天拉过一次即可全天复用，
    跨到新交易日才重拉。用临时目录替换 CACHE_DIR，避免污染真实缓存。
    """

    def setUp(self):
        self._orig_dir = scan.CACHE_DIR
        self.tmp = Path(tempfile.mkdtemp())
        scan.CACHE_DIR = self.tmp
        self.today = datetime.now().strftime("%Y-%m-%d")
        self.yst = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    def tearDown(self):
        scan.CACHE_DIR = self._orig_dir

    def test_save_cache_strips_today(self):
        # 含当日行的 df，写入后当日行被剔除，历史行保留
        df = pd.DataFrame({"date": [self.yst, self.today], "close": [1.0, 2.0]})
        scan.save_cache("TEST", df)
        saved = pd.read_csv(self.tmp / "TEST.csv")
        dates = list(saved["date"].astype(str))
        self.assertNotIn(self.today, dates)
        self.assertIn(self.yst, dates)

    def test_save_cache_keeps_all_history(self):
        # 全是历史行时不误删
        df = pd.DataFrame({"date": [self.yst], "close": [1.0]})
        scan.save_cache("TEST", df)
        saved = pd.read_csv(self.tmp / "TEST.csv")
        self.assertEqual(list(saved["date"].astype(str)), [self.yst])

    def test_fresh_when_written_today(self):
        scan.save_cache("TEST", pd.DataFrame({"date": [self.yst], "close": [1.0]}))
        self.assertTrue(scan.is_cache_fresh("TEST"))

    def test_stale_when_written_yesterday(self):
        scan.save_cache("TEST", pd.DataFrame({"date": [self.yst], "close": [1.0]}))
        old = (datetime.now() - timedelta(days=1)).timestamp()
        os.utime(self.tmp / "TEST.csv", (old, old))
        self.assertFalse(scan.is_cache_fresh("TEST"))

    def test_stale_when_missing(self):
        self.assertFalse(scan.is_cache_fresh("NOPE"))


if __name__ == "__main__":
    unittest.main()
