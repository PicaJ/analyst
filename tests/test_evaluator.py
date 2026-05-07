"""测试: 自评估器"""

import pytest

from analyst.config import AnalystConfig
from analyst.evaluator import Evaluator, EvaluationResult


def _make_nodes(count=5):
    """生成测试用链节点"""
    return [
        {
            "id": f"news_{i}",
            "title": f"测试新闻 {i}",
            "mentioned_companies": ["比亚迪", "特斯拉"],
            "related_sectors": ["新能源", "汽车"],
        }
        for i in range(count)
    ]


def _make_good_insight():
    """高质量洞察"""
    return {
        "thesis": "新能源板块因政策利好迎来上涨周期",
        "confidence": 0.85,
        "time_horizon": "中期(1-4周)",
        "key_findings": [
            {
                "finding": "补贴政策落地",
                "evidence_ids": ["news_0", "news_1", "news_2"],
                "reasoning": "政策文件明确提到新能源汽车补贴延续",
            },
            {
                "finding": "销量数据超预期",
                "evidence_ids": ["news_3", "news_4"],
                "reasoning": "月度销量同比增长40%，超出市场预期",
            },
        ],
        "hidden_signals": [
            {
                "signal": "上游锂矿尚未涨价",
                "implication": "成本端短期无忧",
                "not_priced_in": True,
            },
            {
                "signal": "海外订单激增但未被关注",
                "implication": "出口数据可能超预期",
                "not_priced_in": True,
            },
        ],
        "risk_factors": ["补贴政策可能调整", "原材料价格波动"],
        "actionable_items": [
            {
                "action": "关注比亚迪",
                "urgency": "high",
                "targets": ["002594.SZ", "比亚迪"],
            },
            {
                "action": "关注新能源ETF",
                "urgency": "medium",
                "targets": ["新能源"],
            },
        ],
    }


def _make_poor_insight():
    """低质量洞察"""
    return {
        "thesis": "市场有变化",
        "confidence": 0.3,
        "key_findings": [],
        "hidden_signals": [],
        "risk_factors": [],
        "actionable_items": [],
    }


class TestEvaluator:
    def test_high_quality_passes(self):
        evaluator = Evaluator(AnalystConfig(quality_threshold=0.65))
        nodes = _make_nodes()
        insight = _make_good_insight()
        result = evaluator.evaluate(insight, nodes)

        assert result.passed is True
        assert result.overall_score >= 0.65
        assert result.evidence_coverage > 0
        assert result.reasoning_quality > 0
        assert result.specificity > 0

    def test_low_quality_fails(self):
        evaluator = Evaluator(AnalystConfig(quality_threshold=0.65))
        nodes = _make_nodes()
        insight = _make_poor_insight()
        result = evaluator.evaluate(insight, nodes)

        assert result.passed is False
        assert result.overall_score < 0.65
        assert len(result.critique) > 0

    def test_hallucination_detection(self):
        """引用不存在的 evidence_id 应被检测"""
        evaluator = Evaluator(AnalystConfig(quality_threshold=0.65))
        nodes = _make_nodes(3)
        insight = {
            "thesis": "测试",
            "confidence": 0.5,
            "key_findings": [
                {
                    "finding": "虚假发现",
                    "evidence_ids": ["nonexistent_id_1", "nonexistent_id_2"],
                    "reasoning": "这是虚构的",
                }
            ],
            "hidden_signals": [],
            "risk_factors": [],
            "actionable_items": [
                {
                    "action": "买入某股票",
                    "urgency": "high",
                    "targets": ["完全不相关的公司XYZ"],
                }
            ],
        }

        result = evaluator.evaluate(insight, nodes)
        assert len(result.hallucination_flags) > 0
        # 幻觉惩罚应降低分数
        assert result.overall_score < result.evidence_coverage * 0.25 + 0.75

    def test_evaluate_batch(self):
        evaluator = Evaluator(AnalystConfig(quality_threshold=0.65))
        nodes = _make_nodes()
        good = _make_good_insight()
        good["chain_id"] = "chain_1"
        poor = _make_poor_insight()
        poor["chain_id"] = "chain_2"

        result = evaluator.evaluate_batch(
            [good, poor],
            {"chain_1": nodes, "chain_2": nodes},
        )

        assert "overall_score" in result
        assert "individual_results" in result
        assert len(result["individual_results"]) == 2

    def test_empty_insights(self):
        evaluator = Evaluator(AnalystConfig(quality_threshold=0.65))
        result = evaluator.evaluate_batch([], {})
        assert result["passed"] is False
        assert result["overall_score"] == 0.0

    def test_critique_generation(self):
        evaluator = Evaluator(AnalystConfig(quality_threshold=0.65))
        nodes = _make_nodes()
        insight = _make_poor_insight()
        result = evaluator.evaluate(insight, nodes)

        assert isinstance(result.critique, str)
        assert len(result.critique) > 0


class TestEvaluationResult:
    def test_to_dict(self):
        r = EvaluationResult()
        r.evidence_coverage = 0.8
        r.reasoning_quality = 0.7
        r.overall_score = 0.75
        r.passed = True
        r.hallucination_flags = []
        r.critique = "达标"

        d = r.to_dict()
        assert d["evidence_coverage"] == 0.8
        assert d["passed"] is True
        assert "overall_score" in d
