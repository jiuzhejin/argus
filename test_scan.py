"""scan.py 核心纯函数与持仓聚合逻辑的单元测试。

纯 stdlib unittest，不引入 pytest。跑法:
    .venv/bin/python -m unittest test_scan.py -v

覆盖:
    _add_advice   持仓感知加仓判据(5%/10% 分档 + 未持仓空串)
    _parse_dist   距MA50 字符串解析与异常回退
    _load_holdings / _held_codes  净份额聚合、清仓排除、份额回退
    check_holdings  关键分支(接近支撑加仓 / 趋势偏弱止损分档)
"""
import unittest
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
            price=1.0, ma5=0.9, ma10=0.9):
    """构造 check_holdings 需要的最小 df 行。"""
    return {
        "代码": code, "名称": name, "状态": status, "距MA50": dist,
        "量比": vol, "MA5拐头": ma5turn, "现价": price, "MA5": ma5, "MA10": ma10,
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
        # 趋势偏弱 + 放量(量比>=1.5)跌破 → 🔴 止损
        alerts = self._run(
            [_rec("562500", shares=1000)],
            [_df_row("562500", "✗ 趋势偏弱", "-1.0%", vol=1.8)],
        )
        self.assertEqual(alerts[0]["级别"], "🔴 止损")

    def test_row_missing_skipped(self):
        # 持仓票不在当日扫描 df 中 → 跳过，不报错
        alerts = self._run(
            [_rec("999999", shares=1000)],
            [_df_row("510300", "▲ 接近支撑", "+0.4%")],
        )
        self.assertEqual(alerts, [])


if __name__ == "__main__":
    unittest.main()
