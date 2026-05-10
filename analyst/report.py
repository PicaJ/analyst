"""
分析报告生成

将 LLM 分析结果输出为 Markdown 和 JSON 报告。
报告包含: 洞察详情 + 评估分数 + 迭代历史 + 风险因素 + 行动建议。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Template
from loguru import logger

from .state import RunContext
from .stock_holders import fetch_top_holders


REPORT_TEMPLATE = """\
# 财经新闻深度分析报告

> 生成时间: {{ generated_at }}
> 运行 ID: {{ ctx.run_id }}
> 迭代次数: {{ ctx.iteration }}/{{ ctx.max_iterations }}
> 质量分数: {{ "%.3f" | format(ctx.evaluation.get("overall_score", 0)) }}
> LLM 调用: {{ ctx.total_llm_calls }} 次
> 耗时: {{ "%.0f" | format(ctx.total_latency_ms) }}ms

---

## 分析过程

1. **数据扫描**: 近 {{ ctx.time_window_days }} 天财经新闻，共扫描 {{ scan_total }} 条
2. **线索链构建**: {{ chain_count }} 条，类型: {% for ct, cnt in chain_type_counts %}{{ ct }} {{ cnt }}条{% if not loop.last %}、{% endif %}{% endfor %}
3. **LLM 深度分析**: 大模型 {{ llm_model }} 分析，综合评分 {{ "%.3f" | format(ctx.evaluation.get("overall_score", 0)) }}
4. **去重过滤**: 最终保留 {{ insights | length }} 条分析
5. **高频词** (阈值≥{{ hot_keyword_threshold }}次): {% for kw, count in hot_keywords %}{{ kw }}({{ count }}){% if not loop.last %}、{% endif %}{% endfor %}{% if not hot_keywords %}无{% endif %}
6. **常驻跟踪关键词**: {% for kw in tracking_keywords %}`{{ kw }}`{% if not loop.last %}、{% endif %}{% endfor %}
7. **跟踪关键词命中**: {% for kw, count in tracking_hits %}{{ kw }}({{ count }}){% if not loop.last %}、{% endif %}{% endfor %}{% if not tracking_hits %}无{% endif %}

### 链路构建参数

| 参数 | 值 | 说明 |
|------|-----|------|
| chain_significance_filter | {{ chain_significance_filter }} | 链重要性过滤阈值 (低于此值的链被丢弃) |
| min_cluster_size | {{ min_cluster_size }} | 最小聚类大小 |
| hot_keyword_threshold | {{ hot_keyword_threshold }} | 高频词最低出现次数 |
| chain_split_threshold | {{ chain_split_threshold }} | 超过此节点数时按子主题拆分 |
| max_subtopic_chains | {{ max_subtopic_chains }} | 每个大链最多拆出的子链数 |
| chain_time_window_days | {{ ctx.time_window_days }} | 时间窗口 (天) |
| max_timeline_chains | {{ max_timeline_chains }} | timeline 链上限 |
| max_sector_chains | {{ max_sector_chains }} | sector 链上限 |

---

## 分析概要

| # | 核心论点 | 置信度 | 时间维度 | 未定价信号 | 链类型 |
|---|---------|-------|---------|-----------|--------|
{%- for ins in insights %}
| {{ loop.index }} | {{ ins.get("thesis", "")[:50] }} | {{ "%.0f" | format(ins.get("confidence", 0) * 100) }}% | {{ ins.get("time_horizon", "-") }} | {{ ins.get("hidden_signals", []) | selectattr("not_priced_in") | list | length }} | {{ ins.get("chain_type", "-") }} |
{%- endfor %}

---
{%- for ins in insights %}

## {{ loop.index }}. {{ ins.get("thesis", "未命名分析") }}

- **置信度**: {{ "%.0f" | format(ins.get("confidence", 0) * 100) }}%
- **逻辑评分**: {{ ins.get("logic_score", "-") }}/100
- **线索链评分**: {{ ins.get("chain_score", "-") }}/100
- **时间维度**: {{ ins.get("time_horizon", "未知") }}
- **线索链类型**: {{ ins.get("chain_type", "未知") }}
- **涉及新闻数**: {{ ins.get("node_count", 0) }} 条
- **时间跨度**: {{ ins.get("time_span", "未知") }}
{%- if ins.get("chain_improvement") %}

