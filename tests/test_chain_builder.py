"""测试: 线索链构建器"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from analyst.chain_builder import ChainBuilder, ChainNode, ChainLink, ClueChain
from analyst.config import AnalystConfig


@pytest.fixture
def config(tmp_path):
    return AnalystConfig(
        data_dir=tmp_path,
        db_path=str(tmp_path / "news.db"),
        index_dir=str(tmp_path / "vectors"),
    )


@pytest.fixture
def sample_news():
    """生成模拟新闻数据"""
    now = datetime.utcnow()
    items = []
    companies = ["比亚迪", "宁德时代", "特斯拉"]
    sectors = ["新能源", "锂电池", "汽车"]

    for i in range(10):
        t = (now - timedelta(days=i)).isoformat()
        items.append({
            "id": f"news_{i:03d}",
            "title": f"新能源板块动态 {i}",
            "content": f"这是第 {i} 条新闻内容",
            "summary": f"摘要 {i}",
            "url": f"https://example.com/{i}",
            "source": "cls" if i % 2 == 0 else "xueqiu",
            "category": "finance",
            "publish_time": t,
            "collect_time": t,
            "ts_codes": [f"{600000 + i}.SH"],
            "tags": ["新能源"],
            "keywords": ["新能源", "补贴"],
            "author": "test",
            "source_priority": 3 + (i % 3),
            "sentiment": "positive" if i % 3 == 0 else ("negative" if i % 3 == 1 else "neutral"),
            "sentiment_score": 0.5,
            "impact_scope": "sector",
            "related_sectors": [sectors[i % len(sectors)]],
            "policy_level": None,
            "urgency": "urgent" if i == 0 else "normal",
            "mentioned_companies": [companies[i % len(companies)]],
            "mentioned_persons": [],
            "mentioned_amounts": [],
            "content_hash": f"hash_{i}",
            "title_hash": f"thash_{i}",
            "event_id": None,
            "is_primary": 0,
            "extra": "{}",
        })
    return items


class TestChainNode:
    def test_from_dict(self, sample_news):
        node = ChainNode.from_dict(sample_news[0])
        assert node.news_id == "news_000"
        assert node.title == "新能源板块动态 0"
        assert node.source == "cls"
        assert node.source_priority >= 3
        assert len(node.mentioned_companies) > 0

    def test_from_dict_missing_fields(self):
        node = ChainNode.from_dict({"id": "x", "title": "test"})
        assert node.news_id == "x"
        assert node.urgency == "normal"


class TestClueChain:
    def test_properties(self):
        nodes = [
            ChainNode("1", "a", "2024-01-01T00:00:00", "src", 3, "cat"),
            ChainNode("2", "b", "2024-01-15T00:00:00", "src", 3, "cat"),
        ]
        chain = ClueChain("c1", "timeline", "test", nodes=nodes, significance=0.8)

        assert chain.node_count == 2
        assert chain.time_span == "2024-01-01 ~ 2024-01-15"

    def test_empty_chain(self):
        chain = ClueChain("c1", "timeline", "test")
        assert chain.node_count == 0
        assert chain.time_span == ""

    def test_to_dict(self):
        nodes = [ChainNode("1", "a", "2024-01-01T00:00:00", "src", 3, "cat")]
        chain = ClueChain("c1", "timeline", "test", nodes=nodes)
        d = chain.to_dict()

        assert d["chain_id"] == "c1"
        assert d["chain_type"] == "timeline"
        assert len(d["nodes"]) == 1


class TestChainBuilder:
    @pytest.mark.asyncio
    async def test_build_timeline_chain(self, config, sample_news):
        builder = ChainBuilder(config)

        with patch.object(builder.query, "search_hybrid", new_callable=AsyncMock) as mock_hybrid, \
             patch.object(builder.query, "get_timeline", new_callable=AsyncMock) as mock_timeline:
            mock_hybrid.return_value = sample_news[:5]
            mock_timeline.return_value = sample_news[3:]
            chains = await builder.build_timeline_chain("比亚迪", "company", days=30)

        assert len(chains) >= 1
        assert chains[0].chain_type == "timeline"
        assert "比亚迪" in chains[0].theme
        assert len(chains[0].nodes) > 0

    @pytest.mark.asyncio
    async def test_build_timeline_chain_hybrid_fails(self, config, sample_news):
        """search_hybrid 失败时降级为纯 SQLite 查询"""
        builder = ChainBuilder(config)

        with patch.object(builder.query, "search_hybrid", new_callable=AsyncMock) as mock_hybrid, \
             patch.object(builder.query, "get_timeline", new_callable=AsyncMock) as mock_timeline:
            mock_hybrid.side_effect = Exception("no sentence_transformers")
            mock_timeline.return_value = sample_news
            chains = await builder.build_timeline_chain("比亚迪", "company", days=30)

        assert len(chains) >= 1
        assert chains[0].chain_type == "timeline"

    @pytest.mark.asyncio
    async def test_build_timeline_chain_insufficient_data(self, config):
        builder = ChainBuilder(config)

        with patch.object(builder.query, "search_hybrid", new_callable=AsyncMock) as mock_hybrid, \
             patch.object(builder.query, "get_timeline", new_callable=AsyncMock) as mock_timeline:
            mock_hybrid.return_value = [{"id": "1", "title": "single"}]
            mock_timeline.return_value = []
            chains = await builder.build_timeline_chain("某某公司", "company")

        assert len(chains) == 0

    @pytest.mark.asyncio
    async def test_build_sector_propagation(self, config, sample_news):
        builder = ChainBuilder(config)

        with patch.object(builder.query, "search_hybrid", new_callable=AsyncMock) as mock_hybrid, \
             patch.object(builder.query, "get_timeline", new_callable=AsyncMock) as mock_timeline:
            mock_hybrid.return_value = sample_news[:5]
            mock_timeline.return_value = sample_news[3:]
            chains = await builder.build_sector_propagation_chain(
                ["新能源", "补贴"], days=30
            )

        assert len(chains) >= 1
        assert chains[0].chain_type == "sector_propagation"

    @pytest.mark.asyncio
    async def test_build_anomaly_chains(self, config, sample_news):
        builder = ChainBuilder(config)

        with patch.object(builder.query, "get_urgent", new_callable=AsyncMock) as mock:
            mock.return_value = sample_news
            chains = await builder.build_anomaly_chains(days=30)

        # 结果取决于数据中实体密度
        assert isinstance(chains, list)

    @pytest.mark.asyncio
    async def test_build_entity_cross_chains(self, config, sample_news):
        builder = ChainBuilder(config)

        with patch.object(builder.query, "get_by_time_range", new_callable=AsyncMock) as mock:
            mock.return_value = sample_news
            chains = await builder.build_entity_cross_chains(days=30)

        assert isinstance(chains, list)
