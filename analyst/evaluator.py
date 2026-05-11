"""
自评估器 — Agent 输出质量的多维度评分与幻觉检测

评分维度 (权重可配):
  1. evidence_coverage   — 结论引用了多少链中节点
  2. reasoning_quality   — 推理逻辑是否连贯
  3. specificity         — 可操作项是否具体 (股票代码/时间)
  4. signal_novelty      — 是否发现了非显而易见的信号
  5. self_consistency    — 结论之间是否矛盾
  6. investment_relevance — 投资相关性和市场价值
"""

import re
from typing import Any, Dict, List, Optional, Set

from loguru import logger

from .config import AnalystConfig


class EvaluationResult:
    """评估结果"""

    def __init__(self):
        self.evidence_coverage: float = 0.0
        self.reasoning_quality: float = 0.0
        self.specificity: float = 0.0
        self.signal_novelty: float = 0.0
        self.self_consistency: float = 0.0
        self.investment_relevance: float = 0.0
        self.hallucination_flags: List[str] = []
        self.overall_score: float = 0.0
        self.passed: bool = False
        self.critique: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evidence_coverage": round(self.evidence_coverage, 3),
            "reasoning_quality": round(self.reasoning_quality, 3),
            "specificity": round(self.specificity, 3),
            "signal_novelty": round(self.signal_novelty, 3),
            "self_consistency": round(self.self_consistency, 3),
            "investment_relevance": round(self.investment_relevance, 3),
            "overall_score": round(self.overall_score, 3),
            "passed": self.passed,
            "hallucination_flags": self.hallucination_flags,
            "critique": self.critique,
        }


