"""
配置管理 — 所有可调参数集中在此

分组:
  data   — 数据路径
  llm    — LLM 连接参数
  chain  — 线索链构建参数
  eval   — 评估器参数
  query  — 查询参数
  agent  — 闭环 Agent 参数
  harness — 调度器参数
  log    — 日志参数
"""

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Optional


def _default_data_dir() -> Path:
    base = Path(os.environ.get("TRADING_DATA_DIR", "~/github_tradingpro/trading/data"))
    return base.expanduser()


@dataclass
class AnalystConfig:
    # ── data ──
    data_dir: Path = field(default_factory=_default_data_dir)
    db_path: str = ""
    index_dir: str = ""
    report_dir: str = ""

    # ── llm ──
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.3
    llm_timeout: float = 120.0
    llm_connect_timeout: float = 30.0

    # ── chain ──
    chain_max_depth: int = 5
    chain_time_window_days: int = 90
    chain_significance_filter: float = 0.3
    chain_timeline_strength: float = 0.5
    chain_sector_strength: float = 0.6
    chain_anomaly_strength: float = 0.8
    chain_cross_strength: float = 0.7
    chain_anomaly_significance: float = 0.8
    chain_cross_base_significance: float = 0.6
    chain_cross_overlap_bonus: float = 0.1
    chain_cross_max_overlap: int = 4
    chain_burst_density_threshold: float = 0.5
    min_cluster_size: int = 3
    insight_max_news: int = 50

    # ── chain significance weights ──
    chain_weight_source_priority: float = 0.3
    chain_weight_sentiment_polarity: float = 0.3
    chain_weight_urgency: float = 0.2
    chain_weight_node_count: float = 0.2
    chain_max_priority_divisor: float = 5.0
    chain_node_count_normalizer: float = 10.0

    # ── eval ──
    quality_threshold: float = 0.65
    eval_weight_evidence: float = 0.25
    eval_weight_reasoning: float = 0.20
    eval_weight_specificity: float = 0.20
    eval_weight_signal: float = 0.20
    eval_weight_consistency: float = 0.15
    eval_hallucination_penalty_per_flag: float = 0.1
    eval_hallucination_max_penalty: float = 0.3
    eval_coverage_multiplier: float = 2.0
    eval_max_hallucination_flags: int = 5
    eval_pass_rate_threshold: float = 0.5

    # ── search ──
    search_mode: str = "hybrid"      # hybrid / keyword / entity / time
    hybrid_alpha: float = 0.7         # 向量权重 (0=纯关键词, 1=纯向量)

    # ── query limits ──
    query_limit_time_range: int = 200
    query_limit_entity: int = 100
    query_limit_urgent: int = 50
    query_limit_timeline: int = 200
    query_limit_cross: int = 300
    query_plan_limit: int = 500

    # ── agent ──
    max_iterations: int = 3
    expansion_factor: float = 1.5

    # ── harness ──
    circuit_breaker_threshold: int = 3

    # ── log ──
    log_dir: str = ""
    log_level: str = "INFO"
    log_retention_days: str = "7 days"
    log_error_retention_days: str = "14 days"
    log_max_size_gb: float = 8.0          # 日志目录达到此大小时触发清理
    log_cleanup_size_gb: float = 5.0      # 清理时删除的最旧日志大小

    def __post_init__(self):
        if isinstance(self.data_dir, str):
            self.data_dir = Path(self.data_dir).expanduser()
        if not self.db_path:
            self.db_path = str(self.data_dir / "news.db")
        if not self.index_dir:
            self.index_dir = str(self.data_dir / "vectors")
        if not self.report_dir:
            self.report_dir = str(self.data_dir / "reports")
        if not self.log_dir:
            self.log_dir = str(Path(__file__).resolve().parent.parent / "logs")

        self.data_dir.mkdir(parents=True, exist_ok=True)
        Path(self.report_dir).mkdir(parents=True, exist_ok=True)

        if not self.llm_api_key:
            self.llm_api_key = os.environ.get("LLM_API_KEY", "")
        if not self.llm_base_url:
            env_url = os.environ.get("LLM_BASE_URL", "")
            if env_url:
                self.llm_base_url = env_url

    def validate(self) -> list[str]:
        errors = []
        if self.quality_threshold < 0 or self.quality_threshold > 1:
            errors.append(f"quality_threshold 范围应为 [0,1], 当前: {self.quality_threshold}")
        if self.max_iterations < 1:
            errors.append(f"max_iterations 应 >= 1, 当前: {self.max_iterations}")
        if self.circuit_breaker_threshold < 1:
            errors.append(f"circuit_breaker_threshold 应 >= 1, 当前: {self.circuit_breaker_threshold}")
        weights_sum = (self.eval_weight_evidence + self.eval_weight_reasoning
                       + self.eval_weight_specificity + self.eval_weight_signal
                       + self.eval_weight_consistency)
        if abs(weights_sum - 1.0) > 0.01:
            errors.append(f"评估权重之和应为 1.0, 当前: {weights_sum:.2f}")
        return errors


def _find_default_config() -> str | None:
    """自动搜索 analyst.yaml 配置文件"""
    candidates = [
        Path("analyst.yaml"),
        Path("config/analyst.yaml"),
        Path(__file__).resolve().parent.parent / "analyst.yaml",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


def load_config(yaml_path: str | None = None) -> AnalystConfig:
    config = AnalystConfig()
    path = yaml_path or _find_default_config()
    if path and Path(path).exists():
        import yaml
        with open(path) as f:
            d = yaml.safe_load(f) or {}
        for k, v in d.items():
            if hasattr(config, k):
                setattr(config, k, v)
        # yaml 覆盖了 dataclass 默认值后，需要重新处理路径展开等逻辑
        config.__post_init__()
    return config
