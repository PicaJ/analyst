"""
分析报告生成

将 LLM 分析结果输出为 Markdown 和 JSON 报告。
报告包含: 洞察详情 + 评估分数 + 迭代历史 + 风险因素 + 行动建议。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from jinja2 import Template
from loguru import logger

from .state import RunContext


REPORT_TEMPLATE = """\
# 财经新闻深度分析报告

> 生成时间: {{ generated_at }}
> 运行 ID: {{ ctx.run_id }}
> 迭代次数: {{ ctx.iteration }}/{{ ctx.max_iterations }}
> 质量分数: {{ "%.3f" | format(ctx.evaluation.get("overall_score", 0)) }}
> LLM 调用: {{ ctx.total_llm_calls }} 次
> 耗时: {{ "%.0f" | format(ctx.total_latency_ms) }}ms

---

## 分析概要

| # | 核心论点 | 置信度 | 时间维度 | 未定价信号 | 链类型 |
|---|---------|-------|---------|-----------|--------|
{% for ins in insights %}
| {{ loop.index }} | {{ ins.get("thesis", "")[:50] }} | {{ "%.0f" | format(ins.get("confidence", 0) * 100) }}% | {{ ins.get("time_horizon", "-") }} | {{ ins.get("hidden_signals", []) | selectattr("not_priced_in") | list | length }} | {{ ins.get("chain_type", "-") }} |
{% endfor %}

---

{% for ins in insights %}
## {{ loop.index }}. {{ ins.get("thesis", "未命名分析") }}

- **置信度**: {{ "%.0f" | format(ins.get("confidence", 0) * 100) }}%
- **时间维度**: {{ ins.get("time_horizon", "未知") }}
- **线索链类型**: {{ ins.get("chain_type", "未知") }}
- **涉及新闻数**: {{ ins.get("node_count", 0) }} 条
- **时间跨度**: {{ ins.get("time_span", "未知") }}

### 核心发现

{% for finding in ins.get("key_findings", []) %}
{{ loop.index }}. **{{ finding.get("finding", "") }}**
   - 推导逻辑: {{ finding.get("reasoning", "") }}
   - 证据: {{ finding.get("evidence_ids", []) | join(", ") }}
{% endfor %}

### 隐蔽信号

{% for signal in ins.get("hidden_signals", []) %}
- **{{ signal.get("signal", "") }}**
  - 潜在影响: {{ signal.get("implication", "") }}
  - {% if signal.get("not_priced_in") %}**尚未被市场定价**{% else %}可能已部分反映在价格中{% endif %}
{% endfor %}

### 风险因素

{% for risk in ins.get("risk_factors", []) %}
- {{ risk }}
{% endfor %}

### 可操作项

{% for item in ins.get("actionable_items", []) %}
- **[{{ item.get("urgency", "medium") | upper }}]** {{ item.get("action", "") }}
  - 目标: {{ item.get("targets", []) | join(", ") }}
{% endfor %}

---

{% endfor %}

## 评估详情

| 维度 | 分数 |
|------|------|
{% for dim_name, dim_score in eval_dims %}
| {{ dim_name }} | {{ "%.3f" | format(dim_score) }} |
{% endfor %}

- **综合评分**: {{ "%.3f" | format(ctx.evaluation.get("overall_score", 0)) }}
- **通过率**: {{ "%.0f" | format(ctx.evaluation.get("pass_rate", 0) * 100) }}%
- **幻觉标记**: {{ ctx.evaluation.get("hallucination_count", 0) }}
- **修正策略**: {{ ctx.refinement_strategy or "无" }}

{% if ctx.errors %}
## 错误记录

{% for e in ctx.errors %}
- {{ e }}
{% endfor %}
{% endif %}

---
*由 analyst 深度分析引擎自动生成 | run_id={{ ctx.run_id }}*
"""


def generate_report(ctx: RunContext, output_dir: str) -> str:
    """生成 Markdown 分析报告"""
    now = datetime.utcnow()
    report_dir = Path(output_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    filename = f"analysis_{now.strftime('%Y%m%d_%H%M%S')}.md"
    filepath = report_dir / filename

    insights = ctx.insights or []
    evaluation = ctx.evaluation or {}

    # 构建评估维度表
    dim_names = {
        "evidence_coverage": "证据覆盖率",
        "reasoning_quality": "推理质量",
        "specificity": "具体性",
        "signal_novelty": "信号新颖性",
        "self_consistency": "自洽性",
    }
    eval_dims = []
    for key, label in dim_names.items():
        score = evaluation.get(key, 0)
        eval_dims.append((label, score))

    template = Template(REPORT_TEMPLATE)
    content = template.render(
        generated_at=now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        ctx=ctx,
        insights=insights,
        eval_dims=eval_dims,
    )

    filepath.write_text(content, encoding="utf-8")
    logger.info("Report saved: {}", filepath)
    return str(filepath)


def generate_json_report(ctx: RunContext, output_dir: str) -> str:
    """生成 JSON 格式报告 (供其他 agent / 程序消费)"""
    now = datetime.utcnow()
    report_dir = Path(output_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    filename = f"analysis_{now.strftime('%Y%m%d_%H%M%S')}.json"
    filepath = report_dir / filename

    report = {
        "generated_at": now.isoformat(),
        "run_id": ctx.run_id,
        "state": ctx.state.value,
        "iteration": ctx.iteration,
        "quality_score": ctx.quality_score,
        "evaluation": ctx.evaluation,
        "total_llm_calls": ctx.total_llm_calls,
        "total_latency_ms": ctx.total_latency_ms,
        "refinement_strategy": ctx.refinement_strategy,
        "focus_entity": ctx.focus_entity,
        "focus_keywords": ctx.focus_keywords,
        "time_window_days": ctx.time_window_days,
        "total_chains": len(ctx.chains),
        "total_insights": len(ctx.insights),
        "insights": ctx.insights,
        "errors": ctx.errors,
    }

    filepath.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("JSON report saved: {}", filepath)
    return str(filepath)
