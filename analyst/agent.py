"""
Agent 核心 — ReAct 闭环推理引擎

闭环循环:
  Plan    → 扫描数据，决定分析策略
  Execute → 构建线索链 + LLM 分析
  Evaluate → 自评质量 (evaluator)
  Reflect → 质量不够? 选择修正策略
  Refine  → 用新策略调整上下文后重新 Execute

修正策略:
  1. expand_context — 扩大时间窗口/增加新闻数
  2. add_chains     — 补充更多类型的链
  3. critique_revise — 把批评意见反馈给 LLM 重写
"""

import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from loguru import logger

from .config import AnalystConfig
from .state import RunContext, AgentState
from .query import NewsQuery
from .chain_builder import ChainBuilder, ClueChain, _count_title_keywords
from .insight_engine import InsightEngine, LLMClient
from .evaluator import Evaluator


class AnalysisAgent:
    """闭环分析 Agent

    agent.run() 驱动完整的 Plan→Execute→Evaluate→Refine 循环，
    harness 只负责初始化、熔断、报告等调度职责。
    """

    def __init__(self, config: AnalystConfig):
        self.config = config
        self.query = NewsQuery(config)
        self.chain_builder = ChainBuilder(config)
        self.evaluator = Evaluator(config)
        self._chains_data: Dict[str, List[Dict]] = {}
        self._iteration_log: List[Dict[str, Any]] = []

    async def run(self, ctx: RunContext) -> RunContext:
        """执行闭环 Agent 流程

        整体流程:
          1. 如果 ctx 还没有 plan (刚初始化或 resume 从 IDLE) → Plan
          2. 进入 Execute → Evaluate → (Refine → Execute)* 的循环
          3. 循环结束条件: 质量通过 或 达到最大迭代次数
        """
        logger.info("[{}] Agent run started (entity={}, keywords={})",
                     ctx.run_id, ctx.focus_entity, ctx.focus_keywords)

        # ── Phase 1: Plan (仅在首次或从 IDLE 恢复时) ──
        if ctx.state == AgentState.IDLE:
            if not ctx.transition(AgentState.PLANNING):
                return ctx
            try:
                plan = await self._plan(ctx)
                ctx.analysis_plan = plan
                logger.info("[{}] Plan: {} chains to build",
                            ctx.run_id, len(plan.get("chains", [])))
            except Exception as e:
                ctx.errors.append(f"Planning error: {e}")
                ctx.transition(AgentState.FAILED)
                return ctx

        # ── ReAct Loop: Execute → Evaluate → Refine ──
        while ctx.iteration < ctx.max_iterations:
            ctx.iteration += 1
            self._iteration_log.append({
                "iteration": ctx.iteration,
                "strategy": ctx.refinement_strategy,
                "time_window_days": ctx.time_window_days,
            })
            logger.info("[{}] Iteration {}/{} strategy='{}'",
                        ctx.run_id, ctx.iteration, ctx.max_iterations,
                        ctx.refinement_strategy or "initial")

            # ── Phase 2: Execute ──
            if not self._try_transition(ctx, AgentState.EXECUTING):
                return ctx
            try:
                chains, insights = await self._execute(ctx)
                ctx.chains = [c.to_dict() for c in chains]
                ctx.insights = insights
                ctx.total_llm_calls += len(insights)
            except Exception as e:
                ctx.errors.append(f"Execution error (iter {ctx.iteration}): {e}")
                logger.error("[{}] Execution error: {}", ctx.run_id, e)
                if not ctx.can_retry:
                    ctx.transition(AgentState.FAILED)
                    return ctx
                self._apply_refine(ctx, "retry")
                continue

            # ── Phase 3: Evaluate ──
            if not self._try_transition(ctx, AgentState.EVALUATING):
                return ctx
            try:
                evaluation = self.evaluator.evaluate_batch(
                    ctx.insights, self._chains_data
                )
                ctx.evaluation = evaluation
                logger.info("[{}] Eval: score={:.3f}, passed={}",
                            ctx.run_id, evaluation["overall_score"],
                            evaluation["passed"])
            except Exception as e:
                ctx.errors.append(f"Evaluation error: {e}")
                ctx.transition(AgentState.FAILED)
                return ctx

            # ── Decision: pass or refine ──
            if evaluation["passed"]:
                logger.info("[{}] Quality PASSED (score={:.3f})",
                            ctx.run_id, evaluation["overall_score"])
                ctx.transition(AgentState.COMPLETE)
                return ctx

            # ── Phase 4: Reflect + Refine ──
            ctx.critique = evaluation.get("critique", "")
            if not ctx.can_retry:
                logger.warning("[{}] Max iterations reached, score={:.3f}",
                               ctx.run_id, evaluation["overall_score"])
                ctx.transition(AgentState.FAILED)
                return ctx

            strategy = self._select_refinement_strategy(ctx)
            ctx.refinement_strategy = strategy
            logger.info("[{}] Refine: strategy='{}', critique='{}'",
                        ctx.run_id, strategy, ctx.critique[:100])
            self._apply_refine(ctx, strategy)
            ctx.transition(AgentState.REFINE)
            # 循环回到 Phase 2

        # 保险: 不应该到这里
        if ctx.state not in (AgentState.COMPLETE, AgentState.FAILED):
            ctx.transition(AgentState.FAILED)
        return ctx

    # ========== Phase 1: Plan ==========

    async def _plan(self, ctx: RunContext) -> Dict[str, Any]:
        """扫描数据，规划分析策略

        使用混合检索提升初始扫描相关性:
          - 有 entity/keywords 时: 向量+关键词混合搜索
          - 无明确主题时: 时间范围扫描
        """
        plan: Dict[str, Any] = {"chains": [], "scan_summary": {}}

        cutoff = (datetime.utcnow() - timedelta(days=ctx.time_window_days)).isoformat()

        # 根据是否有明确主题选择搜索策略
        search_query = ctx.focus_entity or (
            " ".join(ctx.focus_keywords) if ctx.focus_keywords else ""
        )
        if search_query and self.config.search_mode in ("hybrid", "vector"):
            try:
                recent = await self.query.search_hybrid(
                    query=search_query,
                    top_k=self.config.query_plan_limit,
                    days=ctx.time_window_days,
                    alpha=self.config.hybrid_alpha,
                )
                plan["scan_summary"]["search_mode"] = "hybrid"
            except Exception as e:
                logger.warning("search_hybrid 失败, 降级为 time_range: {}", e)
                recent = await self.query.get_by_time_range(
                    cutoff, datetime.utcnow().isoformat(), limit=self.config.query_plan_limit
                )
                plan["scan_summary"]["search_mode"] = "time_range_fallback"
        else:
            recent = await self.query.get_by_time_range(
                cutoff, datetime.utcnow().isoformat(), limit=self.config.query_plan_limit
            )
            plan["scan_summary"]["search_mode"] = "time_range"

        plan["scan_summary"]["total_recent"] = len(recent)

        # 统计活跃实体
        entity_counts: Dict[str, int] = {}
        sector_counts: Dict[str, int] = {}
        for item in recent:
            for c in (item.get("mentioned_companies") or []):
                entity_counts[c] = entity_counts.get(c, 0) + 1
            for s in (item.get("related_sectors") or []):
                sector_counts[s] = sector_counts.get(s, 0) + 1

        plan["scan_summary"]["top_entities"] = sorted(
            entity_counts.items(), key=lambda x: -x[1]
        )[:10]
        plan["scan_summary"]["top_sectors"] = sorted(
            sector_counts.items(), key=lambda x: -x[1]
        )[:10]

        # 确定要构建的链类型
        chains_to_build: List[Dict[str, Any]] = []

        if ctx.focus_entity:
            chains_to_build.append({
                "type": "timeline",
                "entity": ctx.focus_entity,
                "entity_type": "company",
                "days": ctx.time_window_days,
            })

        if ctx.focus_keywords:
            chains_to_build.append({
                "type": "sector_propagation",
                "keywords": ctx.focus_keywords,
                "days": ctx.time_window_days,
            })

        if len(recent) >= 5:
            chains_to_build.append({
                "type": "anomaly",
                "days": ctx.time_window_days,
            })
        if len(recent) >= 3:
            chains_to_build.append({
                "type": "entity_cross",
                "days": ctx.time_window_days,
            })

        # 自动选热门实体
        if not ctx.focus_entity and not ctx.focus_keywords:
            # 实体字段可能为空，优先用标题关键词
            auto_entities = []
            if entity_counts:
                for entity, count in sorted(
                    entity_counts.items(), key=lambda x: -x[1]
                )[:3]:
                    if count >= 2:
                        auto_entities.append(entity)
            if not auto_entities:
                # 实体字段为空，从标题提取高频关键词
                title_kws = _count_title_keywords(recent, top_n=10)
                for kw, count in title_kws:
                    if count >= 2:
                        auto_entities.append(kw)
                    if len(auto_entities) >= 3:
                        break
            for entity in auto_entities:
                chains_to_build.append({
                    "type": "timeline",
                    "entity": entity,
                    "entity_type": "company",
                    "days": ctx.time_window_days,
                })

        plan["chains"] = chains_to_build
        return plan

    # ========== Phase 2: Execute ==========

    async def _execute(self, ctx: RunContext) -> tuple:
        """构建链 + LLM 分析"""
        all_chains: List[ClueChain] = []
        for chain_spec in ctx.analysis_plan.get("chains", []):
            try:
                chains = await self._build_chain(chain_spec, ctx)
                all_chains.extend(chains)
            except Exception as e:
                logger.warning("[{}] Chain build error for {}: {}",
                               ctx.run_id, chain_spec.get("type"), e)

        if not all_chains:
            logger.warning("[{}] No chains built", ctx.run_id)
            return [], []

        # 过滤低显著性链
        all_chains = [c for c in all_chains if c.significance >= self.config.chain_significance_filter]
        logger.info("[{}] Built {} chains (after significance filter)",
                    ctx.run_id, len(all_chains))

        # 保存链节点数据供评估用
        self._chains_data = {}
        for c in all_chains:
            self._chains_data[c.chain_id] = [
                {
                    "id": n.news_id,
                    "title": n.title,
                    "mentioned_companies": n.mentioned_companies,
                    "related_sectors": n.related_sectors,
                }
                for n in c.nodes
            ]

        # LLM 分析
        engine = InsightEngine(self.config)
        if ctx.critique and ctx.refinement_strategy == "critique_revise":
            engine.set_critique(ctx.critique)

        insights = await engine.analyze_chains(all_chains)
        return all_chains, insights

    async def _build_chain(
        self, spec: Dict[str, Any], ctx: RunContext
    ) -> List[ClueChain]:
        """根据规格构建链"""
        chain_type = spec.get("type", "")
        days = spec.get("days", ctx.time_window_days)

        if chain_type == "timeline":
            return await self.chain_builder.build_timeline_chain(
                entity=spec["entity"],
                entity_type=spec.get("entity_type", "company"),
                days=days,
            )
        elif chain_type == "sector_propagation":
            return await self.chain_builder.build_sector_propagation_chain(
                policy_keywords=spec["keywords"],
                days=days,
            )
        elif chain_type == "anomaly":
            return await self.chain_builder.build_anomaly_chains(days=days)
        elif chain_type == "entity_cross":
            return await self.chain_builder.build_entity_cross_chains(days=days)
        return []

    # ========== Phase 4: Refine ==========

    def _select_refinement_strategy(self, ctx: RunContext) -> str:
        """根据最弱维度选择修正策略 (而非关键词匹配)

        策略映射:
          evidence_coverage 最低 → expand_context (扩大数据窗口)
          reasoning_quality 最低 → critique_revise (反馈批评让 LLM 重写)
          specificity 最低       → critique_revise (反馈批评让 LLM 重写)
          signal_novelty 最低    → add_chains (补充更多链类型)
          幻觉存在              → critique_revise
          兜底                  → expand_context
        """
        evaluation = ctx.evaluation
        individuals = evaluation.get("individual_results", [])

        if not individuals:
            # 没有详细评估结果，从 critique 关键词推断
            return self._strategy_from_critique(ctx.critique)

        # 找所有未通过洞察的最弱维度
        failed = [r for r in individuals if not r.get("passed")]
        if not failed:
            return "expand_context"

        dim_scores = {
            "evidence_coverage": 1.0,
            "reasoning_quality": 1.0,
            "specificity": 1.0,
            "signal_novelty": 1.0,
        }
        for r in failed:
            for dim in dim_scores:
                score = r.get(dim, 0.5)
                dim_scores[dim] = min(dim_scores[dim], score)

        # 幻觉检测优先
        for r in failed:
            if r.get("hallucination_flags"):
                return "critique_revise"

        # 找最低维度
        worst_dim = min(dim_scores, key=dim_scores.get)

        if worst_dim == "evidence_coverage":
            return "expand_context"
        elif worst_dim == "signal_novelty":
            return "add_chains"
        else:
            return "critique_revise"

    @staticmethod
    def _strategy_from_critique(critique: str) -> str:
        """从 critique 关键词推断策略 (fallback)"""
        critique_lower = critique.lower()
        if "证据引用不足" in critique_lower:
            return "expand_context"
        if "隐蔽信号不明显" in critique_lower:
            return "add_chains"
        if "幻觉" in critique_lower:
            return "critique_revise"
        return "expand_context"

    def _apply_refine(self, ctx: RunContext, strategy: str):
        """根据修正策略调整上下文 (在下一轮迭代前执行)"""
        if strategy == "expand_context":
            ctx.time_window_days = int(ctx.time_window_days * self.config.expansion_factor)
            # 同步更新 plan 中各链的 days
            for chain_spec in ctx.analysis_plan.get("chains", []):
                chain_spec["days"] = ctx.time_window_days
            logger.info("[{}] Expanded time window to {} days",
                        ctx.run_id, ctx.time_window_days)

        elif strategy == "add_chains":
            existing_types = {
                c.get("type") for c in ctx.analysis_plan.get("chains", [])
            }
            if "anomaly" not in existing_types:
                ctx.analysis_plan.setdefault("chains", []).append(
                    {"type": "anomaly", "days": ctx.time_window_days}
                )
            if "entity_cross" not in existing_types:
                ctx.analysis_plan.setdefault("chains", []).append(
                    {"type": "entity_cross", "days": ctx.time_window_days}
                )

        elif strategy == "critique_revise":
            # 不改参数，insight_engine 会在下一轮迭代中读到 ctx.critique
            pass

        elif strategy == "retry":
            # 执行出错后的简单重试，不做额外调整
            pass

    # ========== 工具方法 ==========

    @staticmethod
    def _try_transition(ctx: RunContext, target: AgentState) -> bool:
        """尝试状态转换，失败时设置 FAILED"""
        if not ctx.transition(target):
            logger.error("[{}] Cannot transition to {} from {}",
                         ctx.run_id, target.value, ctx.state.value)
            ctx.transition(AgentState.FAILED)
            return False
        return True
