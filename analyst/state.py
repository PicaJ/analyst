"""
状态机 — Agent 运行状态管理与持久化

状态流转:
  IDLE → PLANNING → EXECUTING → EVALUATING → COMPLETE
                                    │
                                    ├→ REFINE → EXECUTING (重试, 最多 N 次)
                                    └→ FAILED (质量不达标且重试耗尽)

持久化:
  每次状态变更写入 JSON 文件，支持崩溃恢复和 resume
"""

import json
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


class AgentState(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    EVALUATING = "evaluating"
    REFINE = "refine"
    COMPLETE = "complete"
    FAILED = "failed"


# 合法的状态转换
TRANSITIONS = {
    AgentState.IDLE:       {AgentState.PLANNING},
    AgentState.PLANNING:   {AgentState.EXECUTING, AgentState.FAILED},
    AgentState.EXECUTING:  {AgentState.EVALUATING, AgentState.FAILED},
    AgentState.EVALUATING: {AgentState.COMPLETE, AgentState.REFINE, AgentState.FAILED},
    AgentState.REFINE:     {AgentState.EXECUTING, AgentState.FAILED},
    AgentState.COMPLETE:   set(),  # 终态
    AgentState.FAILED:     set(),  # 终态
}


class RunContext:
    """单次 Agent 运行的上下文 — 在闭环迭代中逐步积累"""

    def __init__(self, run_id: Optional[str] = None):
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.state = AgentState.IDLE
        self.created_at = datetime.utcnow().isoformat()
        self.updated_at = self.created_at

        # 输入参数
        self.focus_entity: Optional[str] = None
        self.focus_keywords: List[str] = []
        self.time_window_days: int = 90

        # 执行过程中积累
        self.analysis_plan: Dict[str, Any] = {}
        self.chains: List[Dict[str, Any]] = []
        self.insights: List[Dict[str, Any]] = []
        self.evaluation: Dict[str, Any] = {}
        self.critique: str = ""
        self.refinement_strategy: str = ""

        # 迭代控制
        self.iteration: int = 0
        self.max_iterations: int = 3

        # 指标
        self.total_llm_calls: int = 0
        self.total_tokens: int = 0
        self.total_latency_ms: float = 0
        self.errors: List[str] = []

        # 输出
        self.report_path: Optional[str] = None

    def transition(self, new_state: AgentState) -> bool:
        """状态转换，校验合法性"""
        allowed = TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            logger.warning("Invalid transition: {} → {} (allowed: {})",
                           self.state.value, new_state.value,
                           ", ".join(s.value for s in allowed) if allowed else "none")
            return False
        old_state = self.state
        self.state = new_state
        self.updated_at = datetime.utcnow().isoformat()
        logger.info("[{}] State: {} → {}", self.run_id, old_state.value, new_state.value)
        return True

    @property
    def can_retry(self) -> bool:
        return self.iteration < self.max_iterations

    @property
    def quality_score(self) -> float:
        return self.evaluation.get("overall_score", 0.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "focus_entity": self.focus_entity,
            "focus_keywords": self.focus_keywords,
            "time_window_days": self.time_window_days,
            "analysis_plan": self.analysis_plan,
            "chains": self.chains,
            "insights": self.insights,
            "evaluation": self.evaluation,
            "critique": self.critique,
            "refinement_strategy": self.refinement_strategy,
            "iteration": self.iteration,
            "max_iterations": self.max_iterations,
            "total_llm_calls": self.total_llm_calls,
            "total_tokens": self.total_tokens,
            "total_latency_ms": self.total_latency_ms,
            "errors": self.errors,
            "report_path": self.report_path,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RunContext":
        ctx = cls(run_id=d.get("run_id"))
        ctx.state = AgentState(d.get("state", "idle"))
        ctx.created_at = d.get("created_at", "")
        ctx.updated_at = d.get("updated_at", "")
        ctx.focus_entity = d.get("focus_entity")
        ctx.focus_keywords = d.get("focus_keywords", [])
        ctx.time_window_days = d.get("time_window_days", 90)
        ctx.analysis_plan = d.get("analysis_plan", {})
        ctx.chains = d.get("chains", [])
        ctx.insights = d.get("insights", [])
        ctx.evaluation = d.get("evaluation", {})
        ctx.critique = d.get("critique", "")
        ctx.refinement_strategy = d.get("refinement_strategy", "")
        ctx.iteration = d.get("iteration", 0)
        ctx.max_iterations = d.get("max_iterations", 3)
        ctx.total_llm_calls = d.get("total_llm_calls", 0)
        ctx.total_tokens = d.get("total_tokens", 0)
        ctx.total_latency_ms = d.get("total_latency_ms", 0)
        ctx.errors = d.get("errors", [])
        ctx.report_path = d.get("report_path")
        return ctx


class StateStore:
    """状态持久化 — JSON 文件存储"""

    def __init__(self, state_dir: str):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        return self.state_dir / f"{run_id}.json"

    def save(self, ctx: RunContext):
        """原子写入: 先写临时文件再 rename，防止写到一半崩溃"""
        target = self._path(ctx.run_id)
        tmp = target.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(ctx.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(target)
            logger.debug("State saved: {}", target.name)
        except Exception as e:
            logger.error("Failed to save state {}: {}", ctx.run_id, e)
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def load(self, run_id: str) -> Optional[RunContext]:
        """加载已保存的状态"""
        path = self._path(run_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return RunContext.from_dict(data)
        except Exception as e:
            logger.error("Failed to load state {}: {}", run_id, e)
            return None

    def list_runs(self) -> List[Dict[str, Any]]:
        """列出所有运行记录"""
        runs = []
        for path in sorted(self.state_dir.glob("*.json"), reverse=True):
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
                runs.append({
                    "run_id": d["run_id"],
                    "state": d["state"],
                    "iteration": d.get("iteration", 0),
                    "score": d.get("evaluation", {}).get("overall_score", 0),
                    "updated_at": d.get("updated_at", ""),
                    "errors_count": len(d.get("errors", [])),
                })
            except Exception:
                pass
        return runs
