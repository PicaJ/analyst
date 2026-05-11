"""
龙虎榜机构席位数据 — 从东方财富获取龙虎榜机构买卖汇总

数据源: AKShare stock_lhb_jgmmtj_em (机构买卖每日统计)
缓存策略: 本地 JSON 缓存, 龙虎榜数据日更, 缓存 4 小时有效
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger


_CACHE_FILENAME = "lhb_cache.json"
_CACHE_MAX_AGE_HOURS = 4

# 模块级内存缓存
_lhb_data: Optional[Dict[str, Dict]] = None
_lhb_fetched_at: Optional[datetime] = None


def _cache_path(data_dir: Path) -> Path:
    return data_dir / _CACHE_FILENAME


def _load_cache(data_dir: Path) -> Optional[Dict[str, Dict]]:
    path = _cache_path(data_dir)
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        saved = datetime.fromisoformat(d["fetched_at"])
        if (datetime.now() - saved).total_seconds() < _CACHE_MAX_AGE_HOURS * 3600:
            logger.info("Using cached LHB data (fetched at {})", d["fetched_at"][:19])
            return d["data"]
    except Exception:
        pass
    return None


def _save_cache(data_dir: Path, data: Dict[str, Dict]):
    path = _cache_path(data_dir)
    path.write_text(
        json.dumps({"fetched_at": datetime.now().isoformat(), "data": data},
                    ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Cached LHB data: {} stocks", len(data))


def _format_amount(amount: float) -> str:
    if abs(amount) >= 1e8:
        return f"{amount / 1e8:.2f}亿"
    elif abs(amount) >= 1e4:
        return f"{amount / 1e4:.0f}万"
    else:
        return f"{amount:.0f}元"


def fetch_lhb_summary(data_dir: str = "", days: int = 30) -> Dict[str, Dict]:
    """批量获取全市场龙虎榜机构买卖汇总

    返回 dict: pure_code (如 "002027") → { lhb_date, buy_inst_count, ... }
    同一股票多次上榜取最近一次。
    """
    global _lhb_data, _lhb_fetched_at

    # 内存缓存
    if _lhb_data and _lhb_fetched_at:
        if (datetime.now() - _lhb_fetched_at).total_seconds() < _CACHE_MAX_AGE_HOURS * 3600:
            return _lhb_data

    # 文件缓存
    dir_path = Path(data_dir) if data_dir else Path(".")
    cached = _load_cache(dir_path)
    if cached:
        _lhb_data = cached
        _lhb_fetched_at = datetime.now()
        return _lhb_data

    # AKShare 获取
    try:
        import akshare as ak
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        df = ak.stock_lhb_jgmmtj_em(start_date=start_date, end_date=end_date)
    except Exception as e:
        logger.warning("Failed to fetch LHB data: {}", e)
        return _lhb_data or {}

    # 构建 dict: 按日期倒序，后出现的覆盖前面的 → 取最近一次
    result: Dict[str, Dict] = {}
    for _, row in df.iterrows():
        code = str(row.get("代码", "")).strip()
        if not code:
            continue
        net = float(row.get("机构买入净额", 0))
        result[code] = {
            "lhb_date": str(row.get("上榜日期", "")),
            "name": str(row.get("名称", "")),
            "buy_inst_count": int(row.get("买方机构数", 0)),
            "sell_inst_count": int(row.get("卖方机构数", 0)),
            "buy_amount": float(row.get("机构买入总额", 0)),
            "sell_amount": float(row.get("机构卖出总额", 0)),
            "net_amount": net,
            "net_amount_str": _format_amount(net),
            "reason": str(row.get("上榜原因", "")),
        }

    # 保存缓存
    _save_cache(dir_path, result)
    _lhb_data = result
    _lhb_fetched_at = datetime.now()
    logger.info("Fetched LHB data: {} stocks in {} days", len(result), days)
    return result


def get_lhb_for_stock(pure_code: str, data_dir: str = "") -> Optional[Dict]:
    """查单只股票的龙虎榜数据

    Args:
        pure_code: 纯数字代码 (如 "688041")
        data_dir: 数据目录
    """
    data = fetch_lhb_summary(data_dir)
    return data.get(pure_code)
