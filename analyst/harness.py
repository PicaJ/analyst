"""
Harness — Agent 调度器与生命周期管理

职责:
  1. 初始化: 创建 RunContext, 注入参数
  2. 生命周期: 驱动 Agent Loop, 处理状态转换
  3. 错误处理: 捕获异常, 记录失败, 熔断保护
  4. 持久化: 每次状态变更保存到磁盘
  5. 可恢复性: 支持从上次中断处 resume
  6. 指标: 记录运行时间、LLM 调用量、质量分数
  7. 报告: 生成 MD + JSON 输出
"""

import time as _time
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from .config import AnalystConfig
from .state import RunContext, AgentState, StateStore
from .agent import AnalysisAgent
from .report import generate_report, generate_json_report


class HarnessMetrics:
    """运行指标"""

    def __init__(self):
        self.runs_total: int = 0
        self.runs_success: int = 0
        self.runs_failed: int = 0
        self.total_llm_calls: int = 0
        self.total_evaluations: int = 0
        self.total_iterations: int = 0
        self.avg_quality_score: float = 0.0
        self.avg_latency_ms: float = 0.0
        self.circuit_breaker_trips: int = 0

    def record_run(self, ctx: RunContext):
        self.runs_total += 1
        self.total_llm_calls += ctx.total_llm_calls
        self.total_iterations += ctx.iteration
        self.total_evaluations += 1

        if ctx.state == AgentState.COMPLETE:
            self.runs_success += 1
        else:
            self.runs_failed += 1

        n = self.runs_total
        self.avg_quality_score = (
            (self.avg_quality_score * (n - 1) + ctx.quality_score) / n
        )
        self.avg_latency_ms = (
            (self.avg_latency_ms * (n - 1) + ctx.total_latency_ms) / n
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "runs_total": self.runs_total,
            "runs_success": self.runs_success,
            "runs_failed": self.runs_failed,
            "success_rate": round(self.runs_success / max(self.runs_total, 1), 3),
            "total_llm_calls": self.total_llm_calls,
            "total_evaluations": self.total_evaluations,
            "total_iterations": self.total_iterations,
            "avg_quality_score": round(self.avg_quality_score, 3),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "circuit_breaker_trips": self.circuit_breaker_trips,
        }


class Harness:
    """Agent 调度器

    harness 只负责: 初始化上下文 → 调用 agent.run() → 收集结果 → 生成报告
    agent.run() 内部完成整个 Plan→Execute→Evaluate→Refine 闭环
    """

    def __init__(self, config: AnalystConfig):
        self.config = config
        self.state_store = StateStore(str(Path(config.data_dir) / "state"))
        self.metrics = HarnessMetrics()
        self._consecutive_failures = 0

    async def run_analysis(
        self,
        entity: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        days: int = 90,
        max_iterations: int = 3,
        output_report: bool = True,
    ) -> RunContext:
        """启动一次完整的闭环分析"""
        # 熔断检查
        if self._consecutive_failures >= self.config.circuit_breaker_threshold:
            logger.error("Circuit breaker OPEN: {} consecutive failures",
                         self._consecutive_failures)
            self.metrics.circuit_breaker_trips += 1
            ctx = RunContext()
            ctx.errors.append(
                f"熔断保护: 连续 {self._consecutive_failures} 次失败，请检查后重试"
            )
            ctx.state = AgentState.FAILED
            return ctx

        # 初始化上下文
        ctx = RunContext()
        ctx.focus_entity = entity
        ctx.focus_keywords = keywords or []
        ctx.time_window_days = days
        ctx.max_iterations = max_iterations

        logger.info("=== Harness: starting run {} ===", ctx.run_id)
        start_time = _time.monotonic()

        try:
            agent = AnalysisAgent(self.config)
            ctx = await agent.run(ctx)
        except Exception as e:
            logger.error("[{}] Unhandled error: {}", ctx.run_id, e)
            ctx.errors.append(f"Unhandled: {e}")
            ctx.transition(AgentState.FAILED)

        # 持久化最终状态
        elapsed_ms = (_time.monotonic() - start_time) * 1000
        ctx.total_latency_ms = elapsed_ms
        self.state_store.save(ctx)

        # 生成报告
        if output_report:
            try:
                self._generate_output(ctx)
            except Exception as e:
                ctx.errors.append(f"Report generation error: {e}")

        # 更新指标
        self.metrics.record_run(ctx)
        if ctx.state == AgentState.COMPLETE:
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1

        logger.info(
            "=== Harness: run {} finished: state={}, score={:.3f}, "
            "iter={}, latency={:.0f}ms ===",
            ctx.run_id, ctx.state.value, ctx.quality_score,
            ctx.iteration, elapsed_ms,
        )

        return ctx

    async def resume(self, run_id: str) -> Optional[RunContext]:
        """从上次中断处恢复运行"""
        ctx = self.state_store.load(run_id)
        if ctx is None:
            logger.error("Run {} not found", run_id)
            return None

        if ctx.state in (AgentState.COMPLETE, AgentState.IDLE):
            logger.info("Run {} already completed", run_id)
            return ctx

        logger.info("Resuming run {} from state {}", run_id, ctx.state.value)

        start_time = _time.monotonic()
        try:
            agent = AnalysisAgent(self.config)
            ctx = await agent.run(ctx)
        except Exception as e:
            logger.error("[{}] Resume error: {}", run_id, e)
            ctx.errors.append(f"Resume error: {e}")
            ctx.transition(AgentState.FAILED)

        elapsed_ms = (_time.monotonic() - start_time) * 1000
        ctx.total_latency_ms += elapsed_ms
        self.state_store.save(ctx)
        self.metrics.record_run(ctx)

        return ctx

    def status(self) -> Dict[str, Any]:
        """获取 Harness 状态"""
        return {
            "metrics": self.metrics.to_dict(),
            "circuit_breaker": {
                "consecutive_failures": self._consecutive_failures,
                "threshold": self.config.circuit_breaker_threshold,
                "is_open": self._consecutive_failures >= self.config.circuit_breaker_threshold,
            },
            "recent_runs": self.state_store.list_runs()[:10],
        }

    def _generate_output(self, ctx: RunContext):
        """生成输出报告"""
        report_dir = self.config.report_dir

        generate_json_report(ctx, report_dir)

        if ctx.state == AgentState.COMPLETE and ctx.insights:
            path = generate_report(ctx, report_dir)
            ctx.report_path = path