class Evaluator:
    """自评估器"""

    _STOCK_CODE_RE = re.compile(r'\d{6}\.[A-Z]{2}')

    def __init__(self, config: AnalystConfig):
        self.config = config
        self._NO_VALUE_KEYWORDS = frozenset(config.no_value_keywords)

    def evaluate(
        self,
        insight: Dict[str, Any],
        chain_nodes: List[Dict[str, Any]],
    ) -> EvaluationResult:
        result = EvaluationResult()

        result.evidence_coverage = self._score_evidence_coverage(insight, chain_nodes)
        result.reasoning_quality = self._score_reasoning_quality(insight)
        result.specificity = self._score_specificity(insight)
        result.signal_novelty = self._score_signal_novelty(insight)
        result.self_consistency = self._score_self_consistency(insight)
        result.investment_relevance = self._score_investment_relevance(insight)

        result.hallucination_flags = self._detect_hallucinations(
            insight, chain_nodes
        )[:self.config.eval_max_hallucination_flags]

        cfg = self.config
        result.overall_score = (
            result.evidence_coverage * cfg.eval_weight_evidence
            + result.reasoning_quality * cfg.eval_weight_reasoning
            + result.specificity * cfg.eval_weight_specificity
            + result.signal_novelty * cfg.eval_weight_signal
            + result.self_consistency * cfg.eval_weight_consistency
            + result.investment_relevance * cfg.eval_weight_investment_relevance
        )

        if result.hallucination_flags:
            penalty = min(
                len(result.hallucination_flags) * cfg.eval_hallucination_penalty_per_flag,
                cfg.eval_hallucination_max_penalty,
            )
            result.overall_score = max(0, result.overall_score - penalty)

        result.passed = result.overall_score >= cfg.quality_threshold
        result.critique = self._generate_critique(result, insight)
        return result

    def evaluate_batch(
        self,
        insights: List[Dict[str, Any]],
        chains_data: Dict[str, List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        results = []
        all_scores = []

        for insight in insights:
            chain_id = insight.get("chain_id", "")
            nodes = chains_data.get(chain_id, [])
            ev = self.evaluate(insight, nodes)
            results.append(ev)
            all_scores.append(ev.overall_score)

        if not all_scores:
            return {
                "overall_score": 0.0,
                "passed": False,
                "individual_results": [],
                "critique": "无洞察结果可评估",
            }

        avg_score = sum(all_scores) / len(all_scores)
        passed_count = sum(1 for ev in results if ev.passed)
        pass_rate = passed_count / len(results)
        cfg = self.config

        overall_passed = (
            avg_score >= cfg.quality_threshold
            and pass_rate >= cfg.eval_pass_rate_threshold
        )

        hallucinations = []
        for ev in results:
            hallucinations.extend(ev.hallucination_flags)

        aggregate_critique = ""
        if not overall_passed:
            parts = []
            if avg_score < cfg.quality_threshold:
                parts.append(f"平均质量分 {avg_score:.2f} 低于阈值 {cfg.quality_threshold}")
            if pass_rate < cfg.eval_pass_rate_threshold:
                parts.append(f"仅 {passed_count}/{len(results)} 条通过评估")
            if hallucinations:
                parts.append(f"检测到 {len(hallucinations)} 个幻觉标记")

            for i, ev in enumerate(results):
                if not ev.passed and ev.critique and ev.critique != "质量达标":
                    parts.append(f"洞察{i+1}: {ev.critique}")

            aggregate_critique = "; ".join(parts)

        # 计算各维度平均分 (供报告展示)
        dim_keys = ["evidence_coverage", "reasoning_quality", "specificity",
                     "signal_novelty", "self_consistency", "investment_relevance"]
        dim_avgs = {}
        for dim in dim_keys:
            vals = [getattr(ev, dim, 0) for ev in results]
            dim_avgs[dim] = round(sum(vals) / len(vals), 3) if vals else 0.0

        return {
            "overall_score": round(avg_score, 3),
            "pass_rate": round(pass_rate, 3),
            "passed": overall_passed,
            "individual_results": [ev.to_dict() for ev in results],
            "hallucination_count": len(hallucinations),
            "critique": aggregate_critique,
            **dim_avgs,
        }

    # ========== 评分方法 ==========

    def _score_evidence_coverage(self, insight: Dict, nodes: List[Dict]) -> float:
        if not nodes:
            return 0.5
        node_ids = {n.get("id") or n.get("news_id", "") for n in nodes}
        cited_ids: Set[str] = set()
        for finding in insight.get("key_findings", []):
            for eid in finding.get("evidence_ids", []):
                cited_ids.add(eid)
        if not cited_ids:
            return 0.1
        coverage = len(cited_ids & node_ids) / max(len(node_ids), 1)
        return min(coverage * self.config.eval_coverage_multiplier, 1.0)

    def _score_reasoning_quality(self, insight: Dict) -> float:
        findings = insight.get("key_findings", [])
        if not findings:
            return 0.1
        has_reasoning = sum(1 for f in findings if f.get("reasoning", "").strip())
        has_finding = sum(1 for f in findings if f.get("finding", "").strip())
        return 0.5 * (has_finding / len(findings)) + 0.5 * (has_reasoning / len(findings))

    def _score_specificity(self, insight: Dict) -> float:
        items = insight.get("actionable_items", [])
        if not items:
            return 0.1  # 无可操作项，直接低分
        score = 0.0
        for item in items:
            if item.get("action", "").strip():
                score += 0.15
            targets = item.get("targets", [])
            has_stock_code = any(self._STOCK_CODE_RE.match(str(t)) for t in targets)
            if has_stock_code:
                score += 0.45
            elif targets and any(len(str(t)) > 2 for t in targets):
                score += 0.05
            if item.get("urgency") in ("high", "medium", "low"):
                score += 0.1
            if item.get("reason", "").strip():
                score += 0.1
            # 验证信息加分
            if item.get("verified"):
                score += 0.2
        return min(score / len(items), 1.0)

    def _score_signal_novelty(self, insight: Dict) -> float:
        signals = insight.get("hidden_signals", [])
        if not signals:
            return 0.2
        not_priced = sum(1 for s in signals if s.get("not_priced_in"))
        has_implication = sum(1 for s in signals if s.get("implication", "").strip())
        return 0.5 * (not_priced / len(signals)) + 0.5 * (has_implication / len(signals))

    def _score_self_consistency(self, insight: Dict) -> float:
        thesis = insight.get("thesis", "").lower()
        confidence = insight.get("confidence", 0.5)
        findings = insight.get("key_findings", [])
        risks = insight.get("risk_factors", [])

        score = 0.5
        if thesis:
            score += 0.15
        if 0 < confidence <= 1:
            score += 0.1
        if findings and risks:
            score += 0.15
        if findings and confidence > 0.7 and len(findings) >= 2:
            score += 0.1

        return min(score, 1.0)

    def _score_investment_relevance(self, insight: Dict) -> float:
        """投资相关性评分 — 无市场价值的事件得极低分"""
        thesis = insight.get("thesis", "")

        # 检测纯政治/社会事件
        if any(kw in thesis for kw in self._NO_VALUE_KEYWORDS):
            return 0.0

        items = insight.get("actionable_items", [])
        if not items:
            # 无可操作项 → 0.1
            return 0.1

        # 有具体股票代码的可操作项 → 高分
        has_codes = False
        for item in items:
            targets = item.get("targets", [])
            if any(self._STOCK_CODE_RE.match(str(t)) for t in targets):
                has_codes = True
                break

        if has_codes:
            score = 0.7
            # 有推荐理由再加
            if any(item.get("reason", "").strip() for item in items):
                score += 0.15
            # 有验证信息再加
            if any(item.get("verified") for item in items):
                score += 0.15
            return min(score, 1.0)

        # 有可操作项但无股票代码 → 中低分
        return 0.3

    def _detect_hallucinations(self, insight: Dict, nodes: List[Dict]) -> List[str]:
        flags = []
        if not nodes:
            return flags

        source_companies: Set[str] = set()
        source_sectors: Set[str] = set()
        for n in nodes:
            for c in (n.get("mentioned_companies") or []):
                source_companies.add(c)
            for s in (n.get("related_sectors") or []):
                source_sectors.add(s)

        # 构建合法 ID 集合: 同时接受原始 news_id 和序号 (字符串 "1", "2", ...)
        node_ids = {n.get("id") or n.get("news_id", "") for n in nodes}
        seq_ids = {str(i) for i in range(1, len(nodes) + 1)}
        valid_ids = node_ids | seq_ids
        for finding in insight.get("key_findings", []):
            for eid in finding.get("evidence_ids", []):
                eid_str = str(eid).strip()
                if eid_str and eid_str not in valid_ids:
                    flags.append(f"引用了不存在的证据ID: {eid_str}")

        for item in insight.get("actionable_items", []):
            for t in item.get("targets", []):
                t_str = str(t)
                if self._STOCK_CODE_RE.match(t_str):
                    continue
                if "." in t_str:
                    continue
                if t_str not in source_companies and t_str not in source_sectors:
                    flags.append(f"操作目标 '{t_str}' 不在源数据实体中")

        return flags

    def _generate_critique(self, result: EvaluationResult, insight: Dict) -> str:
        if result.passed:
            return "质量达标"

        parts = []
        if result.investment_relevance < 0.2:
            parts.append("投资相关性极低，缺乏具体股票推荐或涉及纯政治/社会事件")
        if result.evidence_coverage < 0.4:
            parts.append("证据引用不足，需更多关联到具体新闻")
        if result.reasoning_quality < 0.4:
            parts.append("推理逻辑缺失，需补充 finding → reasoning 链条")
        if result.specificity < 0.4:
            parts.append("可操作项过于笼统，需指定具体股票代码和操作方向")
        if result.signal_novelty < 0.4:
            parts.append("隐蔽信号不明显，需深入挖掘表面之下的关联")
        if result.self_consistency < 0.4:
            parts.append("论点和发现不一致，需加强逻辑连贯性")
        if result.hallucination_flags:
            parts.append(f"检测到幻觉: {'; '.join(result.hallucination_flags[:3])}")

        if not parts:
            parts.append(f"综合评分 {result.overall_score:.2f} 略低于阈值，需进一步打磨")

        return " | ".join(parts)