### 线索链优化建议
{{ ins.get("chain_improvement") }}
{%- endif %}
{%- if ins.get("detected_stocks") %}

### 消息中搜索到的股票
{{ ins.get("detected_stocks") }}
{%- endif %}

### 核心发现
{%- for finding in ins.get("key_findings", []) %}

{{ loop.index }}. **{{ finding.get("finding", "") }}**
   - 推导逻辑: {{ finding.get("reasoning", "") }}
{%- endfor %}

### 消息来源
{%- set _sources = [] %}
{%- for finding in ins.get("key_findings", []) %}
{%- for eid in finding.get("evidence_ids", []) %}
{%- if eid in news_map %}
{%- if _sources.append(eid) %}{% endif %}
- [{{ news_map[eid].get("source", "") }}] {{ news_map[eid].get("title", "") }} ({{ news_map[eid].get("time", "")[:10] }})
{%- endif %}
{%- endfor %}
{%- endfor %}
{%- if _sources | length == 0 %}
{%- for n in chain_nodes_map.get(ins.get("chain_id", ""), [])[:20] %}
- [{{ n.get("source", "") }}] {{ n.get("title", "") }} ({{ n.get("time", "")[:10] }})
{%- endfor %}
{%- endif %}

### 隐蔽信号
{%- for signal in ins.get("hidden_signals", []) %}

- **{{ signal.get("signal", "") }}**
  - 潜在影响: {{ signal.get("implication", "") }}
  - {% if signal.get("not_priced_in") %}**尚未被市场定价**{% else %}可能已部分反映在价格中{% endif %}
{%- endfor %}

### 风险因素
{%- for risk in ins.get("risk_factors", []) %}
- {{ risk }}
{%- endfor %}

### 可操作项
{%- set items = ins.get("actionable_items", []) %}
{%- if items %}
{%- for item in items %}
- **[{{ item.get("urgency", "medium") | upper }}]** {{ item.get("action", "") }}
{%- if item.get("verified") and item.get("verify_details") %}
{%- for vd in item.get("verify_details", []) %}
{%- if vd.get("verified") %}
  - {{ vd.get("stock_name", vd["code"]) }} ({{ vd["code"] }}){% if vd.get("industry") %} | {{ vd["industry"] }}{% endif %}{% if vd.get("board") %} | {{ vd["board"] }}{% endif %} | 最新价: {{ vd.get("price", "N/A") }} | 今日: {{ vd.get("change_pct", "N/A") }}%{% if vd.get("recent_trend") %} | {{ vd["recent_trend"] }}{% endif %}{% if vd.get("trend_match") == true %} | 走势吻合{% elif vd.get("trend_match") == false %} | 走势不符{% endif %}{% if vd.get("business_match") == false %} | **业务不匹配**: {{ vd.get("business_match_note", "") }}{% endif %}{% if vd.get("disclosure_date") %} | 财报披露: {{ vd["disclosure_date"] }}{% endif %}
{%- if vd.get("alternatives") %}
    - 主板平替: {% for alt in vd["alternatives"] %}{{ alt.name }}({{ alt.code }}) {{ alt.price }}元{% if alt.get("recent_trend") %} {{ alt.recent_trend }}{% endif %}{% if not loop.last %}；{% endif %}{% endfor %}
{%- endif %}
{%- else %}
  - {{ vd["code"] }}: {{ vd.get("error", "验证失败") }}
{%- endif %}
{%- endfor %}
{%- elif item.get("targets") %}
  - 目标: {{ item.get("targets", []) | join(", ") }}
{%- endif %}
{%- for tr in item.get("target_reasons", []) %}
  - **{{ tr.get("code", "") }}**: {{ tr.get("reason", "") }}{% if tr.get("actual_name") %}（实际: {{ tr.get("actual_name") }}{% if tr.get("actual_industry") %}，{{ tr.get("actual_industry") }}{% endif %}）{% endif %}
{%- if tr.get("main_business") %}
    - 主营业务: {{ tr.get("main_business", "") }}{% if tr.get("actual_industry") and tr.get("actual_industry") not in tr.get("main_business", "") %} ⚠️ 实际行业: {{ tr.get("actual_industry") }}{% endif %}
{%- endif %}
{%- if tr.get("core_advantage") %}
    - 核心竞争力: {{ tr.get("core_advantage", "") }}
{%- endif %}
{%- if tr.get("industry_position") %}
    - 行业地位: {{ tr.get("industry_position", "") }}
{%- endif %}
{%- if tr.get("financial_highlight") %}
    - 财报要点: {{ tr.get("financial_highlight", "") }}
{%- endif %}
{%- if tr.get("holder_structure") %}
    - 股东结构: {{ tr.get("holder_structure", "") }}
{%- endif %}
{%- endfor %}
{%- if item.get("reason") and not item.get("target_reasons") %}
  - 推荐理由: {{ item.get("reason") }}
{%- endif %}
{%- endfor %}
{%- else %}
（本分析暂无具体操作建议）
{%- endif %}

