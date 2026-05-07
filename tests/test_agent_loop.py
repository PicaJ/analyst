"""测试: Agent 闭环循环 (Plan → Execute → Evaluate → Refine)"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any, Dict, List

import pytest

from analyst.config import AnalystConfig
from analyst.state import RunContext, AgentState
from analyst.agent import AnalysisAgent
from analyst.chain_builder import ClueChain, ChainNode
from analyst.evaluator import Evaluator


@pytest.fixture
def config(tmp_path):
    return AnalystConfig(
        data_dir=tmp_path,
        db_path=str(tmp_path / "news.db"),
        max_iterations=3,
        quality_threshold=0.65,
    )


@pytest.fixture
def agent(config):
    return AnalysisAgent(config)


def _make_chain(chain_id="test", theme="test chain", significance=0.7):
    """创建模拟线索链"""
    nodes = [
        ChainNode(f"n{i}", f"标题{i}", f"2024-01-{i+1:02d}T10:00:00",
                  "cls", 4, "finance", sentiment="positive",
                  mentioned_companies=["比亚迪"], related_sectors=["新能源"])
        for i in range(5)
    ]
    return ClueChain(chain_id, "timeline", theme, nodes=nodes,
                     significance=significance, hidden_signals=["测试信号"])


def _make_good_insight(chain_id="test"):
    return {
        "chain_id": chain_id,
        "thesis": "新能源板块因政策利好迎来上涨周期",
        "confidence": 0.85,
        "time_horizon": "中期(1-4周)",
        "key_findings": [
            {
                "finding": "补贴政策落地",
                "evidence_ids": ["n0", "n1", "n2"],
                "reasoning": "政策文件明确提到补贴延续",
            }
        ],
        "hidden_signals": [
            {
                "signal": "上游锂矿尚未涨价",
                "implication": "成本端短期无忧",
                "not_priced_in": True,
            }
        ],
        "risk_factors": ["补贴调整风险"],
        "actionable_items": [
            {"action": "关注比亚迪", "urgency": "high", "targets": ["002594.SZ"]}
        ],
    }


def _make_poor_insight(chain_id="test"):
    return {
        "chain_id": chain_id,
        "thesis": "市场有变化",
        "confidence": 0.2,
        "key_findings": [],
        "hidden_signals": [],
        "risk_factors": [],
        "actionable_items": [],
    }


class TestAgentPlan:
    @pytest.mark.asyncio
    async def test_plan_with_entity(self, agent):
        ctx = RunContext()
        ctx.focus_entity = "比亚迪"
        ctx.time_window_days = 30

        mock_news = [
            {"mentioned_companies": ["比亚迪"], "related_sectors": ["新能源"]}
            for _ in range(5)
        ]

        with patch.object(agent.query, "search_hybrid", new_callable=AsyncMock) as mock:
            mock.return_value = mock_news
            plan = await agent._plan(ctx)

        assert "chains" in plan
        chain_types = [c["type"] for c in plan["chains"]]
        assert "timeline" in chain_types

    @pytest.mark.asyncio
    async def test_plan_with_keywords(self, agent):
        ctx = RunContext()
        ctx.focus_keywords = ["芯片", "制裁"]
        ctx.time_window_days = 30

        with patch.object(agent.query, "search_hybrid", new_callable=AsyncMock) as mock:
            mock.return_value = [{"mentioned_companies": [], "related_sectors": []}]
            plan = await agent._plan(ctx)

        chain_types = [c["type"] for c in plan["chains"]]
        assert "sector_propagation" in chain_types

    @pytest.mark.asyncio
    async def test_plan_auto_mode(self, agent):
        """未指定 entity/keywords 时自动选热门实体"""
        ctx = RunContext()
        ctx.time_window_days = 30

        mock_news = [
            {"mentioned_companies": ["比亚迪", "特斯拉"], "related_sectors": ["新能源"]},
            {"mentioned_companies": ["比亚迪"], "related_sectors": ["新能源"]},
            {"mentioned_companies": ["特斯拉"], "related_sectors": ["汽车"]},
        ]

        with patch.object(agent.query, "get_by_time_range", new_callable=AsyncMock) as mock:
            mock.return_value = mock_news
            plan = await agent._plan(ctx)

        chain_types = [c["type"] for c in plan["chains"]]
        assert "timeline" in chain_types


class TestAgentRefine:
    def _make_eval_ctx(self, **dim_overrides):
        """构造带评估数据的 ctx"""
        defaults = {
            "evidence_coverage": 0.7,
            "reasoning_quality": 0.7,
            "specificity": 0.7,
            "signal_novelty": 0.7,
            "self_consistency": 0.7,
        }
        defaults.update(dim_overrides)
        ctx = RunContext()
        ctx.evaluation = {
            "overall_score": 0.4,
            "passed": False,
            "individual_results": [
                {
                    "passed": False,
                    **defaults,
                    "hallucination_flags": [],
                }
            ],
        }
        return ctx

    def test_select_expand_context(self, agent):
        """evidence_coverage 最低 → expand_context"""
        ctx = self._make_eval_ctx(evidence_coverage=0.1, signal_novelty=0.5)
        assert agent._select_refinement_strategy(ctx) == "expand_context"

    def test_select_critique_revise_by_reasoning(self, agent):
        """reasoning_quality 最低 → critique_revise"""
        ctx = self._make_eval_ctx(reasoning_quality=0.1, evidence_coverage=0.5)
        assert agent._select_refinement_strategy(ctx) == "critique_revise"

    def test_select_critique_revise_by_specificity(self, agent):
        """specificity 最低 → critique_revise"""
        ctx = self._make_eval_ctx(specificity=0.1, signal_novelty=0.5)
        assert agent._select_refinement_strategy(ctx) == "critique_revise"

    def test_select_add_chains(self, agent):
        """signal_novelty 最低 → add_chains"""
        ctx = self._make_eval_ctx(signal_novelty=0.1, evidence_coverage=0.5)
        assert agent._select_refinement_strategy(ctx) == "add_chains"

    def test_select_critique_revise_on_hallucination(self, agent):
        """幻觉存在 → critique_revise"""
        ctx = self._make_eval_ctx(evidence_coverage=0.1)
        ctx.evaluation["individual_results"][0]["hallucination_flags"] = ["引用不存在"]
        assert agent._select_refinement_strategy(ctx) == "critique_revise"

    def test_default_expand_context(self, agent):
        """无评估数据 → fallback 到关键词匹配 → expand_context"""
        ctx = RunContext()
        ctx.critique = "综合评分略低于阈值"
        assert agent._select_refinement_strategy(ctx) == "expand_context"

    def test_fallback_critique_keyword(self, agent):
        """无评估数据时从 critique 关键词推断"""
        ctx = RunContext()
        ctx.critique = "隐蔽信号不明显"
        assert agent._select_refinement_strategy(ctx) == "add_chains"

    def test_apply_expand_context(self, agent):
        ctx = RunContext()
        ctx.time_window_days = 60
        ctx.analysis_plan = {"chains": [
            {"type": "timeline", "entity": "比亚迪", "days": 60}
        ]}
        agent._apply_refine(ctx, "expand_context")
        assert ctx.time_window_days == 90
        assert ctx.analysis_plan["chains"][0]["days"] == 90

    def test_apply_add_chains(self, agent):
        ctx = RunContext()
        ctx.analysis_plan = {"chains": [
            {"type": "timeline", "entity": "比亚迪", "days": 90}
        ]}
        agent._apply_refine(ctx, "add_chains")
        chain_types = [c["type"] for c in ctx.analysis_plan["chains"]]
        assert "anomaly" in chain_types
        assert "entity_cross" in chain_types


class TestAgentLoop:
    @pytest.mark.asyncio
    async def test_full_loop_high_quality(self, agent):
        """高质量洞察应一次通过"""
        ctx = RunContext()
        ctx.focus_entity = "比亚迪"
        ctx.time_window_days = 30
        ctx.max_iterations = 3

        mock_chain = _make_chain()

        with patch.object(agent, "_plan", new_callable=AsyncMock) as mock_plan, \
             patch.object(agent, "_execute", new_callable=AsyncMock) as mock_exec:

            mock_plan.return_value = {
                "chains": [{"type": "timeline", "entity": "比亚迪", "days": 30}],
                "scan_summary": {},
            }
            good_insight = _make_good_insight()
            mock_exec.return_value = ([mock_chain], [good_insight])

            result = await agent.run(ctx)

        assert result.state == AgentState.COMPLETE
        assert result.iteration == 1
        assert len(result.insights) == 1

    @pytest.mark.asyncio
    async def test_refine_loop_then_pass(self, agent):
        """低质量 → Refine → 高质量 → 通过"""
        ctx = RunContext()
        ctx.focus_entity = "比亚迪"
        ctx.time_window_days = 30
        ctx.max_iterations = 3

        mock_chain = _make_chain()
        poor_insight = _make_poor_insight()
        good_insight = _make_good_insight()

        exec_count = 0

        async def mock_execute(c):
            nonlocal exec_count
            exec_count += 1
            if exec_count == 1:
                return [mock_chain], [poor_insight]
            else:
                return [mock_chain], [good_insight]

        with patch.object(agent, "_plan", new_callable=AsyncMock) as mock_plan, \
             patch.object(agent, "_execute", new_callable=AsyncMock) as mock_exec:

            mock_plan.return_value = {
                "chains": [{"type": "timeline", "entity": "比亚迪", "days": 30}],
                "scan_summary": {},
            }
            mock_exec.side_effect = mock_execute

            result = await agent.run(ctx)

        assert result.state == AgentState.COMPLETE
        assert result.iteration == 2
        assert result.quality_score >= 0.65

    @pytest.mark.asyncio
    async def test_max_iterations_exceeded(self, agent):
        """连续低质量，达到最大迭代次数后 FAILED"""
        ctx = RunContext()
        ctx.focus_entity = "比亚迪"
        ctx.time_window_days = 30
        ctx.max_iterations = 2

        mock_chain = _make_chain()
        poor_insight = _make_poor_insight()

        with patch.object(agent, "_plan", new_callable=AsyncMock) as mock_plan, \
             patch.object(agent, "_execute", new_callable=AsyncMock) as mock_exec:

            mock_plan.return_value = {
                "chains": [{"type": "timeline", "entity": "比亚迪", "days": 30}],
                "scan_summary": {},
            }
            mock_exec.return_value = ([mock_chain], [poor_insight])

            result = await agent.run(ctx)

        assert result.state == AgentState.FAILED
        assert result.iteration == 2

    @pytest.mark.asyncio
    async def test_resume_from_refine_state(self, agent):
        """从 REFINE 状态恢复后继续执行"""
        ctx = RunContext()
        ctx.focus_entity = "比亚迪"
        ctx.time_window_days = 30
        ctx.max_iterations = 3
        # 模拟已经完成了 Plan 和第一次 Execute
        ctx.transition(AgentState.PLANNING)
        ctx.analysis_plan = {
            "chains": [{"type": "timeline", "entity": "比亚迪", "days": 30}],
            "scan_summary": {},
        }

        mock_chain = _make_chain()
        good_insight = _make_good_insight()

        with patch.object(agent, "_execute", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = ([mock_chain], [good_insight])
            result = await agent.run(ctx)

        assert result.state == AgentState.COMPLETE
