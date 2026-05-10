"""
A股散户排名 — 从东方财富获取股东户数数据 + 大股东/散户占比

数据来源: 东方财富 datacenter API (RPT_HOLDERNUMLATEST) + AKShare (stock_main_stock_holder)
缓存策略: 本地 JSON 缓存，数据每季度更新，缓存 7 天有效
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger


_CACHE_FILENAME = "top_holders_cache.json"
_HOLDER_RATIO_CACHE = "holder_ratio_cache.json"


def _cache_path(data_dir: Path, filename: str = _CACHE_FILENAME) -> Path:
    return data_dir / filename


def _load_cache(data_dir: Path, max_age_days: int = 7,
                filename: str = _CACHE_FILENAME) -> Optional[List[Dict[str, Any]]]:
    path = _cache_path(data_dir, filename)
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        saved = datetime.fromisoformat(d["fetched_at"])
        if (datetime.now() - saved).days < max_age_days:
            logger.info("Using cached holder data (fetched at {})", d["fetched_at"][:19])
            return d["data"]
    except Exception:
        pass
    return None


def _save_cache(data_dir: Path, data: List[Dict[str, Any]],
                filename: str = _CACHE_FILENAME):
    path = _cache_path(data_dir, filename)
    path.write_text(
        json.dumps({"fetched_at": datetime.now().isoformat(), "data": data},
                    ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Cached holder data: {} stocks", len(data))


def _fetch_holder_ratio(pure_code: str) -> Optional[Dict[str, float]]:
    """获取个股大股东/散户持股占比 (基于前10大股东)"""
    try:
        import akshare as ak
        df = ak.stock_main_stock_holder(stock=pure_code)
        if df is None or df.empty:
            return None
        # 取最新一期
        latest = df.iloc[0]
        total_holders = latest.get("股东总数", 0)
        # 前10大股东持股比例之和
        top_ratio = 0.0
        for _, row in df.head(10).iterrows():
            ratio = float(row.get("持股比例", 0) or 0)
            top_ratio += ratio
        top_ratio = min(top_ratio, 100.0)
        retail_ratio = round(100.0 - top_ratio, 2)
        return {
            "major_holder_pct": round(top_ratio, 2),
            "retail_holder_pct": retail_ratio,
        }
    except Exception as e:
        logger.debug("Failed to fetch holder ratio for {}: {}", pure_code, e)
        return None


def _load_ratio_cache(data_dir: Path) -> Dict[str, Dict]:
    """加载股东占比缓存"""
    path = _cache_path(data_dir, _HOLDER_RATIO_CACHE)
    if path.exists():
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            saved = datetime.fromisoformat(d["fetched_at"])
            if (datetime.now() - saved).days < 7:
                return d.get("data", {})
        except Exception:
            pass
    return {}


def _save_ratio_cache(data_dir: Path, data: Dict[str, Dict]):
    """保存股东占比缓存"""
    path = _cache_path(data_dir, _HOLDER_RATIO_CACHE)
    path.write_text(
        json.dumps({"fetched_at": datetime.now().isoformat(), "data": data},
                    ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def fetch_top_holders(top_n: int = 50, data_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """获取 A 股股东户数排名（散户最多的股票）

    Returns:
        按 HOLDER_NUM 降序排列的列表，每项包含:
          code, name, end_date, holder_num, holder_change, close_price,
          major_holder_pct, retail_holder_pct
    """
    # 尝试缓存
    if data_dir:
        cache = _load_cache(Path(data_dir))
        if cache:
            return cache[:top_n]

    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_HOLDERNUMLATEST",
        "columns": "ALL",
        "pageNumber": "1",
        "pageSize": str(min(top_n, 200)),
        "sortTypes": "-1",
        "sortColumns": "HOLDER_NUM",
        "source": "WEB",
        "client": "WEB",
    }

    try:
        resp = httpx.get(url, params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        logger.error("Failed to fetch holder data: {}", e)
        return []

    if not body.get("success") or not body.get("result", {}).get("data"):
        logger.warning("EastMoney API returned no data")
        return []

    # 加载占比缓存
    ratio_cache = _load_ratio_cache(Path(data_dir)) if data_dir else {}

    rows = body["result"]["data"]
    result = []
    for r in rows:
        end_date = r.get("END_DATE", "")[:10]
        close_price = r.get("CLOSE_PRICE")
        code = r.get("SECURITY_CODE", "")

        # 获取大股东/散户占比
        major_pct = None
        retail_pct = None
        if code in ratio_cache:
            major_pct = ratio_cache[code].get("major_holder_pct")
            retail_pct = ratio_cache[code].get("retail_holder_pct")
        else:
            ratio = _fetch_holder_ratio(code)
            if ratio:
                major_pct = ratio["major_holder_pct"]
                retail_pct = ratio["retail_holder_pct"]
                if data_dir:
                    ratio_cache[code] = ratio

        result.append({
            "code": code,
            "name": r.get("SECURITY_NAME_ABBR", ""),
            "end_date": end_date,
            "holder_num": r.get("HOLDER_NUM", 0),
            "holder_change": r.get("HOLDER_NUM_CHANGE"),
            "avg_market_cap": r.get("AVG_MARKET_CAP"),
            "close_price": close_price,
            "major_holder_pct": major_pct,
            "retail_holder_pct": retail_pct,
        })

    # 保存占比缓存
    if data_dir and ratio_cache:
        _save_ratio_cache(Path(data_dir), ratio_cache)

    # 缓存
    if data_dir:
        _save_cache(Path(data_dir), result)

    return result[:top_n]
