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

import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

from loguru import logger

from .config import AnalystConfig
from .state import RunContext, AgentState
from .query import NewsQuery
from .chain_builder import ChainBuilder, ClueChain, _count_title_keywords
from .insight_engine import InsightEngine, LLMClient
from .evaluator import Evaluator
from .stock_verify import verify_insight_stocks
from .enrich import NewsEnricher


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
                    ctx.insights, self._chains_data,
                    chains=ctx.chains,
                    scan_total=ctx.analysis_plan.get("scan_summary", {}).get("total_recent", 0),
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

    # 快讯类数据源 (市场热点) vs 公告类数据源 vs 排除的非财经源
    _FLASH_SOURCES = frozenset([
        "cls", "jin10", "thx", "xueqiu", "eastmoney", "sina",
        "wallstreetcn", "cctv", "akshare_cctv", "thepaper",
    ])
    _FILING_SOURCES = frozenset(["eastmoney_notice", "cninfo"])
    _EXCLUDED_SOURCES = frozenset(["gov", "miit"])

    async def _plan(self, ctx: RunContext) -> Dict[str, Any]:
        """扫描数据，规划分析策略

        分层扫描:
          1. 优先扫快讯源 (cls/jin10/thx 等) → 捕获市场热点（全量，不截断）
          2. 补充扫公告源 (eastmoney_notice/cninfo) → 补充个股事件
        实体提取:
          - 优先用数据库字段 (mentioned_companies/ts_codes)
          - 字段为空时从标题分词中提取

        链型选择策略:
          --auto   : timeline + sector_propagation + anomaly + entity_cross (全覆盖)
          --entity : timeline(实体) + 实体扩展链 + sector_propagation + anomaly + entity_cross
          --keywords: sector_propagation(关键词) + timeline(自动) + anomaly + entity_cross
        """
        plan: Dict[str, Any] = {"chains": [], "scan_summary": {}}

        cutoff = (datetime.utcnow() - timedelta(days=ctx.time_window_days)).isoformat()
        now_iso = datetime.utcnow().isoformat()

        # ── 分层扫描 ──
        # 第一层: 快讯源（市场热点，全量获取，并行查询）
        import asyncio
        flash_tasks = [
            self.query.get_by_time_range(cutoff, now_iso, source=src, limit=50000)
            for src in self._FLASH_SOURCES
        ]
        flash_results = await asyncio.gather(*flash_tasks, return_exceptions=True)
        flash_items = []
        for src, result in zip(self._FLASH_SOURCES, flash_results):
            if isinstance(result, Exception):
                logger.warning("[{}] Flash source '{}' query failed: {}", ctx.run_id, src, result)
            else:
                flash_items.extend(result)
        logger.info("[{}] Flash sources: {} items from {} sources",
                     ctx.run_id, len(flash_items), len(self._FLASH_SOURCES))

        # 第二层: 公告源（eastmoney_notice/cninfo，并行查询）
        filing_tasks = [
            self.query.get_by_time_range(cutoff, now_iso, source=src, limit=5000)
            for src in self._FILING_SOURCES
        ]
        filing_results = await asyncio.gather(*filing_tasks, return_exceptions=True)
        filing_items = []
        for src, result in zip(self._FILING_SOURCES, filing_results):
            if isinstance(result, Exception):
                logger.warning("[{}] Filing source '{}' query failed: {}", ctx.run_id, src, result)
            else:
                filing_items.extend(result)

        # 过滤公告源中的常规合规文件 (内部控制、审计报告等，无投资价值)
        filing_before = len(filing_items)
        filing_items = self._filter_routine_filings(filing_items)
        if len(filing_items) < filing_before:
            logger.info("[{}] Filtered routine filings: {} → {}",
                        ctx.run_id, filing_before, len(filing_items))

        # 合并: 快讯在前（优先被分析），公告补充
        recent = flash_items + filing_items
        plan["scan_summary"]["search_mode"] = "layered"
        plan["scan_summary"]["flash_count"] = len(flash_items)
        plan["scan_summary"]["filing_count"] = len(filing_items)
        plan["scan_summary"]["total_recent"] = len(recent)

        # ── Tier 1 规则富化: 补全空字段 (ts_codes→公司名/行业) ──
        enricher = NewsEnricher(self.config)
        recent = enricher.enrich_items(recent)

        # ── 实体统计 ──
        # 快讯实体: 从快讯标题分词中提取（真正的市场热点）
        entity_counts: Dict[str, int] = {}
        sector_counts: Dict[str, int] = {}

        # 快讯实体: 从快讯结构化字段提取
        for item in flash_items:
            for c in (item.get("mentioned_companies") or []):
                entity_counts[c] = entity_counts.get(c, 0) + 1
            for s in (item.get("related_sectors") or []):
                sector_counts[s] = sector_counts.get(s, 0) + 1

        # 公告实体: 从 ts_codes 提取（个股维度）
        for item in filing_items:
            companies = item.get("mentioned_companies") or []
            sectors = item.get("related_sectors") or []
            for c in companies:
                entity_counts[c] = entity_counts.get(c, 0) + 1
            for s in sectors:
                sector_counts[s] = sector_counts.get(s, 0) + 1
            ts_codes = item.get("ts_codes") or []
            for tc in ts_codes:
                entity_counts[tc] = entity_counts.get(tc, 0) + 1

        # 快讯实体: 从标题分词提取（市场热点词）
        flash_kws = _count_title_keywords(flash_items, top_n=30)

        # 记录原始实体集合（用于高频词去重，避免已建链的实体重复出线）
        pre_existing_entities = set(entity_counts.keys())

        for kw, count in flash_kws:
            if count >= 2 and kw not in entity_counts:
                entity_counts[kw] = count

        plan["scan_summary"]["top_entities"] = sorted(
            entity_counts.items(), key=lambda x: -x[1]
        )[:15]
        plan["scan_summary"]["top_sectors"] = sorted(
            sector_counts.items(), key=lambda x: -x[1]
        )[:10]

        # ── 高频词检索: 仅从快讯源提取（避免公告泛词淹没热点）──
        hot_keywords = []
        no_value_kw = frozenset(self.config.no_value_keywords)
        chain_sw = frozenset(self.config.chain_stop_words)
        for kw, count in flash_kws:
            if kw in pre_existing_entities:
                continue
            if any(pk in kw for pk in self._POLITICAL_KEYWORDS):
                continue
            if any(nk in kw for nk in no_value_kw):
                continue
            if kw in chain_sw:
                continue
            if count >= self.config.hot_keyword_threshold:
                hot_keywords.append((kw, count))
        plan["scan_summary"]["hot_keywords"] = hot_keywords[:15]
        if hot_keywords:
            logger.info("[{}] Hot keywords (from flash): {}",
                        ctx.run_id, ", ".join(f"{k}({c})" for k, c in hot_keywords[:15]))

        # ── 行业识别: 从高频词中匹配行业板块 ──
        auto_sector_keywords = self._infer_sectors_from_keywords(
            flash_kws, sector_counts,
        )
        if auto_sector_keywords:
            logger.info("[{}] Auto-detected sectors: {}",
                        ctx.run_id,
                        ", ".join(f"{s}(kws={kws})" for s, kws in auto_sector_keywords))

        # ── tracking keywords 匹配: 检查常驻跟踪关键词是否出现在新闻中 ──
        tracking_kws = self.config.tracking_keywords
        tracking_hits = self._match_tracking_keywords(
            tracking_kws, flash_items + filing_items,
        )
        if tracking_hits:
            logger.info("[{}] Tracking keywords hit: {}",
                        ctx.run_id,
                        ", ".join(f"{k}({c})" for k, c in tracking_hits[:15]))
        plan["scan_summary"]["tracking_hits"] = tracking_hits

        # ── 建链 ──
        chains_to_build: List[Dict[str, Any]] = []
        cfg = self.config

        # === 指定实体模式: timeline + 实体扩展 ===
        if ctx.focus_entity:
            chains_to_build.append({
                "type": "timeline",
                "entity": ctx.focus_entity,
                "entity_type": "company",
                "days": ctx.time_window_days,
            })
            # 实体扩展: 从指定实体的新闻中提取关联板块和实体
            expansion = self._expand_entity(
                ctx.focus_entity, flash_items + filing_items,
            )
            for rel_entity in expansion["related_entities"][:cfg.max_entity_expand_chains]:
                chains_to_build.append({
                    "type": "timeline",
                    "entity": rel_entity,
                    "entity_type": "keyword",
                    "days": ctx.time_window_days,
                })
            for sector_kws in expansion["sector_keywords"][:cfg.max_sector_expand_chains]:
                chains_to_build.append({
                    "type": "sector_propagation",
                    "keywords": sector_kws,
                    "days": ctx.time_window_days,
                })

        # === 指定关键词模式: sector_propagation ===
        if ctx.focus_keywords:
            chains_to_build.append({
                "type": "sector_propagation",
                "keywords": ctx.focus_keywords,
                "days": ctx.time_window_days,
            })

        # === 常驻链: anomaly + entity_cross + semantic (所有模式) ===
        if len(recent) >= 5:
            for _ in range(cfg.max_anomaly_chains):
                chains_to_build.append({
                    "type": "anomaly",
                    "days": ctx.time_window_days,
                })
        if len(recent) >= 3:
            for _ in range(cfg.max_entity_cross_chains):
                chains_to_build.append({
                    "type": "entity_cross",
                    "days": ctx.time_window_days,
                })
        # 语义主题发现: 从未覆盖新闻中发现新兴投资主题
        if len(recent) >= 10:
            chains_to_build.append({
                "type": "semantic_cluster",
                "days": ctx.time_window_days,
            })

        # === auto 模式: 自动选热门实体 + 自动板块链 + tracking keywords ===
        if not ctx.focus_entity and not ctx.focus_keywords:
            auto_entities = []
            # 链构建停用词: 不应独立建链的泛词
            chain_sw = frozenset(cfg.chain_stop_words)
            # tracking keywords 优先: 常驻跟踪关键词在新闻中命中的
            # 但必须跳过 chain_stop_words 中的泛词 (如"美联储"等无直接投资价值的宏观词)
            for kw, _count in tracking_hits:
                if kw not in chain_sw:
                    auto_entities.append(kw)
            # 快讯标题关键词补充 (跳过停用词 + 质量门槛)
            # 构建行业关键词集合，用于判断是否为投资相关实体
            industry_kws = set()
            for aliases in getattr(cfg, "industry_alias", {}).values():
                for a in aliases:
                    if len(a) >= 2:
                        industry_kws.add(a)
            for kw, count in flash_kws:
                if count >= 2 and kw not in auto_entities and kw not in chain_sw:
                    # 质量门槛: 跳过过短词 (< 2 字符) 和过于宽泛的词 (出现次数 > 总新闻数 50%)
                    if len(kw) < 2:
                        continue
                    if count > len(flash_items) * 0.5:
                        continue
                    # 投资相关性门槛: 2 字符词必须匹配行业关键词才允许建链
                    if len(kw) == 2 and kw not in industry_kws:
                        continue
                    # 个股名称过滤: 公司名不应独立建链 (只应作为节点出现在行业链中)
                    if self._is_company_name(kw):
                        continue
                    auto_entities.append(kw)
                if len(auto_entities) >= cfg.max_timeline_chains:
                    break
            # 补充: 公告源的 ts_codes 高频实体 (跳过停用词 + 质量门槛)
            if len(auto_entities) < cfg.max_timeline_chains:
                for entity, count in sorted(entity_counts.items(), key=lambda x: -x[1]):
                    if entity not in auto_entities and count >= 3 and entity not in chain_sw:
                        # 质量门槛: 2 字符词必须匹配行业关键词
                        if len(entity) == 2 and entity not in industry_kws:
                            continue
                        # 个股名称过滤
                        if self._is_company_name(entity):
                            continue
                        auto_entities.append(entity)
                    if len(auto_entities) >= cfg.max_timeline_chains:
                        break
            for entity in auto_entities:
                chains_to_build.append({
                    "type": "timeline",
                    "entity": entity,
                    "entity_type": "keyword",
                    "days": ctx.time_window_days,
                })

            # 自动板块传导链: 行业推断 + tracking keywords 匹配行业
            sector_added = 0
            # tracking keywords 命中且匹配到行业的，优先建板块链
            # 同样跳过 chain_stop_words 中的泛词
            for kw, _count in tracking_hits:
                if sector_added >= cfg.max_sector_chains:
                    break
                if kw in chain_sw:
                    continue
                sector_kws = self._keyword_to_sector_keywords(kw)
                if sector_kws:
                    chains_to_build.append({
                        "type": "sector_propagation",
                        "keywords": sector_kws,
                        "days": ctx.time_window_days,
                    })
                    sector_added += 1
            # 行业推断补充
            for _sector_name, sector_kws in auto_sector_keywords:
                if sector_added >= cfg.max_sector_chains:
                    break
                chains_to_build.append({
                    "type": "sector_propagation",
                    "keywords": sector_kws,
                    "days": ctx.time_window_days,
                })
                sector_added += 1

        # === 非关键词模式下也补建板块链 (有行业热点时) ===
        elif not ctx.focus_keywords and auto_sector_keywords:
            for _sector_name, sector_kws in auto_sector_keywords[:cfg.max_auto_sector_chains]:
                chains_to_build.append({
                    "type": "sector_propagation",
                    "keywords": sector_kws,
                    "days": ctx.time_window_days,
                })

        # 高频词建链 — 为未被已有链覆盖的高频词构建 timeline 链
        covered_entities = {c.get("entity") for c in chains_to_build if c.get("entity")}
        timeline_count = sum(1 for c in chains_to_build if c.get("type") == "timeline")
        for kw, count in hot_keywords:
            if timeline_count >= cfg.max_timeline_chains:
                break
            if kw not in covered_entities:
                if self._is_company_name(kw):
                    continue
                chains_to_build.append({
                    "type": "timeline",
                    "entity": kw,
                    "entity_type": "keyword",
                    "days": ctx.time_window_days,
                })
                covered_entities.add(kw)
                timeline_count += 1

        # === 公司级链: 已禁用 — 个股作为节点出现在行业链中，不独立建链 ===
        if False and not ctx.focus_entity and not ctx.focus_keywords:
            company_ts_counts: Dict[str, int] = {}
            for item in filing_items:
                codes = item.get("ts_codes") or []
                if isinstance(codes, str):
                    try:
                        import json as _json
                        codes = _json.loads(codes)
                    except Exception:
                        codes = []
                for tc in codes:
                    company_ts_counts[tc] = company_ts_counts.get(tc, 0) + 1
            # 高频公司: 公告数 >= 5 条 且未被现有链覆盖
            max_company_chains = getattr(cfg, "max_company_chains", 3)
            company_added = 0
            for tc, cnt in sorted(company_ts_counts.items(), key=lambda x: -x[1]):
                if cnt < 5 or company_added >= max_company_chains:
                    break
                if tc in covered_entities:
                    continue
                chains_to_build.append({
                    "type": "timeline",
                    "entity": tc,
                    "entity_type": "ts_code",
                    "days": ctx.time_window_days,
                })
                covered_entities.add(tc)
                company_added += 1
                logger.debug("[{}] Company chain: {} ({} filings)", ctx.run_id, tc, cnt)

        plan["chains"] = chains_to_build
        logger.info("[{}] Plan: {} chains (timeline={}, sector={}, anomaly={}, cross={}, semantic={})",
                    ctx.run_id, len(chains_to_build),
                    sum(1 for c in chains_to_build if c.get("type") == "timeline"),
                    sum(1 for c in chains_to_build if c.get("type") == "sector_propagation"),
                    sum(1 for c in chains_to_build if c.get("type") == "anomaly"),
                    sum(1 for c in chains_to_build if c.get("type") == "entity_cross"),
                    sum(1 for c in chains_to_build if c.get("type") == "semantic_cluster"))
        return plan

    # ========== Plan 辅助方法 ==========

    def _match_tracking_keywords(
        self,
        tracking_kws: List[str],
        items: List[Dict[str, Any]],
    ) -> List[tuple]:
        """检查常驻跟踪关键词是否出现在新闻标题/实体中，返回命中的关键词及出现次数"""
        kw_counts: Dict[str, int] = {}
        for item in items:
            title = item.get("title", "")
            companies = " ".join(item.get("mentioned_companies") or [])
            sectors = " ".join(item.get("related_sectors") or [])
            text = f"{title} {companies} {sectors}"
            for kw in tracking_kws:
                if kw in text:
                    kw_counts[kw] = kw_counts.get(kw, 0) + 1
        return sorted(kw_counts.items(), key=lambda x: -x[1])

    def _keyword_to_sector_keywords(self, keyword: str) -> List[str] | None:
        """将关键词映射到 industry_alias 中的行业别名列表

        如果关键词出现在某个行业的别名中，返回该行业的别名列表。
        """
        industry_alias = self.config.industry_alias
        for industry, aliases in industry_alias.items():
            if keyword in aliases or keyword == industry:
                return aliases[:5]
        return None

    def _infer_sectors_from_keywords(
        self,
        flash_kws: List[tuple],
        sector_counts: Dict[str, int],
    ) -> List[tuple]:
        """从高频词和板块统计中推断热点行业，生成板块传导关键词

        返回: [(行业名, [关键词列表]), ...]
        """
        industry_alias = self.config.industry_alias
        all_hot_words = set(kw for kw, count in flash_kws if count >= 2)
        # 把板块统计中的高频板块也纳入
        for sector, count in sector_counts.items():
            if count >= 3:
                all_hot_words.add(sector)

        matched: Dict[str, List[str]] = {}
        for industry, aliases in industry_alias.items():
            hits = [a for a in aliases if a in all_hot_words]
            if hits:
                matched[industry] = hits

        result = []
        for industry, aliases in sorted(
            matched.items(), key=lambda x: -len(x[1])
        ):
            result.append((industry, aliases[:5]))
            if len(result) >= 5:
                break
        return result

    def _expand_entity(
        self,
        entity: str,
        items: List[Dict[str, Any]],
        max_related: int = 5,
    ) -> Dict[str, Any]:
        """从指定实体的新闻中提取关联板块和上下游实体

        返回:
          related_entities: 与指定实体共现的其他实体 (去重，最多 max_related 个)
          sector_keywords: 关联板块的行业别名关键词列表
        """
        from collections import Counter

        entity_lower = entity.lower()
        related_companies: Counter = Counter()
        related_sectors: Counter = Counter()

        for item in items:
            # 检查该条新闻是否涉及指定实体
            title = item.get("title", "").lower()
            companies = item.get("mentioned_companies") or []
            sectors = item.get("related_sectors") or []
            ts_codes = item.get("ts_codes") or []

            entity_mentioned = (
                entity_lower in title
                or entity in companies
                or entity in sectors
                or any(entity in tc for tc in ts_codes)
            )
            if not entity_mentioned:
                continue

            # 收集同一条新闻中的其他实体
            for c in companies:
                if c != entity:
                    related_companies[c] += 1
            for s in sectors:
                if s != entity:
                    related_sectors[s] += 1

        # 选取共现 >= 2 次的实体
        top_entities = [
            e for e, cnt in related_companies.most_common(max_related)
            if cnt >= 2
        ]

        # 从关联板块映射到行业别名关键词
        sector_kws_list = []
        industry_alias = self.config.industry_alias
        seen_industries: set = set()
        for sector, _ in related_sectors.most_common(5):
            for industry, aliases in industry_alias.items():
                if industry in seen_industries:
                    continue
                if sector in aliases or sector == industry:
                    sector_kws_list.append(aliases[:5])
                    seen_industries.add(industry)
                    break

        return {
            "related_entities": top_entities,
            "sector_keywords": sector_kws_list[:3],
        }

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

        # 前置过滤: 跳过无投资价值的链（节省 LLM 调用）
        all_chains = self._filter_investment_irrelevant(all_chains)

        # 总链数上限: 子主题分裂可能导致链数爆炸，按重要性保留 top N
        max_total_chains = self.config.max_timeline_chains * 2
        if len(all_chains) > max_total_chains:
            all_chains.sort(key=lambda c: c.significance, reverse=True)
            dropped = len(all_chains) - max_total_chains
            all_chains = all_chains[:max_total_chains]
            logger.info("[{}] Capped chains: {} → {} (dropped {} lowest-significance)",
                        ctx.run_id, len(all_chains) + dropped, max_total_chains, dropped)

        logger.info("[{}] Built {} chains (after significance filter)",
                    ctx.run_id, len(all_chains))

        # [breadth-first] 保留所有链，不合并 — 每条链是独立线索
        # all_chains = self._merge_overlapping_chains(all_chains)
        logger.info("[{}] Keeping {} chains (merge disabled for breadth)",
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

        # 去重: 主题相似的洞察只保留置信度最高的
        logger.info("[{}] Deduplicating {} insights...", ctx.run_id, len(insights))
        insights = self._deduplicate_insights(insights)

        # 后置过滤: 移除低质量洞察
        logger.info("[{}] Filtering low-quality insights...", ctx.run_id)
        insights = self._filter_low_quality_insights(insights)

        # 跨链交叉标注: 为每条 insight 标注其证据节点在其他链类型中的出现
        self._annotate_cross_chain_types(insights, all_chains)

        # 股票验证: 联网核实推荐股票
        data_dir_str = str(self.config.data_dir)
        total_items = sum(len(ins.get("actionable_items", [])) for ins in insights)
        logger.info("[{}] Verifying {} stocks across {} insights...",
                    ctx.run_id, total_items, len(insights))
        for i, ins in enumerate(insights):
            try:
                verify_insight_stocks(ins, data_dir=data_dir_str)
            except Exception as e:
                logger.warning("Stock verification failed for insight {}: {}", i, e)
        logger.info("[{}] Stock verification done.", ctx.run_id)

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
        elif chain_type == "semantic_cluster":
            return await self.chain_builder.build_semantic_theme_chains(
                days=days, max_chains=getattr(self.config, "max_semantic_chains", 3),
            )
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

    @property
    def _POLITICAL_KEYWORDS(self) -> frozenset:
        return frozenset(self.config.political_keywords)

    _COMPANY_SUFFIXES = frozenset(
        "股份 集团 科技 控股 证券 电气 电子 通讯 通信 "
        "医药 生物 化工 新能源 材料 机械 装备 仪器 仪表 "
        "实业 发展 投资 资本 金融 管理咨询".split()
    )

    @staticmethod
    def _is_company_name(name: str) -> bool:
        """判断是否为个股/公司名称 — 公司名不应独立建链"""
        if not name:
            return False
        # ts_code 格式 (如 000333.SZ)
        import re
        if re.match(r'\d{6}\.[A-Z]{2}', name):
            return True
        # 常见公司后缀
        for suffix in AnalysisAgent._COMPANY_SUFFIXES:
            if name.endswith(suffix) and len(name) >= 3:
                return True
        # ST/*ST 前缀
        if name.startswith("ST") or name.startswith("*ST"):
            return True
        return False

    @property
    def _NO_VALUE_KEYWORDS(self) -> frozenset:
        return frozenset(self.config.no_value_keywords)

    def _filter_routine_filings(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """过滤公告源中的常规合规文件 (内部控制、审计报告等，无投资价值)"""
        _FILING = {"eastmoney_notice", "cninfo"}
        filter_kw = self.config.filing_filter_keywords
        kept = []
        for item in items:
            if item.get("source") in _FILING:
                title = item.get("title", "")
                if any(kw in title for kw in filter_kw):
                    continue
            kept.append(item)
        return kept

    def _filter_investment_irrelevant(self, chains: List[ClueChain]) -> List[ClueChain]:
        """前置过滤: 跳过无投资价值的链"""
        kept = []
        for c in chains:
            theme = c.theme
            has_codes = any(n.ts_codes for n in c.nodes)
            # 政治类关键词过滤 (有 ts_codes 例外)
            if any(kw in theme for kw in self._POLITICAL_KEYWORDS):
                if not has_codes:
                    logger.debug("Skipping non-investment chain: {}", theme)
                    continue
            # 无投资价值关键词过滤 (有 ts_codes 例外)
            if any(kw in theme for kw in self._NO_VALUE_KEYWORDS):
                if not has_codes:
                    logger.debug("Skipping no-value chain: {}", theme)
                    continue
            kept.append(c)
        if len(kept) < len(chains):
            logger.info("Filtered non-investment chains: {} → {}", len(chains), len(kept))
        return kept

    def _annotate_cross_chain_types(
        self,
        insights: List[Dict[str, Any]],
        all_chains: List[ClueChain],
    ) -> None:
        """跨链交叉标注: 为每条 insight 的 evidence_ids 标注在其他链类型中的出现

        让 evaluator 能计算信号深度 (一个 insight 被多少种链类型支撑)。
        """
        # 构建 news_id → set(chain_type) 反向索引
        news_to_types: Dict[str, Set[str]] = {}
        chain_type_of: Dict[str, str] = {}
        for c in all_chains:
            chain_type_of[c.chain_id] = c.chain_type
            for n in c.nodes:
                if n.news_id:
                    news_to_types.setdefault(n.news_id, set()).add(c.chain_type)

        # 构建 chain_id → news_id 序号映射 (evidence_ids 用序号 1,2,3...)
        chain_id_to_seq_map: Dict[str, Dict[str, str]] = {}
        for c in all_chains:
            seq_map: Dict[str, str] = {}
            for i, n in enumerate(c.nodes, 1):
                nid = n.news_id
                if nid:
                    seq_map[str(i)] = nid
            chain_id_to_seq_map[c.chain_id] = seq_map

        cross_count = 0
        for ins in insights:
            cid = ins.get("chain_id", "")
            own_type = chain_type_of.get(cid, "")

            # 收集该 insight 涉及的所有链类型
            all_types: Set[str] = set()
            if own_type:
                all_types.add(own_type)

            # 获取该 insight 所属链的序号→news_id 映射
            seq_map = chain_id_to_seq_map.get(cid, {})

            # 从 evidence_ids 中查找跨链类型
            for finding in ins.get("key_findings", []):
                for eid in finding.get("evidence_ids", []):
                    eid_str = str(eid).strip()
                    # 先尝试序号映射
                    resolved_id = seq_map.get(eid_str, eid_str)
                    if resolved_id in news_to_types:
                        all_types.update(news_to_types[resolved_id])

            # 写入 cross_chain_types 字段
            cross_types = all_types - {own_type} if own_type else all_types - {""}
            ins["cross_chain_types"] = sorted(cross_types)
            if len(all_types) >= 2:
                cross_count += 1

        if cross_count:
            logger.info("Cross-chain annotation: {}/{} insights have multi-type support",
                        cross_count, len(insights))

    @staticmethod
    def _merge_overlapping_chains(chains: List[ClueChain], threshold: float = 0.25) -> List[ClueChain]:
        """Pre-LLM 链合并: Jaccard ≥ threshold 的链合并为一条，确定性操作。

        减少主题重叠链数量，降低 LLM 调用次数，提升分析稳定性。
        """
        if len(chains) <= 1:
            return chains

        # 构建每条链的 news_id 集合
        chain_node_ids: List[Set[str]] = []
        for c in chains:
            nids = {n.news_id for n in c.nodes if n.news_id}
            chain_node_ids.append(nids)

        # Union-Find 分组
        n = len(chains)
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(n):
            if not chain_node_ids[i]:
                continue
            for j in range(i + 1, n):
                if not chain_node_ids[j]:
                    continue
                inter = len(chain_node_ids[i] & chain_node_ids[j])
                union_size = len(chain_node_ids[i] | chain_node_ids[j])
                if union_size > 0 and inter / union_size >= threshold:
                    union(i, j)

        # 按组合并
        groups: Dict[int, List[int]] = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(i)

        merged = []
        for members in groups.values():
            if len(members) == 1:
                merged.append(chains[members[0]])
                continue

            # 按 significance 降序排列，保留最高的为主链
            members.sort(key=lambda idx: chains[idx].significance, reverse=True)
            primary = chains[members[0]]
            secondary_themes = []

            # 合并 nodes (去重)
            seen_nids: Set[str] = set()
            all_nodes = []
            for idx in members:
                for node in chains[idx].nodes:
                    if node.news_id not in seen_nids:
                        seen_nids.add(node.news_id)
                        all_nodes.append(node)
                if idx != members[0]:
                    t = chains[idx].theme.split("(")[0].strip()
                    if t not in secondary_themes:
                        secondary_themes.append(t)

            primary.nodes = all_nodes
            if secondary_themes:
                short = primary.theme.split("(")[0].strip()
                extra = " + ".join(secondary_themes[:3])
                primary.theme = f"{short} + {extra} (合并链)"
            primary.links = []
            merged.append(primary)

        if len(merged) < len(chains):
            logger.info("Merged overlapping chains: {} → {}", len(chains), len(merged))
        return merged

    @staticmethod
    def _filter_low_quality_insights(insights: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """广度优先: 仅移除分析失败 (error) 或解析失败 (confidence==0) 的洞察"""
        kept = []
        for ins in insights:
            if ins.get("error") or ins.get("confidence", 0) == 0:
                continue
            kept.append(ins)
        if len(kept) < len(insights):
            logger.info("Filtered failed insights: {} → {}", len(insights), len(kept))
        return kept

    @staticmethod
    def _deduplicate_insights(insights: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """稳定化去重: chain_id分组 + 提高文字匹配阈值"""
        if len(insights) <= 1:
            return insights

        # 步骤0: 按 chain_id 分组，同一条链只保留最高 confidence
        by_chain: Dict[str, List[Dict]] = {}
        for ins in insights:
            cid = ins.get("chain_id", "")
            by_chain.setdefault(cid, []).append(ins)
        unique_chain_insights = []
        for group in by_chain.values():
            if len(group) == 1:
                unique_chain_insights.append(group[0])
            else:
                group.sort(key=lambda x: x.get("confidence", 0), reverse=True)
                unique_chain_insights.append(group[0])
        if len(unique_chain_insights) < len(insights):
            logger.info("Chain-id dedup: {} → {}", len(insights), len(unique_chain_insights))

        insights = unique_chain_insights

        def _lcs_len(s1: str, s2: str) -> int:
            m, n = len(s1), len(s2)
            if m == 0 or n == 0:
                return 0
            prev = [0] * (n + 1)
            best = 0
            for i in range(1, m + 1):
                curr = [0] * (n + 1)
                for j in range(1, n + 1):
                    if s1[i-1] == s2[j-1]:
                        curr[j] = prev[j-1] + 1
                        if curr[j] > best:
                            best = curr[j]
                prev = curr
            return best

        def _ngrams(text: str, n: int = 3) -> Set[str]:
            punct = '，。、！？,:；;""''（）()[]{}'
            clean = text.translate(str.maketrans('', '', punct + ' '))
            return set(clean[i:i+n] for i in range(max(len(clean)-n+1, 1)) if len(clean[i:i+n]) == n)

        kept: List[Dict[str, Any]] = []
        for ins in insights:
            thesis = ins.get("thesis", "")
            # 跳过分析失败的 (空 thesis 或有 error)
            if not thesis or ins.get("error"):
                continue
            is_dup = False
            for i, existing in enumerate(kept):
                ex_thesis = existing.get("thesis", "")
                # 仅当两个 thesis 有较长公共子串 (>=30字) 才视为重复
                if _lcs_len(thesis, ex_thesis) >= 30:
                    is_dup = True
                if is_dup:
                    if ins.get("confidence", 0) > existing.get("confidence", 0):
                        kept[i] = ins
                    break
            if not is_dup:
                kept.append(ins)

        if len(kept) < len(insights):
            logger.info("Deduplicated insights: {} → {}", len(insights), len(kept))
        return kept
