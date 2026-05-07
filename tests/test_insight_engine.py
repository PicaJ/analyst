"""测试: LLM 客户端与洞察引擎"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from analyst.config import AnalystConfig
from analyst.insight_engine import LLMClient, InsightEngine, _format_news_list
from analyst.chain_builder import ChainNode, ClueChain


@pytest.fixture
def config(tmp_path):
    return AnalystConfig(
        data_dir=tmp_path,
        db_path=str(tmp_path / "news.db"),
        llm_provider="deepseek",
        llm_model="test-model",
        llm_base_url="https://api.example.com",
        llm_api_key="test-key",
    )


@pytest.fixture
def mock_chain():
    nodes = [
        ChainNode("n1", "新闻标题1", "2024-01-01T10:00:00", "cls", 4, "finance",
                   sentiment="positive", mentioned_companies=["比亚迪"],
                   related_sectors=["新能源"]),
        ChainNode("n2", "新闻标题2", "2024-01-02T10:00:00", "xueqiu", 3, "finance",
                   sentiment="neutral", mentioned_companies=["宁德时代"],
                   related_sectors=["锂电池"]),
    ]
    return ClueChain(
        "test_chain", "timeline", "测试链",
        nodes=nodes, significance=0.7,
        hidden_signals=["情绪转变信号"],
    )


class TestLLMClient:
    def test_resolve_deepseek_url(self, config):
        client = LLMClient(config)
        assert client._resolve_base_url() == "https://api.example.com"

    def test_resolve_default_deepseek_url(self, tmp_path):
        cfg = AnalystConfig(data_dir=tmp_path, db_path=str(tmp_path / "x"),
                           llm_provider="deepseek", llm_api_key="k")
        client = LLMClient(cfg)
        assert client._resolve_base_url() == "https://api.deepseek.com"

    def test_resolve_default_openai_url(self, tmp_path):
        cfg = AnalystConfig(data_dir=tmp_path, db_path=str(tmp_path / "x"),
                           llm_provider="openai", llm_api_key="k")
        client = LLMClient(cfg)
        assert client._resolve_base_url() == "https://api.openai.com"

    @pytest.mark.asyncio
    async def test_call_openai_compat(self, config):
        client = LLMClient(config)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"thesis": "test"}'}}]
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.post.return_value = mock_response
            instance.is_closed = False
            MockClient.return_value = instance

            result = await client.complete("system", "user")
            assert result == '{"thesis": "test"}'

    @pytest.mark.asyncio
    async def test_connection_reuse(self, config):
        """验证 HTTP 连接被复用"""
        client = LLMClient(config)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"thesis": "test"}'}}]
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.post.return_value = mock_response
            instance.is_closed = False
            MockClient.return_value = instance

            await client.complete("s", "u")
            await client.complete("s", "u")
            # 应该只创建一个 client
            assert MockClient.call_count == 1


class TestInsightEngine:
    def test_parse_json_response(self, config):
        engine = InsightEngine(config)
        raw = json.dumps({"thesis": "test", "confidence": 0.8})
        result = engine._parse_llm_response(raw)
        assert result["thesis"] == "test"

    def test_parse_markdown_wrapped_json(self, config):
        engine = InsightEngine(config)
        raw = '```json\n{"thesis": "test"}\n```'
        result = engine._parse_llm_response(raw)
        assert result["thesis"] == "test"

    def test_parse_json_with_prefix(self, config):
        engine = InsightEngine(config)
        raw = 'Here is the analysis:\n{"thesis": "test", "confidence": 0.7}\nEnd.'
        result = engine._parse_llm_response(raw)
        assert result["thesis"] == "test"

    def test_parse_invalid_response(self, config):
        engine = InsightEngine(config)
        raw = "This is not JSON at all"
        result = engine._parse_llm_response(raw)
        assert "raw_text" in result

    def test_set_critique(self, config):
        engine = InsightEngine(config)
        engine.set_critique("证据引用不足")
        assert engine._critique == "证据引用不足"

    @pytest.mark.asyncio
    async def test_analyze_chain_error_handling(self, config, mock_chain):
        """LLM 调用失败时返回错误结果而非抛异常"""
        engine = InsightEngine(config)

        with patch.object(engine.llm, "complete", new_callable=AsyncMock) as mock:
            mock.side_effect = Exception("API Error")
            result = await engine.analyze_chain(mock_chain)

        assert "error" in result
        assert result["confidence"] == 0.0


class TestFormatNewsList:
    def test_basic_formatting(self):
        nodes = [
            ChainNode("n1", "测试标题", "2024-01-01T10:00:00", "cls", 3, "finance"),
        ]
        result = _format_news_list(nodes)
        assert "n1" in result
        assert "测试标题" in result

    def test_max_items_limit(self):
        nodes = [ChainNode(f"n{i}", f"标题{i}", "2024-01-01", "s", 3, "c") for i in range(50)]
        result = _format_news_list(nodes, max_items=10)
        assert "还有 40 条" in result
