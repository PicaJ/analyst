"""测试: Harness 端到端与熔断保护"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from analyst.config import AnalystConfig
from analyst.state import RunContext, AgentState
from analyst.harness import Harness, HarnessMetrics
from analyst.agent import AnalysisAgent


@pytest.fixture
def config(tmp_path):
    return AnalystConfig(
        data_dir=tmp_path,
        db_path=str(tmp_path / "news.db"),
        index_dir=str(tmp_path / "vectors"),
        report_dir=str(tmp_path / "reports"),
        max_iterations=3,
        quality_threshold=0.65,
        circuit_breaker_threshold=3,
    )


@pytest.fixture
def sample_news():
    now = datetime.utcnow()
    items = []
    for i in range(10):
        t = (now - timedelta(days=i)).isoformat()
        items.append({
            "id": f"news_{i:03d}",
            "title": f"测试新闻 {i}",
            "content": f"内容 {i}",
            "summary": f"摘要 {i}",
            "url": f"https://example.com/{i}",
            "source": "cls",
            "category": "finance",
            "publish_time": t,
            "collect_time": t,
            "ts_codes": "[]",
            "tags": "[]",
            "keywords": "[]",
            "author": "test",
            "source_priority": 3,
            "sentiment": "positive",
            "sentiment_score": 0.5,
            "impact_scope": "sector",
            "related_sectors": json.dumps(["新能源"]),
            "policy_level": None,
            "urgency": "normal",
            "mentioned_companies": json.dumps(["比亚迪"]),
            "mentioned_persons": "[]",
            "mentioned_amounts": "[]",
            "content_hash": f"hash_{i}",
            "title_hash": f"thash_{i}",
            "event_id": None,
            "is_primary": 0,
            "extra": "{}",
        })
    return items


class TestHarnessMetrics:
    def test_record_success(self):
        m = HarnessMetrics()
        ctx = RunContext()
        ctx.state = AgentState.COMPLETE
        ctx.evaluation = {"overall_score": 0.8}

        m.record_run(ctx)
        assert m.runs_total == 1
        assert m.runs_success == 1
        assert m.runs_failed == 0

    def test_record_failure(self):
        m = HarnessMetrics()
        ctx = RunContext()
        ctx.state = AgentState.FAILED
        ctx.evaluation = {"overall_score": 0.3}

        m.record_run(ctx)
        assert m.runs_total == 1
        assert m.runs_success == 0
        assert m.runs_failed == 1

    def test_to_dict(self):
        m = HarnessMetrics()
        m.runs_total = 5
        m.runs_success = 4
        d = m.to_dict()
        assert d["success_rate"] == 0.8


class TestHarnessCircuitBreaker:
    @pytest.mark.asyncio
    async def test_circuit_breaker_trips(self, config):
        harness = Harness(config)
        harness._consecutive_failures = config.circuit_breaker_threshold

        result = await harness.run_analysis(entity="test")
        assert result.state == AgentState.FAILED
        assert "熔断保护" in result.errors[0]
        assert harness.metrics.circuit_breaker_trips == 1

    @pytest.mark.asyncio
    async def test_circuit_breaker_resets_on_success(self, config):
        harness = Harness(config)
        harness._consecutive_failures = 2

        # 模拟成功的 agent 运行
        mock_ctx = RunContext()
        mock_ctx.state = AgentState.COMPLETE
        mock_ctx.evaluation = {"overall_score": 0.8}

        with patch.object(AnalysisAgent, "run", new_callable=AsyncMock) as mock:
            mock.return_value = mock_ctx
            result = await harness.run_analysis(entity="test")

        assert harness._consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_circuit_breaker_increments_on_failure(self, config):
        harness = Harness(config)

        mock_ctx = RunContext()
        mock_ctx.state = AgentState.FAILED
        mock_ctx.errors = ["test error"]

        with patch.object(AnalysisAgent, "run", new_callable=AsyncMock) as mock:
            mock.return_value = mock_ctx
            await harness.run_analysis(entity="test")

        assert harness._consecutive_failures == 1


class TestHarnessResume:
    @pytest.mark.asyncio
    async def test_resume_nonexistent(self, config):
        harness = Harness(config)
        result = await harness.resume("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_resume_completed(self, config):
        harness = Harness(config)
        ctx = RunContext(run_id="done")
        ctx.state = AgentState.COMPLETE
        harness.state_store.save(ctx)

        result = await harness.resume("done")
        assert result.run_id == "done"


class TestHarnessStatus:
    def test_status_structure(self, config):
        harness = Harness(config)
        s = harness.status()

        assert "metrics" in s
        assert "circuit_breaker" in s
        assert "recent_runs" in s
        assert s["circuit_breaker"]["is_open"] is False
