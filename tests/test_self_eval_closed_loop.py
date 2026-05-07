"""
闭环自检测试 — 验证 Execute → Evaluate → Refine → Re-Execute 完整闭环

逐路径追踪:
  1. 高质量一次通过
  2. 低质量 → expand_context → 通过
  3. 低质量 → add_chains → 通过
  4. 低质量 → critique_revise → 通过
  5. 连续失败到 max_iterations → FAILED
  6. Refine 后 chains 被正确更新
  7. critique 被正确注入 LLM prompt
  8. Resume 从各中间状态
  9. 中间状态持久化
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from analyst.config import AnalystConfig
from analyst.state import RunContext, AgentState, StateStore
from analyst.agent import AnalysisAgent
from analyst.harness import Harness
from analyst.chain_builder import ClueChain, ChainNode
from analyst.evaluator import Evaluator


# ── 辅助工具 ──

def _make_chain(chain_id="test", significance=0.7):
    nodes = [
        ChainNode(f"n{i}", f"标题{i}", f"2024-01-{i+1:02d}T10:00:00",
                  "cls", 4, "finance", sentiment="positive",
                  mentioned_companies=["比亚迪"], related_sectors=["新能源"])
        for i in range(5)
    ]
    return ClueChain(chain_id, "timeline", f"测试链 {chain_id}",
                     nodes=nodes, significance=significance,
                     hidden_signals=["测试信号"])


def _good_insight(chain_id="test"):
    """高质量: 证据充分、推理完整、具体目标、隐蔽信号未定价"""
    return {
        "chain_id": chain_id,
        "thesis": "新能源板块因补贴政策延续迎来上涨周期",
        "confidence": 0.85,
        "time_horizon": "中期(1-4周)",
        "key_findings": [
            {
                "finding": "财政部发布新能源汽车补贴延续政策",
                "evidence_ids": ["n0", "n1", "n2"],
                "reasoning": "政策文件明确2024年补贴不退坡",
            },
            {
                "finding": "比亚迪月度销量同比增长40%",
                "evidence_ids": ["n3", "n4"],
                "reasoning": "销量数据超出市场预期15个百分点",
            },
        ],
        "hidden_signals": [
            {
                "signal": "上游锂矿价格持续走低但下游整车未降价",
                "implication": "毛利率扩张，盈利能力提升",
                "not_priced_in": True,
            },
        ],
        "risk_factors": ["补贴政策可能在年底调整"],
        "actionable_items": [
            {"action": "关注比亚迪及产业链", "urgency": "high",
             "targets": ["002594.SZ", "比亚迪"]},
        ],
    }


def _poor_insight(chain_id="test", reason="generic"):
    """低质量洞察 — 精心设计以触发特定修正策略

    策略选择基于最低维度分:
      expand_context  ← evidence_coverage 最低
      add_chains      ← signal_novelty 最低
      critique_revise ← reasoning/specificity 最低 或 幻觉

    reason:
      "generic"      — 完全空白
      "no_evidence"  — evidence_coverage=0.1(最低), 其他维度>0.1
      "no_reasoning" — reasoning 缺失
      "vague"        — 可操作项笼统
      "no_signal"    — signal_novelty=0.2(最低), 其他维度>0.2, 总分<0.65
    """
    base = {
        "chain_id": chain_id,
        "thesis": "市场有变化",
        "confidence": 0.2,
        "time_horizon": "短期(1-5天)",
        "key_findings": [],
        "hidden_signals": [],
        "risk_factors": [],
        "actionable_items": [],
    }

    if reason == "no_evidence":
        # evidence_coverage=0.1(无引用), reasoning=1.0, specificity=0.6, signal=0.2
        # overall ≈ 0.50 < 0.65, evidence最低
        base["thesis"] = "政策面有所调整"
        base["confidence"] = 0.4
        base["key_findings"] = [
            {"finding": "有政策变化", "evidence_ids": [], "reasoning": "基于多角度分析"}
        ]
        base["actionable_items"] = [
            {"action": "关注政策受益股", "urgency": "medium", "targets": ["比亚迪"]}
        ]

    elif reason == "no_reasoning":
        base["key_findings"] = [
            {"finding": "补贴政策", "evidence_ids": ["n0"], "reasoning": ""}
        ]
    elif reason == "vague":
        base["actionable_items"] = [
            {"action": "关注", "urgency": "medium", "targets": []}
        ]

    elif reason == "no_signal":
        # signal_novelty=0.2(最低), evidence_coverage=0.8, reasoning=0.5, specificity=0.3
        # overall ≈ 0.51 < 0.65, signal最低
        base["thesis"] = "有些变化"
        base["confidence"] = 0.3
        base["key_findings"] = [
            {"finding": "数据变化", "evidence_ids": ["n0", "n1"], "reasoning": ""}
        ]
        base["hidden_signals"] = []
        base["risk_factors"] = []
        base["actionable_items"] = [
            {"action": "观望", "urgency": "medium", "targets": []}
        ]
    return base


@pytest.fixture
def config(tmp_path):
    return AnalystConfig(
        data_dir=tmp_path,
        db_path=str(tmp_path / "news.db"),
        report_dir=str(tmp_path / "reports"),
        max_iterations=3,
        quality_threshold=0.65,
    )


@pytest.fixture
def agent(config):
    return AnalysisAgent(config)


# ── 1. 高质量一次通过 ──

class TestHappyPath:
    @pytest.mark.asyncio
    async def test_one_iteration_complete(self, agent):
        ctx = RunContext()
        ctx.focus_entity = "比亚迪"
        ctx.max_iterations = 3

        mock_chain = _make_chain()

        with patch.object(agent, "_plan", new_callable=AsyncMock) as plan_mock, \
             patch.object(agent, "_execute", new_callable=AsyncMock) as exec_mock:
            plan_mock.return_value = {
                "chains": [{"type": "timeline", "entity": "比亚迪", "days": 90}],
                "scan_summary": {},
            }
            exec_mock.return_value = ([mock_chain], [_good_insight()])

            result = await agent.run(ctx)

        assert result.state == AgentState.COMPLETE
        assert result.iteration == 1
        assert result.quality_score >= 0.65
        assert len(result.insights) == 1
        assert result.insights[0]["thesis"] != ""


# ── 2. 低质量 → expand_context → 通过 ──

class TestExpandContextStrategy:
    @pytest.mark.asyncio
    async def test_expand_then_pass(self, agent):
        """evidence_coverage 最低 → expand_context"""
        ctx = RunContext()
        ctx.focus_entity = "比亚迪"
        ctx.time_window_days = 30
        ctx.max_iterations = 3

        mock_chain = _make_chain()
        poor = _poor_insight(reason="generic")  # 全空, 总分很低
        good = _good_insight()

        exec_count = 0
        async def mock_execute(c):
            nonlocal exec_count
            exec_count += 1
            if exec_count == 1:
                return [mock_chain], [poor]
            return [mock_chain], [good]

        with patch.object(agent, "_plan", new_callable=AsyncMock) as plan_mock, \
             patch.object(agent, "_execute", new_callable=AsyncMock) as exec_mock:
            plan_mock.return_value = {
                "chains": [{"type": "timeline", "entity": "比亚迪", "days": 30}],
                "scan_summary": {},
            }
            exec_mock.side_effect = mock_execute

            result = await agent.run(ctx)

        assert result.state == AgentState.COMPLETE
        assert result.iteration == 2

    def test_strategy_selects_expand_when_evidence_worst(self, agent):
        """策略选择: evidence_coverage 最低 → expand_context"""
        ctx = RunContext()
        ctx.evaluation = {
            "overall_score": 0.4,
            "passed": False,
            "individual_results": [
                {
                    "passed": False,
                    "evidence_coverage": 0.1,   # 最差
                    "reasoning_quality": 0.7,
                    "specificity": 0.8,
                    "signal_novelty": 0.5,
                    "self_consistency": 0.9,
                    "hallucination_flags": [],
                }
            ],
        }
        strategy = agent._select_refinement_strategy(ctx)
        assert strategy == "expand_context"

    def test_apply_refine_expand_updates_all_plan_days(self, agent):
        """_apply_refine('expand_context') 同步更新所有链的 days"""
        ctx = RunContext()
        ctx.time_window_days = 60
        ctx.analysis_plan = {
            "chains": [
                {"type": "timeline", "entity": "比亚迪", "days": 60},
                {"type": "anomaly", "days": 60},
            ],
        }
        agent._apply_refine(ctx, "expand_context")

        assert ctx.time_window_days == 90  # 60 * 1.5
        for spec in ctx.analysis_plan["chains"]:
            assert spec["days"] == 90


# ── 3. 低质量 → add_chains → 通过 ──

class TestAddChainsStrategy:
    @pytest.mark.asyncio
    async def test_add_chains_then_pass(self, agent):
        """signal_novelty 最低 → add_chains 策略"""
        ctx = RunContext()
        ctx.focus_entity = "比亚迪"
        ctx.time_window_days = 90
        ctx.max_iterations = 3

        mock_chain = _make_chain()

        # 使用 no_signal: 证据/推理/具体性都还行, 但隐蔽信号缺失
        poor = _poor_insight(reason="no_signal")

        exec_count = 0
        async def mock_execute(c):
            nonlocal exec_count
            exec_count += 1
            if exec_count == 1:
                return [mock_chain], [poor]
            return [mock_chain], [_good_insight()]

        with patch.object(agent, "_plan", new_callable=AsyncMock) as plan_mock, \
             patch.object(agent, "_execute", new_callable=AsyncMock) as exec_mock:
            plan_mock.return_value = {
                "chains": [{"type": "timeline", "entity": "比亚迪", "days": 90}],
                "scan_summary": {},
            }
            exec_mock.side_effect = mock_execute

            result = await agent.run(ctx)

        assert result.state == AgentState.COMPLETE
        # 验证策略是 add_chains
        assert ctx.refinement_strategy == "add_chains"
        # 验证 plan 中补充了 anomaly 和 entity_cross
        chain_types = [c["type"] for c in ctx.analysis_plan["chains"]]
        assert "anomaly" in chain_types
        assert "entity_cross" in chain_types


# ── 4. 低质量 → critique_revise → 通过 ──

class TestCritiqueReviseStrategy:
    @pytest.mark.asyncio
    async def test_critique_injected_into_llm(self, agent):
        ctx = RunContext()
        ctx.focus_entity = "比亚迪"
        ctx.time_window_days = 90
        ctx.max_iterations = 3

        mock_chain = _make_chain()

        # 极差洞察: 无发现、无信号、无操作、笼统
        poor = {
            "chain_id": "test",
            "thesis": "有变动",
            "confidence": 0.1,
            "key_findings": [],
            "hidden_signals": [],
            "risk_factors": [],
            "actionable_items": [
                {"action": "观望", "urgency": "low", "targets": []}
            ],
        }

        exec_count = 0
        async def mock_execute(c):
            nonlocal exec_count
            exec_count += 1
            if exec_count == 1:
                return [mock_chain], [poor]
            return [mock_chain], [_good_insight()]

        with patch.object(agent, "_plan", new_callable=AsyncMock) as plan_mock, \
             patch.object(agent, "_execute", new_callable=AsyncMock) as exec_mock:
            plan_mock.return_value = {
                "chains": [{"type": "timeline", "entity": "比亚迪", "days": 90}],
                "scan_summary": {},
            }
            exec_mock.side_effect = mock_execute

            result = await agent.run(ctx)

        assert result.state == AgentState.COMPLETE
        # critique 应包含具体批评
        assert len(ctx.critique) > 0
        # 由于洞察极差，aggregated critique 应包含来自 evaluate 的具体维度批评
        critique_lower = ctx.critique.lower()
        # 至少应命中一个改进方向
        assert any(kw in critique_lower for kw in [
            "证据", "推理", "笼统", "信号", "幻觉", "质量分", "通过评估"
        ])


# ── 5. 连续失败到 max_iterations ──

class TestMaxIterations:
    @pytest.mark.asyncio
    async def test_three_failures_then_failed(self, agent):
        ctx = RunContext()
        ctx.focus_entity = "比亚迪"
        ctx.max_iterations = 3

        mock_chain = _make_chain()

        with patch.object(agent, "_plan", new_callable=AsyncMock) as plan_mock, \
             patch.object(agent, "_execute", new_callable=AsyncMock) as exec_mock:
            plan_mock.return_value = {
                "chains": [{"type": "timeline", "entity": "比亚迪", "days": 90}],
                "scan_summary": {},
            }
            exec_mock.return_value = ([mock_chain], [_poor_insight()])

            result = await agent.run(ctx)

        assert result.state == AgentState.FAILED
        assert result.iteration == 3
        assert result.quality_score < 0.65

    @pytest.mark.asyncio
    async def test_max_iterations_1(self, agent):
        """max_iterations=1 时一次失败就 FAILED，不 refine"""
        ctx = RunContext()
        ctx.focus_entity = "比亚迪"
        ctx.max_iterations = 1

        mock_chain = _make_chain()

        with patch.object(agent, "_plan", new_callable=AsyncMock) as plan_mock, \
             patch.object(agent, "_execute", new_callable=AsyncMock) as exec_mock:
            plan_mock.return_value = {
                "chains": [{"type": "timeline", "entity": "比亚迪", "days": 90}],
                "scan_summary": {},
            }
            exec_mock.return_value = ([mock_chain], [_poor_insight()])

            result = await agent.run(ctx)

        assert result.state == AgentState.FAILED
        assert result.iteration == 1


# ── 6. Evaluation 批评意见正确生成 ──

class TestEvaluationCritique:
    def test_evaluate_generates_critique_for_poor_insight(self, config):
        evaluator = Evaluator(AnalystConfig(quality_threshold=0.65))
        nodes = [
            {"id": f"n{i}", "title": f"新闻{i}",
             "mentioned_companies": ["比亚迪"], "related_sectors": ["新能源"]}
            for i in range(5)
        ]

        poor = _poor_insight()
        result = evaluator.evaluate(poor, nodes)

        assert result.passed is False
        assert len(result.critique) > 0
        # 批评应包含具体的改进方向
        critique_lower = result.critique.lower()
        assert any(kw in critique_lower for kw in [
            "证据", "推理", "笼统", "信号", "幻觉", "评分"
        ])

    def test_batch_critique_aggregates(self, config):
        evaluator = Evaluator(AnalystConfig(quality_threshold=0.65))
        nodes = [
            {"id": f"n{i}", "title": f"新闻{i}",
             "mentioned_companies": ["比亚迪"], "related_sectors": ["新能源"]}
            for i in range(5)
        ]

        result = evaluator.evaluate_batch(
            [_poor_insight(reason="no_evidence")],
            {"test": nodes},
        )

        assert result["passed"] is False
        assert "critique" in result
        assert len(result["critique"]) > 0


# ── 7. Harness 端到端闭环 ──

class TestHarnessEndToEnd:
    @pytest.mark.asyncio
    async def test_full_loop_with_refine(self, config):
        harness = Harness(config)
        mock_chain = _make_chain()

        exec_count = 0

        async def mock_agent_run(ctx):
            """模拟完整的 agent.run 行为"""
            nonlocal exec_count
            # Plan
            if ctx.state == AgentState.IDLE:
                ctx.transition(AgentState.PLANNING)
                ctx.analysis_plan = {
                    "chains": [{"type": "timeline", "entity": "比亚迪", "days": 90}],
                    "scan_summary": {},
                }

            # Iteration loop
            while ctx.iteration < ctx.max_iterations:
                ctx.iteration += 1
                exec_count += 1

                ctx.transition(AgentState.EXECUTING)
                if exec_count == 1:
                    ctx.chains = [mock_chain.to_dict()]
                    ctx.insights = [_poor_insight()]
                    ctx.total_llm_calls += 1
                else:
                    ctx.chains = [mock_chain.to_dict()]
                    ctx.insights = [_good_insight()]
                    ctx.total_llm_calls += 1

                ctx.transition(AgentState.EVALUATING)
                evaluator = Evaluator(config)
                evaluation = evaluator.evaluate_batch(
                    ctx.insights,
                    {"test": [
                        {"id": f"n{i}", "title": "", "mentioned_companies": ["比亚迪"],
                         "related_sectors": ["新能源"]}
                        for i in range(5)
                    ]},
                )
                ctx.evaluation = evaluation

                if evaluation["passed"]:
                    ctx.transition(AgentState.COMPLETE)
                    return ctx

                ctx.critique = evaluation.get("critique", "")
                if not ctx.can_retry:
                    ctx.transition(AgentState.FAILED)
                    return ctx

                ctx.refinement_strategy = "expand_context"
                ctx.time_window_days = int(ctx.time_window_days * 1.5)
                ctx.transition(AgentState.REFINE)

            ctx.transition(AgentState.FAILED)
            return ctx

        with patch.object(AnalysisAgent, "run", new_callable=AsyncMock) as mock:
            mock.side_effect = mock_agent_run
            result = await harness.run_analysis(entity="比亚迪")

        assert result.state == AgentState.COMPLETE
        assert exec_count == 2
        assert result.quality_score >= 0.65


# ── 8. Resume 从中间状态 ──

class TestResume:
    @pytest.mark.asyncio
    async def test_resume_from_refine(self, config):
        """从 REFINE 状态恢复 → agent 应跳过 Plan 直接进入 Execute"""
        harness = Harness(config)

        # 手动构造一个 REFINE 状态的 ctx
        ctx = RunContext(run_id="resume_test")
        ctx.state = AgentState.REFINE
        ctx.focus_entity = "比亚迪"
        ctx.time_window_days = 135  # 已 expand 过
        ctx.max_iterations = 3
        ctx.iteration = 1
        ctx.analysis_plan = {
            "chains": [{"type": "timeline", "entity": "比亚迪", "days": 135}],
            "scan_summary": {},
        }
        ctx.critique = "证据引用不足"
        ctx.refinement_strategy = "expand_context"

        harness.state_store.save(ctx)

        # 模拟 resume 后 agent 直接从 REFINE → EXECUTING
        async def mock_agent_run(c):
            # 不应再 Plan (state != IDLE)
            assert c.state == AgentState.REFINE

            c.transition(AgentState.EXECUTING)
            c.chains = [{"chain_id": "test"}]
            c.insights = [_good_insight()]
            c.total_llm_calls += 1
            c.iteration = 2

            c.transition(AgentState.EVALUATING)
            c.evaluation = {"overall_score": 0.85, "passed": True}
            c.transition(AgentState.COMPLETE)
            return c

        with patch.object(AnalysisAgent, "run", side_effect=mock_agent_run):
            result = await harness.resume("resume_test")

        assert result.state == AgentState.COMPLETE
        assert result.iteration == 2

    @pytest.mark.asyncio
    async def test_resume_from_planning(self, config):
        """从 PLANNING 状态恢复 → 应直接进入 Execute"""
        harness = Harness(config)

        ctx = RunContext(run_id="plan_resume")
        ctx.state = AgentState.PLANNING
        ctx.analysis_plan = {
            "chains": [{"type": "timeline", "entity": "比亚迪", "days": 90}],
            "scan_summary": {},
        }

        harness.state_store.save(ctx)

        async def mock_agent_run(c):
            assert c.state == AgentState.PLANNING
            c.transition(AgentState.EXECUTING)
            c.chains = []
            c.insights = [_good_insight()]
            c.iteration = 1
            c.total_llm_calls += 1

            c.transition(AgentState.EVALUATING)
            c.evaluation = {"overall_score": 0.8, "passed": True}
            c.transition(AgentState.COMPLETE)
            return c

        with patch.object(AnalysisAgent, "run", side_effect=mock_agent_run):
            result = await harness.resume("plan_resume")

        assert result.state == AgentState.COMPLETE


# ── 9. 状态机完整性 ──

class TestStateMachineIntegrity:
    def test_no_skip_planning(self):
        """不能从 IDLE 跳到 EXECUTING"""
        ctx = RunContext()
        assert ctx.transition(AgentState.EXECUTING) is False
        assert ctx.state == AgentState.IDLE

    def test_no_skip_evaluate(self):
        """不能从 EXECUTING 跳到 COMPLETE"""
        ctx = RunContext()
        ctx.transition(AgentState.PLANNING)
        ctx.transition(AgentState.EXECUTING)
        assert ctx.transition(AgentState.COMPLETE) is False
        assert ctx.state == AgentState.EXECUTING

    def test_refine_must_go_through_execute(self):
        """REFINE 只能到 EXECUTING"""
        ctx = RunContext()
        ctx.transition(AgentState.PLANNING)
        ctx.transition(AgentState.EXECUTING)
        ctx.transition(AgentState.EVALUATING)
        ctx.transition(AgentState.REFINE)
        # 不能直接到 EVALUATING
        assert ctx.transition(AgentState.EVALUATING) is False
        # 只能先到 EXECUTING
        assert ctx.transition(AgentState.EXECUTING) is True

    def test_terminal_states_locked(self):
        """终态不能再转换"""
        for terminal in [AgentState.COMPLETE, AgentState.FAILED]:
            ctx = RunContext()
            ctx.state = terminal
            for target in AgentState:
                if target == terminal:
                    continue
                assert ctx.transition(target) is False
