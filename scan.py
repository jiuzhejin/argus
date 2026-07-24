"""
Argus - ETF 均线信号扫描器

扫描 ETF 池，按均线+量价信号分类：
  ★ 低位确认：低位主买点，可先买一点
              回踩MA50确认(近5日) → MA20站上MA50 → MA5/10/20较顺
              温和放量 + MA50上行，且距MA50较近(默认<4.5%)
  ◇ 转强初期：MA50/MA100下方的早期转强，可少量参与
              MA5>MA10>MA20 且短线继续走强，但中期趋势尚未完全扭转
              用来承认“可以先买一点看看”，不是确认型买点
  ◆ 趋势跟随：趋势延续型买点，可继续少量参与
              技术面基本达标但不属最佳低位，或处于强势反转早期
              可以继续少量参与，已有持仓则继续拿，不在高位追加太多
  ▲ 接近支撑：短期均线压制 + 中期均线支撑（关注回调机会）
  □ 多头排列：趋势健康，持有为主

用法:
  .venv/bin/python scan.py              # 扫描全部(显示完整指标)
  .venv/bin/python scan.py --refresh    # 强制刷新缓存
  .venv/bin/python scan.py --no-xhs     # 不生成小红书日志(盘中模式)
  .venv/bin/python scan.py --compare    # 与盘中快照对比(盘后模式)
  .venv/bin/python scan.py --morning    # 早盘分析(实时数据，不缓存)
  .venv/bin/python scan.py --code 512480 # 查询单只ETF的分析信息
"""

import argparse
import io
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import json

import akshare as ak
import pandas as pd
import requests as _requests

pd.set_option("display.unicode.east_asian_width", True)

# ===== 配置 =====
CACHE_DIR = Path(__file__).parent / ".cache"
LOG_DIR = Path(__file__).parent / "logs"


class TeeStream:
    """同时写入终端和日志文件"""

    def __init__(self, terminal, log_buf):
        self.terminal = terminal
        self.log_buf = log_buf

    def write(self, msg):
        self.terminal.write(msg)
        # 日志中过滤掉 \r 覆盖行（进度条），只保留完整行
        if msg and not msg.startswith("\r"):
            self.log_buf.write(msg)

    def flush(self):
        self.terminal.flush()
        self.log_buf.flush()

    def isatty(self):
        return self.terminal.isatty()

# ===== 场外联接基金(C类，短期持有费率更低) =====
OTC_FUND = {
    "510300": ("006131", "华泰柏瑞沪深300ETF联接C"),
    "510500": ("004348", "南方中证500ETF联接C(LOF)"),
    "159915": ("004744", "易方达创业板ETF联接C"),
    "588000": ("011613", "华夏科创50ETF联接C"),
    "512480": ("007301", "国联安半导体ETF联接C"),
    "159995": ("008888", "华夏半导体芯片ETF联接C"),
    "515880": ("007818", "国泰通信设备ETF联接C"),
    "515050": ("008087", "华夏5G通信ETF联接C"),
    "562800": ("014111", "嘉实稀有金属ETF联接C"),
    "512010": ("007883", "易方达医药ETF联接C"),
    "512690": None,
    "516110": ("012974", "国泰汽车整车ETF联接C"),
    "512800": ("006697", "华宝中证银行ETF联接C"),
    "512200": ("004643", "南方房地产ETF联接C"),
    "512880": ("012363", "国泰证券ETF联接C"),
    "512400": ("004433", "南方有色金属ETF联接C"),
    "517520": ("020412", "永赢黄金股ETF联接C"),
    "512100": ("011861", "南方中证1000ETF联接C"),
    "515070": ("008586", "华夏人工智能ETF联接C"),
    "516160": ("012832", "南方新能源ETF联接C"),
    "515790": ("012680", "华泰柏瑞光伏ETF联接C"),
    "512980": ("004753", "广发传媒ETF联接C"),
    "512660": ("005693", "广发军工ETF联接C"),
    "159869": ("012769", "华夏中证动漫游戏ETF联接C"),
    "512580": ("002984", "广发环保ETF联接C"),
    "513180": ("013403", "华夏恒生科技ETF联接C"),
    "510880": ("012762", "华泰柏瑞红利ETF联接C"),
    "512890": ("007467", "华泰柏瑞红利低波ETF联接C"),
    "159611": ("016186", "广发中证全指电力ETF联接C"),
    "512620": ("010770", "天弘中证农业主题ETF联接C"),
    "562500": ("018345", "华夏中证机器人ETF联接C"),
    "159566": ("021034", "易方达国证新能源电池ETF联接发起式C"),
    "562550": ("018735", "华夏中证绿色电力ETF发起式联接C"),
    "159326": ("025857", "华夏中证电网设备主题ETF发起式联接C"),
}

# 联接基金代码 → ETF代码 反向映射
FUND_TO_ETF = {otc[0]: code for code, otc in OTC_FUND.items() if otc}
# 补充C类等其他份额
# 补充A类和其他份额(兼容已有记录)
FUND_TO_ETF.update({
    "004347": "510500",  # 南方中证500ETF联接A(LOF)
    "160119": "510500",  # 南方中证500ETF联接A(LOF)主代码
    "006382": "512500",  # 华夏中证500ETF联接C(对应场内512500，非510500)
    "000051": "510300",  # 华夏沪深300联接A
    "001052": "512500",  # 华夏中证500ETF联接A(对应场内512500)
    "110026": "159915",  # 易方达创业板ETF联接A
    "001592": "159977",  # 天弘创业板ETF联接A(对应场内159977，非159915)
    "001593": "159977",  # 天弘创业板ETF联接C(对应场内159977)
    "011612": "588000",  # 华夏科创50联接A
    "007300": "512480",  # 国联安半导体联接A
    "008887": "159995",  # 华夏半导体芯片联接A
    "007817": "515880",  # 国泰通信设备联接A
    "014110": "562800",  # 嘉实稀有金属联接A
    "001344": "512010",  # 易方达医药联接A
    "012973": "516110",  # 国泰汽车整车联接A
    "240019": "512800",  # 华宝银行ETF联接A
    "001594": "515290",  # 天弘中证银行联接A(对应场内515290，非512800)
    "001595": "515290",  # 天弘中证银行联接C(对应场内515290)
    "004642": "512200",  # 南方房地产联接A
    "012362": "512880",  # 国泰证券ETF联接A
    "006098": "512000",  # 华宝券商ETF联接A(对应场内512000，非512880)
    "004432": "512400",  # 南方有色金属联接A
    "020411": "517520",  # 永赢黄金股联接A
    "014974": "512100",  # 南方中证1000联接A
    "011832": "515070",  # 华夏人工智能联接A
    "012831": "516160",  # 南方新能源联接A
    "012679": "515790",  # 华泰柏瑞光伏联接A
    "004752": "512980",  # 广发传媒联接A
    "003017": "512660",  # 广发军工联接A
    "012728": "159869",  # 国泰动漫游戏联接A
    "001064": "512580",  # 广发环保联接A
    "012348": "520920",  # 天弘恒生科技联接A(对应场内520920，非513180)
    "013402": "513180",  # 华夏恒生科技ETF联接A
    "012761": "510880",  # 华泰柏瑞红利ETF联接A
    "007466": "512890",  # 华泰柏瑞红利低波ETF联接A
    "016185": "159611",  # 广发中证全指电力ETF联接A
    "010769": "512620",  # 天弘中证农业主题ETF联接A
    "018344": "562500",  # 华夏中证机器人ETF联接A
    "021033": "159566",  # 易方达国证新能源电池ETF联接发起式A
    "018734": "562550",  # 华夏中证绿色电力ETF发起式联接A
    "025856": "159326",  # 华夏中证电网设备主题ETF发起式联接A
})

# ETF代码 → 名称
ETF_NAME = {}  # 在 ETF_POOL 定义后填充

# ===== ETF 池分层 =====
ETF_BUCKETS = {
    # 日常主要盯盘和择时的核心交易池
    "core": [
        ("510300", "沪深300ETF"),
        ("510500", "中证500ETF"),
        ("588000", "科创50ETF"),
        ("512480", "半导体ETF"),
        ("515070", "人工智能ETF"),
        ("512010", "医药ETF"),
        ("512880", "证券ETF"),
        ("512660", "军工ETF"),
        ("513180", "恒生科技ETF"),
        ("562500", "机器人ETF"),
        ("159326", "电网设备ETF"),
    ],
    # 主题对、但优先级略低，适合观察轮动和升级
    "watch": [
        ("512100", "中证1000ETF"),
        ("159915", "创业板ETF"),
        ("515880", "通信ETF"),
        ("562800", "稀有金属ETF"),
        ("516110", "汽车ETF"),
        ("512800", "银行ETF"),
        ("512400", "有色金属ETF"),
        ("517520", "黄金股ETF"),
        ("516160", "新能源ETF"),
        ("159611", "电力ETF"),
        ("562550", "绿电ETF"),
        ("159566", "储能电池ETF"),
        ("512620", "农业ETF"),
        ("159869", "动漫游戏ETF"),
    ],
    # 不按波段主逻辑处理，单列出来避免和交易池混用
    "dca": [
        ("512890", "红利低波ETF"),
    ],
}
ETF_POOL = [item for bucket in ETF_BUCKETS.values() for item in bucket]
ETF_NAME = {code: name for code, name in ETF_POOL}
ETF_BUCKET_LABELS = {
    "core": "核心交易池",
    "watch": "主题观察池",
    "dca": "定投池",
}
ETF_TO_BUCKET = {
    code: bucket
    for bucket, items in ETF_BUCKETS.items()
    for code, _ in items
}


def get_cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol}.csv"


