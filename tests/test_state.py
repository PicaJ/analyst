"""测试: 状态机转换与持久化"""

import json
import tempfile
from pathlib import Path

import pytest

from analyst.state import AgentState, RunContext, StateStore, TRANSITIONS


class TestAgentState:
    """状态枚举与转换规则"""

    def test_all_states_exist(self):
        assert AgentState.IDLE
        assert AgentState.PLANNING
        assert AgentState.EXECUTING
        assert AgentState.EVALUATING
        assert AgentState.REFINE
        assert AgentState.COMPLETE
        assert AgentState.FAILED

    def test_valid_transitions(self):
        assert AgentState.PLANNING in TRANSITIONS[AgentState.IDLE]
        assert AgentState.EXECUTING in TRANSITIONS[AgentState.PLANNING]
        assert AgentState.EVALUATING in TRANSITIONS[AgentState.EXECUTING]
        assert AgentState.COMPLETE in TRANSITIONS[AgentState.EVALUATING]
        assert AgentState.REFINE in TRANSITIONS[AgentState.EVALUATING]
        assert AgentState.EXECUTING in TRANSITIONS[AgentState.REFINE]

    def test_terminal_states_have_no_exits(self):
        assert len(TRANSITIONS[AgentState.COMPLETE]) == 0
        assert len(TRANSITIONS[AgentState.FAILED]) == 0


class TestRunContext:
    """RunContext 基本功能"""

    def test_create_default(self):
        ctx = RunContext()
        assert ctx.run_id
        assert ctx.state == AgentState.IDLE
        assert ctx.iteration == 0
        assert ctx.errors == []

    def test_create_with_id(self):
        ctx = RunContext(run_id="test123")
        assert ctx.run_id == "test123"

    def test_valid_transition(self):
        ctx = RunContext()
        assert ctx.transition(AgentState.PLANNING) is True
        assert ctx.state == AgentState.PLANNING

    def test_invalid_transition(self):
        ctx = RunContext()
        assert ctx.transition(AgentState.COMPLETE) is False
        assert ctx.state == AgentState.IDLE

    def test_full_happy_path(self):
        """IDLE → PLANNING → EXECUTING → EVALUATING → COMPLETE"""
        ctx = RunContext()
        assert ctx.transition(AgentState.PLANNING)
        assert ctx.transition(AgentState.EXECUTING)
        assert ctx.transition(AgentState.EVALUATING)
        assert ctx.transition(AgentState.COMPLETE)

    def test_refine_loop(self):
        """EVALUATING → REFINE → EXECUTING → EVALUATING → COMPLETE"""
        ctx = RunContext()
        ctx.transition(AgentState.PLANNING)
        ctx.transition(AgentState.EXECUTING)
        ctx.transition(AgentState.EVALUATING)
        assert ctx.transition(AgentState.REFINE)
        assert ctx.transition(AgentState.EXECUTING)
        assert ctx.transition(AgentState.EVALUATING)
        assert ctx.transition(AgentState.COMPLETE)

    def test_can_retry(self):
        ctx = RunContext()
        ctx.max_iterations = 3
        assert ctx.can_retry is True
        ctx.iteration = 3
        assert ctx.can_retry is False

    def test_quality_score(self):
        ctx = RunContext()
        assert ctx.quality_score == 0.0
        ctx.evaluation = {"overall_score": 0.85}
        assert ctx.quality_score == 0.85

    def test_serialization_roundtrip(self):
        """to_dict → from_dict 保留关键字段"""
        ctx = RunContext(run_id="roundtrip")
        ctx.focus_entity = "比亚迪"
        ctx.focus_keywords = ["新能源"]
        ctx.time_window_days = 60
        ctx.iteration = 2
        ctx.max_iterations = 3
        ctx.evaluation = {"overall_score": 0.72, "passed": True}
        ctx.chains = [{"chain_id": "test", "theme": "test chain"}]
        ctx.insights = [{"thesis": "test insight", "confidence": 0.8}]
        ctx.errors = ["error1"]
        ctx.total_llm_calls = 3

        d = ctx.to_dict()
        ctx2 = RunContext.from_dict(d)

        assert ctx2.run_id == "roundtrip"
        assert ctx2.focus_entity == "比亚迪"
        assert ctx2.focus_keywords == ["新能源"]
        assert ctx2.time_window_days == 60
        assert ctx2.iteration == 2
        assert ctx2.max_iterations == 3
        assert ctx2.quality_score == 0.72
        assert len(ctx2.chains) == 1
        assert len(ctx2.insights) == 1
        assert ctx2.errors == ["error1"]
        assert ctx2.total_llm_calls == 3


class TestStateStore:
    """状态持久化"""

    def test_save_and_load(self, tmp_path):
        store = StateStore(str(tmp_path / "state"))
        ctx = RunContext(run_id="persist1")
        ctx.focus_entity = "特斯拉"
        ctx.evaluation = {"overall_score": 0.9}

        store.save(ctx)
        loaded = store.load("persist1")

        assert loaded is not None
        assert loaded.run_id == "persist1"
        assert loaded.focus_entity == "特斯拉"
        assert loaded.quality_score == 0.9

    def test_load_nonexistent(self, tmp_path):
        store = StateStore(str(tmp_path / "state"))
        assert store.load("nonexistent") is None

    def test_list_runs(self, tmp_path):
        store = StateStore(str(tmp_path / "state"))

        for i in range(3):
            ctx = RunContext(run_id=f"run_{i}")
            ctx.evaluation = {"overall_score": 0.5 + i * 0.1}
            store.save(ctx)

        runs = store.list_runs()
        assert len(runs) == 3
        # 按 updated_at 倒序
        assert runs[0]["run_id"] == "run_2"

    def test_atomic_write(self, tmp_path):
        """验证保存是原子的 (没有残留 .tmp 文件)"""
        store = StateStore(str(tmp_path / "state"))
        ctx = RunContext(run_id="atomic")
        store.save(ctx)

        state_dir = tmp_path / "state"
        tmp_files = list(state_dir.glob("*.tmp"))
        assert len(tmp_files) == 0
        assert (state_dir / "atomic.json").exists()
