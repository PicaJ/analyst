"""测试: 配置与报告"""

import json
from pathlib import Path

import pytest

from analyst.config import AnalystConfig, load_config
from analyst.state import RunContext, AgentState
from analyst.report import generate_report, generate_json_report


class TestAnalystConfig:
    def test_default_config(self, tmp_path):
        cfg = AnalystConfig(data_dir=tmp_path)
        assert cfg.db_path == str(tmp_path / "news.db")
        assert cfg.index_dir == str(tmp_path / "vectors")
        assert cfg.report_dir == str(tmp_path / "reports")
        assert tmp_path.exists()

    def test_validate_ok(self, tmp_path):
        cfg = AnalystConfig(data_dir=tmp_path)
        errors = cfg.validate()
        assert len(errors) == 0

    def test_validate_bad_threshold(self, tmp_path):
        cfg = AnalystConfig(data_dir=tmp_path, quality_threshold=1.5)
        errors = cfg.validate()
        assert any("quality_threshold" in e for e in errors)

    def test_validate_bad_iterations(self, tmp_path):
        cfg = AnalystConfig(data_dir=tmp_path, max_iterations=0)
        errors = cfg.validate()
        assert any("max_iterations" in e for e in errors)

    def test_load_from_yaml(self, tmp_path):
        yaml_path = tmp_path / "test.yaml"
        yaml_path.write_text(
            "llm_provider: deepseek\n"
            "llm_model: deepseek-v4\n"
            "quality_threshold: 0.7\n",
            encoding="utf-8",
        )
        cfg = load_config(str(yaml_path))
        assert cfg.llm_provider == "deepseek"
        assert cfg.llm_model == "deepseek-v4"
        assert cfg.quality_threshold == 0.7

    def test_load_nonexistent_yaml(self):
        cfg = load_config("/nonexistent/path.yaml")
        assert cfg.llm_provider == "openai"  # default

    def test_env_var_api_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "env-test-key")
        cfg = AnalystConfig(data_dir=tmp_path)
        assert cfg.llm_api_key == "env-test-key"


class TestReport:
    def test_generate_json_report(self, tmp_path):
        ctx = RunContext(run_id="report_test")
        ctx.state = AgentState.COMPLETE
        ctx.iteration = 2
        ctx.evaluation = {"overall_score": 0.8, "pass_rate": 1.0}
        ctx.insights = [
            {"thesis": "测试论点", "confidence": 0.8, "chain_type": "timeline"}
        ]
        ctx.chains = [{"chain_id": "c1"}]
        ctx.total_llm_calls = 3
        ctx.total_latency_ms = 1500
        ctx.refinement_strategy = "expand_context"

        output_dir = str(tmp_path / "reports")
        path = generate_json_report(ctx, output_dir)

        assert Path(path).exists()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["run_id"] == "report_test"
        assert data["state"] == "complete"
        assert data["iteration"] == 2
        assert len(data["insights"]) == 1

    def test_generate_markdown_report(self, tmp_path):
        ctx = RunContext(run_id="md_test")
        ctx.state = AgentState.COMPLETE
        ctx.iteration = 1
        ctx.evaluation = {
            "overall_score": 0.75,
            "pass_rate": 1.0,
            "evidence_coverage": 0.8,
            "reasoning_quality": 0.7,
            "specificity": 0.6,
            "signal_novelty": 0.8,
            "self_consistency": 0.9,
        }
        ctx.insights = [
            {
                "thesis": "新能源板块看涨",
                "confidence": 0.85,
                "time_horizon": "中期(1-4周)",
                "chain_type": "timeline",
                "node_count": 5,
                "time_span": "2024-01-01 ~ 2024-01-15",
                "key_findings": [
                    {"finding": "补贴政策", "evidence_ids": ["n1"], "reasoning": "政策利好"}
                ],
                "hidden_signals": [
                    {"signal": "锂矿未涨价", "implication": "成本低", "not_priced_in": True}
                ],
                "risk_factors": ["补贴调整"],
                "actionable_items": [
                    {"action": "关注比亚迪", "urgency": "high", "targets": ["002594.SZ"]}
                ],
            }
        ]
        ctx.chains = [{"chain_id": "c1"}]
        ctx.total_llm_calls = 1
        ctx.total_latency_ms = 2000
        ctx.refinement_strategy = ""

        output_dir = str(tmp_path / "reports")
        path = generate_report(ctx, output_dir)

        assert Path(path).exists()
        content = Path(path).read_text(encoding="utf-8")
        assert "新能源板块看涨" in content
        assert "0.750" in content or "75.0%" in content
        assert "比亚迪" in content