def _is_trading_hours() -> bool:
    """判断当前是否在交易时段(9:15-15:00 工作日)，收盘后延长到16:00以覆盖日K未更新的窗口"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    from datetime import time as _time
    return _time(9, 15) <= t <= _time(16, 0)


def _morning_elapsed_ratio() -> float:
    """当日已过交易时间占比(用于早盘量能归一化)
    早盘 9:30-11:30(120min) + 午盘 13:00-15:00(120min) = 240min
    """
    now = datetime.now()
    t = now.hour * 60 + now.minute
    if t < 570:          # < 9:30
        return 0.0
    elif t <= 690:       # 9:30 - 11:30
        return (t - 570) / 240
    elif t < 780:        # 11:30 - 13:00 午休
        return 120 / 240
    elif t <= 900:       # 13:00 - 15:00
        return (120 + t - 780) / 240
    return 1.0


def _bucket_display_order(bucket: str) -> int:
    return {"core": 0, "watch": 1, "dca": 2}.get(bucket, 9)


def _print_bucket_overview(df: pd.DataFrame):
    if "池子" not in df.columns:
        return
    print(f"\n{'='*60}")
    print("  🎯 池子分层")
    print(f"{'='*60}")
    for bucket in ("core", "watch", "dca"):
        group = df[df["池子key"] == bucket]
        if group.empty:
            continue
        buy_n = int(group["状态"].isin(["★ 低位确认", "◆ 趋势跟随", "◇ 转强初期"]).sum())
        strong_n = int(group["状态"].isin(["★ 低位确认", "◆ 趋势跟随", "◇ 转强初期", "□ 多头排列", "- 趋势完好"]).sum())
        crit_n = int((group["状态"] == "◈ 临界观察").sum())
        weak_n = int((group["状态"] == "✗ 趋势偏弱").sum())
        crit_str = f"  临界{crit_n}" if crit_n else ""
        print(f"  {ETF_BUCKET_LABELS.get(bucket, bucket)}: {len(group)}只  可参与{buy_n}  走强/完好{strong_n}{crit_str}  偏弱{weak_n}")
    print()


def _run_etf_agent_cli(symbols: list[str]) -> dict[str, dict]:
    """调用 etf-agent 的正式 CLI 契约，只读取 JSON 结果。"""
    if not symbols:
        return {}
    root = Path("/Users/bytedance/etf-agent")
    start = root / "start.sh"
    if not start.exists():
        print(f"  🤖 etf-agent: 未找到 start.sh({start})，跳过 LLM 复核")
        return {}

    print(f"  🤖 etf-agent: 请求 {len(symbols)} 只 -> {', '.join(symbols)}")
    t0 = time.time()
    try:
        proc = subprocess.run(
            [str(start), "analyze", *symbols, "--json"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        print(f"  🤖 etf-agent: ⚠ 子进程超时(>300s)，本次无 LLM 结果")
        return {}
    except Exception as e:
        print(f"  🤖 etf-agent: ⚠ 子进程启动失败: {e}")
        return {}

    elapsed = time.time() - t0
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        print(f"  🤖 etf-agent: ⚠ 退出码 {proc.returncode}（耗时 {elapsed:.1f}s）"
              f"{('  stderr: ' + stderr[-300:]) if stderr else ''}")

    stdout = (proc.stdout or "").strip()
    if not stdout:
        print(f"  🤖 etf-agent: ⚠ stdout 为空（耗时 {elapsed:.1f}s）"
              f"{('  stderr: ' + stderr[-300:]) if stderr else ''}")
        return {}
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        print(f"  🤖 etf-agent: ⚠ JSON 解析失败: {e}  stdout 前 300 字: {stdout[:300]}")
        return {}

    out = {}
    failed = []
    for row in payload.get("results", []):
        code = str(row.get("code", "")).strip()
        if not code:
            continue
        if row.get("error"):
            failed.append(f"{code}({row.get('error')})")
            continue
        out[code] = {
            "score": row.get("score", "?"),
            "if_empty": row.get("if_empty", "?"),
            "if_holding": row.get("if_holding", "?"),
            "reason": row.get("reason", ""),
            "breakdown": row.get("breakdown", ""),
        }

    stats = payload.get("stats", {}) or {}
    print(f"  🤖 etf-agent: 成功 {len(out)} 只  失败 {len(failed)} 只  "
          f"llm_calls={stats.get('llm_calls', '?')}  cache_hits={stats.get('cache_hits', '?')}  "
          f"耗时 {elapsed:.1f}s")
    if failed:
        print(f"  🤖 etf-agent: ⚠ 无结果明细: {'; '.join(failed)}")
    return out


def is_cache_fresh(symbol: str) -> bool:
    """历史K缓存是否新鲜。

    缓存里只存"截止昨天"的历史日K(不含当日价，当日价永远走实时源现抓)。
    历史K一旦收盘就不再变，因此"当天拉过一次"即可全天复用——用缓存文件
    mtime 是否为今天判定。跨到新交易日时 mtime 落在昨天 → 判过期 → 重拉一次，
    补进最近一个已收盘交易日的K。
    """
    path = get_cache_path(symbol)
    if not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return mtime.date() == datetime.now().date()


def load_cached(symbol: str) -> pd.DataFrame | None:
    path = get_cache_path(symbol)
    if path.exists():
        return pd.read_csv(path)
    return None


def save_cache(symbol: str, df: pd.DataFrame):
    """写入历史K缓存。

    写前剔除"当日行"：部分数据源(如东财)盘中/收盘后会返回一个当日行，
    其价格可能是盘中中间态。缓存只保留确定的历史K，当日价一律由实时源
    每次现抓追加(见 fetch_hist)，从根上杜绝脏当日价被持久化。
    """
    CACHE_DIR.mkdir(exist_ok=True)
    if "date" in df.columns and not df.empty:
        today_str = datetime.now().strftime("%Y-%m-%d")
        df = df[df["date"].astype(str).str[:10] < today_str]
    df.to_csv(get_cache_path(symbol), index=False)


SNAPSHOT_DIR = Path(__file__).parent / ".cache"


def save_snapshot(results: list, tag: str):
    """保存扫描结果快照(用于盘中/盘后对比)"""
    date_tag = datetime.now().strftime("%Y%m%d")
    path = SNAPSHOT_DIR / f"snapshot_{date_tag}_{tag}.json"
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


def load_snapshot(tag: str) -> list | None:
    """加载指定快照"""
    date_tag = datetime.now().strftime("%Y%m%d")
    path = SNAPSHOT_DIR / f"snapshot_{date_tag}_{tag}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print(f"  ⚠ 快照文件损坏: {path.name}")
        return None


def _market_prefix(symbol: str) -> str:
    """判断沪深市场前缀。
    深市 ETF/基金：15x / 16x 开头 → sz
    沪市 ETF/基金：5x（含 51x/56x/58x）开头 → sh
    注意：56/58 开头是沪市(上交所)，不是深市——早期误判为 sz 会导致
          新浪实时返回空、腾讯日K缺当日，进而用昨收当现价。
    """
    return "sz" if symbol.startswith(("15", "16")) else "sh"


def sina_symbol(symbol: str) -> str:
    """转换为新浪格式：沪市加sh，深市加sz"""
    return f"{_market_prefix(symbol)}{symbol}"


def fetch_hist_tx(symbol: str) -> pd.DataFrame:
    """腾讯日K线原始接口"""
    code = f"{_market_prefix(symbol)}{symbol}"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,250,qfq"
    r = _requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json().get("data", {}).get(code, {})
    klines = data.get("qfqday") or data.get("day") or []
    if not klines:
        return pd.DataFrame()
    df = pd.DataFrame(klines, columns=["date", "open", "close", "high", "low", "volume"])
    for col in ["open", "close", "high", "low", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def fetch_realtime(symbol: str) -> dict | None:
    """获取实时行情(新浪), 盘中使用"""
    sina_code = sina_symbol(symbol)
    url = f"https://hq.sinajs.cn/list={sina_code}"
    try:
        r = _requests.get(url, timeout=5, headers={"Referer": "https://finance.sina.com.cn"})
        r.encoding = "gbk"
        data = r.text.strip().split('"')[1]
        if not data:
            return None
        f = data.split(",")
        return {
            "date": f[30], "open": float(f[1]), "close": float(f[3]),
            "high": float(f[4]), "low": float(f[5]),
            "volume": float(f[8]), "amount": float(f[9]),
        }
    except Exception as e:
        print(f"  ⚠ 实时行情获取失败({symbol}): {e}")
        return None


def fetch_hist(symbol: str, max_retries: int = 3, skip_cache: bool = False) -> pd.DataFrame:
    """获取历史数据，优先用缓存；盘中追加实时行情"""
    if not skip_cache and is_cache_fresh(symbol):
        df = load_cached(symbol)
    else:
        df = None
        # 优先新浪，失败回退东方财富，再回退腾讯
        sources = [
            lambda: ak.fund_etf_hist_sina(symbol=sina_symbol(symbol)),
            lambda: ak.fund_etf_hist_em(symbol=symbol, adjust="qfq").rename(
                columns={"日期": "date", "收盘": "close", "成交量": "volume",
                          "开盘": "open", "最高": "high", "最低": "low", "成交额": "amount"}
            ),
            lambda: fetch_hist_tx(symbol),
        ]
        for src in sources:
            for attempt in range(max_retries):
                try:
                    result = src()
                    if result is not None and not result.empty and "date" in result.columns:
                        # 复权校验：近10日内任意相邻两日跳变超30%说明复权异常，换源
                        closes = result["close"].astype(float).tail(10)
                        pct = closes.pct_change().abs()
                        if (pct > 0.3).any():
                            break  # 复权异常，换下一个源
                        save_cache(symbol, result)
                        df = result
                        break
                    break  # 空数据或格式不对，换下一个源
                except Exception:
                    if attempt < max_retries - 1:
                        time.sleep(3 * (attempt + 1))
                        continue  # 重试当前数据源
                    break  # 重试耗尽，换下一个源
            if df is not None:
                break

        if df is None:
            cached = load_cached(symbol)
            if cached is not None:
                df = cached
            else:
                raise RuntimeError(f"{symbol} 所有数据源均失败")

    # 盘中追加当日实时行情；收盘后若日K尚未更新也用实时数据补齐
    today_str = datetime.now().strftime("%Y-%m-%d")
    last_date_str = str(df["date"].iloc[-1])[:10]
    need_realtime = _is_trading_hours() or (
        datetime.now().weekday() < 5 and last_date_str < today_str
    )
    if need_realtime:
        rt = fetch_realtime(symbol)
        if rt is not None:
            # 统一 date 类型(历史源可能返回 datetime.date 或 str)
            last_date = df["date"].iloc[-1]
            rt_date = rt["date"]
            if hasattr(last_date, "isoformat"):
                from datetime import date as _date
                rt_date = _date.fromisoformat(rt["date"])
                rt["date"] = rt_date
            if str(last_date) == str(rt["date"]):
                # 今日已有记录(部分数据源会包含当日)，更新为最新价
                for col in ["open", "close", "high", "low", "volume"]:
                    if col in df.columns:
                        df.loc[df.index[-1], col] = rt[col]
            else:
                # 历史数据只到昨天，追加今日实时行
                row = {col: rt.get(col) for col in df.columns if col in rt}
                df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)

    return df


# ◈ 临界观察确认项标签(顺序即展示优先级)
CRIT_CONFIRM_LABELS = ("均线未理顺(MA5>10>20)", "MA5未拐头↑", "MA20未拐头↑", "量能未确认")


def critical_watch_eval(gate_ok: bool, ma_aligned: bool, ma5_up: bool,
                        ma20_up: bool, volume_ok: bool) -> tuple[bool, str]:
    """◈ 临界观察判定(纯函数，便于单测)。

    门槛项(gate_ok，须由调用方全过)：仍在长期均线下方、不太深、价站上短均线。
    确认项共 4 项：均线理顺 / MA5拐头 / MA20拐头 / 放量确认。
    规则：门槛全过 且 4 项确认"恰好差 1 项" → 临界观察；
          差 0 项即 ◇ 转强初期，差 ≥2 项仍 ✗ 偏弱。
    返回 (是否临界, 缺项文案)；非临界时文案为空串。
    """
    confirms = (ma_aligned, ma5_up, ma20_up, volume_ok)
    miss = [label for label, ok in zip(CRIT_CONFIRM_LABELS, confirms) if not ok]
    is_crit = gate_ok and len(miss) == 1
    return is_crit, (miss[0] if is_crit else "")


def analyze(symbol: str, name: str, as_of_date: str = None,
            skip_cache: bool = False, morning: bool = False) -> dict:
    """计算均线信号。as_of_date='2026-05-14' 时只用该日及之前的数据。"""
    try:
        df = fetch_hist(symbol, skip_cache=skip_cache)
        df = df.sort_values("date")
        if as_of_date:
            df = df[df["date"].astype(str).str[:10] <= as_of_date]
        df = df.tail(250)
        if len(df) < 140:
            return {"代码": symbol, "名称": name, "状态": "⚠ 数据不足"}

        close = df["close"].astype(float)
        volume = df["volume"].astype(float)

        # 均线
        ma5 = close.rolling(5).mean()
        ma10 = close.rolling(10).mean()
        ma20 = close.rolling(20).mean()
        ma50 = close.rolling(50).mean()
        ma100 = close.rolling(100).mean()

        # 成交量均线
        vol_avg20 = volume.shift(1).rolling(20).mean()

        # 最新值
        c = close.iloc[-1]
        m5, m10, m20 = ma5.iloc[-1], ma10.iloc[-1], ma20.iloc[-1]
        m50, m100 = ma50.iloc[-1], ma100.iloc[-1]
        vol = volume.iloc[-1]
        va20 = vol_avg20.iloc[-1]

        # 派生指标
        # 回踩确认：只认"近5日"曾贴近MA50。10日窗口会把"十天前贴MA50、
        # 现已拉飞+20%"的票误判成"刚回踩"，导致追涨被标成买入。收紧到5日。
        recent_low_5d = close.tail(5).min()
        recent_low_10d = close.tail(10).min()
        support_tested = recent_low_5d <= m50 * 1.015
        support_recent_10d = recent_low_10d <= m50 * 1.02
        ma5_up = ma5.iloc[-1] > ma5.iloc[-2] and ma5.iloc[-2] > ma5.iloc[-3]
        ma20_up = ma20.iloc[-1] > ma20.iloc[-2] and ma20.iloc[-2] > ma20.iloc[-3]
        vol_ratio = round(vol / va20, 2) if pd.notna(va20) and va20 > 0 else 0
        partial_day = morning or _is_trading_hours()
        event_end = len(volume) - 1 if partial_day else len(volume)
        event_start = max(0, event_end - 5)
        event_ratios = (volume.iloc[event_start:event_end] /
                        vol_avg20.iloc[event_start:event_end])
        recent_volume_event = bool((event_ratios >= 1.5).fillna(False).any())
        event_feedback_ok = False
        if recent_volume_event:
            event_offset = int(event_ratios.fillna(0).argmax())
            event_index = event_start + event_offset
            event_close = close.iloc[event_index]
            post_event = close.iloc[event_index + 1:event_end + 1]
            event_feedback_ok = bool(
                c >= event_close * 0.98
                and (post_event.empty or post_event.min() >= event_close * 0.98)
            )
        recent_volume_ratio = round(
            (volume.iloc[max(0, event_end - 3):event_end].mean() / va20)
            if pd.notna(va20) and va20 > 0 and event_end > 0 else 0,
            2,
        )
        reversal_volume_ok = (
            0.8 <= vol_ratio <= 1.8
            or (
                recent_volume_event
                and event_feedback_ok
                and 0.5 <= vol_ratio <= 1.8
                and recent_volume_ratio <= 2.5
            )
        )
        dist_ma50_pct = round((c - m50) / m50 * 100, 2)

        # ========== 数据校验 ==========
        if abs(c - m5) / m5 > 0.3:
            return {"代码": symbol, "名称": name, "状态": "⚠ 数据异常(价格偏离MA5超30%)"}

        # ========== 分类 ==========
        above_long = c > m50 and c > m100
        above_mid = c > m50
        bull_align = c > m5 and c > m10 and c > m20 and above_long

        # MA50近10日斜率
        ma50_slope = (ma50.iloc[-1] - ma50.iloc[-10]) / ma50.iloc[-10] * 100

        # 趋势买入的全部技术条件是否达标
        buy_core = (
            c > m5 and c > m10
            and above_long
            and 0.8 <= vol_ratio <= 2.5      # 极端放量排除(脉冲/出货)
            and ma5_up
            and support_tested
            and ma50_slope > 0               # MA50下行时不发买入信号
        )
        # ========== 突破提醒 ==========
        breakout = ""
        days_below = 0
        recent_closes = close.iloc[-6:-1]   # 前5个交易日(不含今天)
        recent_ma50 = ma50.iloc[-6:-1]
        if len(recent_closes) >= 5 and len(recent_ma50) >= 5:
            days_below = int((recent_closes < recent_ma50).sum())
            if above_long and days_below >= 3:
                breakout = f"⬆ 突破({days_below}/5日在MA50下)"

        # ★ 保留低位主买点；◆ 则作为可继续少量参与的趋势跟随信号。
        # 历史样本里拖后腿的◆，大多是 MA20 尚未稳稳站上 MA50，
        # 或 MA20 只是刚刚贴着 MA50，趋势结构还太脆。这里把◆收紧为：
        # 1) 中长期结构至少满足 MA20 > MA50 > MA100
        # 2) MA20 相对 MA50 至少留出一层最小安全垫
        # 3) 但若价格本身就紧贴 MA50，可放宽成“近轴跟随”
        # 早期右侧跟随用于处理刚站回MA50、但MA100仍略有压制的票。
        # 这类票不能等到完全多头后才承认，但必须要求短中期结构
        # 明确走强，且价格已经逼近MA100。
        ENTRY_DIST_CAP = 4.5
        trend_ready = m20 > m50 and ma50_slope > 0.1
        executable_breakout = breakout != ""
        strict_buy = (
            buy_core
            and dist_ma50_pct < ENTRY_DIST_CAP
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
            and reversal_volume_ok
            and dist_ma50_pct <= 5.0
            and -1.8 < ma50_slope <= 0
        )
        learning_signal = (
            not above_long
            and c > m5 and c > m10 and c > m20
            and m5 > m10 > m20
            and ma5_up
            and ma20_up
            and reversal_volume_ok
            and dist_ma50_pct <= 8.0
            and c >= m50 * 0.94
        )
        near_support = above_long and dist_ma50_pct <= 5.0 and c < m20

        # ◈ 临界观察：只读的可见性标注，不是买点。
        # 把"结构已开始转强、只差最后一道确认"的票从偏弱里挑出来提示。
        # 门槛项(必须全过)：仍在长期均线下方、不能跌太深、价已站上自己的短均线；
        # 确认项(共4项)：均线理顺 / MA5拐头 / MA20拐头 / 放量确认。
        # 判定：门槛全过 且 4项确认"恰好差1项"→ ◈；差0项即 ◇ 转强初期，差≥2项仍 ✗ 偏弱。
        crit_gate = (
            not above_long
            and dist_ma50_pct <= 8.0
            and c >= m50 * 0.94
            and c > m5 and c > m10 and c > m20
        )
        critical_watch, crit_miss = critical_watch_eval(
            crit_gate, m5 > m10 > m20, ma5_up, ma20_up, reversal_volume_ok)

        # □ 多头排列的入场条件：结构低位 + 温和放量 + 拐头向上 + 近期贴过MA50
        # 用来把"从下往上翻越型多头"和"高位脉冲加速型多头"分开，前者可先买一点
        # dist ≤ 3% 是回放得出的准确率与期望值均改善的分割线，>3% 命中率骤降
        bull_entry_ok = (
            bull_align
            and dist_ma50_pct <= 3.0
            and 0.85 <= vol_ratio <= 1.9
            and ma5_up
            and support_tested
        )

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
        elif critical_watch:
            status = "◈ 临界观察"
        else:
            status = "✗ 趋势偏弱"

        # ========== 左侧试探评估 ==========
        probe = ""
        if status == "▲ 接近支撑":
            base_ok = (
                dist_ma50_pct <= 3.0         # 距MA50很近
                and ma50_slope >= 0           # MA50未下行
                and 0.2 <= vol_ratio < 2.0   # 缩量回调佳，但排除极端无量和恐慌抛售
                and m100 <= m50               # MA100在下方提供二级支撑
            )
            if base_ok:
                safety = round((m50 - m100) / m100 * 100, 1)
                if support_tested:
                    probe = f"◆ 安全垫{safety}%"
                else:
                    probe = f"◇ 待验证 安全垫{safety}%"

        # ========== 买入信号可信度评估 ==========
        assess = ""
        risk_tier = ""
        if status in ("★ 低位确认", "◆ 趋势跟随", "◇ 转强初期"):
            score = 0
            warns = []

            # 0) 距MA50风险等级
            if dist_ma50_pct < ENTRY_DIST_CAP:
                risk_tier = "低位"
            elif dist_ma50_pct < 10:
                risk_tier = "追涨"
            else:
                risk_tier = "高位博弈"

            # 1) 均线完全多头排列: MA5>MA10>MA20>MA50>MA100
            if m5 > m10 > m20 > m50 > m100:
                score += 2
            elif m5 > m10 > m20:
                score += 1
                warns.append("长期均线未理顺")
            else:
                warns.append("均线紊乱")

            # 2) MA50趋势: 近10日MA50斜率(已在分类阶段计算)
            if ma50_slope > 0.5:
                score += 2
            elif ma50_slope > 0.1:
                score += 1
            else:
                warns.append("MA50下行")

            # 3) 量比合理性: 温和放量好，暴量可能是脉冲
            if 0.9 <= vol_ratio <= 1.8:
                score += 2
            elif vol_ratio <= 2.5:
                score += 1
                warns.append("量偏大")
            else:
                warns.append("放量异常")

            # 4) 距250日高点位置
            high_250 = close.max()
            dist_high = (c - high_250) / high_250 * 100
            if dist_high > -10:
                score += 2
            elif dist_high > -30:
                score += 1
                warns.append(f"距高点{dist_high:.0f}%")
            else:
                warns.append(f"距高点{dist_high:.0f}%")

            # 5) 近20日趋势一致性: 收阳天数
            up_days = int((close.tail(20).diff().dropna() > 0).sum())
            if up_days >= 12:
                score += 2
            elif up_days >= 9:
                score += 1
            else:
                warns.append("近期阴多阳少")

            # 综合评级
            if score >= 8:
                level = "强"
            elif score >= 5:
                level = "中"
            else:
                level = "弱"
            reason = ",".join(warns[:2]) if warns else "各指标确认"
            assess = f"{level}|{reason}"

        result = {
            "代码": symbol, "名称": name, "现价": round(c, 3),
            "MA5": round(m5, 3), "MA10": round(m10, 3), "MA20": round(m20, 3),
            "MA50": round(m50, 3), "MA100": round(m100, 3),
            "距MA50": f"{dist_ma50_pct:+.1f}%",
            "量比": vol_ratio,
            "近3日量比": recent_volume_ratio,
            "近5日放量": "是" if recent_volume_event else "否",
            "放量后守住": "是" if event_feedback_ok else "否",
            "回踩MA50": "是" if support_tested else "否",
            "MA5拐头": "↑" if ma5_up else "↓",
            "状态": status,
            "试探": probe,
            "突破": breakout,
            "多头入场": "是" if bull_entry_ok else "",
            "临界原因": crit_miss if status == "◈ 临界观察" else "",
        }
        # 今日涨跌幅(现价 vs 昨收)——任何模式都产出，供持仓判断区分放量上涨/下跌
        if len(close) >= 2:
            _prev_close = close.iloc[-2]
            if _prev_close > 0:
                result["今日涨跌"] = round((c - _prev_close) / _prev_close * 100, 2)
        if assess:
            result["信号评估"] = assess
            result["风险等级"] = risk_tier
            otc = OTC_FUND.get(symbol)
            result["场外基金"] = f"{otc[0]} {otc[1]}" if otc else "无"

        # 早盘指标
        if morning and len(df) >= 2:
            prev_close = close.iloc[-2]
            today_open = df["open"].astype(float).iloc[-1]
            open_chg = round((today_open - prev_close) / prev_close * 100, 2)
            morning_chg = round((c - prev_close) / prev_close * 100, 2)
            elapsed = _morning_elapsed_ratio()
            expected_vol = va20 * elapsed if elapsed > 0 else va20
            morning_vol = round(vol / expected_vol, 2) if expected_vol > 0 else 0
            result["昨收"] = round(prev_close, 3)
            result["开盘涨跌"] = f"{open_chg:+.2f}%"
            result["早盘涨跌"] = f"{morning_chg:+.2f}%"
            result["早盘量能"] = morning_vol

        return result

    except Exception as e:
        return {"代码": symbol, "名称": name, "状态": f"⚠ {str(e)[:40]}"}


STATUS_ORDER = [
    "★ 低位确认", "◆ 趋势跟随", "◇ 转强初期",
    "▲ 接近支撑", "□ 多头排列", "- 趋势完好", "◈ 临界观察", "✗ 趋势偏弱",
]

# 状态优先级（数字越小越好）
_STATUS_RANK = {s: i for i, s in enumerate(STATUS_ORDER)}

RECORDS_PATH = Path(__file__).parent / "logs" / "trade_records.json"
DCA_RECORDS_PATH = Path(__file__).parent / "logs" / "dca_records.json"

# ===== 定投模块配置 =====
# ⚠ "越弱越投"逻辑仅适用于宽基/红利等"均值回归、不会归零"的品种；
#   未来扩展窄基/个股需按品种分流(带基本面/趋势止损)，不可无脑越跌越买。
DCA_CONFIG = [
    {"etf": "512890", "fund": "007466", "name": "红利低波",
     "amount": 800, "type": "宽基/红利"},
]


def _load_holdings() -> dict:
    """读取交易记录并按ETF汇总净份额，返回 {code: 最近一笔买入记录}(仍有持仓的)。

    单一事实来源：持仓监控(check_holdings)与买入建议的持仓感知都复用它。
    """
    if not RECORDS_PATH.exists():
        return {}
    try:
        from record import _load_records
        records = _load_records()
    except Exception:
        try:
            records = json.loads(RECORDS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print(f"  ⚠ 交易记录文件损坏，跳过持仓监控")
            return {}
    if not records:
        return {}

    # 按ETF汇总净份额，排除已清仓
    from collections import defaultdict
    _shares = defaultdict(float)
    _last_buy = {}
    for r in records:
        code = r["ETF代码"]
        typ = r.get("类型", "买入")
        if typ == "清仓":
            _shares[code] = 0
            continue
        # 优先使用直接记录的份额（卖出按份额操作时）
        if r.get("份额") is not None and r["份额"] > 0:
            s = r["份额"]
        else:
            amt = r.get("金额") if r.get("金额") is not None else r.get("买入金额", 0)
            nav = r.get("净值") if r.get("净值") is not None else r.get("买入净值", 0)
            if amt and nav and nav > 0:
                s = amt / nav
            elif amt and amt > 0:
                # 净值未知时用金额作为替代份额，保证持仓不被遗漏
                s = amt
            else:
                s = 0
        if s > 0:
            if typ == "卖出":
                _shares[code] -= s
            else:
                _shares[code] += s
        if typ not in ("卖出", "清仓"):
            _last_buy[code] = r

    holdings = {}
    for code, rec in _last_buy.items():
        if _shares.get(code, 0) > 1:  # 还有持仓(忽略<1份的舍入误差)
            holdings[code] = rec
    return holdings


def _held_codes() -> set:
    """当前仍有持仓的ETF代码集合(字符串)。"""
    return {str(code) for code in _load_holdings().keys()}


def _add_advice(dist_val: float, held: bool) -> str:
    """已持仓时的加仓判据(以距MA50位置为主，5%/10% 分档)。

    未持仓返回空串(走各分组原有建仓文案)；阈值与 check_holdings 的 tier 一致。
    """
    if not held:
        return ""
    if dist_val < 5:
        return "已持有·回踩低位可再补一点 [低位]"
    if dist_val < 10:
        return "已持有·位置偏高，追涨慎加，持有为主 [追涨]"
    return "已持有·高位不建议再加，持有为主 [高位博弈]"


def _parse_dist(dist_str) -> float:
    """解析距MA50字符串(如 '+0.4%')为浮点数，失败返回0。"""
    try:
        return float(str(dist_str).replace("%", "").replace("+", ""))
    except (ValueError, AttributeError):
        return 0.0


# ===== C组合止损参数(经 tools/stoploss_backtest.py 近2年回测+样本外验证: P7/C-5) =====
# 目的：解决"刚买就止损/联接C来回折腾吃赎回费"。买入后保护期内不硬砍(除非放量破位)，
# 且相对买入成本亏损未到阈值不硬砍。保护期与C类惩罚性赎回费窗口(7天)对齐。
HOLD_PROTECT_DAYS = 7       # 买入后N个自然日内不触发硬止损(除非放量破位)
COST_STOP_PCT = -5.0        # 相对买入成本亏损超过该阈值(%)才允许硬止损


def _hold_days(rec: dict) -> int:
    """持有自然日数(距最近一笔买入)。无法解析时返回大数,视为已过保护期(不阻断止损)。"""
    ts = rec.get("时间", "")
    try:
        buy_dt = datetime.strptime(str(ts)[:10], "%Y-%m-%d")
        return (datetime.now() - buy_dt).days
    except (ValueError, TypeError):
        return 9999


def _cost_pnl_pct(code: str, rec: dict, cur_price: float) -> float | None:
    """相对买入成本的盈亏%。成本基准 = 买入日该ETF缓存收盘价(与现价同基准)。

    不能直接用 rec['净值'](那是联接基金净值,与ETF现价不同基准会算出错值)。
    取不到买入日缓存价时返回 None,调用方据此跳过成本闸门(保守放行止损)。
    """
    if cur_price <= 0:
        return None
    path = get_cache_path(str(code))
    if not path.exists():
        return None
    try:
        hist = pd.read_csv(path)
        buy_day = str(rec.get("时间", ""))[:10]
        hit = hist[hist["date"].astype(str).str[:10] == buy_day]
        if hit.empty:
            return None
        cost = float(hit["close"].iloc[-1])
        if cost <= 0:
            return None
        return (cur_price / cost - 1) * 100
    except (OSError, ValueError, KeyError):
        return None


def check_holdings(df: pd.DataFrame) -> list:
    """检查持仓信号变化，返回预警列表"""
    holdings = _load_holdings()
    if not holdings:
        return []

    alerts = []
    for code, rec in holdings.items():
        row = df[df["代码"] == code]
        if row.empty:
            continue
        row = row.iloc[0]
        cur_status = row.get("状态", "")
        buy_status = rec.get("当时信号", "")

        name = rec["ETF名称"]
        fund = rec["联接基金"]

        # agent 综合判断(--llm 时才有)：持有维度建议 + 理由，稍后附到本轮 alert 上
        _agent_hold = row.get("Agent持有", "")
        _agent_reason = row.get("Agent理由", "")
        _prev_n = len(alerts)

        # 跌破MA50 → 止损警告
        dist_str = row.get("距MA50", "+0%")
        dist_val = _parse_dist(dist_str)

        price = float(row.get("现价", 0) or 0)
        ma5 = float(row.get("MA5", 0) or 0)
        ma10 = float(row.get("MA10", 0) or 0)

        # 判定持仓健康度
        # 先区分"止盈观察"和真正的"止盈"：
        # 观察 = 已明显脱离低位，应开始盯利润保护；
        # 止盈 = 结构已转弱，需要兑现利润。
        take_profit_watch = (
            dist_val >= 8
            and cur_status in ("◆ 趋势跟随", "□ 多头排列", "- 趋势完好", "▲ 接近支撑")
        )
        take_profit_soft = (
            dist_val >= 8
            and ma5 > 0
            and price < ma5
            and row.get("MA5拐头") == "↓"
            and cur_status in ("◆ 趋势跟随", "□ 多头排列", "- 趋势完好", "▲ 接近支撑")
        )
        take_profit_hard = (
            dist_val >= 10
            and ma10 > 0
            and price < ma10
            and cur_status in ("□ 多头排列", "- 趋势完好", "▲ 接近支撑")
        )

        # 买入信号→多头排列/趋势完好 是正常的信号兑现，不算降级
        # 真正的降级是跌到"趋势偏弱"；但若高位趋势开始钝化，则先止盈。
        if take_profit_hard:
            alerts.append({
                "基金": fund, "ETF": name, "级别": "🟠 止盈",
                "信号变化": f"{buy_status} → {cur_status}",
                "距MA50": dist_str,
                "建议": "高位跌破MA10，建议优先兑现利润",
            })
        elif take_profit_soft:
            alerts.append({
                "基金": fund, "ETF": name, "级别": "🟠 止盈",
                "信号变化": f"{buy_status} → {cur_status}",
                "距MA50": dist_str,
                "建议": "高位回落并跌回MA5下，建议先止盈/减仓一半",
            })
        elif take_profit_watch:
            alerts.append({
                "基金": fund, "ETF": name, "级别": "🟣 止盈观察",
                "信号变化": f"{buy_status} → {cur_status}",
                "距MA50": dist_str,
                "建议": "已进入利润保护区，开始盯MA5/MA10是否转弱",
            })
        elif cur_status == "✗ 趋势偏弱" or cur_status not in _STATUS_RANK:
            # 状态转弱不等于要立刻割：区分"真破位"和"浅回踩/缩量假摔"。
            # 真止损需要确认——明显跌破MA50，或放量跌破；否则只是刚破线，先观察。
            # 注意：这里用"状态是否已知"判断异常，不用 rank 阈值——
            #       STATUS_ORDER 增删枚举会改变各状态的 rank，硬编码数字会误伤。
            vol_ratio = float(row.get("量比", 0) or 0)
            today_chg = float(row.get("今日涨跌", 0) or 0)
            confirmed_breakdown = (
                cur_status not in _STATUS_RANK              # 状态异常/未知，保守止损
                or dist_val <= -3                           # 已明显跌破MA50(超3%)
                or (dist_val < 0 and vol_ratio >= 1.5 and today_chg < 0)  # 放量且今日下跌才算破位
            )
            # C组合两道人性化闸门(P7/C-5)：避免"刚买就割"和C类来回折腾吃赎回费。
            # 放量破位(量比>=1.5且今日下跌)属于急跌,任何时候都放行硬止损,不受闸门保护。
            vol_breakdown = vol_ratio >= 1.5 and today_chg < 0
            hold_days = _hold_days(rec)
            cost_pnl = _cost_pnl_pct(code, rec, price)
            in_protect = hold_days < HOLD_PROTECT_DAYS               # 闸门1:保护期内
            cost_ok = cost_pnl is not None and cost_pnl > COST_STOP_PCT  # 闸门2:亏损未到-5%
            gated = confirmed_breakdown and not vol_breakdown and (in_protect or cost_ok)

            if confirmed_breakdown and not gated:
                alerts.append({
                    "基金": fund, "ETF": name, "级别": "🔴 止损",
                    "信号变化": f"{buy_status} → {cur_status}",
                    "距MA50": dist_str,
                    "建议": "趋势走坏(明显跌破MA50或放量破位)，建议止损",
                })
            elif gated:
                # 位置已破位,但被保护期/成本线拦下——降级为观察,说明拦截原因。
                # gated 为真时:要么在保护期内,要么 cost_ok(此时 cost_pnl 必非 None)。
                if in_protect:
                    why = f"持有{hold_days}天(<{HOLD_PROTECT_DAYS}天保护期)"
                else:
                    why = f"浮亏{cost_pnl:+.1f}%(未破{COST_STOP_PCT}%成本线)"
                alerts.append({
                    "基金": fund, "ETF": name, "级别": "🟠 止损观察",
                    "信号变化": f"{buy_status} → {cur_status}",
                    "距MA50": dist_str,
                    "建议": f"位置转弱但{why}，先持有观察；放量破位或跌破成本线再止损",
                })
            else:
                alerts.append({
                    "基金": fund, "ETF": name, "级别": "🟠 止损观察",
                    "信号变化": f"{buy_status} → {cur_status}",
                    "距MA50": dist_str,
                    "建议": "刚跌破MA50但幅度浅且缩量，先观察；跌破关键支撑或放量下跌再止损",
                })
        elif cur_status == "- 趋势完好":
            alerts.append({
                "基金": fund, "ETF": name, "级别": "🟡 关注",
                "信号变化": f"{buy_status} → {cur_status}",
                "距MA50": dist_str,
                "建议": "趋势转弱，密切关注",
            })
        elif cur_status == "★ 低位确认":
            # 加仓风险等级
            if dist_val < 5:
                tier = "低位"
            elif dist_val < 10:
                tier = "追涨"
            else:
                tier = "高位博弈"
            alerts.append({
                "基金": fund, "ETF": name, "级别": "🟢 加仓",
                "信号变化": f"{buy_status} → {cur_status}",
                "距MA50": dist_str, "风险等级": tier,
                "建议": f"低位确认，可以再买一点 [{tier}]",
            })
        elif cur_status == "◆ 趋势跟随":
            # 技术面仍好但已脱离低位 → 持有兑现，不追高追加太多
            tier = "追涨" if dist_val < 10 else "高位博弈"
            alerts.append({
                "基金": fund, "ETF": name, "级别": "🔵 持有",
                "信号变化": f"{buy_status} → {cur_status}",
                "距MA50": dist_str, "风险等级": tier,
                "建议": f"趋势跟随阶段，持有为主，已{tier}不建议再多买",
            })
        elif cur_status == "◇ 转强初期":
            alerts.append({
                "基金": fund, "ETF": name, "级别": "🟡 观察",
                "信号变化": f"{buy_status} → {cur_status}",
                "距MA50": dist_str, "风险等级": "早期",
                "建议": "短线转强但中期趋势未确认，如要参与只先买一点",
            })
        elif cur_status == "□ 多头排列":
            entry_ok = str(row.get("多头入场", "")) == "是"
            vol_ratio = float(row.get("量比", 0) or 0)
            ma5_up_row = row.get("MA5拐头") == "↑"
            if dist_val < 5:
                tier = "低位"
            elif dist_val < 10:
                tier = "追涨"
            else:
                tier = "高位博弈"
            if entry_ok:
                alerts.append({
                    "基金": fund, "ETF": name, "级别": "🟢 加仓",
                    "信号变化": f"{buy_status} → {cur_status}",
                    "距MA50": dist_str, "风险等级": tier,
                    "建议": f"多头排列且回踩确认，可以再买一点 [{tier}]",
                })
            elif dist_val <= 6 and ma5_up_row and 0.85 <= vol_ratio <= 2.0:
                alerts.append({
                    "基金": fund, "ETF": name, "级别": "🔵 持有",
                    "信号变化": f"{buy_status} → {cur_status}",
                    "距MA50": dist_str, "风险等级": tier,
                    "建议": f"多头排列但未回踩MA50，持有为主，等回踩再补 [{tier}]",
                })
            else:
                alerts.append({
                    "基金": fund, "ETF": name, "级别": "🔵 持有",
                    "信号变化": f"{buy_status} → {cur_status}",
                    "距MA50": dist_str, "风险等级": tier,
                    "建议": f"多头排列但已{tier}或量能异常，持有为主，不建议追加",
                })
        elif cur_status == "▲ 接近支撑" and row.get("MA5拐头") == "↑":
            alerts.append({
                "基金": fund, "ETF": name, "级别": "🟢 加仓",
                "信号变化": f"{buy_status} → {cur_status}",
                "距MA50": dist_str, "风险等级": "低位",
                "建议": "支撑位企稳，MA5拐头，可以再买一点 [低位]",
            })
        else:
            # ▲ 接近支撑(MA5↓) — 回调未企稳，持有观望
            alerts.append({
                "基金": fund, "ETF": name, "级别": "🔵 持有",
                "信号变化": f"{buy_status} → {cur_status}",
                "距MA50": dist_str,
                "建议": "持有观望",
            })

        # 把本轮新增的 alert 都补上 agent 综合判断(有则填，无则留空不影响原有渲染)
        for _a in alerts[_prev_n:]:
            if _agent_hold:
                _a["Agent持有"] = _agent_hold
            if _agent_reason:
                _a["Agent理由"] = _agent_reason

    return alerts


# ============================================================
#  定投模块 (智能择时，仅决定"哪天投"，不决定"投多少")
#  设计哲学：定投择时与波段相反——越弱越跌越便宜越该投，
#  边界日(15/28)无条件兜底，绝不让"择时"变成"不投"。
# ============================================================

def _dca_window(today):
    """当前定投窗口。窗口①1-15(兜底15)，窗口②16-28(兜底28)；>28视为下半窗口已过。"""
    d, m = today.day, today.month
    if d <= 15:
        return {"label": "①", "lo": 1, "hi": 15, "remain": 15 - d,
                "range": f"{m}/1-{m}/15", "deadline": f"{m}/15", "expired": False}
    elif d <= 28:
        return {"label": "②", "lo": 16, "hi": 28, "remain": 28 - d,
                "range": f"{m}/16-{m}/28", "deadline": f"{m}/28", "expired": False}
    return {"label": "②", "lo": 16, "hi": 28, "remain": -1,
            "range": f"{m}/16-{m}/28", "deadline": f"{m}/28", "expired": True}


def _dca_threshold(remain):
    """触发线随剩余自然日放松；兜底日(remain<=0)无条件触发。"""
    if remain <= 0:
        return 0
    if remain <= 3:
        return 45
    if remain <= 7:
        return 60
    return 75


def _dca_metrics(code):
    """定投专用指标(与analyze解耦)：近30日分位、距MA10/20、趋势细分档、恐慌放量。"""
    try:
        df = fetch_hist(code).sort_values("date").tail(120)
        if len(df) < 30:
            return None
        close = df["close"].astype(float)
        volume = df["volume"].astype(float)
        c = close.iloc[-1]
        ma5 = close.rolling(5).mean().iloc[-1]
        ma10 = close.rolling(10).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        ma50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else float("nan")
        win30 = close.tail(30)
        pctile = int(round((win30 < c).sum() / len(win30) * 100))
        dist_ma10 = round((c - ma10) / ma10 * 100, 2)
        dist_ma20 = round((c - ma20) / ma20 * 100, 2)
        # 趋势细分档(定投模块专用，越靠后越弱越便宜)
        if pd.notna(ma50) and c < ma20 < ma50:
            trend = "空头排列"
        elif pd.notna(ma50) and c < ma50:
            trend = "趋势走弱"
        elif c < ma20:
            trend = "短期回调"
        elif c > ma5 > ma10 > ma20:
            trend = "多头排列"
        else:
            trend = "趋势完好"
        prev = close.iloc[-2]
        va20 = volume.rolling(20).mean().iloc[-1]
        vol_ratio = round(volume.iloc[-1] / va20, 2) if pd.notna(va20) and va20 > 0 else 0
        panic = (c < prev) and vol_ratio >= 1.5
        return {"现价": round(c, 3), "MA20": round(ma20, 3), "分位": pctile,
                "距MA10": dist_ma10, "距MA20": dist_ma20,
                "趋势": trend, "量比": vol_ratio, "恐慌": panic}
    except Exception:
        return None


def _dca_score(m):
    """逆向便宜度评分(0-100，越弱越便宜越高)，返回(score, 明细list)。"""
    parts = []
    base = 100 - m["分位"]
    parts.append(f"便宜度{base}(分位{m['分位']}%)")
    tb = {"空头排列": 15, "趋势走弱": 10, "短期回调": 5,
          "多头排列": -5, "趋势完好": 0}.get(m["趋势"], 0)
    if tb:
        parts.append(f"{m['趋势']}{tb:+d}")
    mb = 0
    if m["距MA20"] < 0:
        mb = min(int(round(-m["距MA20"] * 3)), 15)
        if mb:
            parts.append(f"破MA20+{mb}")
    pb = 8 if m["恐慌"] else 0
    if pb:
        parts.append(f"恐慌放量+{pb}")
    return max(0, min(100, base + tb + mb + pb)), parts


def _dca_load_records():
    """定投记录独立存于 dca_records.json，与正常交易隔离。"""
    if not DCA_RECORDS_PATH.exists():
        return []
    try:
        return json.loads(DCA_RECORDS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _dca_done_in_window(records, etf, today, win):
    """本窗口内该ETF是否已有买入记录(复用记录系统判断，零额外状态)。"""
    for r in records:
        if r.get("ETF代码") != etf or r.get("类型", "买入") != "买入":
            continue
        try:
            dt = datetime.strptime(str(r.get("时间", ""))[:10], "%Y-%m-%d")
        except ValueError:
            continue
        if dt.year == today.year and dt.month == today.month and win["lo"] <= dt.day <= win["hi"]:
            return r
    return None


def dca_advice():
    """生成定投建议，返回(win, items)。"""
    if not DCA_CONFIG:
        return None, []
    today = datetime.now()
    win = _dca_window(today)
    records = _dca_load_records()
    items = []
    for cfg in DCA_CONFIG:
        it = {k: cfg[k] for k in ("name", "etf", "fund", "type", "amount")}
        done = _dca_done_in_window(records, cfg["etf"], today, win)
        m = _dca_metrics(cfg["etf"])
        it["metrics"] = m
        if done:
            it["decision"] = "done"
            it["advice"] = f"✅ 本窗口已定投({str(done.get('时间',''))[:10]})，下次窗口见"
        elif m is None:
            it["decision"] = "nodata"
            it["advice"] = "⚠️ 数据不足，无法判断，请人工定投"
        elif win["expired"]:
            it["decision"] = "expired"
            it["advice"] = "⚠️ 本月下半窗口(16-28)已过且未投，请尽快人工补投"
        else:
            score, parts = _dca_score(m)
            th = _dca_threshold(win["remain"])
            it.update(score=score, threshold=th, parts=parts)
            if win["remain"] <= 0:
                it["decision"] = "deadline"
                it["advice"] = f"🔔 兜底日！今天无条件定投 {cfg['amount']}元"
            elif score >= th:
                it["decision"] = "buy"
                it["advice"] = f"✅ 今天适合定投 {cfg['amount']}元 — 评分{score}≥{th}"
            else:
                it["decision"] = "wait"
                it["advice"] = f"○ 再等等(评分{score}<{th})，便宜度不够；兜底日 {win['deadline']} 必投"
        items.append(it)
    return win, items


def _dca_lines(win, items, mask_amount=False):
    """渲染定投模块文本行。mask_amount=True 隐藏金额(小红书脱敏)。"""
    if not items:
        return []
    marks = {"done": "✅已投", "buy": "🟢可投", "deadline": "🔔必投",
             "wait": "⬜未投", "expired": "⚠️已过", "nodata": "❓"}
    lines = []
    for it in items:
        m = it.get("metrics")
        mark = marks.get(it["decision"], "⬜")
        lines.append(f"  {it['name']} {it['etf']}(A类{it['fund']}) {it['type']}")
        lines.append(f"  窗口{win['label']} {win['range']}  剩{max(win['remain'],0)}天到兜底 | 本窗口:{mark}")
        if m:
            lines.append(f"  现价{m['现价']} | 分位{m['分位']}% | 距MA20 {m['距MA20']:+.1f}% | 趋势:{m['趋势']}")
            if it.get("parts"):
                lines.append(f"  评分 {it['score']}/100 ({' '.join(it['parts'])}) | 触发线{it['threshold']}")
        adv = it["advice"]
        if mask_amount:
            adv = adv.replace(f" {it['amount']}元", "")
        lines.append(f"  → {adv}")
    return lines


def _render_dca(win, items):
    if not items:
        return
    print(f"\n{'='*60}")
    print("  💰 定投模块")
    print(f"{'='*60}")
    for line in _dca_lines(win, items):
        print(line)


def main():
    parser = argparse.ArgumentParser(description="Argus ETF 均线信号扫描器")
    parser.add_argument("--refresh", action="store_true", help="强制刷新缓存")
    parser.add_argument("--no-xhs", action="store_true", help="不生成小红书日志(盘中模式)")
    parser.add_argument("--compare", action="store_true", help="与盘中快照对比(盘后模式)")
    parser.add_argument("--no-cache", action="store_true", help="不使用缓存，全部实时拉取")
    parser.add_argument("--morning", action="store_true", help="早盘分析(实时数据，不缓存)")
    parser.add_argument("--llm", action="store_true", help="调用 etf-agent CLI 做二次判断（默认关闭）")
    parser.add_argument("--code", metavar="CODE", help="查询单只ETF的分析信息(股票代码)")
    args = parser.parse_args()

    # ===== 单只查询模式 =====
    if args.code:
        code = args.code
        name = ETF_NAME.get(code)
        if not name:
            # 尝试从联接基金代码反查
            etf_code = FUND_TO_ETF.get(code)
            if etf_code:
                name = ETF_NAME.get(etf_code, etf_code)
                code = etf_code
            else:
                name = code
        skip = args.no_cache or args.morning
        result = analyze(code, name, skip_cache=skip, morning=args.morning)
        if args.llm:
            cli_rows = _run_etf_agent_cli([code])
            lite = cli_rows.get(code)
            if lite:
                result["Agent空仓"] = lite["if_empty"]
                result["Agent持有"] = lite["if_holding"]
                result["Agent理由"] = lite["reason"]
        print(f"\n{'='*60}")
        print(f"  📋 {name}（{code}）分析详情")
        print(f"{'='*60}")
        for k, v in result.items():
            if v == "" or v is None:
                continue
            print(f"  {k}: {v}")
        return

    # ===== 启用日志 Tee =====
    log_buf = io.StringIO()
    log_buf.write(f"--- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    sys.stdout = TeeStream(sys.__stdout__, log_buf)

    if args.refresh:
        import shutil
        if CACHE_DIR.exists():
            shutil.rmtree(CACHE_DIR)
        print("  缓存已清除\n")

    if args.morning:
        print("🌅 Argus 早盘分析(实时数据)...\n")
    else:
        print("🔭 Argus 正在扫描...\n")

    results = []
    cached_count = 0
    is_tty = sys.stdout.isatty()
    for i, (code, name) in enumerate(ETF_POOL):
        skip = args.no_cache or args.morning
        was_cached = False if skip else is_cache_fresh(code)
        if was_cached:
            cached_count += 1
        r = analyze(code, name, skip_cache=skip, morning=args.morning)
        results.append(r)
        src = "缓存" if was_cached else "在线"
        if is_tty:
            print(f"\r  [{i+1}/{len(ETF_POOL)}] {name} ({src})", end="", flush=True)
        else:
            print(f"  [{i+1}/{len(ETF_POOL)}] {name} ({src})")
        # 非缓存请求才需要间隔
        if not was_cached and i < len(ETF_POOL) - 1:
            time.sleep(5)

    if is_tty:
        print(f"\r  完成! (在线: {len(ETF_POOL)-cached_count}, 缓存: {cached_count})" + " " * 20)
    else:
        print(f"  完成! (在线: {len(ETF_POOL)-cached_count}, 缓存: {cached_count})")

    df = pd.DataFrame(results)
    df["池子key"] = df["代码"].map(ETF_TO_BUCKET).fillna("watch")
    df["池子"] = df["池子key"].map(ETF_BUCKET_LABELS).fillna("主题观察池")
    actionable_statuses = {"★ 低位确认", "◆ 趋势跟随", "◇ 转强初期", "▲ 接近支撑"}
    if args.llm:
        # 买入候选 + 当前持仓 都送 agent：持仓票无论当前状态如何都要拿到综合判断
        _held_for_llm = _held_codes()
        _codes = {str(code) for code in df[df["状态"].isin(actionable_statuses)]["代码"].tolist()}
        _codes |= {c for c in _held_for_llm if c in set(df["代码"].astype(str))}
        actionable_codes = sorted(_codes)
        cli_rows = _run_etf_agent_cli(actionable_codes)
    else:
        cli_rows = {}
    df["Agent空仓"] = df["代码"].astype(str).map(lambda c: cli_rows.get(c, {}).get("if_empty", ""))
    df["Agent持有"] = df["代码"].astype(str).map(lambda c: cli_rows.get(c, {}).get("if_holding", ""))
    df["Agent理由"] = df["代码"].astype(str).map(lambda c: cli_rows.get(c, {}).get("reason", ""))

    # 持仓感知：标记当前仍有持仓的票，供终端与飞书买入建议统一口径
    _held = _held_codes()
    df["已持仓"] = df["代码"].astype(str).isin(_held)

    df["_sort"] = df["状态"].map(_STATUS_RANK).fillna(99)
    df = df.sort_values("_sort")

    agent_cols = ["Agent空仓", "Agent持有"] if args.llm else []
    if args.morning:
        show_cols = ["池子", "代码", "名称", "现价", "昨收", "开盘涨跌", "早盘涨跌", "早盘量能",
                     "距MA50", "量比", "MA5拐头", "状态", *agent_cols,
                     "多头入场", "突破", "试探", "临界原因", "信号评估", "场外基金"]
    else:
        # 盘中/盘后统一显示完整指标列(原 --detail，现已设为默认)。
        show_cols = ["池子", "代码", "名称", "现价", "MA5", "MA10", "MA20", "MA50", "MA100",
                     "距MA50", "量比", "回踩MA50", "MA5拐头", "状态", *agent_cols,
                     "多头入场", "突破", "试探", "临界原因", "风险等级", "信号评估", "场外基金"]

    valid_cols = [c for c in show_cols if c in df.columns]

    _print_bucket_overview(df)

    # 分组输出
    displayed = set()
    detail_statuses = ("★ 低位确认", "◆ 趋势跟随", "◇ 转强初期")
    llm_statuses = (*detail_statuses, "▲ 接近支撑")
    for status_label in STATUS_ORDER:
        group = df[df["状态"] == status_label]
        if group.empty:
            continue
        displayed.update(group.index)
        print(f"\n{'='*60}")
        print(f"  {status_label}  ({len(group)}只)")
        print(f"{'='*60}")
        cols = valid_cols if status_label in detail_statuses else [
            c for c in valid_cols if c not in ("信号评估", "场外基金")]
        if status_label not in llm_statuses:
            cols = [c for c in cols if c not in ("Agent空仓", "Agent持有")]
        # "试探"列只在"接近支撑"分组显示
        if status_label != "▲ 接近支撑":
            cols = [c for c in cols if c != "试探"]
        # "多头入场"列只在"多头排列"分组显示
        if status_label != "□ 多头排列":
            cols = [c for c in cols if c != "多头入场"]
        # "临界原因"列只在"临界观察"分组显示
        if status_label != "◈ 临界观察":
            cols = [c for c in cols if c != "临界原因"]
        # "突破"列只在有突破标记的分组显示
        if "突破" in group.columns and group["突破"].astype(str).str.len().max() == 0:
            cols = [c for c in cols if c != "突破"]
        # 已持仓的票在名称后加 [持] 后缀，与飞书买入建议口径一致
        disp = group.copy()
        if "已持仓" in disp.columns and "名称" in disp.columns:
            disp["名称"] = disp.apply(
                lambda r: f"{r['名称']}[持]" if r.get("已持仓") else r["名称"], axis=1)
        print(disp[cols].to_string(index=False))

    # 异常
    errors = df[~df.index.isin(displayed)]
    if not errors.empty:
        print(f"\n{'='*60}")
        print(f"  ⚠ 异常  ({len(errors)}只)")
        print(f"{'='*60}")
        print(errors[["代码", "名称", "状态"]].to_string(index=False))

    # 统计
    counts = {s: len(df[df["状态"] == s]) for s in STATUS_ORDER}
    probe_count = len(df[df["试探"].astype(str).str.len() > 0]) if "试探" in df.columns else 0
    breakout_count = len(df[df["突破"].astype(str).str.len() > 0]) if "突破" in df.columns else 0
    ok = sum(counts.values())
    print(f"\n{'─'*60}")
    print(f"  扫描: {len(df)}  成功: {ok}  异常: {len(df)-ok}")
    probe_str = f"  ◆试探: {probe_count}" if probe_count > 0 else ""
    breakout_str = f"  ⬆突破: {breakout_count}" if breakout_count > 0 else ""
    crit_str = f"  ◈临界: {counts['◈ 临界观察']}" if counts['◈ 临界观察'] > 0 else ""
    print(f"  ★低位: {counts['★ 低位确认']}  ◆跟随: {counts['◆ 趋势跟随']}  ◇转强: {counts['◇ 转强初期']}  "
          f"▲支撑: {counts['▲ 接近支撑']}  "
          f"□多头: {counts['□ 多头排列']}{crit_str}{probe_str}{breakout_str}")
    print()

    # ===== 早盘概览 =====
    if args.morning and "早盘涨跌" in df.columns:
        df["_mchg"] = df["早盘涨跌"].apply(
            lambda x: float(x.rstrip("%").replace("+", "")) if isinstance(x, str) else float("nan")
        )
        valid = df[df["_mchg"].notna()]
        if not valid.empty:
            up_n = int((valid["_mchg"] > 0).sum())
            dn_n = int((valid["_mchg"] < 0).sum())
            top = valid.nlargest(1, "_mchg").iloc[0]
            bot = valid.nsmallest(1, "_mchg").iloc[0]
            print(f"{'='*60}")
            print(f"  🌅 早盘概览")
            print(f"{'='*60}")
            print(f"  ↑涨 {up_n} 只  ↓跌 {dn_n} 只  平 {len(valid)-up_n-dn_n} 只")
            print(f"  领涨: {top['名称']} {top['早盘涨跌']}  量能 {top.get('早盘量能', '-')}x")
            print(f"  领跌: {bot['名称']} {bot['早盘涨跌']}  量能 {bot.get('早盘量能', '-')}x")
            if "早盘量能" in df.columns:
                hot = df[df["早盘量能"] > 2.0].sort_values("早盘量能", ascending=False)
                if not hot.empty:
                    items = [f"{r['名称']}({r['早盘量能']})" for _, r in hot.iterrows()]
                    print(f"  量能异常(>2x): {'  '.join(items)}")
            print()
        df.drop(columns=["_mchg"], inplace=True)

    # ===== 持仓监控 =====
    alerts = check_holdings(df)
    if alerts:
        print(f"\n{'='*60}")
        print(f"  📊 持仓监控 ({len(alerts)} 只)")
        print(f"{'='*60}")
        for a in alerts:
            print(f"  {a['级别']} {a['ETF']}({a['基金']})")
            print(f"     {a['信号变化']}  距MA50 {a['距MA50']}  → {a['建议']}")
            if a.get("Agent持有"):
                print(f"     🤖 Agent 综合: {a['Agent持有']}"
                      + (f" ｜{a['Agent理由']}" if a.get("Agent理由") else ""))

    # ===== 定投模块 =====
    dca_win, dca_items = dca_advice()
    _render_dca(dca_win, dca_items)

    # ===== 保存快照 =====
    snapshot_tag = "morning" if args.morning else ("intraday" if _is_trading_hours() else "close")
    save_snapshot(results, snapshot_tag)

    # ===== 小红书格式日志 =====
    if not args.no_xhs and not args.morning:
        save_xhs_log(df, counts, holding_alerts=alerts, compare=args.compare,
                     dca=(dca_win, dca_items))

    # ===== 尾盘对比 =====
    if args.compare:
        compare_with_intraday(df, counts)

    # ===== 飞书推送 =====
    notify_feishu(df, counts, compare_report=args.compare, holding_alerts=alerts,
                  morning=args.morning, dca=(dca_win, dca_items))

    # ===== 写入日志文件 =====
    sys.stdout = sys.__stdout__
    LOG_DIR.mkdir(exist_ok=True)
    date_tag = datetime.now().strftime("%Y%m%d")
    suffix = "_morning" if args.morning else ("_intraday" if _is_trading_hours() else "")
    log_path = LOG_DIR / f"scan_{date_tag}{suffix}.log"
    log_path.write_text(log_buf.getvalue(), encoding="utf-8")
    print(f"  📝 日志已保存: {log_path.name}")


def compare_with_intraday(df_close: pd.DataFrame, counts_close: dict):
    """对比盘后(15:30)与盘中(14:45)扫描结果"""
    intraday = load_snapshot("intraday")
    if not intraday:
        print("\n  ⚠ 未找到盘中快照，跳过对比")
        return

    intra_map = {r["代码"]: r for r in intraday}

    print(f"\n{'='*60}")
    print("  🔍 尾盘对比 (14:45 vs 15:00)")
    print(f"{'='*60}")

    changes = []
    for _, row in df_close.iterrows():
        code = row["代码"]
        intra = intra_map.get(code)
        if not intra:
            continue

        price_now = row.get("现价")
        price_intra = intra.get("现价")
        status_now = row.get("状态", "")
        status_intra = intra.get("状态", "")
        vol_now = row.get("量比")
        vol_intra = intra.get("量比")

        # 跳过数据异常的
        if price_now is None or price_intra is None:
            continue

        price_chg = (price_now - price_intra) / price_intra * 100
        status_changed = status_now != status_intra
        vol_chg = (vol_now - vol_intra) if vol_now and vol_intra else 0

        # 只记录有意义的变化: 价格变动>0.3% 或 信号变化
        if abs(price_chg) >= 0.3 or status_changed:
            change = {
                "名称": row["名称"],
                "代码": code,
                "盘中价": round(price_intra, 3),
                "收盘价": round(price_now, 3),
                "涨跌": f"{price_chg:+.2f}%",
                "量比变化": f"{vol_intra}→{vol_now}",
            }
            if status_changed:
                change["信号变化"] = f"{status_intra} → {status_now}"
            else:
                change["信号变化"] = "—"
            changes.append(change)

    if not changes:
        print("  尾盘无显著变化，行情平稳收尾\n")
        return

    # 按价格变动排序
    changes.sort(key=lambda x: abs(float(x["涨跌"].rstrip("%"))), reverse=True)

    cdf = pd.DataFrame(changes)
    show_cols = ["名称", "盘中价", "收盘价", "涨跌", "量比变化", "信号变化"]
    valid = [c for c in show_cols if c in cdf.columns]
    print(cdf[valid].to_string(index=False))

    # 统计
    signal_flips = [c for c in changes if c["信号变化"] != "—"]
    big_moves = [c for c in changes if abs(float(c["涨跌"].rstrip("%"))) >= 1.0]

    print(f"\n{'─'*60}")
    print(f"  尾盘异动: {len(changes)} 只  "
          f"信号翻转: {len(signal_flips)} 只  "
          f"大幅波动(≥1%): {len(big_moves)} 只")

    if signal_flips:
        print("  ⚠ 信号翻转:")
        for c in signal_flips:
            print(f"    {c['名称']}: {c['信号变化']}")

    if big_moves:
        print("  ⚠ 大幅波动:")
        for c in big_moves:
            print(f"    {c['名称']}: {c['涨跌']}")

    print()


def save_xhs_log(df: pd.DataFrame, counts: dict, holding_alerts: list = None, compare: bool = False,
                 dca: tuple = None):
    """生成小红书风格的扫描日志"""
    today = datetime.now().strftime("%m-%d")
    today_full = datetime.now().strftime("%Y-%m-%d")
    ok = sum(counts.values())
    total = len(df)

    lines = []

    # 标题
    buy_count = counts["★ 低位确认"] + counts["◆ 趋势跟随"]
    time_tag = "盘中信号" if _is_trading_hours() else "盘后复盘"
    if buy_count > 0:
        lines.append(f"🚨 今日ETF扫描｜{buy_count}只出买入信号！速看 {today}")
    else:
        lines.append(f"📊 今日ETF扫描｜{today} {time_tag}")
    lines.append("")

    # 概览
    lines.append(f"🔭 Argus 扫描了 {total} 只ETF，成功 {ok} 只")
    lines.append("")
    lines.append("📋 信号分布：")
    lines.append(f"★ 低位确认：{counts['★ 低位确认']} 只")
    lines.append(f"◆ 趋势跟随：{counts['◆ 趋势跟随']} 只")
    lines.append(f"◇ 转强初期：{counts['◇ 转强初期']} 只")
    lines.append(f"▲ 接近支撑：{counts['▲ 接近支撑']} 只")

    lines.append(f"□ 多头排列：{counts['□ 多头排列']} 只")
    lines.append(f"- 趋势完好：{counts['- 趋势完好']} 只")
    lines.append(f"◈ 临界观察：{counts['◈ 临界观察']} 只")
    lines.append(f"✗ 趋势偏弱：{counts['✗ 趋势偏弱']} 只")
    lines.append("")
    if "池子key" in df.columns:
        lines.append("🎯 池子分层：")
        for bucket in ("core", "watch", "dca"):
            group = df[df["池子key"] == bucket]
            if group.empty:
                continue
            buy_n = int(group["状态"].isin(["★ 低位确认", "◆ 趋势跟随", "◇ 转强初期"]).sum())
            strong_n = int(group["状态"].isin(["★ 低位确认", "◆ 趋势跟随", "◇ 转强初期", "□ 多头排列", "- 趋势完好"]).sum())
            crit_n = int((group["状态"] == "◈ 临界观察").sum())
            weak_n = int((group["状态"] == "✗ 趋势偏弱").sum())
            crit_str = f"｜临界{crit_n}" if crit_n else ""
            lines.append(f"{ETF_BUCKET_LABELS.get(bucket, bucket)}：{len(group)}只｜可参与{buy_n}｜走强/完好{strong_n}{crit_str}｜偏弱{weak_n}")
        lines.append("")

    # 买入信号详情
    buy = df[df["状态"] == "★ 低位确认"]
    if not buy.empty:
        lines.append("—" * 20)
        lines.append("🔥 低位确认：")
        lines.append("")
        for _, row in buy.iterrows():
            assess = row.get("信号评估", "")
            otc = row.get("场外基金", "")
            level = assess.split("|")[0] if assess else ""
            reason = assess.split("|")[1] if "|" in assess else ""
            tier = row.get("风险等级", "")
            tier_tag = f" [{tier}]" if tier else ""
            lines.append(f"💰 {row['名称']}（{row['代码']}）{tier_tag}")
            lines.append(f"   现价 {row.get('现价','')} ｜距MA50 {row.get('距MA50','')} ｜量比 {row.get('量比','')}")
            lines.append(f"   MA5拐头{row.get('MA5拐头','')} ｜回踩MA50: {row.get('回踩MA50','')}")
            lines.append(f"   信号评估: {level}（{reason}）")
            if row.get("Agent空仓") or row.get("Agent持有"):
                lines.append(f"   🤖 Agent: 空仓{row.get('Agent空仓','?')} ｜ 持有{row.get('Agent持有','?')}")
                if row.get("Agent理由"):
                    lines.append(f"   综合理由: {row.get('Agent理由','')}")
            if otc:
                lines.append(f"   👉 场外基金: {otc}")
            lines.append("")

    # 趋势跟随（非最佳低位，但技术面已可继续少量参与）
    hold = df[df["状态"] == "◆ 趋势跟随"]
    if not hold.empty:
        lines.append("—" * 20)
        lines.append("◆ 趋势跟随（趋势延续型买点，可继续少量参与，已有持仓继续拿）：")
        for _, row in hold.iterrows():
            tier = row.get("风险等级", "")
            tier_tag = f" [{tier}]" if tier else ""
            agent_tag = ""
            if row.get("Agent空仓") or row.get("Agent持有"):
                agent_tag = f" ｜Agent 空仓{row.get('Agent空仓','?')}/持有{row.get('Agent持有','?')}"
            lines.append(f"   ◆ {row['名称']} {row.get('现价','')} ｜距MA50 {row.get('距MA50','')}{tier_tag}{agent_tag}")
        lines.append("")

    learn = df[df["状态"] == "◇ 转强初期"]
    if not learn.empty:
        lines.append("—" * 20)
        lines.append("◇ 转强初期（短线转强，但中期趋势还没确认，可先买一点看看）：")
        for _, row in learn.iterrows():
            tier = row.get("风险等级", "")
            tier_tag = f" [{tier}]" if tier else ""
            agent_tag = ""
            if row.get("Agent空仓") or row.get("Agent持有"):
                agent_tag = f" ｜Agent 空仓{row.get('Agent空仓','?')}/持有{row.get('Agent持有','?')}"
            lines.append(f"   ◇ {row['名称']} {row.get('现价','')} ｜距MA50 {row.get('距MA50','')}{tier_tag}{agent_tag}")
        lines.append("")

    # 接近支撑
    support = df[df["状态"] == "▲ 接近支撑"]
    if not support.empty:
        lines.append("—" * 20)
        lines.append("⚡ 接近支撑（关注反弹机会）：")
        for _, row in support.iterrows():
            agent_tag = ""
            if row.get("Agent空仓") or row.get("Agent持有"):
                agent_tag = f" ｜Agent 空仓{row.get('Agent空仓','?')}/持有{row.get('Agent持有','?')}"
            lines.append(f"   ▲ {row['名称']} {row.get('现价','')} ｜距MA50 {row.get('距MA50','')}{agent_tag}")
        lines.append("")

    # 突破提醒
    if "突破" in df.columns:
        breakout_df = df[df["突破"].astype(str).str.len() > 0]
        if not breakout_df.empty:
            lines.append("—" * 20)
            lines.append("⬆ MA50突破提醒（近期从弱转强）：")
            for _, row in breakout_df.iterrows():
                lines.append(f"   ⬆ {row['名称']} {row.get('现价','')} ｜距MA50 {row.get('距MA50','')} ｜{row['突破']}")
            lines.append("")

    # 多头排列
    bull = df[df["状态"] == "□ 多头排列"]
    if not bull.empty:
        lines.append("—" * 20)
        lines.append("💪 多头排列（趋势健康，持有为主）：")
        for _, row in bull.iterrows():
            entry_ok = str(row.get("多头入场", "")) == "是"
            entry_tag = "  ◎ 可先买一点" if entry_ok else ""
            lines.append(f"   □ {row['名称']} {row.get('现价','')} ｜距MA50 {row.get('距MA50','')}{entry_tag}")
        lines.append("")

    # 趋势完好
    good = df[df["状态"] == "- 趋势完好"]
    if not good.empty:
        lines.append("👍 趋势完好：")
        for _, row in good.iterrows():
            lines.append(f"   - {row['名称']} {row.get('现价','')} ｜距MA50 {row.get('距MA50','')}")
        lines.append("")

    # 临界观察
    crit = df[df["状态"] == "◈ 临界观察"]
    if not crit.empty:
        lines.append("—" * 20)
        lines.append("◈ 临界观察（结构接近转强，只差一步确认，未确认别追）：")
        for _, row in crit.iterrows():
            reason = row.get("临界原因", "")
            reason_tag = f" ｜{reason}" if reason else ""
            lines.append(f"   ◈ {row['名称']} {row.get('现价','')} ｜距MA50 {row.get('距MA50','')}{reason_tag}")
        lines.append("")

    # 趋势偏弱
    weak = df[df["状态"] == "✗ 趋势偏弱"]
    if not weak.empty:
        lines.append("—" * 20)
        lines.append("📉 趋势偏弱（暂时回避）：")
        for _, row in weak.iterrows():
            lines.append(f"   ✗ {row['名称']} {row.get('现价','')} ｜距MA50 {row.get('距MA50','')}")
        lines.append("")

    # 异常
    errors = df[~df["状态"].isin([s for s in STATUS_ORDER])]
    if not errors.empty:
        lines.append(f"⚠️ 数据异常 {len(errors)} 只：" + "、".join(errors["名称"].tolist()))
        lines.append("")

    # 持仓监控
    if holding_alerts:
        lines.append("—" * 20)
        lines.append(f"📊 持仓监控（{len(holding_alerts)} 只）：")
        lines.append("")
        for a in holding_alerts:
            lines.append(f"   {a['级别']} {a['ETF']}（{a['基金']}）")
            lines.append(f"     {a['信号变化']}  距MA50 {a['距MA50']}")
            lines.append(f"     → {a['建议']}")
            if a.get("Agent持有"):
                lines.append(f"     🤖 Agent 综合: {a['Agent持有']}"
                             + (f" ｜{a['Agent理由']}" if a.get("Agent理由") else ""))
        lines.append("")

    # 定投模块(脱敏：不显示金额)
    if dca and dca[1]:
        lines.append("—" * 20)
        lines.append("💰 定投模块：")
        lines.extend(_dca_lines(dca[0], dca[1], mask_amount=True))
        lines.append("")

    # 尾盘对比
    if compare:
        intraday = load_snapshot("intraday")
        if intraday:
            intra_map = {r["代码"]: r for r in intraday}
            flips, big = [], []
            for _, row in df.iterrows():
                intra = intra_map.get(row["代码"])
                if not intra or intra.get("现价") is None or row.get("现价") is None:
                    continue
                pchg = (row["现价"] - intra["现价"]) / intra["现价"] * 100
                if row.get("状态") != intra.get("状态"):
                    flips.append(f"{row['名称']}: {intra['状态']} → {row['状态']}")
                if abs(pchg) >= 1.0:
                    big.append(f"{row['名称']} {pchg:+.2f}%")
            if flips or big:
                lines.append("—" * 20)
                lines.append("🔍 尾盘对比（14:45 vs 收盘）：")
                if flips:
                    lines.append("   信号翻转: " + "；".join(flips))
                if big:
                    lines.append("   大幅波动: " + "；".join(big))
                lines.append("")

    # 尾部
    lines.append("—" * 20)
    lines.append("📌 信号说明：")
    lines.append("★ 低位确认：回踩MA50确认+放量站回短期均线+MA5拐头")
    lines.append("◇ 转强初期：短线转强但仍在MA50/MA100下方，可先买一点看看")
    lines.append("◆ 趋势跟随：右侧跟随或强势反转早期，可继续少量参与")
    lines.append("◈ 临界观察：结构接近转强但差一步确认，只盯不追")
    lines.append("□ 价格在所有均线之上，趋势健康")
    lines.append("⚠️ 仅供参考，不构成投资建议")
    lines.append("")
    lines.append(f"#ETF #基金定投 #A股 #技术分析 #{today_full}")

    # 写入文件
    LOG_DIR.mkdir(exist_ok=True)
    date_tag = datetime.now().strftime("%Y%m%d")
    log_path = LOG_DIR / f"scan_{date_tag}_xhs.txt"
    log_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  📕 小红书日志已保存: {log_path.name}")


def notify_feishu(df: pd.DataFrame, counts: dict, compare_report: bool = False,
                  holding_alerts: list = None, morning: bool = False, dca: tuple = None):
    """推送扫描摘要到飞书(消息卡片)"""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    env = {}
    for line in env_path.read_text().strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

    app_id = env.get("FEISHU_APP_ID")
    app_secret = env.get("FEISHU_APP_SECRET")
    chat_id = env.get("FEISHU_CHAT_ID")
    if not all([app_id, app_secret, chat_id]):
        return

    try:
        r = _requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret}, timeout=10,
        )
        resp = r.json()
        token = resp.get("tenant_access_token")
        if not token:
            print(f"  ⚠ 飞书token获取失败: {resp.get('msg', r.status_code)}")
            return

        today = datetime.now().strftime("%Y-%m-%d")
        ok = sum(counts.values())
        total = len(df)

        elements = []

        # 概览
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**★ 低位 {counts['★ 低位确认']}**  |  "
                    f"◆ 跟随 {counts['◆ 趋势跟随']}  |  "
                    f"◇ 转强 {counts['◇ 转强初期']}  |  "
                    f"▲ 支撑 {counts['▲ 接近支撑']}  |  "
                    f"□ 多头 {counts['□ 多头排列']}  |  "
                    f"- 完好 {counts['- 趋势完好']}  |  "
                    f"◈ 临界 {counts['◈ 临界观察']}  |  "
                    f"✗ 偏弱 {counts['✗ 趋势偏弱']}\n"
                    f"扫描 {total} 只，成功 {ok}，异常 {total - ok}"
                ),
            },
        })
        if "池子key" in df.columns:
            bucket_lines = ["**🎯 池子分层**"]
            for bucket in ("core", "watch", "dca"):
                group = df[df["池子key"] == bucket]
                if group.empty:
                    continue
                buy_n = int(group["状态"].isin(["★ 低位确认", "◆ 趋势跟随", "◇ 转强初期"]).sum())
                strong_n = int(group["状态"].isin(["★ 低位确认", "◆ 趋势跟随", "◇ 转强初期", "□ 多头排列", "- 趋势完好"]).sum())
                crit_n = int((group["状态"] == "◈ 临界观察").sum())
                weak_n = int((group["状态"] == "✗ 趋势偏弱").sum())
                crit_str = f"  临界{crit_n}" if crit_n else ""
                bucket_lines.append(
                    f"{ETF_BUCKET_LABELS.get(bucket, bucket)}：{len(group)}只  可参与{buy_n}  走强/完好{strong_n}{crit_str}  偏弱{weak_n}"
                )
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(bucket_lines)},
            })

        # 买入信号详情
        buy = df[df["状态"] == "★ 低位确认"]
        if not buy.empty:
            elements.append({"tag": "hr"})
            for _, row in buy.iterrows():
                assess = row.get("信号评估", "")
                otc = row.get("场外基金", "")
                level = assess.split("|")[0] if assess else ""
                reason = assess.split("|")[1] if "|" in assess else ""
                tier = row.get("风险等级", "")
                tier_tag = f"  **[{tier}]**" if tier else ""
                bucket_tag = f"[{row.get('池子', '')}] "
                md = (f"{bucket_tag}**{row['名称']}**  {row.get('现价','')}{tier_tag}\n"
                      f"距MA50 {row.get('距MA50','')}  量比 {row.get('量比','')}\n"
                      f"评估: {level}·{reason}")
                if row.get("Agent空仓") or row.get("Agent持有"):
                    md += f"\n🤖 Agent: 空仓{row.get('Agent空仓','?')} / 持有{row.get('Agent持有','?')}"
                    if row.get("Agent理由"):
                        md += f"\n综合理由: {row.get('Agent理由','')}"
                if otc:
                    md += f"  →  {otc}"
                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": md},
                })

        # 趋势跟随（非最佳低位，但技术面已可继续少量参与）
        hold = df[df["状态"] == "◆ 趋势跟随"]
        if not hold.empty:
            elements.append({"tag": "hr"})
            hlines = ["**◆ 趋势跟随**（趋势延续型买点，可继续少量参与）"]
            for _, row in hold.iterrows():
                tier = row.get("风险等级", "")
                tier_tag = f"  [{tier}]" if tier else ""
                otc = OTC_FUND.get(row["代码"])
                fund_str = f"  →  {otc[0]} {otc[1]}" if otc else ""
                bucket_tag = f"[{row.get('池子', '')}] "
                extra = ""
                if row.get("Agent空仓") or row.get("Agent持有"):
                    extra = f"  ｜Agent 空仓{row.get('Agent空仓','?')}/持有{row.get('Agent持有','?')}"
                held_tag = ""
                if row.get("已持仓"):
                    adv = _add_advice(_parse_dist(row.get("距MA50", "")), True)
                    held_tag = f"  ｜{adv}"
                hlines.append(
                    f"◆ {bucket_tag}{row['名称']}  {row.get('现价','')}  "
                    f"距MA50 {row.get('距MA50','')}{tier_tag}{extra}{held_tag}{fund_str}"
                )
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(hlines)},
            })

        learn = df[df["状态"] == "◇ 转强初期"]
        if not learn.empty:
            elements.append({"tag": "hr"})
            llines = ["**◇ 转强初期**（短线转强，但中期趋势未确认，可先买一点看看）"]
            for _, row in learn.iterrows():
                tier = row.get("风险等级", "")
                tier_tag = f"  [{tier}]" if tier else ""
                otc = OTC_FUND.get(row["代码"])
                fund_str = f"  →  {otc[0]} {otc[1]}" if otc else ""
                bucket_tag = f"[{row.get('池子', '')}] "
                extra = ""
                if row.get("Agent空仓") or row.get("Agent持有"):
                    extra = f"  ｜Agent 空仓{row.get('Agent空仓','?')}/持有{row.get('Agent持有','?')}"
                held_tag = ""
                if row.get("已持仓"):
                    adv = _add_advice(_parse_dist(row.get("距MA50", "")), True)
                    held_tag = f"  ｜{adv}"
                llines.append(
                    f"◇ {bucket_tag}{row['名称']}  {row.get('现价','')}  "
                    f"距MA50 {row.get('距MA50','')}{tier_tag}{extra}{held_tag}{fund_str}"
                )
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(llines)},
            })

        # 接近支撑摘要 + 试探建仓建议
        support = df[df["状态"] == "▲ 接近支撑"]
        if not support.empty:
            elements.append({"tag": "hr"})
            lines = ["**▲ 接近支撑**"]
            for _, row in support.iterrows():
                probe_val = str(row.get("试探", ""))
                otc = OTC_FUND.get(row["代码"])
                fund_str = f"  →  {otc[0]} {otc[1]}" if otc else ""
                bucket_tag = f"[{row.get('池子', '')}] "
                agent_tag = ""
                if row.get("Agent空仓") or row.get("Agent持有"):
                    agent_tag = f"  ｜Agent 空仓{row.get('Agent空仓','?')}/持有{row.get('Agent持有','?')}"
                if probe_val.startswith("◆"):
                    held = bool(row.get("已持仓"))
                    adv = _add_advice(_parse_dist(row.get("距MA50", "")), held)
                    buy_txt = adv if adv else "可以先买一点"
                    lines.append(
                        f"◆ {bucket_tag}**{row['名称']}**  距MA50 {row.get('距MA50','')}  "
                        f"量比 {row.get('量比','')}{agent_tag}\n"
                        f"已回踩验证  {probe_val.replace('◆ ', '')}"
                        f"  {buy_txt}{fund_str}"
                    )
                elif probe_val.startswith("◇"):
                    lines.append(
                        f"◇ {bucket_tag}**{row['名称']}**  距MA50 {row.get('距MA50','')}  "
                        f"量比 {row.get('量比','')}{agent_tag}\n"
                        f"支撑待验证  {probe_val.replace('◇ ', '')}"
                        f"  观望为主{fund_str}"
                    )
                else:
                    lines.append(f"  {bucket_tag}{row['名称']}  距MA50 {row.get('距MA50','')}{agent_tag}{fund_str}")
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(lines)},
            })

        # 多头排列摘要
        bull = df[df["状态"] == "□ 多头排列"]
        if not bull.empty:
            elements.append({"tag": "hr"})
            lines = ["**□ 多头排列**"]
            for _, row in bull.iterrows():
                otc = OTC_FUND.get(row["代码"])
                fund_str = f"  →  {otc[0]} {otc[1]}" if otc else ""
                bucket_tag = f"[{row.get('池子', '')}] "
                entry_ok = str(row.get("多头入场", "")) == "是"
                held = bool(row.get("已持仓"))
                dist_val_row = _parse_dist(row.get("距MA50", ""))
                if entry_ok and not held:
                    tier = "低位" if dist_val_row < 5 else "追涨"
                    entry_tag = f"  ◎ 可先买一点 [{tier}]"
                elif entry_ok and held:
                    entry_tag = "  ◎ 回踩确认，可加仓一点"
                else:
                    entry_tag = ""
                lines.append(f"□ {bucket_tag}{row['名称']}{entry_tag}{fund_str}")
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(lines),
                },
            })

        # 趋势完好摘要
        good = df[df["状态"] == "- 趋势完好"]
        if not good.empty:
            lines = ["**- 趋势完好**"]
            for _, row in good.iterrows():
                otc = OTC_FUND.get(row["代码"])
                fund_str = f"  →  {otc[0]} {otc[1]}" if otc else ""
                bucket_tag = f"[{row.get('池子', '')}] "
                lines.append(f"- {bucket_tag}{row['名称']}{fund_str}")
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(lines),
                },
            })

        # 临界观察摘要
        crit = df[df["状态"] == "◈ 临界观察"]
        if not crit.empty:
            lines = ["**◈ 临界观察**（结构接近转强，差一步确认，只盯不追）"]
            for _, row in crit.iterrows():
                otc = OTC_FUND.get(row["代码"])
                fund_str = f"  →  {otc[0]} {otc[1]}" if otc else ""
                bucket_tag = f"[{row.get('池子', '')}] "
                reason = row.get("临界原因", "")
                reason_tag = f"  {reason}" if reason else ""
                lines.append(f"◈ {bucket_tag}{row['名称']} {row.get('距MA50','')}{reason_tag}{fund_str}")
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(lines),
                },
            })

        # 趋势偏弱摘要
        weak = df[df["状态"] == "✗ 趋势偏弱"]
        if not weak.empty:
            lines = ["**✗ 趋势偏弱**"]
            for _, row in weak.iterrows():
                otc = OTC_FUND.get(row["代码"])
                fund_str = f"  →  {otc[0]} {otc[1]}" if otc else ""
                bucket_tag = f"[{row.get('池子', '')}] "
                lines.append(f"✗ {bucket_tag}{row['名称']}{fund_str}")
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(lines),
                },
            })

        # 持仓监控
        if holding_alerts:
            elements.append({"tag": "hr"})
            lines = ["**📊 持仓监控**"]
            for a in holding_alerts:
                _agent_line = ""
                if a.get("Agent持有"):
                    _agent_line = f"\n🤖 Agent 综合: {a['Agent持有']}" + (
                        f" ｜{a['Agent理由']}" if a.get("Agent理由") else "")
                lines.append(
                    f"{a['级别']} **{a['ETF']}**  "
                    f"{a['信号变化']}  距MA50 {a['距MA50']}\n"
                    f"→ {a['建议']}{_agent_line}"
                )
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(lines)},
            })

        # 定投模块(飞书私人推送：显示金额，不脱敏)
        if dca and dca[1]:
            win_d, items_d = dca
            marks = {"done": "✅已投", "buy": "🟢可投", "deadline": "🔔必投",
                     "wait": "⬜未投", "expired": "⚠️已过", "nodata": "❓"}
            elements.append({"tag": "hr"})
            lines = ["**💰 定投模块**"]
            for it in items_d:
                m = it.get("metrics")
                mark = marks.get(it["decision"], "⬜")
                lines.append(f"{mark} **{it['name']}** {it['etf']}(A类{it['fund']})")
                lines.append(f"窗口{win_d['label']} {win_d['range']} · 剩{max(win_d['remain'],0)}天到兜底")
                if m:
                    lines.append(f"现价{m['现价']} | 分位{m['分位']}% | 距MA20 {m['距MA20']:+.1f}% | {m['趋势']}")
                    if it.get("parts"):
                        lines.append(f"评分{it['score']}/100 | 触发线{it['threshold']}")
                lines.append(f"→ {it['advice']}")
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(lines)},
            })

        # 尾盘对比摘要
        if compare_report:
            intraday = load_snapshot("intraday")
            if intraday:
                intra_map = {r["代码"]: r for r in intraday}
                flips, big = [], []
                for _, row in df.iterrows():
                    intra = intra_map.get(row["代码"])
                    if not intra or intra.get("现价") is None or row.get("现价") is None:
                        continue
                    pchg = (row["现价"] - intra["现价"]) / intra["现价"] * 100
                    if row.get("状态") != intra.get("状态"):
                        flips.append(f"{row['名称']}: {intra['状态']} → {row['状态']}")
                    if abs(pchg) >= 1.0:
                        big.append(f"{row['名称']} {pchg:+.2f}%")

                if flips or big:
                    elements.append({"tag": "hr"})
                    lines = ["**🔍 尾盘对比 (14:45 vs 收盘)**"]
                    if flips:
                        lines.append("信号翻转: " + "；".join(flips))
                    if big:
                        lines.append("大幅波动: " + "；".join(big))
                    if not flips and not big:
                        lines.append("尾盘平稳，无显著异动")
                    elements.append({
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": "\n".join(lines)},
                    })

        # 早盘概览卡片
        if morning and "早盘涨跌" in df.columns:
            df_m = df.copy()
            df_m["_mchg"] = df_m["早盘涨跌"].apply(
                lambda x: float(x.rstrip("%").replace("+", "")) if isinstance(x, str) else float("nan")
            )
            valid = df_m[df_m["_mchg"].notna()]
            if not valid.empty:
                up_n = int((valid["_mchg"] > 0).sum())
                dn_n = int((valid["_mchg"] < 0).sum())
                top3 = valid.nlargest(3, "_mchg")
                bot3 = valid.nsmallest(3, "_mchg")
                elements.append({"tag": "hr"})
                lines = [f"**🌅 早盘概览**  ↑{up_n} ↓{dn_n}"]
                lines.append("领涨: " + "  ".join(
                    f"{r['名称']} {r['早盘涨跌']}" for _, r in top3.iterrows()))
                lines.append("领跌: " + "  ".join(
                    f"{r['名称']} {r['早盘涨跌']}" for _, r in bot3.iterrows()))
                if "早盘量能" in df.columns:
                    hot = df_m[df_m["早盘量能"] > 2.0].sort_values("早盘量能", ascending=False)
                    if not hot.empty:
                        lines.append("量能异常: " + "  ".join(
                            f"{r['名称']}({r['早盘量能']}x)" for _, r in hot.iterrows()))
                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "\n".join(lines)},
                })

        title_tag = "早盘分析" if morning else ("盘中信号" if _is_trading_hours() else "扫描报告")
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"🔭 Argus {title_tag} {today}"},
                "template": "red" if not buy.empty else "blue",
            },
            "elements": elements,
        }

        _requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}"},
            json={"receive_id": chat_id, "msg_type": "interactive",
                  "content": json.dumps(card)},
            timeout=10,
        )
        print("  📨 已推送飞书")
    except Exception as e:
        print(f"  ⚠ 飞书推送失败: {e}")


if __name__ == "__main__":
    main()
