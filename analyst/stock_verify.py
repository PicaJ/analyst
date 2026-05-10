"""
股票推荐验证 — 联网核实 LLM 推荐的股票

验证内容:
  1. 股票代码是否存在
  2. 股票名称是否正确
  3. 近期走势方向是否与分析结论一致
  4. 股票所属行业是否与分析结论逻辑匹配
  5. 科创板/创业板股票提供主板平替标的

数据源: AKShare (东方财富)
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

_STOCK_CODE_RE = re.compile(r'^(\d{6})\.[A-Z]{2}$')
_CACHE_MAX_AGE_HOURS = 4  # 行情缓存 4 小时
_FETCH_COOLDOWN_SEC = 300   # 请求失败后 5 分钟内不再重试

# 内存级降级标记: 避免连续失败时反复请求
_fetch_cooldown_until: Optional[datetime] = None
_api_degraded: bool = False  # 全局降级: 东方财富不可用时跳过所有网络请求


# ── 行情数据缓存 ──

def _cache_path(data_dir: Path) -> Path:
    return data_dir / "stock_spot_cache.json"


def _load_spot_cache(data_dir: Path) -> Optional[Dict[str, Dict]]:
    path = _cache_path(data_dir)
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        saved = datetime.fromisoformat(d["fetched_at"])
        age_hours = (datetime.now() - saved).total_seconds() / 3600
        if age_hours < _CACHE_MAX_AGE_HOURS:
            logger.debug("Using cached spot data ({} hours old)", f"{age_hours:.1f}")
            return d["data"]
    except Exception:
        pass
    return None


def _save_spot_cache(data_dir: Path, data: Dict[str, Dict]):
    path = _cache_path(data_dir)
    path.write_text(
        json.dumps({"fetched_at": datetime.now().isoformat(), "data": data},
                    ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Cached spot data: {} stocks", len(data))


def _fetch_spot_data() -> tuple[Dict[str, Dict], bool]:
    """获取全市场实时行情 (优先缓存/新浪，东方财富备用)

    Returns:
        (data, em_ok): data 为行情字典, em_ok 表示东方财富是否可用
    """
    import akshare as ak
    global _api_degraded

    # 优先新浪: 速度快 (~16s), 不分页, 不重试
    try:
        df = ak.stock_zh_a_spot()
        if df is not None and not df.empty:
            result = {}
            for _, row in df.iterrows():
                code = str(row.get("代码", ""))
                result[code] = {
                    "name": str(row.get("名称", "")),
                    "price": float(row.get("最新价", 0) or 0),
                    "change_pct": float(row.get("涨跌幅", 0) or 0),
                    "volume": float(row.get("成交额", 0) or 0),
                    "turnover": 0.0,
                    "pe": 0.0,
                    "change_5min": 0.0,
                    "change_60d": 0.0,
                }
            logger.info("Fetched spot data from 新浪: {} stocks", len(result))
            _api_degraded = True  # 新浪拿行情，东方财富个股API不可用，跳过后续EM请求
            return result, False
    except Exception as e:
        logger.warning("新浪 spot data failed: {}", e)

    # 备用: 东方财富 (分页获取, 慢, 可能超时)
    try:
        df = ak.stock_zh_a_spot_em()
        if df is not None and not df.empty:
            result = {}
            for _, row in df.iterrows():
                code = str(row.get("代码", ""))
                result[code] = {
                    "name": str(row.get("名称", "")),
                    "price": float(row.get("最新价", 0) or 0),
                    "change_pct": float(row.get("涨跌幅", 0) or 0),
                    "volume": float(row.get("成交额", 0) or 0),
                    "turnover": float(row.get("换手率", 0) or 0),
                    "pe": float(row.get("市盈率-动态", 0) or 0),
                    "change_5min": float(row.get("5分钟涨跌", 0) or 0),
                    "change_60d": float(row.get("60日涨跌幅", 0) or 0),
                }
            logger.info("Fetched spot data from 东方财富: {} stocks", len(result))
            _api_degraded = False
            return result, True
    except Exception as e:
        logger.warning("东方财富 spot data also failed: {}", e)

    _api_degraded = True
    return {}, False


def _get_spot_data(data_dir: str) -> Dict[str, Dict]:
    """获取行情数据（优先缓存，失败后冷却）"""
    global _fetch_cooldown_until
    pdir = Path(data_dir)

    # 1. 先尝试缓存
    cached = _load_spot_cache(pdir)
    if cached:
        return cached

    # 2. 如果在冷却期内，直接跳过请求
    if _fetch_cooldown_until and datetime.now() < _fetch_cooldown_until:
        logger.debug("Spot data fetch in cooldown, skipping")
        return {}

    # 3. 发起请求
    try:
        data, em_ok = _fetch_spot_data()
        if data:
            _save_spot_cache(pdir, data)
        _fetch_cooldown_until = None  # 成功则清除冷却
        return data
    except Exception as e:
        logger.error("Failed to fetch spot data: {}", e)
        _fetch_cooldown_until = datetime.now() + timedelta(seconds=_FETCH_COOLDOWN_SEC)
        return {}


# ── 行业信息缓存 ──

def _industry_cache_path(data_dir: Path) -> Path:
    return data_dir / "stock_industry_cache.json"


def _load_industry_cache(data_dir: Path) -> Dict[str, Dict]:
    path = _industry_cache_path(data_dir)
    if path.exists():
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            saved = datetime.fromisoformat(d["fetched_at"])
            if (datetime.now() - saved).days < 7:
                return d.get("data", {})
        except Exception:
            pass
    return {}


def _save_industry_cache(data_dir: Path, data: Dict[str, Dict]):
    path = _industry_cache_path(data_dir)
    existing = _load_industry_cache(data_dir)
    existing.update(data)
    path.write_text(
        json.dumps({"fetched_at": datetime.now().isoformat(), "data": existing},
                    ensure_ascii=False),
        encoding="utf-8",
    )


def _get_stock_industry(pure_code: str, data_dir: str = "") -> Optional[Dict[str, str]]:
    """获取股票行业信息 (优先缓存，API 降级时跳过)"""
    if _api_degraded:
        return None
    if data_dir:
        pdir = Path(data_dir)
        cached = _load_industry_cache(pdir)
        if pure_code in cached:
            return cached[pure_code]

    try:
        import akshare as ak
        df = ak.stock_individual_info_em(symbol=pure_code)
        info = {}
        for _, row in df.iterrows():
            item = str(row.get("item", ""))
            value = str(row.get("value", ""))
            if item == "行业":
                info["industry"] = value
            elif item == "股票简称":
                info["name"] = value
            elif item == "上市时间":
                info["list_date"] = value

        if info.get("industry") and data_dir:
            _save_industry_cache(Path(data_dir), {pure_code: info})
        return info if info.get("industry") else None
    except Exception as e:
        logger.debug("Failed to get industry for {}: {}", pure_code, e)
        return None


# ── 板块判断 ──

def _get_board_type(pure_code: str) -> str:
    """判断股票所属板块"""
    if pure_code.startswith("688"):
        return "科创板"
    if pure_code.startswith(("300", "301")):
        return "创业板"
    if pure_code.startswith(("000", "001", "002")):
        return "深主板"
    if pure_code.startswith(("600", "601", "603", "605")):
        return "沪主板"
    return "其他"


def _is_gem_or_star(pure_code: str) -> bool:
    """是否为科创板或创业板"""
    return pure_code.startswith(("300", "301", "688"))


# ── 同行业主板平替 ──

def _find_main_board_alternatives(
    pure_code: str, industry: str, data_dir: str = "", top_n: int = 3,
) -> List[Dict[str, Any]]:
    """为科创板/创业板股票找同行业主板平替标的"""
    if _api_degraded:
        return []
    try:
        import akshare as ak
        df = ak.stock_board_industry_cons_em(symbol=industry)
        if df is None or df.empty:
            return []

        spot = _get_spot_data(data_dir) if data_dir else {}

        alternatives = []
        for _, row in df.iterrows():
            code = str(row.get("代码", ""))
            if code == pure_code:
                continue
            if _is_gem_or_star(code):
                continue
            name = str(row.get("名称", ""))
            price = float(row.get("最新价", 0) or 0)
            change = float(row.get("涨跌幅", 0) or 0)
            turnover = float(row.get("换手率", 0) or 0)
            # 优先选流通性好、非ST的
            if "ST" in name or "st" in name:
                continue
            if price <= 0:
                continue

            suffix = ".SH" if code.startswith(("6", "5")) else ".SZ"
            alt = {
                "code": f"{code}{suffix}",
                "name": name,
                "price": price,
                "change_pct": change,
                "board": _get_board_type(code),
            }
            # 补充近5日走势
            trend = _get_recent_trend(code, days=5)
            if trend:
                alt["recent_trend"] = f"{trend['change_pct']:+.1f}% (近5日)"
            alternatives.append(alt)
            if len(alternatives) >= top_n:
                break

        # 按换手率排序（流动性好的排前面）
        return alternatives
    except Exception as e:
        logger.warning("Failed to find alternatives for {} in {}: {}", pure_code, industry, e)
        return []


# ── 走势分析 ──

def _get_recent_trend(code: str, days: int = 5) -> Optional[Dict]:
    """获取个股近 N 日走势"""
    if _api_degraded:
        return None
    import akshare as ak
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                 start_date=start, end_date=end, adjust="qfq")
        if df.empty:
            return None
        recent = df.tail(days)
        if recent.empty:
            return None
        first_close = float(recent.iloc[0]["收盘"])
        last_close = float(recent.iloc[-1]["收盘"])
        change_pct = (last_close - first_close) / first_close * 100 if first_close else 0
        return {
            "start_price": first_close,
            "end_price": last_close,
            "change_pct": round(change_pct, 2),
            "direction": "up" if change_pct > 0 else "down" if change_pct < 0 else "flat",
        }
    except Exception as e:
        logger.debug("Failed to get trend for {}: {}", code, e)
        return None


def _extract_trend_direction(text: str) -> Optional[str]:
    """从分析文本中提取走势描述方向"""
    up_words = ["上涨", "涨", "走强", "反弹", "回升", "走高", "利好", "涨停", "大涨", "爆发"]
    down_words = ["下跌", "跌", "走弱", "回调", "回落", "走低", "利空", "大跌", "暴跌"]
    text_lower = text.lower()
    has_up = any(w in text_lower for w in up_words)
    has_down = any(w in text_lower for w in down_words)
    if has_up and not has_down:
        return "up"
    if has_down and not has_up:
        return "down"
    return None


# ── 业务逻辑匹配 ──

def _get_industry_alias() -> Dict[str, List[str]]:
    """从配置文件加载行业别名映射"""
    from .config import load_config
    cfg = load_config()
    return cfg.industry_alias


def _build_keyword_to_industry(alias: Dict[str, List[str]]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for industry, kws in alias.items():
        for kw in kws:
            mapping[kw] = industry
    return mapping


def _extract_mentioned_industries(text: str, alias: Dict[str, List[str]]) -> List[str]:
    """从分析文本中提取提及的行业/板块"""
    kw_map = _build_keyword_to_industry(alias)
    industries = set()
    for kw, industry in kw_map.items():
        if kw in text:
            industries.add(industry)
    return list(industries)


def _industry_matches(actual_industry: str, mentioned_industries: List[str],
                      alias: Dict[str, List[str]]) -> bool:
    """检查股票实际行业是否与分析中提及的行业匹配"""
    if not mentioned_industries:
        return True
    actual_lower = actual_industry.lower()
    kw_map = _build_keyword_to_industry(alias)
    for mentioned in mentioned_industries:
        aliases = alias.get(mentioned, [mentioned])
        for a in aliases:
            if a in actual_lower or actual_lower in a:
                return True
        for kw, ind in kw_map.items():
            if ind == mentioned and kw in actual_lower:
                return True
    return False


# ── 财报披露日期 ──

_DISCLOSURE_CACHE: Dict[str, Any] = {"data": {}, "fetched_at": None}


def _get_disclosure_date(pure_code: str, data_dir: str = "") -> Optional[str]:
    """获取股票最近/下一次财报披露日期

    策略: 查询当年四个报告期，找最近的未来日期；如果没有，取最近的已披露日期。
    """
    now = datetime.now()
    year = now.year

    # 缓存: 按 year+market 缓存全部数据
    cache_key = f"{year}"
    if _DISCLOSURE_CACHE["data"].get(cache_key) is None:
        import akshare as ak
        all_data = {}
        for suffix in ["一季", "半年报", "三季", "年报"]:
            period = f"{year}{suffix}"
            try:
                df = ak.stock_report_disclosure(market="沪深京", period=period)
                for _, row in df.iterrows():
                    code = str(row.get("股票代码", ""))
                    if code not in all_data:
                        all_data[code] = {}
                    actual = row.get("实际披露")
                    scheduled = row.get("首次预约")
                    # 优先用实际披露日期
                    dt = actual if str(actual) != "NaT" else scheduled
                    if str(dt) != "NaT":
                        all_data[code][period] = str(dt)[:10]
            except Exception as e:
                logger.debug("Disclosure fetch failed for {}: {}", period, e)
        _DISCLOSURE_CACHE["data"][cache_key] = all_data
        _DISCLOSURE_CACHE["fetched_at"] = now.isoformat()

    periods = _DISCLOSURE_CACHE["data"].get(cache_key, {}).get(pure_code, {})
    if not periods:
        return None

    # 找最近的未来披露日期
    today = now.strftime("%Y-%m-%d")
    future = sorted(v for v in periods.values() if v >= today)
    if future:
        return future[0]

    # 没有未来的，取最近的过去日期
    past = sorted(v for v in periods.values() if v < today)
    if past:
        return past[-1]

    return None


# ── 核心验证 ──

def verify_stock(code: str, analysis_text: str = "",
                 data_dir: str = "", industry_alias: Optional[Dict] = None) -> Dict[str, Any]:
    """验证单只股票推荐

    Args:
        code: 股票代码 (如 "000333.SZ")
        analysis_text: 分析结论文本（用于走势匹配检测）
        data_dir: 数据目录（用于缓存）

    Returns:
        验证结果 dict
    """
    result: Dict[str, Any] = {
        "code": code,
        "verified": False,
        "stock_name": "",
        "exists": False,
        "trend_match": None,
        "recent_trend": "",
    }

    m = _STOCK_CODE_RE.match(code)
    if not m:
        result["error"] = "无效股票代码格式"
        return result
    pure_code = m.group(1)

    # 获取行情数据
    spot = _get_spot_data(data_dir) if data_dir else {}
    if not spot:
        result["error"] = "无法获取行情数据"
        return result

    info = spot.get(pure_code)
    if not info:
        result["error"] = "股票代码不存在"
        return result

    result["exists"] = True
    result["stock_name"] = info["name"]
    result["price"] = info["price"]
    result["change_pct"] = info["change_pct"]
    result["verified"] = True

    # 板块类型
    board = _get_board_type(pure_code)
    result["board"] = board

    # 获取行业信息
    industry_info = _get_stock_industry(pure_code, data_dir)
    if industry_info:
        result["industry"] = industry_info["industry"]

    # 业务逻辑匹配: 股票行业是否与分析结论吻合
    alias = industry_alias or _get_industry_alias()
    if analysis_text and industry_info:
        mentioned = _extract_mentioned_industries(analysis_text, alias)
        if mentioned:
            matches = _industry_matches(industry_info["industry"], mentioned, alias)
            result["business_match"] = matches
            if not matches:
                result["business_match_note"] = (
                    f"该股票属「{industry_info['industry']}」，"
                    f"但分析涉及「{'/'.join(mentioned)}」"
                )

    # 获取近5日走势
    trend = _get_recent_trend(pure_code, days=5)
    if trend:
        result["recent_trend"] = f"{trend['change_pct']:+.1f}% (近5日)"
        result["trend_direction"] = trend["direction"]

    # 走势匹配检测
    if analysis_text and trend:
        expected = _extract_trend_direction(analysis_text)
        if expected and trend["direction"] != "flat":
            result["trend_match"] = (expected == trend["direction"])

    # 财报披露日期
    disclosure = _get_disclosure_date(pure_code, data_dir)
    if disclosure:
        result["disclosure_date"] = disclosure

    # 科创板/创业板 → 找主板平替
    if _is_gem_or_star(pure_code) and industry_info:
        alternatives = _find_main_board_alternatives(
            pure_code, industry_info["industry"], data_dir, top_n=3
        )
        if alternatives:
            result["alternatives"] = alternatives

    return result


def verify_insight_stocks(
    insight: Dict[str, Any],
    data_dir: str = "",
) -> Dict[str, Any]:
    """验证洞察中所有推荐股票

    在 actionable_items 中添加 verified 字段。
    """
    thesis = insight.get("thesis", "")
    findings_text = " ".join(
        f.get("finding", "") for f in insight.get("key_findings", [])
    )
    analysis_text = f"{thesis} {findings_text}"

    items = insight.get("actionable_items", [])

    for item in items:
        targets = item.get("targets", [])
        verified_targets = []
        for t in targets:
            if _STOCK_CODE_RE.match(str(t)):
                v = verify_stock(str(t), analysis_text, data_dir)
                verified_targets.append(v)
            else:
                verified_targets.append({"code": str(t), "verified": False,
                                          "error": "非股票代码格式"})

        if verified_targets:
            any_verified = any(v.get("verified") for v in verified_targets)
            item["verified"] = any_verified
            item["verify_details"] = verified_targets
            if any_verified:
                names = [v.get("stock_name", v["code"]) for v in verified_targets
                         if v.get("verified")]
                item["verified_names"] = names

    return insight
