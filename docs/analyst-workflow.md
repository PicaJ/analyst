# Analyst 全流程工作文档

> 本文档详细介绍 analyst 模块从数据输入到报告输出的完整闭环流程，包括每个阶段的工作内容、数据流向和核心代码。

---

## 目录

- [1. 系统架构总览](#1-系统架构总览)
- [2. 数据流全景图](#2-数据流全景图)
- [3. 阶段一：配置加载 (Config)](#3-阶段一配置加载-config)
- [4. 阶段二：数据查询 (Query)](#4-阶段二数据查询-query)
- [5. 阶段三：规划 (Plan)](#5-阶段三规划-plan)
- [6. 阶段四：线索链构建 (Chain Building)](#6-阶段四线索链构建-chain-building)
- [7. 阶段五：LLM 洞察分析 (Insight)](#7-阶段五llm-洞察分析-insight)
- [8. 阶段六：自评估 (Evaluate)](#8-阶段六自评估-evaluate)
- [9. 阶段七：反思与修正 (Reflect & Refine)](#9-阶段七反思与修正-reflect--refine)
- [10. 阶段八：报告输出 (Report)](#10-阶段八报告输出-report)
- [11. 调度器与生命周期管理 (Harness)](#11-调度器与生命周期管理-harness)
- [12. 状态持久化与恢复 (State)](#12-状态持久化与恢复-state)
- [13. CLI 命令参考](#13-cli-命令参考)

---

## 1. 系统架构总览

Analyst 是一个 **ReAct 闭环自评估财经新闻分析 Agent**。与线性管道不同，它通过 Plan → Execute → Evaluate → Reflect → Refine 的迭代循环，不断优化输出质量，直到满足质量阈值或达到最大迭代次数。

### 模块文件结构

```
analyst/
├── main.py                  # CLI 入口
├── analyst.yaml              # YAML 配置文件
├── pyproject.toml            # 项目依赖
└── analyst/                  # 核心包
    ├── config.py             # 配置管理 (AnalystConfig)
    ├── state.py              # 状态机与持久化 (RunContext / StateStore)
    ├── query.py              # 数据查询接口 (NewsQuery)
    ├── chain_builder.py      # 四类线索链构建 (ChainBuilder)
    ├── insight_engine.py     # LLM 分析引擎 (InsightEngine / LLMClient)
    ├── evaluator.py          # 多维度自评估 (Evaluator)
    ├── agent.py              # ReAct 闭环推理引擎 (AnalysisAgent)
    ├── harness.py            # 调度器与生命周期管理 (Harness)
    └── report.py             # 报告生成 (Markdown / JSON)
```

### 核心类关系

```
Harness (调度器)
  └─ AnalysisAgent (闭环引擎)
       ├─ NewsQuery (数据查询)
       ├─ ChainBuilder (线索链构建)
       ├─ InsightEngine → LLMClient (LLM 分析)
       └─ Evaluator (自评估)
  └─ StateStore (状态持久化)
  └─ Report (报告生成)
```

---

## 2. 数据流全景图

```
┌─────────────────────────────────────────────────────────────────┐
│                     indexagent (上游)                            │
│                   SQLite: news.db                                │
│              news_items / news_fts / embeddings                  │
└──────────────────────┬──────────────────────────────────────────┘
                       │ SQL 查询
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│  Query.py (NewsQuery)                                            │
│  get_by_entity / get_by_time_range / get_timeline / get_urgent   │
└──────────────────────┬──────────────────────────────────────────┘
                       │ List[Dict] 原始新闻记录
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│  ChainBuilder                                                    │
│  ┌──────────┐ ┌───────────────┐ ┌──────────┐ ┌──────────────┐   │
│  │ Timeline │ │ Sector Prop.  │ │ Anomaly  │ │ Entity Cross │   │
│  └────┬─────┘ └──────┬────────┘ └────┬─────┘ └──────┬───────┘   │
│       └──────────┬────┴───────────────┴──────┬──────┘           │
│                  ▼                            │                   │
│           List[ClueChain]  ◄─────────────────┘                   │
│           (含 ChainNode + ChainLink + hidden_signals)            │
└──────────────────────┬───────────────────────────────────────────┘
                       │ 结构化线索链
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│  InsightEngine (LLM 分析)                                        │
│  SYSTEM_PROMPT + CHAIN_ANALYSIS_PROMPT → LLM → JSON 洞察        │
│  输出: thesis / key_findings / hidden_signals / actionable_items │
└──────────────────────┬───────────────────────────────────────────┘
                       │ List[Dict] LLM 洞察结果
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│  Evaluator (自评估)                                               │
│  5 维度评分 + 幻觉检测 → overall_score                           │
│  passed? ──YES──→ COMPLETE ──→ Report                           │
│          ──NO───→ critique + refinement_strategy ──→ Refine      │
└──────────────────────┬───────────────────────────────────────────┘
                       │ (未通过时)
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│  Refine (修正策略)                                                │
│  expand_context / add_chains / critique_revise / multi_perspective│
│  → 调整参数 → 回到 Execute 阶段                                  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. 阶段一：配置加载 (Config)

### 工作内容

从 YAML 文件和环境变量加载配置，初始化数据目录和报告目录。配置与 indexagent 共享同一 `data_dir`，确保两个模块读写同一个 SQLite 数据库。

### 数据流

```
analyst.yaml + 环境变量 → AnalystConfig → 注入到所有下游模块
```

### 核心配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `data_dir` | `~/github_tradingpro/trading/data` | 与 indexagent 共享的数据目录 |
| `db_path` | `{data_dir}/news.db` | SQLite 数据库路径 |
| `llm_provider` | `deepseek` | LLM 提供商: openai / anthropic / ollama |
| `llm_model` | `deepseek-v4` | 模型名称 |
| `chain_time_window_days` | 90 | 线索链时间窗口（天） |
| `max_iterations` | 3 | 闭环最大迭代次数 |
| `quality_threshold` | 0.65 | 质量通过阈值 (0~1) |
| `circuit_breaker_threshold` | 3 | 连续失败熔断阈值 |

### 核心代码

```python
# analyst/config.py

@dataclass
class AnalystConfig:
    # 路径 — 与 indexagent 共享同一数据目录
    data_dir: Path = field(default_factory=_default_data_dir)
    db_path: str = ""
    index_dir: str = ""

    # LLM
    llm_provider: str = "openai"      # openai / anthropic / ollama
    llm_model: str = "gpt-4o"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.3

    # 分析参数
    chain_max_depth: int = 5
    chain_time_window_days: int = 90
    min_cluster_size: int = 3
    insight_max_news: int = 50

    # 闭环 Agent 参数
    max_iterations: int = 3
    quality_threshold: float = 0.65
    circuit_breaker_threshold: int = 3

    def __post_init__(self):
        if not self.db_path:
            self.db_path = str(self.data_dir / "news.db")
        if not self.report_dir:
            self.report_dir = str(self.data_dir / "reports")
        # 从环境变量读取 API key
        if not self.llm_api_key:
            self.llm_api_key = os.environ.get("LLM_API_KEY", "")

def load_config(yaml_path: str | None = None) -> AnalystConfig:
    config = AnalystConfig()
    if yaml_path and Path(yaml_path).exists():
        import yaml
        with open(yaml_path) as f:
            d = yaml.safe_load(f) or {}
        for k, v in d.items():
            if hasattr(config, k):
                setattr(config, k, v)
    return config
```

---

## 4. 阶段二：数据查询 (Query)

### 工作内容

NewsQuery 是 analyst 与 indexagent 之间的数据访问层。它直接读取 indexagent 写入的 SQLite 数据库 (`news.db`)，提供按时间范围、实体、关键词等维度的新闻查询，不直接操作 FAISS 向量索引。

### 数据流

```
indexagent 写入 → news.db (SQLite)
                       │
                       ▼
              NewsQuery 异步读取
                       │
          ┌────────────┼────────────┐────────────┐
          ▼            ▼            ▼            ▼
     get_by_entity  get_by_time   get_timeline  get_urgent
          │            │            │            │
          └────────────┴────────────┴────────────┘
                       │
                       ▼
              List[Dict] 原始新闻记录
```

### 查询的数据库字段

NewsQuery 从 `news_items` 表读取以下全部字段：

```
id, title, content, summary, url, source, category,
publish_time, collect_time, ts_codes, tags, keywords,
author, source_priority, sentiment, sentiment_score,
impact_scope, related_sectors, policy_level, urgency,
mentioned_companies, mentioned_persons, mentioned_amounts,
content_hash, title_hash, event_id, is_primary, extra
```

### 核心查询方法

| 方法 | 用途 | 调用方 |
|------|------|--------|
| `get_by_entity()` | 按公司/板块/人物查询 | Plan 扫描、Timeline 链 |
| `get_by_time_range()` | 按时间范围批量获取 | Plan 扫描、Entity Cross 链 |
| `get_timeline()` | 按关键词获取时间线 | Sector Propagation 链 |
| `get_urgent()` | 获取高优先级/紧急新闻 | Anomaly 链 |
| `get_by_ids()` | 按 ID 批量获取 | 内部使用 |

### 核心代码

```python
# analyst/query.py

class NewsQuery:
    """新闻查询接口"""

    def __init__(self, config: AnalystConfig):
        self.config = config
        self.db_path = config.db_path

    async def get_by_entity(
        self,
        company: Optional[str] = None,
        sector: Optional[str] = None,
        person: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """实体查询 — 按 mentioned_companies / related_sectors / mentioned_persons 筛选"""
        db = await self._db()
        try:
            conditions = []
            params: list = []
            if company:
                conditions.append("mentioned_companies LIKE ?")
                params.append(f"%{company}%")
            if sector:
                conditions.append("related_sectors LIKE ?")
                params.append(f"%{sector}%")
            if person:
                conditions.append("mentioned_persons LIKE ?")
                params.append(f"%{person}%")
            if start:
                conditions.append("publish_time >= ?")
                params.append(start)
            if end:
                conditions.append("publish_time <= ?")
                params.append(end)
            where = " AND ".join(conditions) if conditions else "1=1"
            sql = f"SELECT {_COLUMNS} FROM news_items WHERE {where} ORDER BY publish_time ASC LIMIT ?"
            params.append(limit)
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
            return [_parse_row(r) for r in rows]
        finally:
            await db.close()

    async def get_timeline(
        self,
        keywords: Optional[List[str]] = None,
        ts_code: Optional[str] = None,
        days: int = 90,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """获取某个主题的时间线 — 按 title LIKE 关键词筛选"""
        # ... (构建 SQL, 按 publish_time ASC 排序)
```

JSON 字段（`ts_codes`, `tags`, `keywords`, `related_sectors`, `mentioned_companies` 等）在 `_parse_row()` 中自动从 JSON 字符串反序列化为 Python 列表。

---

## 5. 阶段三：规划 (Plan)

### 工作内容

Plan 阶段是 ReAct 闭环的第一个环节。Agent 扫描时间窗口内的全部新闻，统计活跃实体和板块热度，然后根据用户指定的输入参数（实体 / 关键词 / 自动模式）决定构建哪些类型的线索链。

### 数据流

```
用户输入 (entity / keywords / auto)
         │
         ▼
    NewsQuery.get_by_time_range(近N天全部新闻)
         │
         ▼
    统计 mentioned_companies / related_sectors 频次
         │
         ▼
    ┌──────────────────────────────────────┐
    │ 决策逻辑:                             │
    │  指定了 entity    → timeline 链      │
    │  指定了 keywords  → sector 链        │
    │  数据量 >= 5      → anomaly 链       │
    │  数据量 >= 3      → entity_cross 链  │
    │  什么都没指定     → 选 top3 热门实体  │
    └──────────────────────────────────────┘
         │
         ▼
    analysis_plan = {
        "chains": [
            {"type": "timeline", "entity": "比亚迪", "days": 90},
            {"type": "anomaly", "days": 90},
            {"type": "entity_cross", "days": 90}
        ],
        "scan_summary": {
            "total_recent": 150,
            "top_entities": [("比亚迪", 12), ...],
            "top_sectors": [("新能源", 25), ...]
        }
    }
```

### 核心代码

```python
# analyst/agent.py — AnalysisAgent._plan()

async def _plan(self, ctx: RunContext) -> Dict[str, Any]:
    """扫描数据，规划分析策略"""
    plan = {"chains": [], "scan_summary": {}}

    cutoff = (datetime.utcnow() - timedelta(days=ctx.time_window_days)).isoformat()
    recent = await self.query.get_by_time_range(cutoff, datetime.utcnow().isoformat(), limit=500)
    plan["scan_summary"]["total_recent"] = len(recent)

    # 统计活跃实体
    entity_counts: Dict[str, int] = {}
    sector_counts: Dict[str, int] = {}
    for item in recent:
        for c in (item.get("mentioned_companies") or []):
            entity_counts[c] = entity_counts.get(c, 0) + 1
        for s in (item.get("related_sectors") or []):
            sector_counts[s] = sector_counts.get(s, 0) + 1

    # 确定要构建的链类型
    chains_to_build = []
    if ctx.focus_entity:
        chains_to_build.append({
            "type": "timeline", "entity": ctx.focus_entity,
            "entity_type": "company", "days": ctx.time_window_days,
        })
    if ctx.focus_keywords:
        chains_to_build.append({
            "type": "sector_propagation", "keywords": ctx.focus_keywords,
            "days": ctx.time_window_days,
        })
    if len(recent) >= 5:
        chains_to_build.append({"type": "anomaly", "days": ctx.time_window_days})
    if len(recent) >= 3:
        chains_to_build.append({"type": "entity_cross", "days": ctx.time_window_days})

    # 自动模式: 选择热门实体
    if not ctx.focus_entity and not ctx.focus_keywords:
        for entity, count in sorted(entity_counts.items(), key=lambda x: -x[1])[:3]:
            if count >= 2:
                chains_to_build.append({
                    "type": "timeline", "entity": entity,
                    "entity_type": "company", "days": ctx.time_window_days,
                })

    plan["chains"] = chains_to_build
    return plan
```

---

## 6. 阶段四：线索链构建 (Chain Building)

### 工作内容

ChainBuilder 是 analyst 的核心数据结构构建器。它从原始新闻记录中发现隐蔽的因果/关联链条，生成四种类型的结构化线索链。每条链由节点 (`ChainNode`)、边 (`ChainLink`) 和隐蔽信号 (`hidden_signals`) 组成。

### 数据结构

```python
@dataclass
class ChainNode:
    """线索链节点 — 一条新闻"""
    news_id: str
    title: str
    publish_time: str
    source: str
    source_priority: int
    category: str
    sentiment: Optional[str] = None
    urgency: str = "normal"
    ts_codes: List[str] = field(default_factory=list)
    mentioned_companies: List[str] = field(default_factory=list)
    mentioned_persons: List[str] = field(default_factory=list)
    related_sectors: List[str] = field(default_factory=list)

@dataclass
class ChainLink:
    """线索链边 — 两个节点之间的关联"""
    from_id: str
    to_id: str
    link_type: str    # temporal / entity / sector / anomaly
    strength: float   # 0.0 ~ 1.0
    reason: str = ""  # 关联原因（供 LLM 理解）

@dataclass
class ClueChain:
    """线索链"""
    chain_id: str
    chain_type: str   # timeline / entity_cross / sector_propagation / anomaly
    theme: str
    nodes: List[ChainNode] = field(default_factory=list)
    links: List[ChainLink] = field(default_factory=list)
    significance: float = 0.0
    hidden_signals: List[str] = field(default_factory=list)
```

### 四种线索链详解

#### 6.1 时间链 (Timeline Chain)

**目标**：跟踪同一实体（公司/板块/人物）的事件演变，发现情绪转折点。

**算法**：
1. 查询该实体在时间窗口内的所有新闻
2. 按发布时间排序，相邻节点自动创建 `temporal` 链接
3. 检测情绪变化信号（如 neutral → positive/negative，positive → negative 反转）
4. 计算重要性评分（来源权威度 30% + 情绪极性 30% + 紧急度 20% + 节点数 20%）

**数据流**：
```
entity="比亚迪" → get_by_entity(company="比亚迪") → 按时间排序
    → 相邻节点连线 (temporal) → _detect_sentiment_shifts() → ClueChain
```

**隐蔽信号示例**：
- `情绪转变: neutral→positive (从沉默到表态，值得关注)`
- `情绪转变: positive→negative (利好转利空，重大反转信号)`

#### 6.2 板块传导链 (Sector Propagation Chain)

**目标**：追踪政策/事件从上游板块传导到下游板块的路径，发现滞后反应机会。

**算法**：
1. 按关键词查询相关新闻
2. 将新闻按 `related_sectors` 分组
3. 按各板块首次出现的时间排序，构建板块间传导路径
4. 从板块 A 最新新闻 → 板块 B 最早新闻 创建 `sector` 链接

**数据流**：
```
keywords=["芯片","制裁"] → get_timeline(keywords) → 按 related_sectors 分组
    → 按板块首次出现时间排序 → 板块间传导链接 → ClueChain
```

**隐蔽信号示例**：
- `传导路径: 半导体(2024-01-15) → 消费电子(2024-01-22), 消费电子可能存在滞后反应机会`

#### 6.3 异常链 (Anomaly Chain)

**目标**：检测消息密度异常的实体，可能暗示未公开信息（内幕信息泄露）。

**算法**：
1. 获取近期高优先级/紧急新闻
2. 按实体分组，检测消息爆发（短时间大量出现）
3. 时间密度阈值：平均每 2 小时至少 1 条 (`density >= 0.5`)
4. 高密度实体构建 `anomaly` 链，强度固定为 0.8

**数据流**：
```
get_urgent(days=30) → 按实体分组 → _detect_entity_bursts()
    → 过滤 min_cluster_size → 异常链 (strength=0.8)
```

**隐蔽信号示例**：
- `某某公司在30天内出现8条消息，密度异常`
- `可能存在未被市场充分反映的信息`

#### 6.4 实体交叉链 (Entity Cross Chain)

**目标**：发现不同实体（公司/板块）之间的隐蔽关联，通过共同出现在多篇新闻中识别。

**算法**：
1. 查询时间窗口内全部新闻
2. 构建实体 → 新闻的倒排索引
3. 对每个实体，找到与之共同出现在多篇新闻中的其他实体
4. 取交集新闻构建 `entity` 链，重要性 = 0.6 + 0.1 × 共现次数

**数据流**：
```
get_by_time_range() → 构建 entity_map (实体→新闻倒排索引)
    → 找共同关联实体对 → 取交集新闻 → 按 significance 排序 → top 10
```

**隐蔽信号示例**：
- `A公司与B公司出现4次共同报道`
- `两个实体的关联可能尚未被市场充分定价`

### 核心代码 (以时间链为例)

```python
# analyst/chain_builder.py

async def build_timeline_chain(
    self, entity: str, entity_type: str = "company", days: int = 90,
) -> List[ClueChain]:
    """构建时间线索链"""
    kwargs = {}
    if entity_type == "company":
        kwargs["company"] = entity
    elif entity_type == "sector":
        kwargs["sector"] = entity

    items = await self.query.get_by_entity(
        **kwargs,
        start=(datetime.utcnow() - timedelta(days=days)).isoformat(),
        limit=200,
    )
    if len(items) < 2:
        return []

    nodes = [ChainNode.from_dict(it) for it in items]
    links = []

    # 按时间排序，相邻节点自动链接
    nodes.sort(key=lambda n: n.publish_time or "")
    for i in range(len(nodes) - 1):
        n1, n2 = nodes[i], nodes[i + 1]
        links.append(ChainLink(
            from_id=n1.news_id, to_id=n2.news_id,
            link_type="temporal", strength=0.5,
            reason=f"同一{entity_type}({entity})的时间演变",
        ))

    # 检测情绪变化信号
    sentiment_shifts = self._detect_sentiment_shifts(nodes)

    return [ClueChain(
        chain_id=f"timeline_{entity}_{datetime.utcnow().strftime('%Y%m%d')}",
        chain_type="timeline",
        theme=f"{entity} 事件时间线 ({days}天)",
        nodes=nodes, links=links,
        significance=self._calc_significance(nodes),
        hidden_signals=sentiment_shifts,
    )]
```

---

## 7. 阶段五：LLM 洞察分析 (Insight)

### 工作内容

InsightEngine 将结构化线索链交给 LLM 进行深度分析。LLM 基于精心设计的系统提示词（6 条分析原则）和线索链数据，推导隐蔽的因果逻辑，发现市场尚未充分反映的信息，生成结构化 JSON 洞察。

### 数据流

```
List[ClueChain]
      │
      ▼ _format_news_list() 格式化为可读文本
      │
SYSTEM_PROMPT (6条分析原则) + CHAIN_ANALYSIS_PROMPT (线索链数据)
      │
      ▼ LLMClient.complete()
      │
┌─────┴──────┐
│ LLM Provider│
│ openai     │ → POST /v1/chat/completions
│ anthropic  │ → POST /v1/messages
│ ollama     │ → POST /api/chat
└─────┬──────┘
      │ raw JSON string
      ▼ _parse_llm_response()
      │
Dict (结构化洞察):
{
  "chain_id": "...",
  "thesis": "核心论点",
  "confidence": 0.0-1.0,
  "time_horizon": "短期/中期/长期",
  "key_findings": [{"finding", "evidence_ids", "reasoning"}],
  "hidden_signals": [{"signal", "implication", "not_priced_in"}],
  "risk_factors": [...],
  "actionable_items": [{"action", "urgency", "targets"}]
}
```

### LLM 系统提示词（6 条分析原则）

1. **交叉验证**: 多源信息互相印证才可信
2. **时间序列**: 关注事件的时间先后顺序，寻找因果关系
3. **传导链条**: 政策→行业→个股的传导路径
4. **异常信号**: 情绪突然转变、消息密度异常、关联方异动
5. **隐蔽关联**: 表面不相关的事件之间可能存在深层联系
6. **市场定价**: 评估当前信息是否已被股价充分反映

### 核心代码

```python
# analyst/insight_engine.py

SYSTEM_PROMPT = """你是一位资深财经分析师，擅长从大量新闻中寻找隐蔽的因果逻辑和
未被市场充分反映的信息。

你的分析原则:
1. 交叉验证: 多源信息互相印证才可信
2. 时间序列: 关注事件的时间先后顺序，寻找因果关系
3. 传导链条: 政策→行业→个股的传导路径
4. 异常信号: 情绪突然转变、消息密度异常、关联方异动
5. 隐蔽关联: 表面不相关的事件之间可能存在深层联系
6. 市场定价: 评估当前信息是否已被股价充分反映

输出格式要求（严格遵守 JSON）:
{
  "thesis": "核心论点（一句话）",
  "confidence": 0.0-1.0,
  "time_horizon": "短期(1-5天)/中期(1-4周)/长期(1-6月)",
  "key_findings": [{"finding", "evidence_ids", "reasoning"}],
  "hidden_signals": [{"signal", "implication", "not_priced_in"}],
  "risk_factors": ["..."],
  "actionable_items": [{"action", "urgency", "targets"}]
}"""


class InsightEngine:
    async def analyze_chain(self, chain: ClueChain) -> Dict[str, Any]:
        """分析单条线索链"""
        user_prompt = CHAIN_ANALYSIS_PROMPT.format(
            chain_type=chain.chain_type,
            theme=chain.theme,
            time_span=chain.time_span,
            significance=f"{chain.significance:.2f}",
            hidden_signals="; ".join(chain.hidden_signals) or "无",
            news_list=_format_news_list(chain.nodes, self.config.insight_max_news),
        )

        raw = await self.llm.complete(SYSTEM_PROMPT, user_prompt)
        result = self._parse_llm_response(raw)
        result["chain_id"] = chain.chain_id
        return result

    async def analyze_chains(self, chains: List[ClueChain]) -> List[Dict[str, Any]]:
        """批量分析线索链，按置信度排序"""
        results = []
        for i, chain in enumerate(chains):
            result = await self.analyze_chain(chain)
            results.append(result)
        results.sort(key=lambda r: r.get("confidence", 0), reverse=True)
        return results
```

### LLM 客户端 (统一接口)

```python
# analyst/insight_engine.py — LLMClient

class LLMClient:
    """统一的 LLM 客户端，支持 OpenAI / Anthropic / Ollama"""

    async def complete(self, system: str, user: str) -> str:
        if self.provider == "openai":
            return await self._call_openai(system, user)
        elif self.provider == "anthropic":
            return await self._call_anthropic(system, user)
        elif self.provider == "ollama":
            return await self._call_ollama(system, user)

    async def _call_openai(self, system, user):
        url = f"{self.base_url or 'https://api.openai.com'}/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, json=payload, headers=headers)
            return resp.json()["choices"][0]["message"]["content"]

    async def _call_anthropic(self, system, user):
        url = f"{self.base_url or 'https://api.anthropic.com'}/v1/messages"
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, json=payload, headers=headers)
            return resp.json()["content"][0]["text"]

    async def _call_ollama(self, system, user):
        url = f"{self.base_url or 'http://localhost:11434'}/api/chat"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(url, json=payload)
            return resp.json()["message"]["content"]
```

---

## 8. 阶段六：自评估 (Evaluate)

### 工作内容

Evaluator 对 LLM 输出的每条洞察进行 5 个维度的质量评分，并执行幻觉检测。评分结果决定分析是否通过，未通过则生成批评意见驱动下一轮修正。

### 数据流

```
List[Dict] (LLM 洞察) + Dict[str, List[Dict]] (链节点数据)
         │
         ▼ Evaluator.evaluate_batch()
         │
    ┌────┴────────────────────────────────────┐
    │ 对每条洞察执行:                           │
    │                                         │
    │ 1. evidence_coverage (25%)              │
    │    → 结论引用了多少链中节点               │
    │                                         │
    │ 2. reasoning_quality (20%)              │
    │    → finding + reasoning 是否完整        │
    │                                         │
    │ 3. specificity (20%)                    │
    │    → actionable_items 是否有具体目标     │
    │                                         │
    │ 4. signal_novelty (20%)                 │
    │    → hidden_signals 中 not_priced_in    │
    │                                         │
    │ 5. self_consistency (15%)               │
    │    → thesis / findings / risks 一致性   │
    │                                         │
    │ + 幻觉检测:                              │
    │   → evidence_ids 是否存在于源数据        │
    │   → actionable targets 是否在源实体中    │
    │   → 每个幻觉标记扣 0.1 分 (最多扣 0.3)   │
    └────┬────────────────────────────────────┘
         │
         ▼
    evaluation = {
        "overall_score": 0.72,       # 加权平均
        "pass_rate": 0.75,           # 单条通过率
        "passed": true,              # 整体通过条件: 均分 ≥ 0.65 且 通过率 ≥ 50%
        "individual_results": [...], # 每条洞察的详细评分
        "hallucination_count": 0,
        "critique": ""               # 未通过时的批评意见
    }
```

### 评分权重

```
overall_score = evidence_coverage × 0.25
              + reasoning_quality × 0.20
              + specificity       × 0.20
              + signal_novelty    × 0.20
              + self_consistency  × 0.15
              - hallucination_penalty (每个幻觉 × 0.1, 最多 0.3)
```

### 通过条件

```
passed = (avg_score ≥ quality_threshold) AND (pass_rate ≥ 50%)
```

### 核心代码

```python
# analyst/evaluator.py

class Evaluator:
    def evaluate(self, insight: Dict, chain_nodes: List[Dict]) -> EvaluationResult:
        result = EvaluationResult()

        # 1. 证据覆盖率
        result.evidence_coverage = self._score_evidence_coverage(insight, chain_nodes)
        # 2. 推理质量
        result.reasoning_quality = self._score_reasoning_quality(insight)
        # 3. 具体性
        result.specificity = self._score_specificity(insight)
        # 4. 信号新颖性
        result.signal_novelty = self._score_signal_novelty(insight)
        # 5. 自洽性
        result.self_consistency = self._score_self_consistency(insight)

        # 幻觉检测
        result.hallucination_flags = self._detect_hallucinations(insight, chain_nodes)

        # 综合评分 (加权)
        result.overall_score = (
            result.evidence_coverage * 0.25
            + result.reasoning_quality * 0.20
            + result.specificity * 0.20
            + result.signal_novelty * 0.20
            + result.self_consistency * 0.15
        )

        # 幻觉惩罚
        if result.hallucination_flags:
            penalty = min(len(result.hallucination_flags) * 0.1, 0.3)
            result.overall_score = max(0, result.overall_score - penalty)

        result.passed = result.overall_score >= self.quality_threshold
        result.critique = self._generate_critique(result, insight)
        return result

    def evaluate_batch(self, insights, chains_data) -> Dict:
        """批量评估 → 聚合"""
        results = [self.evaluate(insight, chains_data.get(insight.get("chain_id", ""), []))
                   for insight in insights]
        avg_score = sum(ev.overall_score for ev in results) / len(results)
        pass_rate = sum(1 for ev in results if ev.passed) / len(results)
        overall_passed = avg_score >= self.quality_threshold and pass_rate >= 0.5
        return {
            "overall_score": round(avg_score, 3),
            "pass_rate": round(pass_rate, 3),
            "passed": overall_passed,
            "individual_results": [ev.to_dict() for ev in results],
            "critique": aggregate_critique,
        }
```

### 幻觉检测逻辑

```python
def _detect_hallucinations(self, insight, nodes) -> List[str]:
    """验证 LLM 输出与源数据的一致性"""
    flags = []

    # 收集源数据实体
    source_companies = {c for n in nodes for c in (n.get("mentioned_companies") or [])}
    source_sectors = {s for n in nodes for s in (n.get("related_sectors") or [])}
    node_ids = {n.get("id") or n.get("news_id", "") for n in nodes}

    # 检查 evidence_ids 是否存在
    for finding in insight.get("key_findings", []):
        for eid in finding.get("evidence_ids", []):
            if eid and eid not in node_ids:
                flags.append(f"引用了不存在的证据ID: {eid}")

    # 检查 action targets 是否在源数据实体中
    for item in insight.get("actionable_items", []):
        for t in item.get("targets", []):
            if "." not in t and t not in source_companies and t not in source_sectors:
                flags.append(f"操作目标 '{t}' 不在源数据实体中")

    return flags[:5]
```

---

## 9. 阶段七：反思与修正 (Reflect & Refine)

### 工作内容

当评估未通过时，Agent 进入反思阶段：根据批评意见自动选择修正策略，调整上下文参数后重新进入 Execute 阶段。这是 ReAct 闭环的核心——系统自我纠正。

### 数据流

```
evaluation.passed = false
         │
         ▼
    critique = evaluation["critique"]
         │
         ▼ _select_refinement_strategy()
         │
    ┌────┴─────────────────────────────────────────┐
    │ 策略选择逻辑:                                  │
    │                                              │
    │ "证据引用不足"  → expand_context               │
    │ "推理逻辑缺失"  → critique_revise              │
    │ "笼统/具体"     → critique_revise              │
    │ "隐蔽信号不明显" → add_chains                  │
    │ "幻觉"          → critique_revise              │
    │ 默认            → expand_context               │
    └────┬─────────────────────────────────────────┘
         │
         ▼ refine_context()
         │
    ┌──────────────────────────────────┐
    │ expand_context:                   │
    │   time_window_days *= 1.5        │
    │                                  │
    │ add_chains:                       │
    │   补充 anomaly / entity_cross 链 │
    │                                  │
    │ critique_revise:                  │
    │   不改参数，LLM 用批评意见重写    │
    └──────────────────────────────────┘
         │
         ▼ 回到 Execute 阶段 (iteration++)
```

### 核心代码

```python
# analyst/agent.py

def _select_refinement_strategy(self, ctx: RunContext) -> str:
    """根据批评意见选择修正策略"""
    critique = ctx.critique.lower()

    if "证据引用不足" in critique:
        return "expand_context"
    if "推理逻辑缺失" in critique:
        return "critique_revise"
    if "笼统" in critique or "具体" in critique:
        return "critique_revise"
    if "隐蔽信号不明显" in critique:
        return "add_chains"
    if "幻觉" in critique:
        return "critique_revise"
    return "expand_context"

async def refine_context(self, ctx: RunContext) -> None:
    """根据修正策略调整上下文"""
    strategy = ctx.refinement_strategy

    if strategy == "expand_context":
        ctx.time_window_days = int(ctx.time_window_days * 1.5)

    elif strategy == "add_chains":
        existing_types = {c.get("type") for c in ctx.analysis_plan.get("chains", [])}
        if "anomaly" not in existing_types:
            ctx.analysis_plan.setdefault("chains", []).append(
                {"type": "anomaly", "days": ctx.time_window_days}
            )
        if "entity_cross" not in existing_types:
            ctx.analysis_plan.setdefault("chains", []).append(
                {"type": "entity_cross", "days": ctx.time_window_days}
            )

    elif strategy == "critique_revise":
        pass  # 不改参数，靠 ctx.critique 注入 LLM prompt
```

### ReAct 闭环主循环

```python
# analyst/agent.py — AnalysisAgent.run()

async def run(self, ctx: RunContext) -> RunContext:
    # Phase 1: Plan
    ctx.transition(AgentState.PLANNING)
    ctx.analysis_plan = await self._plan(ctx)

    # ReAct Loop: Execute → Evaluate → Refine
    while ctx.iteration < ctx.max_iterations:
        ctx.iteration += 1

        # Phase 2: Execute (构建链 + LLM 分析)
        ctx.transition(AgentState.EXECUTING)
        chains, insights = await self._execute(ctx)
        ctx.chains = [c.to_dict() for c in chains]
        ctx.insights = insights

        # Phase 3: Evaluate (5 维度评分 + 幻觉检测)
        ctx.transition(AgentState.EVALUATING)
        evaluation = self.evaluator.evaluate_batch(ctx.insights, self._chains_data)
        ctx.evaluation = evaluation

        # 通过 → 完成
        if evaluation["passed"]:
            ctx.transition(AgentState.COMPLETE)
            return ctx

        # Phase 4: Reflect (选择修正策略)
        ctx.critique = evaluation.get("critique", "")
        if not ctx.can_retry:
            ctx.transition(AgentState.FAILED)
            return ctx

        ctx.refinement_strategy = self._select_refinement_strategy(ctx)
        ctx.transition(AgentState.REFINE)
        # → 回到循环顶部, refine_context 由 Harness 调用

    ctx.transition(AgentState.FAILED)
    return ctx
```

---

## 10. 阶段八：报告输出 (Report)

### 工作内容

将 LLM 分析结果输出为 Markdown 和 JSON 两种格式的报告。JSON 报告供其他 agent 消费，Markdown 报告供人类阅读。

### 数据流

```
ctx.insights (List[Dict])
         │
         ├──→ generate_json_report() → analysis_YYYYMMDD_HHMMSS.json
         │    (供其他 agent 程序消费)
         │
         └──→ generate_report() → analysis_YYYYMMDD_HHMMSS.md
              (人类可读的 Markdown 报告)
```

### Markdown 报告结构

```markdown
# 财经新闻深度分析报告

> 生成时间: 2024-01-15 10:30:00 UTC
> 分析链数: 3
> 数据时间范围: 2024-01-01 ~ 2024-01-15

---

## 1. 半导体板块受制裁影响传导至消费电子
- **置信度**: 85%
- **时间维度**: 中期(1-4周)
- **线索链类型**: sector_propagation

### 核心发现
1. **美国对华芯片出口管制升级**
   - 推导逻辑: 从政策到行业的传导
   - 证据: news_001, news_005

### 隐蔽信号
- **消费电子板块存在滞后反应机会**
  - 潜在影响: 可能带来 2-3 周的窗口期
  - **尚未被市场定价**

### 可操作项
- [HIGH] 关注消费电子板块低估值标的
  - 目标: 000001.SZ, 000002.SZ

---

## 分析摘要
| # | 核心论点 | 置信度 | 时间维度 | 未定价信号 |
|---|---------|-------|---------|-----------|
| 1 | ...     | 85%   | 中期    | 2         |
```

### 核心代码

```python
# analyst/report.py

def generate_report(chains, output_dir, time_range="") -> str:
    """生成 Markdown 分析报告"""
    now = datetime.utcnow()
    report_dir = Path(output_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    filename = f"analysis_{now.strftime('%Y%m%d_%H%M%S')}.md"
    filepath = report_dir / filename

    template = Template(REPORT_TEMPLATE)
    content = template.render(
        generated_at=now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        chains=chains,
        time_range=time_range or "未知",
    )
    filepath.write_text(content, encoding="utf-8")
    return str(filepath)

def generate_json_report(chains, output_dir) -> str:
    """生成 JSON 格式报告"""
    report = {
        "generated_at": now.isoformat(),
        "total_chains": len(chains),
        "chains": chains,
    }
    filepath.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return str(filepath)
```

---

## 11. 调度器与生命周期管理 (Harness)

### 工作内容

Harness 是 analyst 的顶层调度器，负责初始化上下文、驱动 Agent 闭环循环、处理状态持久化、实现熔断保护、记录运行指标。

### 数据流

```
CLI / API 调用
      │
      ▼ Harness.run_analysis()
      │
      ├── 1. 熔断检查 (连续失败 ≥ 3 次?)
      │
      ├── 2. 创建 RunContext (注入 entity/keywords/days)
      │
      ├── 3. 循环驱动 Agent:
      │     while state not in (COMPLETE, FAILED):
      │       agent.run(ctx)
      │       state_store.save(ctx)
      │       if REFINE:
      │         agent.refine_context(ctx)
      │         state_store.save(ctx)
      │
      ├── 4. 生成报告 (JSON + Markdown)
      │
      ├── 5. 最终持久化
      │
      └── 6. 更新 metrics
             ├── 成功 → consecutive_failures = 0
             └── 失败 → consecutive_failures += 1
```

### 熔断保护机制

```
连续失败 0 次 → CLOSED (正常)
连续失败 1 次 → CLOSED
连续失败 2 次 → CLOSED
连续失败 3 次 → OPEN (熔断, 拒绝新请求)
                → 需要人工检查后重置
```

### 核心代码

```python
# analyst/harness.py

class Harness:
    CIRCUIT_BREAKER_THRESHOLD = 3

    async def run_analysis(self, entity=None, keywords=None, days=90,
                           max_iterations=3, output_report=True) -> RunContext:
        # 熔断检查
        if self._consecutive_failures >= self.CIRCUIT_BREAKER_THRESHOLD:
            logger.error("Circuit breaker OPEN")
            # ... 返回失败上下文

        # 初始化上下文
        ctx = RunContext()
        ctx.focus_entity = entity
        ctx.focus_keywords = keywords or []
        ctx.time_window_days = days
        ctx.max_iterations = max_iterations

        start_time = _time.monotonic()
        try:
            agent = AnalysisAgent(self.config)
            while ctx.state not in (AgentState.COMPLETE, AgentState.FAILED):
                ctx = await agent.run(ctx)
                self.state_store.save(ctx)
                if ctx.state == AgentState.REFINE:
                    await agent.refine_context(ctx)
                    self.state_store.save(ctx)
                    continue
                # 防卡死: 状态不变且非终态
                if ctx.state == prev_state and ctx.state not in terminal_states:
                    ctx.transition(AgentState.FAILED)
                    break
        except Exception as e:
            ctx.errors.append(f"Unhandled: {e}")
            ctx.transition(AgentState.FAILED)

        # 报告 + 持久化 + 指标
        if output_report:
            self._generate_output(ctx)
        self.state_store.save(ctx)
        self.metrics.record_run(ctx)

        return ctx

    async def resume(self, run_id: str) -> Optional[RunContext]:
        """从上次中断处恢复运行"""
        ctx = self.state_store.load(run_id)
        agent = AnalysisAgent(self.config)
        while ctx.state not in (AgentState.COMPLETE, AgentState.FAILED):
            ctx = await agent.run(ctx)
            self.state_store.save(ctx)
            if ctx.state == AgentState.REFINE:
                await agent.refine_context(ctx)
        self.metrics.record_run(ctx)
        return ctx
```

---

## 12. 状态持久化与恢复 (State)

### 工作内容

State 模块管理 Agent 的运行状态、上下文积累和崩溃恢复。每次状态变更自动写入 JSON 文件，支持从任意中断点恢复。

### 状态机转换图

```
IDLE ──→ PLANNING ──→ EXECUTING ──→ EVALUATING ──→ COMPLETE
                         ↑               │               ↑
                         │               ↓               │
                       REFINE ←───── (未通过)          (通过)
                         │
                         └──→ FAILED (重试耗尽 / 异常)
```

合法转换：
```
IDLE       → {PLANNING}
PLANNING   → {EXECUTING, FAILED}
EXECUTING  → {EVALUATING, FAILED}
EVALUATING → {COMPLETE, REFINE, FAILED}
REFINE     → {EXECUTING, FAILED}
COMPLETE   → {IDLE}
FAILED     → {IDLE}
```

### RunContext 在闭环迭代中的积累过程

```
iteration 0:
  analysis_plan = {} → {chains: [...], scan_summary: {...}}

iteration 1 (first execute):
  chains = [ClueChain1, ClueChain2, ...]
  insights = [{thesis, confidence, key_findings, ...}, ...]
  evaluation = {overall_score: 0.55, passed: false, critique: "..."}
  critique = "证据引用不足..."
  refinement_strategy = "expand_context"

iteration 2 (refined execute):
  chains = [new chains with expanded window...]
  insights = [refined insights...]
  evaluation = {overall_score: 0.72, passed: true}
  → COMPLETE
```

### 核心代码

```python
# analyst/state.py

class RunContext:
    """单次 Agent 运行的上下文"""
    def __init__(self, run_id=None):
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.state = AgentState.IDLE

        # 输入参数
        self.focus_entity = None
        self.focus_keywords = []
        self.time_window_days = 90

        # 执行积累
        self.analysis_plan = {}
        self.chains = []
        self.insights = []
        self.evaluation = {}
        self.critique = ""
        self.refinement_strategy = ""

        # 迭代控制
        self.iteration = 0
        self.max_iterations = 3

        # 指标
        self.total_llm_calls = 0
        self.total_tokens = 0
        self.total_latency_ms = 0.0

    def transition(self, new_state: AgentState) -> bool:
        if new_state not in TRANSITIONS.get(self.state, set()):
            return False
        self.state = new_state
        return True

class StateStore:
    """状态持久化 — 每次状态变更写入 JSON 文件"""
    def save(self, ctx: RunContext):
        path = self.state_dir / f"{ctx.run_id}.json"
        path.write_text(json.dumps(ctx.to_dict(), ensure_ascii=False, indent=2))

    def load(self, run_id: str) -> Optional[RunContext]:
        path = self.state_dir / f"{run_id}.json"
        return RunContext.from_dict(json.loads(path.read_text()))
```

---

## 13. CLI 命令参考

### 主命令：闭环 Agent 分析

```bash
# 聚焦某个实体
python main.py run --entity "比亚迪" --days 90

# 按关键词分析板块传导
python main.py run --keywords "芯片,制裁" --days 60

# 自动模式：自动选择热门实体分析
python main.py run --auto --days 30

# 自定义迭代次数
python main.py run --entity "比亚迪" --days 90 --max-iter 5

# 不生成报告文件
python main.py run --auto --no-report
```

### 线索链构建（不需要 LLM）

```bash
# 时间链
python main.py chain timeline --entity "比亚迪" --type company --days 90

# 板块传导链
python main.py chain sector --keywords "新能源,补贴" --days 90

# 异常链
python main.py chain anomaly --days 30

# 实体交叉链
python main.py chain cross --days 60
```

### 生命周期管理

```bash
# 查看 Harness 状态、指标、熔断器
python main.py status

# 恢复中断的运行
python main.py resume <run_id>

# 列出所有运行记录
python main.py runs
```

---

## 附录：完整闭环执行时序

```
用户: python main.py run --entity "比亚迪" --days 90
  │
  ├─ Harness.run_analysis(entity="比亚迪", days=90)
  │    │
  │    ├─ 创建 RunContext(run_id="a1b2c3d4e5f6")
  │    │
  │    ├─ Iteration 1:
  │    │    ├─ Plan → 扫描 90 天新闻 → 热门实体统计
  │    │    │   → plan.chains = [timeline(比亚迪), anomaly, entity_cross]
  │    │    │
  │    │    ├─ Execute:
  │    │    │   ├─ build_timeline_chain("比亚迪") → 1 chain, 45 nodes
  │    │    │   ├─ build_anomaly_chains(90) → 2 chains
  │    │    │   ├─ build_entity_cross_chains(90) → 3 chains
  │    │    │   └─ InsightEngine.analyze_chains(6 chains)
  │    │    │       ├─ chain 1 → LLM call → {thesis, confidence: 0.8, ...}
  │    │    │       ├─ chain 2 → LLM call → {thesis, confidence: 0.6, ...}
  │    │    │       └─ ... (共 6 次 LLM 调用)
  │    │    │
  │    │    ├─ Evaluate:
  │    │    │   ├─ 5 维度评分 × 6 条洞察
  │    │    │   ├─ 幻觉检测 × 6
  │    │    │   └─ overall_score = 0.58, passed = false
  │    │    │
  │    │    └─ Reflect:
  │    │        ├─ critique = "证据引用不足; 隐蔽信号不明显"
  │    │        └─ strategy = "expand_context"
  │    │
  │    ├─ refine_context: days = 90 × 1.5 = 135
  │    │
  │    ├─ Iteration 2:
  │    │    ├─ Execute (扩大时间窗口到 135 天)
  │    │    │   └─ 更多新闻 → 更丰富的链 → 更好的 LLM 分析
  │    │    │
  │    │    ├─ Evaluate:
  │    │    │   └─ overall_score = 0.74, passed = true
  │    │    │
  │    │    └─ → COMPLETE
  │    │
  │    ├─ 生成报告:
  │    │    ├─ analysis_20240115_103000.json
  │    │    └─ analysis_20240115_103000.md
  │    │
  │    └─ 持久化 + 更新 metrics
  │
  └─ 输出运行结果摘要
```
