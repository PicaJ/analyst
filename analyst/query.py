"""
查询接口 — 通过 indexagent SDK 检索新闻数据

analyst 不直接操作 SQLite/FAISS，而是通过 indexagent 的 Python SDK 检索。
支持向量语义搜索、关键词搜索和混合搜索。
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from .config import AnalystConfig

# 将 indexagent 项目根目录加入 sys.path
_indexagent_root = str(Path(__file__).resolve().parent.parent.parent / "indexagent")
if _indexagent_root not in sys.path:
    sys.path.insert(0, _indexagent_root)

from indexagent.sdk import IndexAgent


class NewsQuery:
    """新闻查询接口 — 封装 indexagent SDK

    对外保持与旧版兼容的方法签名，内部全部委托给 IndexAgent。
    新增 search_hybrid() 方法支持向量+关键词混合检索。
    """

    def __init__(self, config: AnalystConfig):
        self.config = config
        self._agent = IndexAgent(data_dir=str(config.data_dir))

    async def search_hybrid(
        self,
        query: str,
        top_k: int = 50,
        days: int | None = None,
        alpha: float | None = None,
    ) -> List[Dict[str, Any]]:
        """混合检索: 向量语义 + 关键词，按相关性排序

        alpha: 向量权重 (0=纯关键词, 1=纯向量, 默认用配置值)
        """
        if alpha is None:
            alpha = getattr(self.config, "hybrid_alpha", 0.7)
        kwargs = {
            "query": query,
            "mode": "hybrid",
            "top_k": top_k,
            "alpha": alpha,
        }
        if days:
            cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
            kwargs["start_time"] = cutoff
        logger.debug("混合检索 | query={} alpha={} top_k={}", query, alpha, top_k)
        return await self._agent.search(**kwargs)

    async def get_by_ids(self, news_ids: List[str]) -> List[Dict[str, Any]]:
        """按 ID 批量获取"""
        return await self._agent.get_by_ids(news_ids)

    async def get_by_time_range(
        self,
        start: str,
        end: str,
        source: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        """时间范围查询"""
        if limit <= 0:
            limit = self.config.query_limit_time_range
        return await self._agent.search_by_time(
            start_time=start, end_time=end,
            source=source, category=category,
            limit=limit,
        )

    async def get_by_entity(
        self,
        company: Optional[str] = None,
        sector: Optional[str] = None,
        person: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        """实体查询"""
        if limit <= 0:
            limit = self.config.query_limit_entity
        return await self._agent.search_by_entity(
            company=company, sector=sector, person=person,
            start_time=start, end_time=end,
            limit=limit,
        )

    async def get_urgent(
        self,
        days: int = 7,
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        """获取近期高优先级/紧急新闻"""
        if limit <= 0:
            limit = self.config.query_limit_urgent
        return await self._agent.get_urgent(days=days, limit=limit)

    async def get_timeline(
        self,
        keywords: Optional[List[str]] = None,
        ts_code: Optional[str] = None,
        days: int = 90,
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        """获取某个主题的时间线 (用于线索链构建)"""
        if limit <= 0:
            limit = self.config.query_limit_timeline
        return await self._agent.get_timeline(
            keywords=keywords, ts_code=ts_code,
            days=days, limit=limit,
        )