### LLM 分析详情
{%- if ins.get("llm_raw") %}

**LLM 输出:**

```
{{ ins.get("llm_raw", "") }}
```
{%- endif %}
{%- if ins.get("llm_input") %}

**LLM 输入:**

```
{{ ins.get("llm_input", "") }}
```
{%- endif %}
{%- endfor %}

---

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

## A 股散户持仓排名 Top 50

> 数据截至: {{ holders_end_date }}（来源: 东方财富）

| 排名 | 代码 | 名称 | 股东户数 | 变化 | 收盘价 | 大股东占比 | 散户占比 |
|------|------|------|---------|------|--------|-----------|---------|
{% for h in top_holders %}
| {{ loop.index }} | {{ h.code }} | {{ h.name }} | {{ h.holder_num_display }} | {{ h.change_display }} | {{ h.price_display }} | {{ h.major_pct_display }} | {{ h.retail_pct_display }} |
{% endfor %}

---
*由 analyst 深度分析引擎自动生成 | run_id={{ ctx.run_id }}*
"""


def generate_report(ctx: RunContext, output_dir: str, config=None) -> str:
    """生成 Markdown 分析报告"""
    now = datetime.now()
    report_dir = Path(output_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    filename = f"analysis_{now.strftime('%Y%m%d_%H%M%S')}.md"
    filepath = report_dir / filename

    insights = ctx.insights or []
    evaluation = ctx.evaluation or {}
    chains = ctx.chains or []

    # 从链数据构建 news_id → {title, source, time} 映射
    news_map: Dict[str, Dict] = {}
    chain_nodes_map: Dict[str, list] = {}  # chain_id → [{title, source, time}, ...]
    for chain in chains:
        cid = chain.get("chain_id", "")
        nodes_list = []
        for node in chain.get("nodes", []):
            nid = node.get("id", "")
            node_info = {
                "title": node.get("title", ""),
                "source": node.get("source", ""),
                "time": node.get("time", ""),
                "companies": node.get("companies", []),
            }
            nodes_list.append(node_info)
            if nid and nid not in news_map:
                news_map[nid] = node_info
        if cid:
            chain_nodes_map[cid] = nodes_list

    # 对每条链的节点按标题去重
    for chain in chains:
        seen_titles = set()
        deduped = []
        for node in chain.get("nodes", []):
            title = node.get("title", "").strip()
            # 去掉 [视频] 等 CCTV 前缀再比较
            clean_title = title.lstrip("[").split("]", 1)[-1].strip() if title.startswith("[") else title
            if clean_title not in seen_titles:
                seen_titles.add(clean_title)
                deduped.append(node)
        chain["deduped_nodes"] = deduped
        chain["deduped_count"] = len(deduped)

    # 统计链类型
    from collections import Counter
    chain_type_counts = Counter(c.get("chain_type", "unknown") for c in chains).most_common()
    chain_type_labels = {
        "timeline": "时间线链（同一实体事件演变）",
        "entity_cross": "实体交叉链（不同实体通过共同关联串联）",
        "sector_propagation": "板块传导链（政策/事件从上游传导到下游）",
        "anomaly": "异常信号链（情绪/频率突变）",
    }
    chain_type_display = [(chain_type_labels.get(ct, ct), cnt) for ct, cnt in chain_type_counts]

    # 扫描总数
    scan_total = ctx.analysis_plan.get("scan_summary", {}).get("total_recent", 0)

    # 高频词
    hot_keywords = ctx.analysis_plan.get("scan_summary", {}).get("hot_keywords", [])

    # 常驻跟踪关键词 & 命中
    tracking_keywords = config.tracking_keywords if config else []
    tracking_hits = ctx.analysis_plan.get("scan_summary", {}).get("tracking_hits", [])

    # LLM 模型名
    llm_model = f"{config.llm_provider}/{config.llm_model}" if config else "unknown"

    # 构建评估维度表
    dim_names = {
        "evidence_coverage": "证据覆盖率",
        "reasoning_quality": "推理质量",
        "specificity": "具体性",
        "signal_novelty": "信号新颖性",
        "self_consistency": "自洽性",
        "investment_relevance": "投资相关性",
    }
    eval_dims = []
    for key, label in dim_names.items():
        score = evaluation.get(key, 0)
        eval_dims.append((label, score))

    # A 股散户持仓排名 Top 50
    data_dir_str = str(config.data_dir) if config else None
    try:
        holders_raw = fetch_top_holders(top_n=50, data_dir=data_dir_str)
    except Exception as e:
        logger.warning("Failed to fetch holder ranking: {}", e)
        holders_raw = []

    holders_end_date = ""
    top_holders = []
    for h in holders_raw:
        if not holders_end_date and h.get("end_date"):
            holders_end_date = h["end_date"]
        holder_num = h.get("holder_num", 0)
        change = h.get("holder_change")
        price = h.get("close_price")
        major_pct = h.get("major_holder_pct")
        retail_pct = h.get("retail_holder_pct")
        top_holders.append({
            "code": h.get("code", ""),
            "name": h.get("name", ""),
            "holder_num_display": f"{holder_num:,}",
            "change_display": f"{change:+,}" if change else "-",
            "price_display": f"{price:.2f}" if price else "-",
            "major_pct_display": f"{major_pct:.1f}%" if major_pct is not None else "-",
            "retail_pct_display": f"{retail_pct:.1f}%" if retail_pct is not None else "-",
        })

    template = Template(REPORT_TEMPLATE)
    content = template.render(
        generated_at=now.strftime("%Y-%m-%d %H:%M:%S"),
        ctx=ctx,
        insights=insights,
        eval_dims=eval_dims,
        news_map=news_map,
        scan_total=scan_total,
        chain_count=len(chains),
        chain_type_counts=chain_type_display,
        llm_model=llm_model,
        hot_keywords=hot_keywords,
        hot_keyword_threshold=config.hot_keyword_threshold if config else 80,
        chain_significance_filter=config.chain_significance_filter if config else 0.3,
        min_cluster_size=config.min_cluster_size if config else 3,
        chain_split_threshold=config.chain_split_threshold if config else 100,
        max_subtopic_chains=config.max_subtopic_chains if config else 5,
        max_timeline_chains=config.max_timeline_chains if config else 20,
        max_sector_chains=config.max_sector_chains if config else 8,
        top_holders=top_holders,
        holders_end_date=holders_end_date or "N/A",
        chains=chains,
        chain_nodes_map=chain_nodes_map,
        chain_type_labels=chain_type_labels,
        tracking_keywords=tracking_keywords,
        tracking_hits=tracking_hits,
    )

    filepath.write_text(content, encoding="utf-8")
    logger.info("Report saved: {}", filepath)
    return str(filepath)


def generate_json_report(ctx: RunContext, output_dir: str) -> str:
    """生成 JSON 格式报告 (供其他 agent / 程序消费)"""
    now = datetime.now()
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
        "chains": ctx.chains,
        "insights": ctx.insights,
        "errors": ctx.errors,
    }

    filepath.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("JSON report saved: {}", filepath)
    return str(filepath)
