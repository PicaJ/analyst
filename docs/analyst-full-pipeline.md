# Analyst 全链条解析：从数据筛选到荐股的完整流程

> 本文档详细解析 analyst 模块从原始新闻数据出发，经过多层筛选、线索链构建、LLM 分析、质量评估、自我修正，最终输出具体 A 股推荐标的的完整过程。

---

## 目录

- [一、全局流程概览](#一全局流程概览)
- [二、数据筛选机制（多层过滤）](#二数据筛选机制多层过滤)
- [三、线索链构建（核心数据结构）](#三线索链构建核心数据结构)
- [四、LLM 深度分析（从链到洞察）](#四llm-深度分析从链到洞察)
- [五、质量评估体系（6 维评分 + 幻觉检测）](#五质量评估体系6-维评分--幻觉检测)
- [六、自我修正机制（ReAct 闭环）](#六自我修正机制react-闭环)
- [七、股票推荐验证（联网核实）](#七股票推荐验证联网核实)
- [八、报告生成与输出](#八报告生成与输出)
- [九、完整执行时序示例](#九完整执行时序示例)
- [十、关键参数速查表](#十关键参数速查表)

---

## 一、全局流程概览

### 1.1 三阶段系统架构

```
collectagent          indexagent              analyst
(采集新闻)    →     (构建索引/向量)    →     (深度分析荐股)
```

analyst 依赖前两个模块的数据：collectagent 负责从各个财经数据源采集新闻，indexagent 负责将新闻写入 SQLite 数据库并构建 FAISS 向量索引和 FTS5 全文索引。analyst 读取这些已索引的数据进行分析。

### 1.2 内部闭环流程

```
┌──────────────────────────────────────────────────────────────────┐
│  Phase 1: Plan (规划)                                            │
│  分层扫描数据源 → 实体统计 → 高频词提取 → 决定构建哪些链         │
└──────────────────┬───────────────────────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────────────────────┐
│  Phase 2: Execute (执行)                                         │
│  构建线索链 → 投资相关性过滤 → LLM 分析 → 去重 → 股票验证      │
└──────────────────┬───────────────────────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────────────────────┐
│  Phase 3: Evaluate (评估)                                        │
│  6 维度评分 + 幻觉检测 → 通过? → COMPLETE → 生成报告            │
│                                未通过? → Phase 4                  │
└──────────────────┬───────────────────────────────────────────────┘
                   ▼ (未通过时)
┌──────────────────────────────────────────────────────────────────┐
│  Phase 4: Refine (修正)                                          │
│  找最弱维度 → 选择修正策略 → 调整参数 → 回到 Phase 2             │
│  最多迭代 3 次 (可配置)                                          │
└──────────────────────────────────────────────────────────────────┘
```

### 1.3 数据流总览

```
indexagent 写入的 SQLite (news.db)
         │
         ▼
    ┌────────────────────────────────────────────┐
    │  NewsQuery (query.py)                      │
    │  混合检索: FAISS向量 + FTS5全文 + SQLite   │
    └────────────────┬───────────────────────────┘
                     │ 原始新闻列表
                     ▼
    ┌────────────────────────────────────────────┐
    │  ChainBuilder (chain_builder.py)           │
    │  4种线索链: timeline/sector/anomaly/cross   │
    └────────────────┬───────────────────────────┘
                     │ 结构化线索链
                     ▼
    ┌────────────────────────────────────────────┐
    │  InsightEngine (insight_engine.py)         │
    │  LLM 分析 → 结构化 JSON 洞察              │
    └────────────────┬───────────────────────────┘
                     │ 洞察结果 (含股票推荐)
                     ▼
    ┌────────────────────────────────────────────┐
    │  Evaluator (evaluator.py)                  │
    │  6维评分 + 幻觉检测 → 通过/不通过         │
    └────────────────┬───────────────────────────┘
                     │ 通过的洞察
                     ▼
    ┌────────────────────────────────────────────┐
    │  stock_verify.py                           │
    │  联网核实股票: 存在性/走势/行业/财报       │
    └────────────────┬───────────────────────────┘
                     │ 验证后的推荐
                     ▼
    ┌────────────────────────────────────────────┐
    │  Report (report.py)                        │
    │  Markdown + JSON 报告                      │
    └────────────────────────────────────────────┘
```

---

## 二、数据筛选机制（多层过滤）

analyst 在不同阶段设置了 **6 层过滤**，确保只有真正有投资价值的信息进入分析流程。

### 2.1 第一层：数据源分层扫描

Plan 阶段将数据源分为三层，区别对待：

```python
# 快讯源 — 市场热点，全量获取（每个源最多 50000 条）
_FLASH_SOURCES = ["cls", "jin10", "thx", "xueqiu", "eastmoney",
                  "sina", "wallstreetcn", "cctv", "akshare_cctv", "thepaper"]

# 公告源 — 个股事件，取最近 5000 条
_FILING_SOURCES = ["eastmoney_notice", "cninfo"]

# 排除源 — 非财经数据
_EXCLUDED_SOURCES = ["gov", "miit"]
```

| 层级 | 数据源 | 数量上限 | 用途 |
|------|--------|---------|------|
| 快讯层 | cls, jin10, thx 等 10 个 | 每源 50000 条 | 捕获市场热点、情绪变化 |
| 公告层 | eastmoney_notice, cninfo | 每源 5000 条 | 补充个股事件、公告信息 |
| 排除层 | gov, miit | 0 条 | 过滤政府文件、工信部通知等 |

**为什么分层？** 快讯源（如财联社、金十数据）直接反映市场情绪和突发事件，需要全量扫描；公告源（如巨潮资讯）以个股维度补充信息，但数据量大且噪音多，需要限制数量。

### 2.2 第二层：政治/社会关键词过滤

在提取高频词和建链前，通过 `political_keywords` 列表过滤掉纯政治/社会事件：

```yaml
# analyst.yaml 中配置的 34 个政治关键词
political_keywords:
  - 回信, 慰问, 贺信, 致辞, 讲话, 会见, 署名文章
  - 访问, 抵达, 出席, 发表演讲, 联合声明, 友好访问
  - 共青团, 妇联, 工会, 精神文明, 文明创建
  - 作出重要指示, 作出批示, 排查整治, 安全生产
  # ... 共 34 个关键词
```

过滤逻辑：
- 高频词提取时：跳过包含政治关键词的词
- 建链时：如果链主题包含政治关键词且无具体 ts_codes，则丢弃

```python
# agent.py — 过滤逻辑
if any(kw in theme for kw in self._POLITICAL_KEYWORDS):
    has_codes = any(n.ts_codes for n in c.nodes)
    if not has_codes:  # 没有具体股票代码 → 丢弃
        continue
```

### 2.3 第三层：无投资价值过滤（no_value_keywords）

评估阶段使用 `no_value_keywords` 列表，对 LLM 输出做进一步过滤：

```yaml
no_value_keywords:
  - 回信, 慰问, 贺信, 致辞, 讲话, 会见, 署名文章
  - 共青团, 妇联, 工会, 精神文明, 荣誉称号, 表彰大会
  - 劳动模范, 先进工作者, 道德模范
```

评估器的 `investment_relevance` 维度会检测：如果 thesis 包含这些关键词，直接给 **0 分**。

### 2.4 第四层：链显著性过滤

构建完线索链后，按 `significance`（重要性评分）过滤低价值链：

```python
# 配置项: chain_significance_filter = 0.3
all_chains = [c for c in all_chains if c.significance >= 0.3]
```

重要性评分算法（满分 1.0）：

```
significance = source_priority_score × 0.30    # 来源权威度
             + sentiment_polarity × 0.30       # 情绪极性（有 positive/negative 的占比）
             + urgency_ratio × 0.20            # 紧急度比例
             + node_count_ratio × 0.20         # 节点数量（越多越重要）
```

具体计算：
- **来源权威度**: `max_priority / 5.0 × 0.3`（priority 值越高越权威）
- **情绪极性**: `(positive或negative的数量 / 有情绪标记的总数) × 0.3`
- **紧急度**: `(urgent或important的数量 / 总数) × 0.2`
- **节点数**: `(节点数 / 10.0) × 0.2`（10 个节点为满分）

### 2.5 第五层：LLM 输出后置过滤

LLM 分析完成后，对洞察结果做三层清洗：

```python
# 1. 过滤低置信度（< 0.3 的丢弃）
if ins.get("confidence", 0) < 0.3:
    continue

# 2. 过滤无股票标的的洞察（没有具体操作建议的不算投资分析）
items = ins.get("actionable_items", [])
has_stock_targets = any(item.get("targets") for item in items)
if not has_stock_targets:
    continue

# 3. 过滤分析失败的
if ins.get("error") or ins.get("confidence", 0) == 0:
    continue
```

### 2.6 第六层：双重去重

对通过过滤的洞察做去重，保留置信度最高的：

| 去重方法 | 阈值 | 说明 |
|---------|------|------|
| LCS 最长公共子串 | >= 6 个字符 | 两个 thesis 有长段相同 → 重复 |
| 3-gram Jaccard 相似度 | >= 0.25 | 3 字符滑动窗口的集合交集/并集比 |

```python
# LCS 最长公共子串
if _lcs_len(thesis1, thesis2) >= 6:
    # 重复 → 保留置信度更高的

# 3-gram Jaccard
ng1 = set(clean_text[i:i+3] for ...)  # 滑动 3 字符窗口
ng2 = set(clean_text[i:i+3] for ...)
jaccard = len(ng1 & ng2) / len(ng1 | ng2)
if jaccard >= 0.25:
    # 重复 → 保留置信度更高的
```

### 2.7 过滤流程总结

```
原始新闻 (数万条)
    │
    ├─ 分层扫描 (快讯全量 + 公告限制)     → 过滤掉大量无关源
    │
    ├─ 政治关键词过滤                     → 排除纯政治/社会新闻
    │
    ├─ 链显著性过滤 (significance ≥ 0.3)  → 过滤掉低价值链
    │
    ├─ 投资相关性过滤                     → 无股票代码的链丢弃
    │
    ├─ LLM 后置过滤                       → 低置信度/无标的丢弃
    │
    └─ 双重去重 (LCS + Jaccard)           → 主题重复的保留最高的
                                          → 最终输出: 几条到十几条高质量洞察
```

---

## 三、线索链构建（核心数据结构）

### 3.1 数据结构定义

每条线索链 (`ClueChain`) 由三类元素组成：

```
ClueChain (线索链)
├── chain_id: 唯一标识
├── chain_type: 类型 (timeline/sector_propagation/anomaly/entity_cross)
├── theme: 主题描述
├── significance: 重要性评分 (0~1)
├── nodes: List[ChainNode]  — 节点（每条新闻）
│   ├── news_id, title, publish_time
│   ├── source, source_priority
│   ├── sentiment, urgency
│   ├── ts_codes, mentioned_companies
│   └── related_sectors
├── links: List[ChainLink]  — 边（节点间关联）
│   ├── from_id → to_id
│   ├── link_type (temporal/entity/sector/anomaly)
│   ├── strength (0~1)
│   └── reason (关联原因)
└── hidden_signals: List[str]  — 已发现的隐蔽信号
```

### 3.2 混合检索策略

所有链构建都使用统一的混合检索策略：**FAISS 向量 + FTS5 全文 + SQLite 标题匹配**，三路结果合并去重。

```
查询: entity="比亚迪"
         │
         ├── search_hybrid(FAISS + FTS5) → 语义相关 + 关键词匹配 → 结果集 A
         │
         ├── get_timeline(keywords=["比亚迪"]) → SQLite LIKE → 结果集 B
         │
         └── _merge_dedup(A, B) → 按 ID 去重 → 合并结果集
```

- **FAISS 语义搜索**: 理解语义含义，能找到"新能源汽车"相关的"电动车"新闻
- **FTS5 全文搜索**: 精确关键词匹配，确保不漏掉明确包含实体名的新闻
- **SQLite LIKE**: 保底策略，当前两者不可用时确保基本查询能力
- **alpha 参数**: `hybrid_alpha = 0.7`，向量搜索权重 70%，关键词搜索权重 30%

### 3.3 实体提取算法

新闻的实体信息提取分两个层级：

**优先级 1 — 数据库字段（高准确度）**：
```python
# 直接使用 indexagent 写入的结构化字段
mentioned_companies  # 涉及的公司名
related_sectors      # 关联的板块
ts_codes             # 股票代码
mentioned_persons    # 涉及的人物
```

**优先级 2 — 标题分词提取（数据库字段为空时降级）**：
```python
# 使用 jieba 分词
import jieba
words = list(jieba.cut(title))
# 过滤: 长度 >= 2 + 不在停用词列表 + 不全是数字/标点
keywords = [w for w in words if len(w) >= 2 and w not in _STOP_WORDS
            and not all(c in "0123456789.%％万亿元角分" for c in w)]
```

停用词列表包含约 160 个中文虚词和财经新闻泛词（如"公司"、"集团"、"公告"、"表示"、"亿元"等），确保提取出的关键词是有实际含义的实体。

### 3.4 四种线索链详解

#### 链型一：时间链 (Timeline Chain)

**目标**: 跟踪同一实体的事件演变，发现情绪转折点。

```
实体="比亚迪", 90天 →
  [新闻1: 比亚迪1月销量大增] ──temporal→ [新闻2: 比亚迪2月发布新车型]
       ──temporal→ [新闻3: 比亚迪3月出口创新高] ──temporal→ [新闻4: ...]

  隐蔽信号: 情绪转变 neutral→positive (从沉默到表态，值得关注)
```

**构建步骤**:
1. 混合检索获取该实体的所有相关新闻
2. 按 `publish_time` 排序
3. 相邻节点自动创建 `temporal` 类型连接，强度 0.5
4. 检测情绪变化信号（neutral→positive/negative 表示表态，positive→negative 表示反转）

**情绪变化信号检测**:
| 转变类型 | 含义 |
|---------|------|
| neutral → positive | 从沉默到表态，值得关注 |
| neutral → negative | 从中性到负面，突发利空 |
| positive → negative | 利好转利空，重大反转信号 |

#### 链型二：板块传导链 (Sector Propagation Chain)

**目标**: 追踪政策/事件从上游板块传导到下游板块的路径，发现滞后反应机会。

```
关键词=["芯片", "制裁"] →
  [半导体板块: 1月15日新闻] ──sector(传导)→ [消费电子板块: 1月22日新闻]
       ──sector(传导)→ [汽车电子板块: 1月30日新闻]

  隐蔽信号: 传导路径: 半导体(01-15) → 消费电子(01-22), 消费电子可能存在滞后反应机会
```

**构建步骤**:
1. 混合检索获取关键词相关的所有新闻
2. 每条新闻按 `related_sectors` 字段分组
3. 计算每个板块首次出现的时间
4. 按首次出现时间排序板块
5. 上游板块最新新闻 → 下游板块最早新闻 创建 `sector` 类型连接

**传导信号**: 记录板块间的时间差，标记可能存在"滞后反应机会"的下游板块。

#### 链型三：异常链 (Anomaly Chain)

**目标**: 检测消息密度异常的实体，可能暗示未公开信息（内幕信息泄露）。

```
某公司在30天内出现8条消息，密度异常 →
  [新闻1] ──anomaly(聚集)→ [新闻2] ──anomaly→ [新闻3] ... ──anomaly→ [新闻8]

  隐蔽信号: 某公司30天内出现8条消息，密度异常
           可能存在未被市场充分反映的信息
```

**构建步骤**:
1. 获取近期高优先级/紧急新闻（仅快讯源，过滤公告源噪音）
2. 按实体分组，提取实体（优先数据库字段，降级用标题分词）
3. 计算时间密度: `density = 消息数 / 时间跨度(小时)`
4. 密度阈值: `density >= 0.5 条/小时`（即平均每2小时至少1条）
5. 最小聚类大小: >= 3 条新闻
6. 异常链强度固定 0.8，重要性固定 0.8
7. 最多返回 5 条异常链

**爆发检测算法**:
```python
# 对每个实体:
times = sorted([publish_time for all related news])
span_hours = (times[-1] - times[0]).total_seconds() / 3600
density = len(times) / max(span_hours, 1)
if density >= 0.5:  # 阈值可配置
    # → 该实体有消息爆发
```

#### 链型四：实体交叉链 (Entity Cross Chain)

**目标**: 发现不同实体（公司/板块）之间的隐蔽关联，通过共同出现在多篇新闻中识别。

```
实体A="比亚迪" 和 实体B="宁德时代" 出现4次共同报道 →
  [共同新闻1: 电池合作] ──entity(交叉)→ [共同新闻2: 工厂签约]
       ──entity→ [共同新闻3: ...] ──entity→ [共同新闻4: ...]

  隐蔽信号: 比亚迪 与 宁德时代 出现4次共同报道
           两个实体的关联可能尚未被市场充分定价
```

**构建步骤**:
1. 获取时间窗口内全部新闻（最多 5000 条，仅快讯源）
2. 构建实体→新闻的倒排索引（`entity_map`）
3. 对每个实体，统计与之共同出现在新闻中的其他实体
4. 共现次数 >= 2 的实体对建立交叉关系
5. 取交集新闻作为链节点
6. 重要性计算: `0.6 + 0.1 × min(共现次数, 4)`
7. 按重要性排序，返回 Top 10

**实体交叉发现逻辑**:
```python
# 倒排索引: entity → [news1, news2, ...]
entity_map = defaultdict(list)

# 对每个实体:
for entity, enodes in entity_map.items():
    # 统计共现实体
    for node in enodes:
        for other_entity in node.mentioned_companies + node.related_sectors:
            if other_entity != entity:
                related_entities[other_entity] += 1

    # 找共现 >= 2 的实体对
    for rel_entity, overlap in related_entities.items():
        if overlap >= 2:
            # 取交集新闻 → 构建链
```

### 3.5 建链决策逻辑

Plan 阶段根据用户输入和数据量决定构建哪些链：

```
用户指定了 entity    → 必定构建 timeline 链（实体时间线）
用户指定了 keywords  → 必定构建 sector_propagation 链（板块传导）
新闻总量 >= 5        → 自动构建 anomaly 链（异常检测）
新闻总量 >= 3        → 自动构建 entity_cross 链（实体交叉）

自动模式（无 entity 和 keywords）:
  → 从快讯标题高频词中选 Top 10 实体 → 为每个构建 timeline 链
  → 补充公告源 ts_codes 高频实体
  → 为未被覆盖的高频词构建 timeline 链
```

---

## 四、LLM 深度分析（从链到洞察）

### 4.1 Prompt 设计

LLM 分析使用两段式 Prompt 设计：

**System Prompt（固定，约 1500 字）**：

6 条核心分析原则：
1. **投资导向**: 只分析与 A 股投资直接相关的信息
2. **具体标的**: 必须指向具体的 A 股代码（如 000333.SZ）
3. **因果链条**: 必须有明确因果传导路径（事件→行业→个股）
4. **交叉验证**: 多源信息互相印证才可信
5. **时间序列**: 关注事件的时间先后顺序
6. **市场定价**: 评估信息是否已被股价充分反映

严格禁止的输出：
- "相关股票代码或板块" 等泛泛之词
- "关注XX行业" 而不给出具体股票代码
- 纯政治事件的投资解读
- 没有直接因果链的牵强关联

**User Prompt（动态生成）**：

包含链的完整上下文信息：
```
## 线索链信息
- 类型: {chain_type}
- 主题: {theme}
- 时间跨度: {time_span}
- 重要性评分: {significance}
- 已发现的隐蔽信号: {hidden_signals}

## 可用股票代码（本链新闻中出现的）
300750.SZ, 002594.SZ, ...

## 线索链中的新闻（按时间顺序）
[1] ID: news_001
    时间: 2024-01-15 10:30
    来源: cls (优先级: 5)
    标题: 比亚迪1月销量突破30万辆
    股票代码: 002594.SZ
    情绪: positive
    涉及公司: 比亚迪
    关联板块: 新能源汽车

[2] ...
```

**关键设计**: 传入 `available_ts_codes`（本链新闻中实际出现的股票代码），要求 LLM 优先从中选取，减少幻觉。

### 4.2 LLM 输出结构

LLM 必须返回严格 JSON 格式：

```json
{
  "thesis": "核心投资论点（一句话，必须包含因果关系）",
  "confidence": 0.8,
  "investment_relevance": "high",
  "time_horizon": "中期(1-4周)",
  "key_findings": [
    {
      "finding": "发现描述",
      "evidence_ids": ["news_001", "news_005"],
      "reasoning": "推导逻辑（必须说明因果传导路径）"
    }
  ],
  "hidden_signals": [
    {
      "signal": "隐蔽信号描述",
      "implication": "对具体标的的潜在影响",
      "not_priced_in": true
    }
  ],
  "risk_factors": ["风险因素1", "风险因素2"],
  "actionable_items": [
    {
      "action": "具体操作建议（含推荐理由）",
      "urgency": "high",
      "targets": ["000333.SZ", "601318.SH"],
      "reason": "推荐该标的的具体原因"
    }
  ]
}
```

### 4.3 LLM 客户端

支持 8 种 LLM 提供商，统一接口：

| 提供商 | API 格式 | 默认 Base URL |
|--------|---------|--------------|
| openai | OpenAI | https://api.openai.com |
| deepseek | OpenAI 兼容 | https://api.deepseek.com |
| siliconflow | OpenAI 兼容 | (需配置) |
| moonshot | OpenAI 兼容 | (需配置) |
| qwen | OpenAI 兼容 | (需配置) |
| glm | OpenAI 兼容 | https://open.bigmodel.cn/api/paas/v4/ |
| anthropic | Anthropic | https://api.anthropic.com |
| ollama | Ollama | http://localhost:11434 |

当前配置使用 `glm-4-flash`（智谱 AI）。

**容错机制**:
- httpx 连接池复用，避免重复创建连接
- 超时重试（最多 2 次），指数退避（5s, 10s）
- JSON 解析容错（处理 markdown 代码块、多余文本）

---

## 五、质量评估体系（6 维评分 + 幻觉检测）

### 5.1 六维评分模型

每条 LLM 洞察在 6 个维度上独立评分（每个维度 0~1 分），加权平均得到总分：

| 维度 | 权重 | 评分逻辑 | 重点检测 |
|------|------|---------|---------|
| **evidence_coverage** | 15% | 引用的 evidence_ids 占链总节点的比例 × 2.0 | 是否充分引用了源数据 |
| **reasoning_quality** | 15% | 0.5 × (有finding的占比) + 0.5 × (有reasoning的占比) | 每条发现是否有推理链 |
| **specificity** | **25%** | 每个 actionable_item 的具体性累加（股票代码+0.45, 推荐理由+0.1, 紧急度+0.1, 验证+0.2） | **是否给出了具体股票代码** |
| **signal_novelty** | 15% | 0.5 × (not_priced_in占比) + 0.5 × (有implication占比) | 是否发现了非显而易见的信号 |
| **self_consistency** | 10% | 基础分 0.5 + thesis(0.15) + confidence合理(0.1) + findings&risks并存(0.15) + 高置信度≥2个发现(0.1) | 论点与发现是否矛盾 |
| **investment_relevance** | **20%** | 纯政治事件=0, 无标的=0.1, 有标的但无代码=0.3, 有代码+理由=0.85, 有验证=1.0 | **是否真正有投资价值** |

**综合评分公式**:
```
overall_score = evidence_coverage × 0.15
              + reasoning_quality × 0.15
              + specificity       × 0.25
              + signal_novelty    × 0.15
              + self_consistency  × 0.10
              + investment_relevance × 0.20
              - hallucination_penalty (每个幻觉 × 0.1, 最多扣 0.3)
```

权重设计理念: `specificity`(25%) 和 `investment_relevance`(20%) 权重最高，强调分析必须有**具体股票代码**且**真正有投资价值**。

### 5.2 幻觉检测

检测 LLM 是否编造了不存在的信息：

| 检测类型 | 检测方法 | 标记内容 |
|---------|---------|---------|
| 虚假引用 | evidence_ids 是否存在于源节点 ID 集合中 | "引用了不存在的证据ID: xxx" |
| 虚假标的 | actionable targets 是否在源数据实体中 | "操作目标 'xxx' 不在源数据实体中" |

幻觉惩罚: 每个幻觉标记扣 0.1 分，最多扣 0.3 分。

### 5.3 通过条件

```
通过 = (平均分 >= 0.65) AND (单条通过率 >= 50%)

其中单条通过 = (该条 overall_score >= 0.65)
```

### 5.4 批评意见生成

未通过时自动生成批评意见，指导下一轮修正：

```
investment_relevance < 0.2 → "投资相关性极低，缺乏具体股票推荐"
evidence_coverage < 0.4    → "证据引用不足，需更多关联到具体新闻"
reasoning_quality < 0.4    → "推理逻辑缺失，需补充 finding → reasoning 链条"
specificity < 0.4          → "可操作项过于笼统，需指定具体股票代码和操作方向"
signal_novelty < 0.4       → "隐蔽信号不明显，需深入挖掘表面之下的关联"
hallucination_flags        → "检测到幻觉: xxx"
```

---

## 六、自我修正机制（ReAct 闭环）

### 6.1 修正策略选择

当评估未通过时，系统自动选择修正策略。选择逻辑基于**最弱维度**（而非关键词匹配）：

```
1. 如果有幻觉标记                → critique_revise（反馈批评让 LLM 重写）
2. evidence_coverage 最低        → expand_context（扩大时间窗口）
3. signal_novelty 最低           → add_chains（补充更多链类型）
4. reasoning_quality 最低        → critique_revise
5. specificity 最低              → critique_revise
6. 默认                          → expand_context
```

### 6.2 三种修正策略

| 策略 | 执行内容 | 效果 |
|------|---------|------|
| **expand_context** | `time_window_days *= 1.5`（如 90→135→202 天） | 更多新闻数据 → 更丰富的链 → 更好的分析 |
| **add_chains** | 补充 anomaly 和 entity_cross 链（如果还没有） | 引入新的分析视角 |
| **critique_revise** | 不改参数，把批评意见注入下一轮 LLM Prompt | 让 LLM 针对性改进输出 |

**critique_revise 的 Prompt 注入**:
```python
# insight_engine.py
if self._critique:
    critique_section = (
        f"## 上一轮评估的批评意见\n"
        f"请针对以下批评改进你的分析:\n{self._critique}\n"
    )
```

### 6.3 闭环迭代过程

```
Iteration 1 (初始):
  chains = [timeline(比亚迪), anomaly, entity_cross]
  → LLM 分析 → 6 维评分
  → overall = 0.58, passed = false
  → 最弱维度: evidence_coverage = 0.25
  → 策略: expand_context

Iteration 2 (扩大窗口):
  time_window_days = 90 × 1.5 = 135
  chains = [更多新闻 → 更丰富的链]
  → LLM 分析 → 6 维评分
  → overall = 0.72, passed = true → COMPLETE
```

最多迭代 3 次（可配置 `max_iterations`）。如果 3 次都未通过，标记为 FAILED。

### 6.4 状态机流转

```
IDLE → PLANNING → EXECUTING → EVALUATING
                                │
            ┌───────────────────┤
            │                   │
            ▼ (未通过)          ▼ (通过)
          REFINE → EXECUTING   COMPLETE → 生成报告
            │
            └→ FAILED (重试耗尽)
```

每次状态变更自动持久化到 JSON 文件，支持从任意中断点恢复。

---

## 七、股票推荐验证（联网核实）

### 7.1 验证流程

LLM 输出推荐股票后，`stock_verify.py` 对每只股票做联网核实：

```
LLM 推荐: ["000333.SZ", "300750.SZ"]
         │
         ▼
    ┌────────────────────────────────┐
    │  Step 1: 代码格式校验          │
    │  正则: ^(\d{6})\.[A-Z]{2}$    │
    │  不匹配 → 标记"无效代码格式"   │
    └────────────┬───────────────────┘
                 │
    ┌────────────▼───────────────────┐
    │  Step 2: 存在性校验            │
    │  AKShare: stock_zh_a_spot_em() │
    │  全市场实时行情数据            │
    │  不存在 → 标记"代码不存在"     │
    └────────────┬───────────────────┘
                 │
    ┌────────────▼───────────────────┐
    │  Step 3: 行业信息获取          │
    │  AKShare: stock_individual_info│
    │  缓存 7 天                     │
    └────────────┬───────────────────┘
                 │
    ┌────────────▼───────────────────┐
    │  Step 4: 走势匹配检测          │
    │  AKShare: stock_zh_a_hist()    │
    │  近 5 日实际走势 vs 分析结论   │
    │  "上涨" → 实际 5 日涨了吗?     │
    └────────────┬───────────────────┘
                 │
    ┌────────────▼───────────────────┐
    │  Step 5: 业务逻辑匹配          │
    │  股票行业 vs 分析涉及行业      │
    │  "半导体"股票出现在"新能源"   │
    │  分析中 → 标记"业务不匹配"     │
    └────────────┬───────────────────┘
                 │
    ┌────────────▼───────────────────┐
    │  Step 6: 财报披露日期          │
    │  AKShare: stock_report_disclosure│
    │  找最近的披露日               │
    └────────────┬───────────────────┘
                 │
    ┌────────────▼───────────────────┐
    │  Step 7: 主板平替（科创板/创业板）│
    │  688xxx/300xxx → 找同行业主板标的│
    │  排除 ST、零价格股             │
    └────────────────────────────────┘
```

### 7.2 走势匹配检测

分析 LLM 的 thesis 和 findings 文本中的方向性词语，与实际 5 日走势对比：

```python
# 看涨词语
up_words = ["上涨", "涨", "走强", "反弹", "回升", "走高", "利好", "涨停", "大涨", "爆发"]

# 看跌词语
down_words = ["下跌", "跌", "走弱", "回调", "回落", "走低", "利空", "大跌", "暴跌"]

# 如果分析说"上涨"但实际5日跌了 → trend_match = false（走势不符）
```

### 7.3 业务逻辑匹配

使用 `industry_alias`（行业别名映射，覆盖 25 个行业）检测股票行业是否与分析结论匹配：

```python
# 例: 分析提到"新能源"和"光伏"
# 股票 000333.SZ 行业="白色家电"
# → business_match = false
# → business_match_note = "该股票属「白色家电」，但分析涉及「新能源」"
```

### 7.4 板块判断

```python
688xxx  → 科创板 (688 开头)
300/301 → 创业板 (300/301 开头)
000/001/002 → 深主板
600/601/603/605 → 沪主板
```

科创板/创业板的股票会额外提供同行业主板平替标的（排除 ST 股、零价格股）。

### 7.5 验证结果注入

验证结果直接写入 `actionable_items`：

```json
{
  "action": "关注消费电子板块低估值标的",
  "urgency": "high",
  "targets": ["000333.SZ"],
  "reason": "受益于面板涨价周期",
  "verified": true,
  "verify_details": [
    {
      "code": "000333.SZ",
      "verified": true,
      "stock_name": "美的集团",
      "price": 65.32,
      "change_pct": 1.23,
      "board": "深主板",
      "industry": "白色家电",
      "recent_trend": "+2.5% (近5日)",
      "trend_match": true,
      "disclosure_date": "2024-04-28"
    }
  ]
}
```

---

## 八、报告生成与输出

### 8.1 双格式输出

| 格式 | 文件名 | 用途 |
|------|--------|------|
| Markdown | `analysis_YYYYMMDD_HHMMSS.md` | 人类阅读 |
| JSON | `analysis_YYYYMMDD_HHMMSS.json` | 其他 agent/程序消费 |

### 8.2 Markdown 报告结构

```markdown
# 财经新闻深度分析报告

> 生成时间 / 运行ID / 迭代次数 / 质量分数 / LLM调用次数 / 耗时

## 分析过程
  1. 数据扫描: 近N天新闻共扫描 X 条
  2. 线索链构建: Y 条链，类型分布
  3. LLM 深度分析: 大模型分析
  4. 质量评估: 多维度评分
  5. 去重过滤: 最终保留 Z 条
  6. 高频词检测: 近期热门关键词

## 分析概要（表格）
  | 核心论点 | 置信度 | 时间维度 | 未定价信号 | 链类型 |

## 详细分析 (每条洞察)
  核心发现 + 消息来源 + 隐蔽信号 + 风险因素 + 可操作项
  可操作项包含: 验证状态、股票名称、行业、价格、走势、财报日期、主板平替

## 评估详情
  6 维度分数表 + 综合评分 + 通过率 + 幻觉标记

## A 股散户持仓排名 Top 50
  股东户数排名 + 变化 + 收盘价 + 大股东/散户占比
```

### 8.3 A 股散户持仓排名

报告自动附加东方财富的散户持仓 Top 50 数据：

```
数据源: 东方财富 API /api/data/v1/get?reportName=RPT_HOLDERNUMLATEST
补充: AKShare stock_main_stock_holder() 获取前10大股东占比
计算: 散户占比 = 100% - 大股东占比
缓存: 7 天有效期
```

---

## 九、完整执行时序示例

以 `python main.py run --entity "比亚迪" --days 90` 为例：

```
[00:00] CLI 解析参数: entity="比亚迪", days=90, max_iterations=3
[00:00] Harness 初始化: 创建 RunContext(run_id="a1b2c3d4e5f6")
[00:00] 熔断检查: 连续失败 0 次 → 正常

[00:01] Phase 1: Plan
        ├─ 快讯源扫描: cls/jin10/thx 等 10 个源 → 15000 条
        ├─ 公告源扫描: eastmoney_notice/cninfo → 3000 条
        ├─ 合并: 18000 条
        ├─ 实体统计: "比亚迪"(45次), "宁德时代"(32次), ...
        ├─ 高频词: "新能源"(28), "电动车"(25), "销量"(22), ...
        └─ 建链计划:
            ① timeline(比亚迪, 90天)
            ② anomaly(90天)
            ③ entity_cross(90天)
            ④ timeline(新能源, 90天)
            ⑤ timeline(电动车, 90天)

[00:03] Phase 2: Execute (Iteration 1)
        ├─ build_timeline_chain("比亚迪")
        │   ├─ search_hybrid("比亚迪") → 180 条
        │   ├─ get_timeline(keywords=["比亚迪"]) → 220 条
        │   ├─ 合并去重 → 310 条
        │   ├─ 构建 temporal 链接
        │   ├─ 检测情绪转变: neutral→positive 在 2月
        │   └─ significance = 0.68
        │
        ├─ build_anomaly_chains(90天)
        │   └─ 发现 2 个实体爆发 → 2 条异常链
        │
        ├─ build_entity_cross_chains(90天)
        │   └─ 发现 5 个实体对共现 → 5 条交叉链
        │
        ├─ 总计: 9 条链
        ├─ 显著性过滤(≥0.3): 9 → 7 条
        ├─ 投资相关性过滤: 7 → 6 条
        │
        ├─ LLM 分析: 6 条链 × 1 次调用 = 6 次 LLM
        │   ├─ chain 1 (比亚迪时间线) → thesis="比亚迪新能源销量..." conf=0.82
        │   ├─ chain 2 (异常信号: 宁德时代) → conf=0.75
        │   └─ ...
        │
        ├─ 双重去重: 6 → 5 条洞察
        ├─ 后置过滤: 5 → 4 条 (1条无股票标的)
        └─ 股票验证: 4 条洞察中 8 只股票联网核实

[00:35] Phase 3: Evaluate (Iteration 1)
        ├─ 6 维度评分 × 4 条洞察
        ├─ 幻觉检测: 1 个标记 (evidence_id "news_999" 不存在)
        ├─ 维度平均: evidence=0.52, reasoning=0.68, specificity=0.71,
        │            signal=0.45, consistency=0.78, relevance=0.82
        ├─ overall_score = 0.654
        ├─ pass_rate = 3/4 = 0.75
        ├─ 幻觉扣分: -0.1
        └─ final_score = 0.554 → 未通过 (阈值 0.65)

[00:35] Phase 4: Refine
        ├─ critique: "证据引用不足 | 检测到幻觉: 引用了不存在的证据ID: news_999"
        ├─ 最弱维度: evidence_coverage = 0.52
        └─ 策略: expand_context → days = 90 × 1.5 = 135

[00:36] Phase 2: Execute (Iteration 2, 窗口扩大到 135 天)
        ├─ 重新构建所有链 (更多新闻数据)
        ├─ LLM 分析: 7 条链 → 7 次 LLM
        ├─ 去重: 7 → 6 条
        └─ 后置过滤: 6 → 5 条

[01:12] Phase 3: Evaluate (Iteration 2)
        ├─ overall_score = 0.738
        ├─ pass_rate = 5/5 = 1.0
        └─ PASSED → COMPLETE

[01:12] Report Generation
        ├─ Markdown: analysis_20240115_011200.md
        ├─ JSON: analysis_20240115_011200.json
        └─ 散户持仓 Top 50 附加到报告

[01:12] State Persistence + Metrics Update
```

---

## 十、关键参数速查表

### 10.1 核心阈值

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `quality_threshold` | 0.65 | 评估通过阈值 |
| `chain_significance_filter` | 0.3 | 链重要性过滤阈值 |
| `chain_burst_density_threshold` | 0.5 | 异常爆发密度阈值 (条/小时) |
| `min_cluster_size` | 3 | 异常链最小节点数 |
| `max_iterations` | 3 | 最大修正迭代次数 |
| `expansion_factor` | 1.5 | 时间窗口扩展系数 |
| `hybrid_alpha` | 0.7 | 向量搜索权重 |
| `eval_pass_rate_threshold` | 0.5 | 批量评估通过率阈值 |

### 10.2 评估权重

| 维度 | 权重 |
|------|------|
| specificity (具体性) | 0.25 |
| investment_relevance (投资相关性) | 0.20 |
| evidence_coverage (证据覆盖) | 0.15 |
| reasoning_quality (推理质量) | 0.15 |
| signal_novelty (信号新颖性) | 0.15 |
| self_consistency (自洽性) | 0.10 |

### 10.3 链类型参数

| 参数 | 值 | 说明 |
|------|------|------|
| `chain_timeline_strength` | 0.5 | 时间链连接强度 |
| `chain_sector_strength` | 0.6 | 板块传导链连接强度 |
| `chain_anomaly_strength` | 0.8 | 异常链连接强度 |
| `chain_cross_strength` | 0.7 | 交叉链连接强度 |
| `chain_anomaly_significance` | 0.8 | 异常链默认重要性 |
| `chain_cross_base_significance` | 0.6 | 交叉链基础重要性 |
| `chain_cross_overlap_bonus` | 0.1 | 交叉链重叠加分 |

### 10.4 查询限制

| 参数 | 值 | 用途 |
|------|------|------|
| `query_limit_time_range` | 50000 | 时间范围查询上限 |
| `query_limit_entity` | 500 | 实体查询上限 |
| `query_limit_urgent` | 500 | 紧急新闻查询上限 |
| `query_limit_timeline` | 500 | 时间线查询上限 |
| `query_limit_cross` | 5000 | 交叉链查询上限 |
| `insight_max_news` | 50 | 单次 LLM 最大新闻数 |

### 10.5 缓存策略

| 数据 | 缓存时间 | 来源 |
|------|---------|------|
| 实时行情 (stock_spot_cache.json) | 4 小时 | AKShare stock_zh_a_spot_em |
| 行业信息 (stock_industry_cache.json) | 7 天 | AKShare stock_individual_info_em |
| 散户持仓数据 | 7 天 | 东方财富 API |
| 财报披露日期 | 内存缓存（按年） | AKShare stock_report_disclosure |

---

## 附录：CLI 命令

```bash
# 闭环分析（最常用）
python -m analyst run --entity "比亚迪" --days 90
python -m analyst run --keywords "芯片,制裁" --days 60
python -m analyst run --auto --days 30

# 仅构建链（不调用 LLM）
python -m analyst chain timeline --entity "比亚迪" --days 90
python -m analyst chain sector --keywords "新能源,补贴" --days 90
python -m analyst chain anomaly --days 30
python -m analyst chain cross --days 60

# 生命周期管理
python -m analyst status          # 查看 Harness 状态
python -m analyst resume <run_id> # 恢复中断的运行
python -m analyst runs            # 列出所有运行记录
```
