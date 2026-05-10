# 线索链条构建与推演原理

> 本文档详细说明 analyst 系统中四种线索链的构建机制、推演流程，以及 ReAct 闭环推理引擎的工作原理。

---

## 一、系统总览

analyst 系统的核心目标是：从海量财经新闻中发现**隐蔽的因果/关联关系**，生成具体可执行的投资建议。

整体架构分三层：

```
┌─────────────────────────────────────────────────────┐
│                  Harness (调度器)                      │
│  熔断保护 · 状态持久化 · 报告生成 · 指标统计           │
└───────────────────────┬─────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────┐
│              Agent (ReAct 闭环引擎)                    │
│  Plan → Execute → Evaluate → Reflect → Refine         │
└──────┬──────────┬──────────┬──────────┬──────────────┘
       │          │          │          │
  ┌────▼───┐ ┌───▼────┐ ┌──▼───┐ ┌───▼──────┐
  │ Query  │ │ Enrich │ │Chain │ │ LLM      │
  │ 数据层  │ │富化层  │ │Build │ │ Engine   │
  └────────┘ └───┬────┘ └──┬───┘ └──────────┘
                  │          │
          Tier1: 规则匹配    │
          Tier2: LLM 富化    │
```

**数据富化层 (Enricher)** 位于 Query 和 ChainBuilder 之间，负责补全新闻节点中为空的结构化字段。详见"数据富化层"章节。

---

## 二、线索链的数据模型

所有线索链共享三个核心数据结构：

### 2.1 ChainNode（链节点）

每条新闻就是一个节点，携带的属性决定了后续建链时如何关联：

| 属性 | 说明 | 建链用途 |
|------|------|----------|
| `news_id` | 唯一标识 | 去重、证据引用 |
| `title` | 新闻标题 | 关键词提取、主题匹配 |
| `publish_time` | 发布时间 | 时间排序、密度计算 |
| `source` / `source_priority` | 来源及优先级 | 显著性评分 |
| `sentiment` | 情绪标签 (positive/negative/neutral) | 情绪转变检测 |
| `urgency` | 紧急度 | 显著性评分 |
| `mentioned_companies` | 涉及公司列表 | 实体匹配、交叉关联 |
| `related_sectors` | 关联板块列表 | 板块分组、传导分析 |
| `ts_codes` | A 股代码列表 | 投资标的定位 |

### 2.2 ChainLink（链边）

两个节点之间的关联关系：

| 属性 | 说明 |
|------|------|
| `from_id` → `to_id` | 连接的两个节点 |
| `link_type` | 关联类型：`temporal` / `entity` / `sector` / `anomaly` |
| `strength` | 关联强度 (0.0 ~ 1.0) |
| `reason` | 关联原因描述 |

### 2.3 ClueChain（线索链）

一条完整的线索链：

| 属性 | 说明 |
|------|------|
| `chain_type` | 链类型（四种之一） |
| `theme` | 链的主题概括 |
| `nodes` | 有序节点列表 |
| `links` | 节点间的边 |
| `significance` | 重要性评分 (0.0 ~ 1.0) |
| `hidden_signals` | 已发现的隐蔽信号列表 |

---

## 三、四种线索链的构建原理

### 3.1 时间链 (timeline)

**目标**：追踪同一实体/主题的事件随时间的演变过程。

**构建流程**：

```
输入: entity="比亚迪", days=90
          │
          ▼
┌──────────────────────┐
│ Step 1: 双路数据检索   │
│                      │
│  路径A: search_hybrid │ ← FAISS 语义搜索 + FTS5 关键词搜索
│        (alpha=0.7)    │    高召回，可能漏掉精确匹配
│                      │
│  路径B: get_timeline  │ ← SQLite 标题 LIKE 匹配
│        (keywords)     │    精确但语义覆盖窄
│                      │
│  合并: _merge_dedup() │ ← 按 news_id 去重，取并集
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ Step 2: 节点排序与连边 │
│                      │
│  nodes.sort(时间)     │    按发布时间升序排列
│                      │
│  相邻节点间建立       │
│  ChainLink:           │
│   type = "temporal"   │
│   strength = 0.5      │    (可配置: chain_timeline_strength)
│   reason = "同一实    │
│           体时间演变"  │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ Step 3: 隐蔽信号检测   │
│                      │
│  _detect_sentiment_   │
│  shifts()             │
│                      │
│  检测相邻新闻间情绪    │
│  是否发生转变:         │
│   neutral→positive/   │ ← "从沉默到表态，值得关注"
│     negative          │
│   positive→negative   │ ← "利好转利空，重大反转信号"
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ Step 4: 显著性评分     │
│                      │
│  _calc_significance() │
│  = f(源优先级, 情绪   │
│    极性, 紧急度, 节   │
│    点数量)             │
└──────────────────────┘
```

**关键设计决策**：

1. **双路检索**：hybrid 搜索擅长语义相关（如"新能源汽车"能搜到"电动车"），但可能漏掉标题中直接包含实体名但语义不同的新闻。SQLite LIKE 匹配作为兜底，确保不遗漏。

2. **hybrid 搜索的降级机制**：当 FAISS 向量库不可用或搜索失败时，自动降级为纯 SQLite 查询，保证系统可用性。

3. **相邻节点直接连边**：不做跨节点跳跃，保持时间线的线性结构，便于 LLM 理解事件演变顺序。

---

### 3.2 板块传导链 (sector_propagation)

**目标**：发现政策/事件从上游行业传导到下游行业的时间差和因果路径。

**构建流程**：

```
输入: policy_keywords=["锂电", "新能源补贴"], days=90
          │
          ▼
┌────────────────────────────┐
│ Step 1: 双路数据检索         │
│  (同 timeline，按关键词检索)  │
│  合并去重后得到 items         │
└──────────┬─────────────────┘
           │
           ▼
┌────────────────────────────┐
│ Step 2: 按板块分组           │
│                            │
│  遍历每个 node 的            │
│  related_sectors，构建:      │
│                            │
│  sector_groups = {          │
│    "新能源": [n1, n3, n5],  │
│    "汽车":   [n2, n4],      │
│    "有色":   [n6],          │
│    "未分类": [n7],          │  ← 无板块标签的归入"未分类"
│  }                          │
└──────────┬─────────────────┘
           │
           ▼
┌────────────────────────────┐
│ Step 3: 板块时间线排序       │
│                            │
│  对每个板块取最早新闻时间:    │
│  按时间排序得到传导顺序:      │
│                            │
│  新能源(01-05) →           │
│  有色(01-08) →             │
│  汽车(01-12) →             │
│  未分类(01-15)              │
│                            │
│  这揭示了: 政策先影响上游    │
│  (新能源)，再传导到中游      │
│  (有色/锂矿)，最后到下游     │
│  (汽车整车)                 │
└──────────┬─────────────────┘
           │
           ▼
┌────────────────────────────┐
│ Step 4: 跨板块连边           │
│                            │
│  相邻板块间:                 │
│  板块A最新新闻 ──→ 板块B     │
│  最早新闻                    │
│                            │
│  link_type = "sector"       │
│  strength = 0.6             │  (可配置: chain_sector_strength)
│  reason = "板块传导:         │
│   新能源 → 汽车"             │
└──────────┬─────────────────┘
           │
           ▼
┌────────────────────────────┐
│ Step 5: 传导信号检测         │
│                            │
│  _detect_propagation_       │
│  signals()                  │
│                            │
│  对每个传导路径生成:          │
│  "传导路径: 新能源(01-05)    │
│   → 汽车(01-12),            │
│   汽车可能存在滞后反应机会"   │
└────────────────────────────┘
```

**关键设计决策**：

1. **按板块最早出现时间排序**：这是传导链的核心假设——先出现的板块是上游，后出现的是下游。这个假设在政策驱动的行情中通常成立。

2. **连边方向是板块A的最新→板块B的最早**：连接的是时间上最接近的跨板块新闻，代表信息传导的"桥梁"。

3. **"未分类"兜底**：许多新闻没有板块标签，统一归入"未分类"避免丢失。

---

### 3.3 异常链 (anomaly)

**目标**：检测短期内消息密度异常飙升的实体/主题，捕捉可能存在的未公开信息泄露。

**构建流程**：

```
输入: days=30
          │
          ▼
┌────────────────────────────────┐
│ Step 1: 获取高优先级新闻         │
│                                │
│  get_urgent(days, limit=500)   │
│  获取近期紧急/重要新闻           │
│                                │
│  过滤: 排除公告源噪音            │
│  _FILING = {"eastmoney_notice",│
│             "cninfo"}          │
│  只保留快讯源节点               │
│  (cls, jin10, thx 等)          │
└──────────┬─────────────────────┘
           │
           ▼
┌────────────────────────────────┐
│ Step 2: 实体爆发检测             │
│                                │
│  _detect_entity_bursts()       │
│                                │
│  对每个节点:                     │
│   优先用 mentioned_companies    │
│   和 related_sectors           │
│   为空时从标题关键词提取          │
│                                │
│  构建实体→节点映射:              │
│  {                             │
│    "比亚迪": [n1,n3,n5,n8,n9], │
│    "锂电":   [n2,n4,n6],       │
│    ...                         │
│  }                             │
└──────────┬─────────────────────┘
           │
           ▼
┌────────────────────────────────┐
│ Step 3: 密度计算与筛选           │
│                                │
│  对每个实体:                     │
│                                │
│  density = 消息数 / 时间跨度(小时)│
│                                │
│  例: "比亚迪" 5条消息            │
│      分布在 4 小时内             │
│      density = 5/4 = 1.25      │
│                                │
│  筛选条件:                       │
│   node_count >= min_cluster_   │
│   size (默认3)                  │
│   density >= burst_density_    │
│   threshold (默认0.5)           │
│   entity 不在停用词中            │
└──────────┬─────────────────────┘
           │
           ▼
┌────────────────────────────────┐
│ Step 4: 建链                    │
│                                │
│  对每个爆发实体:                  │
│  节点按时序排列，相邻连边          │
│  link_type = "anomaly"         │
│  strength = 0.8                │
│                                │
│  hidden_signals:                │
│   "比亚迪在30天内出现8条消息，    │
│    密度异常"                     │
│   "可能存在未被市场充分反映的信息" │
│                                │
│  significance = 0.8 (固定高分)  │
│  按 node_count 降序，取 Top 5    │
└────────────────────────────────┘
```

**关键设计决策**：

1. **只用快讯源，排除公告源**：公告（如减持公告、年报）有固定的披露节奏，天然呈现"聚集"特征，会制造大量噪音。快讯源（财联社、金十等）的聚集更可能反映真实的市场异动。

2. **密度而非绝对数量**：10条消息分布在30天不算异常，但10条消息集中在2小时就非常异常。密度 = 消息数 / 时间跨度(小时)。

3. **停用词过滤**：大量新闻标题中出现的高频泛词（"公司"、"公告"、"市场"等）不是有效实体，通过停用词表过滤。

---

### 3.4 实体交叉链 (entity_cross)

**目标**：发现不同实体之间隐藏的关联——当两个看似无关的实体频繁出现在同一条新闻或同一组新闻中时，可能存在市场尚未认知的关系。

**构建流程**：

```
输入: days=60
          │
          ▼
┌────────────────────────────────────┐
│ Step 1: 时间范围查询                 │
│                                    │
│  get_by_time_range(60天, limit=5000)│
│  过滤: 排除公告源噪音                │
└──────────┬─────────────────────────┘
           │
           ▼
┌────────────────────────────────────┐
│ Step 2: 构建实体→节点倒排索引        │
│                                    │
│  对每个节点，提取其关联实体:          │
│   优先: mentioned_companies        │
│         related_sectors            │
│   兜底: 标题关键词提取               │
│                                    │
│  entity_map = {                    │
│    "比亚迪":  [n1,n3,n5,n7],       │
│    "宁德时代":[n2,n3,n6,n7],       │
│    "锂电":    [n1,n4,n5],          │
│    ...                             │
│  }                                 │
└──────────┬─────────────────────────┘
           │
           ▼
┌────────────────────────────────────┐
│ Step 3: 实体间共现分析               │
│                                    │
│  对每个实体 E，遍历其所有节点 N:      │
│  收集 N 中出现的其他实体，统计次数    │
│                                    │
│  例: "比亚迪" 的节点 [n1,n3,n5,n7]  │
│    n1 还有: 锂电, 宁德时代          │
│    n3 还有: 宁德时代                │
│    n5 还有: 锂电                    │
│    n7 还有: 宁德时代, 汽车          │
│                                    │
│  related_entities = {              │
│    "宁德时代": 3,  ← 共现3次        │
│    "锂电": 2,                       │
│    "汽车": 1,                       │
│  }                                 │
└──────────┬─────────────────────────┘
           │
           ▼
┌────────────────────────────────────┐
│ Step 4: 配对与去重                   │
│                                    │
│  筛选条件:                          │
│   overlap >= 2 (至少共现2次)        │
│   pair_key 排序去重                 │
│   (A,B) 和 (B,A) 视为同一对         │
│   必须有共同节点 (common_ids 非空)   │
└──────────┬─────────────────────────┘
           │
           ▼
┌────────────────────────────────────┐
│ Step 5: 建链                        │
│                                    │
│  取两个实体的共同节点作为链节点       │
│  按时间排序，相邻连边                │
│                                    │
│  link_type = "entity"              │
│  strength = 0.7                    │
│                                    │
│  significance 计算:                 │
│   base = 0.6                       │
│   + overlap_bonus × min(overlap,4) │
│   = 0.6 + 0.1 × 3 = 0.9           │
│                                    │
│  hidden_signals:                    │
│   "比亚迪与宁德时代出现3次共同报道"  │
│   "两个实体的关联可能尚未被市场      │
│    充分定价"                        │
│                                    │
│  按 significance 降序，取 Top 10    │
└────────────────────────────────────┘
```

**关键设计决策**：

1. **三层实体提取**：优先用结构化字段（mentioned_companies、related_sectors），为空时回退到标题关键词。这保证即使数据库字段不完整也能工作。

2. **共现次数阈值**：至少共现2次才建链，避免偶然共现制造噪音。

3. **显著性随共现次数递增**：`significance = 0.6 + 0.1 × min(overlap, 4)`，共现越多越重要，但有上限防止溢出。

4. **标题关键词提取使用 jieba 分词 + 停用词过滤**：过滤掉"公司"、"公告"等泛词，只保留有信息量的实体词。

---

## 四、显著性评分算法

所有链共享同一个显著性计算函数 `_calc_significance()`，综合四个维度：

```
significance =
    源优先级得分  × 0.30    ← max(节点源优先级) / 5.0
  + 情绪极性得分  × 0.30    ← 极性情绪(positive/negative)占比
  + 紧急度得分    × 0.20    ← 紧急/重要节点占比
  + 节点数量得分  × 0.20    ← min(节点数/10, 1.0)

最终值 clamp 到 [0, 1]
```

**含义**：
- 源优先级高（如财联社独家）→ 更可信
- 情绪极性强（利空/利好而非中性）→ 更值得关注
- 紧急度高 → 市场可能还没反应
- 节点数量多 → 证据链更完整

**例外**：异常链使用固定显著性 0.8，实体交叉链使用基于共现次数的公式计算。

---

## 五、数据富化层 (Enricher)

### 5.1 问题背景

数据库中 `sentiment`、`urgency`、`mentioned_companies`、`related_sectors` 四个关键字段**全部为空** (0% 填充率)。这些字段是板块传导链分组、实体交叉链关联、情绪转变检测的基础。空值导致这些功能退化为只依赖标题关键词。

### 5.2 分层富化策略

```
全量新闻 items
    │
    ├── Tier 1: 规则匹配 (在 _plan 阶段, 全量处理, 免费)
    │   │
    │   ├── mentioned_companies:
    │   │   ts_codes → ts_code_name.json 映射表 → 公司名称
    │   │   覆盖 94.8% 的公告源数据, 可靠
    │   │
    │   ├── related_sectors:
    │   │   ts_codes → ts_code_industry.json → 行业
    │   │   标题匹配 industry_alias 别名 → 行业
    │   │
    │   ├── sentiment:
    │   │   正面词 (涨停/大涨/利好...) vs 负面词 (跌停/大跌/利空...)
    │   │   计数比较 → positive / negative / neutral
    │   │   注意: 仅作为兜底, Tier 2 LLM 会覆盖
    │   │
    │   └── urgency:
    │       source_priority <= 1 或标题含"突发"/"紧急" → urgent
    │       source_priority <= 2 或标题含"重要" → important
    │       其他 → normal
    │
    └── Tier 2: LLM 批量富化 (在 _execute 阶段, 只处理链中仍为空的节点)
        │
        │   对每条链, 找出 sentiment/companies/sectors 仍全为空的节点
        │   分批 (每批 30 条) 调用 LLM 提取三个字段
        │   用最小最便宜的模型 (glm-4-flash) 即可
        │
        └── 成本: 每次 run 约 10 条链 × 1 次 LLM 调用 ≈ 10 次
            每次约 2000 token 输入 + 500 token 输出
```

### 5.3 调用位置

```
Plan (_plan):
  分层扫描 → flash_items + filing_items
  │
  └── enricher.enrich_items(recent)    ← Tier 1, 全量处理
  │
  后续实体统计、行业推断、建链 (使用 Tier 1 补全后的数据)

Execute (_execute):
  chain_builder 构建链 → all_chains
  │
  └── enricher.enrich_chain_nodes()    ← Tier 2, 只处理链中空节点
  │
  insight_engine.analyze_chains()      ← LLM 分析 (使用 Tier 1+2 补全的数据)
```

### 5.4 ts_codes 映射缓存

ts_codes → 公司名称的映射表首次运行时自动构建：
- 从数据库中扫描公告标题格式 `"公司名:关于..."` 正则提取
- 缓存到 `data/cache/ts_code_name.json`
- 后续运行直接读取缓存

---

## 六、链节点数量与 LLM 展示策略

### 6.1 设计原则

**链构建不设节点数量上限** — 由 `query_limit_*` 参数控制检索量，确保有价值消息不丢失。

**LLM 展示分层处理** — 链可能有几十甚至上百条新闻，但 LLM 上下文有限。超出部分不丢弃，而是按天分组摘要。

### 6.2 展示策略

```
链节点数 <= insight_max_news (默认 80):
  → 全部完整展示给 LLM

链节点数 > insight_max_news:
  → 前 80 条: 完整展示 (ID/时间/来源/标题/公司/板块/情绪)
  → 剩余部分: 按天分组摘要
     [2026-05-03] 15 条: 「标题1」、「标题2」... 等15条
     [2026-05-04] 8 条: 「标题3」、「标题4」... 等8条
```

### 6.3 相关配置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `insight_max_news` | 80 | 传给 LLM 的完整新闻最大条数 |
| `insight_summary_threshold` | 80 | 超过此值时启用按天分组摘要 |
| `query_limit_entity` | 500 | timeline 链检索新闻上限 |
| `query_limit_timeline` | 500 | 板块链检索新闻上限 |
| `query_limit_cross` | 5000 | 交叉链检索新闻上限 |
| `query_limit_urgent` | 500 | 异常链检索新闻上限 |

## 七、ReAct 闭环推演引擎


### 7.1 整体流程

Agent 使用 ReAct (Reasoning + Acting) 范式，在 Plan → Execute → Evaluate → Refine 的循环中不断改进分析质量：

```
                    ┌─────────┐
                    │  IDLE   │
                    └────┬────┘
                         │
                    ┌────▼────┐
                ┌──│ PLANNING │
                │  └────┬────┘
                │       │
                │  ┌────▼─────┐
                │  │ EXECUTING │◄─────────────────┐
                │  └────┬─────┘                   │
                │       │                         │
                │  ┌────▼──────────┐               │
                │  │ EVALUATING    │               │
                │  └────┬──────────┘               │
                │       │                         │
                │   ┌───┴───┐                     │
                │   │passed?│                     │
                │   └┬────┬─┘                     │
                │  yes    no                      │
                │   │      │                      │
                │   │  ┌───▼───┐  ┌───────────┐   │
                │   │  │REFINE │→│选修正策略   │   │
                │   │  └───────┘  └─────┬─────┘   │
                │   │                    │         │
                │   │         ┌──────────┤         │
                │   │         │          │         │
                │   │    ┌────▼───┐ ┌───▼────┐    │
                │   │    │expand  │ │add     │    │
                │   │    │context │ │chains  │    │
                │   │    └────┬───┘ └───┬────┘    │
                │   │         │         │         │
                │   │         │    ┌───▼────┐     │
                │   │         │    │critique│     │
                │   │         │    │revise  │     │
                │   │         │    └───┬────┘     │
                │   │         │        │         │
                │   │         └────────┼─────────┘
                │   │                  │
           ┌────▼───▼──┐              │
           │  COMPLETE  │              │
           └───────────┘              │
                                      │
           ┌───────────┐              │
           │  FAILED   │◄───── max_iterations 耗尽
           └───────────┘
```

### 7.2 Plan 阶段 — 数据扫描与策略生成

Plan 阶段决定"分析什么"和"怎么分析"。采用**分层扫描**策略：

```
┌──────────────────────────────────────────┐
│         分层扫描                            │
│                                          │
│  第一层: 快讯源 (市场热点，全量获取)         │
│  ├── cls (财联社)                         │
│  ├── jin10 (金十数据)                     │
│  ├── thx (同花顺)                         │
│  ├── xueqiu (雪球)                       │
│  ├── eastmoney (东方财富)                 │
│  ├── sina, wallstreetcn, cctv 等         │
│  └── 全量获取，不截断                       │
│                                          │
│  第二层: 公告源 (个股事件，限5000条)         │
│  ├── eastmoney_notice                    │
│  └── cninfo (巨潮信息)                    │
│                                          │
│  合并: 快讯在前 (优先被分析)，公告补充       │
└──────────────────────────────────────────┘
```

**三种运行模式的链型覆盖策略**：

系统支持三种运行模式，每种模式都力求**全面覆盖**四种链型。
所有链数量上限均可通过 `analyst.yaml` 配置：

| 链类型 | `--auto` | `--entity` | `--keywords` | 数量上限配置 |
|--------|----------|------------|--------------|-------------|
| timeline (指定) | - | 1 条 | - | - |
| timeline (tracking) | **常驻跟踪词命中** | - | - | `max_timeline_chains` |
| timeline (扩展) | ~10 条 (自动热门) | ~5 条 (关联实体) | ~10 条 (自动热门) | `max_entity_expand_chains` |
| sector_propagation | **自动推断** + **tracking词匹配行业** | 实体扩展板块 | 指定 + 自动 | `max_sector_chains` |
| anomaly | 有 | 有 | 有 | `max_anomaly_chains` |
| entity_cross | 有 | 有 | 有 | `max_entity_cross_chains` |

**链数量上限配置一览**（`analyst.yaml`）：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `max_timeline_chains` | 20 | timeline 链总数上限 (高频词 + tracking_keywords 合计) |
| `max_sector_chains` | 8 | sector_propagation 链总数上限 (行业推断 + tracking 匹配 + 扩展合计) |
| `max_anomaly_chains` | 1 | anomaly 链数量 (1条即覆盖所有爆发实体) |
| `max_entity_cross_chains` | 1 | entity_cross 链数量 |
| `max_entity_expand_chains` | 5 | --entity 模式: 从指定实体新闻中提取的关联实体数 |
| `max_sector_expand_chains` | 3 | --entity 模式: 关联板块映射为行业后建 sector 链数 |
| `max_auto_sector_chains` | 3 | 非 auto 模式: 行业推断补建 sector 链数 |

**常驻跟踪关键词** (`tracking_keywords`)：

auto 模式下，系统会扫描所有新闻，检查以下关键词是否出现。
命中的关键词**优先于高频词**建链：

```yaml
# analyst.yaml 中配置，可随时增删
tracking_keywords:
  - AI          # 人工智能
  - 机器人      # 机器人 / 人形机器人
  - 特斯拉      # 特斯拉 (美股映射)
  - 商业航天    # 商业航天 / 卫星
  - 航天        # 航天 / 火箭
  - 半导体      # 半导体 / 芯片
  - 存储        # 存储芯片 / 内存
  - 电池        # 电池 / 锂电池 / 固态电池
  - 矿          # 矿产 / 锂矿 / 稀土
  - 算力        # 算力 / GPU / 数据中心
  - 服务器      # 服务器 / AI服务器
  - 黄金        # 黄金 / 贵金属
  - 白银        # 白银 / 沪银
  - 原油        # 原油 / 石油
```

命中后的建链规则：

```
tracking keyword 命中
  │
  ├── 始终: 建立 timeline 链 (追踪该关键词的事件演变)
  │
  └── 如匹配到 industry_alias:
      └── 额外建立 sector_propagation 链
          例: "半导体" → 匹配行业别名 ["半导体","芯片","集成电路","封测"]
              → 建板块传导链

  未匹配到行业的 tracking keyword (如 "特斯拉"、"黄金"):
  只建 timeline 链，不建 sector 链
  (它们是公司/商品，不是行业)
```

**`--auto` 模式**（全自动，无需指定任何焦点）：

```
python main.py run --auto --days 30

Plan 阶段自动执行:
  1. 分层扫描快讯源 + 公告源 → 获取海量新闻
  2. tracking_keywords 匹配 → 检查常驻关键词是否在新闻中出现
  3. 快讯标题分词 → 提取高频关键词 Top 30
  4. 公告实体统计 → 提取 ts_codes 高频实体
  5. 板块统计 → 提取 related_sectors 高频板块
  6. 行业识别 → 将高频词/板块映射到 industry_alias 中的行业
  │
  ├── tracking keywords 命中 → 优先为每个命中的关键词建 timeline 链
  │   └── 匹配到行业的 tracking keyword → 额外建 sector_propagation 链
  ├── 快讯高频词补充 → 建更多 timeline 链 (直到达到 max_timeline_chains)
  ├── 公告高频实体补充 → 继续填充 timeline 链
  ├── 行业推断 → 为匹配到的行业建 sector_propagation 链 (直到达到 max_sector_chains)
  ├── 常驻 anomaly 链 (捕捉消息爆发)
  ├── 常驻 entity_cross 链 (发现实体间隐藏关联)
  └── 未覆盖的高频词 → 补建 timeline 链 (受上限约束)
```

**`--entity` 模式**（指定实体 + 实体扩展）：

```
python main.py run --entity "比亚迪" --days 90

Plan 阶段:
  1. 为 "比亚迪" 建一条 timeline 链
  2. 实体扩展 (_expand_entity):
     ├── 扫描所有涉及 "比亚迪" 的新闻
     ├── 统计共现实体 (如 "宁德时代" 共现 3 次)
     ├── 提取关联板块 (如 "新能源"、"汽车")
     ├── 为共现实体建 timeline 链 (最多 max_entity_expand_chains=5 条)
     └── 将关联板块映射到行业别名 → 建 sector_propagation 链
         (最多 max_sector_expand_chains=3 条)
  3. 常驻 anomaly + entity_cross 链
  4. 如有行业热点 (行业识别命中) → 补建最多 max_auto_sector_chains=3 条 sector_propagation
  5. 未覆盖的高频词 → 补建 timeline 链 (受 max_timeline_chains 约束)
```

**`--keywords` 模式**（指定关键词 + 自动补充）：

```
python main.py run --keywords "芯片,制裁" --days 60

Plan 阶段:
  1. 为 ["芯片","制裁"] 建 sector_propagation 链
  2. tracking_keywords 匹配 → 建 timeline + sector 链 (因为 focus_entity 为空)
  3. 自动选热门实体 → 建 timeline 链
  4. 自动推断行业热点 → 建 sector_propagation 链
  5. 常驻 anomaly + entity_cross 链
  6. 未覆盖的高频词 → 补建 timeline 链
```

**tracking keywords 匹配算法** (`_match_tracking_keywords`)：

```
输入: tracking_keywords=["AI", "机器人", "半导体", ...],
      items=[所有新闻]

对每条新闻，拼接 title + mentioned_companies + related_sectors
检查每个 tracking keyword 是否出现在拼接文本中
统计每个关键词的命中次数

输出 (按命中次数降序):
  [("AI", 15), ("半导体", 8), ("机器人", 5), ...]
```

**关键词→行业映射算法** (`_keyword_to_sector_keywords`)：

```
输入: keyword="半导体"

遍历 industry_alias:
  半导体 → aliases=["半导体","芯片","集成电路","封测"]
           "半导体" 在 aliases 中 → 匹配

输出: ["半导体","芯片","集成电路","封测"]
      → 用于构建 sector_propagation 链的关键词

未匹配的关键词 (如 "特斯拉"、"黄金") 返回 None
→ 只建 timeline 链，不建 sector 链
```

**行业自动识别算法** (`_infer_sectors_from_keywords`)：

```
输入: flash_kws=[("锂电",5), ("光伏",4), ("汽车",3)],
      sector_counts={"新能源":10, "银行":3}

Step 1: 将高频词和板块统计合并为热点词集合
  all_hot_words = {"锂电", "光伏", "汽车", "新能源", "银行"}

Step 2: 遍历 industry_alias 配置表:
  新能源 → aliases=["新能源","光伏","锂电","风电","储能","充电桩"]
           命中: ["新能源","光伏","锂电"] → 匹配
  银行   → aliases=["银行"]
           命中: ["银行"] → 匹配
  汽车   → aliases=["汽车","整车","零部件","新能源车"]
           命中: ["汽车"] → 匹配

Step 3: 按命中数量排序，取 Top 5:
  输出: [("新能源", ["新能源","光伏","锂电"]),
         ("银行", ["银行"]),
         ("汽车", ["汽车"])]

每个匹配到的行业都会自动建一条 sector_propagation 链
```

**实体扩展算法** (`_expand_entity`)：

```
输入: entity="比亚迪",
      items=[{title:"比亚迪与宁德时代合作", companies:["比亚迪","宁德时代"],
              sectors:["新能源","汽车"]}, ...]

Step 1: 找出所有提及 "比亚迪" 的新闻
Step 2: 在这些新闻中统计其他实体出现的次数:
  related_companies = {"宁德时代": 3, "一汽": 1}
  related_sectors = {"新能源": 5, "汽车": 4, "锂电": 2}

Step 3: 选取共现 >= 2 次的实体，最多 5 个:
  related_entities = ["宁德时代"]

Step 4: 从关联板块映射到行业别名:
  "新能源" → industry_alias["新能源"] = ["新能源","光伏","锂电","风电","储能"]
  "汽车"   → industry_alias["汽车"] = ["汽车","整车","零部件","新能源车"]

Step 5: 输出:
  related_entities: ["宁德时代"]
  sector_keywords: [["新能源","光伏","锂电","风电","储能"],
                     ["汽车","整车","零部件","新能源车"]]

→ 为 "宁德时代" 建一条 timeline 链
→ 为 "新能源" 行业建一条 sector_propagation 链
→ 为 "汽车" 行业建一条 sector_propagation 链
```

**高频词提取流程**：

1. 对所有快讯标题做 jieba 分词
2. 过滤停用词（虚词、财经泛词、公告泛词）
3. 过滤数字纯数字和百分比
4. 统计词频，取 Top 30
5. 排除已在公告实体中的词（避免重复）
6. 排除政治关键词

### 7.3 Execute 阶段 — 建链与 LLM 分析

```
                    ┌─────────────────┐
                    │ 遍历 plan.chains │
                    │ 逐个构建线索链    │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ 过滤:            │
                    │ significance     │ ← 低显著性链直接丢弃
                    │ >= 0.3           │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ 前置过滤:        │
                    │ 排除无投资价值链  │ ← 政治新闻链 + 无 ts_codes → 跳过
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ LLM 逐链分析     │ ← InsightEngine
                    │ 每条链调用一次LLM │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ 后处理:          │
                    │ 1. 去重 (LCS+3g) │ ← 主题相似的洞察只保留最高置信度
                    │ 2. 低质量过滤     │ ← 置信度<0.3 或无股票标的 → 移除
                    │ 3. 股票验证       │ ← 联网核实推荐股票是否存在
                    └─────────────────┘
```

**去重算法**（双重策略）：

1. **LCS 最长公共子串**：如果两条洞察的 thesis（核心论点）的最长公共子串 >= 6 个字符，视为重复
2. **3-gram Jaccard 相似度**：将 thesis 切成 3 字符的 n-gram 集合，Jaccard >= 0.25 视为重复
3. 重复时保留置信度更高的那条

### 7.4 Evaluate 阶段 — 六维质量评估

对每条洞察在六个维度上打分：

| 维度 | 权重 | 评分方法 |
|------|------|----------|
| `evidence_coverage` | 15% | LLM 引用的 evidence_ids 占链节点总数的比例 |
| `reasoning_quality` | 15% | key_findings 中有 finding 和 reasoning 描述的比例 |
| `specificity` | 25% | actionable_items 是否包含具体 A 股代码、操作方向、推荐理由 |
| `signal_novelty` | 15% | hidden_signals 中标记 not_priced_in 和有 implication 的比例 |
| `self_consistency` | 10% | thesis、confidence、findings、risks 之间的连贯性 |
| `investment_relevance` | 20% | 是否有具体股票推荐；纯政治事件直接得 0 分 |

**幻觉检测**：

```
检查项:
1. evidence_ids 引用了链中不存在的新闻 ID → 标记"引用了不存在的证据ID"
2. actionable_items.targets 中的目标不在源数据实体中 → 标记"操作目标不在源数据中"

惩罚:
  每个 flag 扣 0.1 分，最多扣 0.3 分
```

**通过条件**：

```
通过 = (平均分 >= quality_threshold) AND (通过率 >= 50%)
```

### 7.5 Refine 阶段 — 三种修正策略

当评估未通过时，根据**最弱维度**自动选择修正策略：

```
评估失败
    │
    ├── 幻觉存在? ──────────→ critique_revise
    │                          把批评意见反馈给 LLM 重写
    │
    ├── 最弱维度是 evidence_coverage?
    │   ──→ expand_context
    │        时间窗口 × 1.5 扩大
    │        例: 90天 → 135天 → 202天
    │
    ├── 最弱维度是 signal_novelty?
    │   ──→ add_chains
    │        补充 anomaly 和 entity_cross 链类型
    │
    └── 最弱维度是 reasoning_quality 或 specificity?
        ──→ critique_revise
             不改参数，把评估器的批评意见
             嵌入到下一轮 LLM prompt 中
```

**迭代控制**：

- 默认最多 3 轮迭代 (`max_iterations=3`)
- 每轮迭代都会触发新的 Execute → Evaluate 循环
- 通过后立即结束，不浪费 LLM 调用

---

## 八、LLM 推演的核心机制

### 8.1 Prompt 工程

系统使用两级 Prompt 结构：

**System Prompt** 定义 LLM 的角色和约束：

- 身份：A股资深投资分析师
- 核心原则：投资导向、具体标的、因果链条、交叉验证、时间序列、市场定价
- 禁止输出：泛泛之词、无代码的"关注XX行业"、纯政治解读、无因果链的牵强关联
- 输出格式：严格 JSON，包含 thesis/confidence/key_findings/hidden_signals/risk_factors/actionable_items

**User Prompt** 提供具体的链数据：

```
链信息: 类型、主题、时间跨度、重要性、已发现信号
可用股票代码: 链中新闻涉及的 ts_codes
新闻列表: 格式化的节点详情（时间、来源、标题、股票代码、情绪、紧急度）
批评意见: (仅 critique_revise 策略时) 上一轮的批评内容
```

### 8.2 LLM 推演过程

LLM 在收到链数据后的推演过程（由 prompt 引导）：

```
输入: 一条线索链（类型 + 主题 + N 个新闻节点 + 隐蔽信号）

Step 1: 阅读所有新闻，建立时间序列理解
        ↓
Step 2: 识别因果关系 (事件A → 行业B → 个股C)
        ← 仅接受有明确传导路径的分析
        ↓
Step 3: 发现隐蔽信号 (表面信息之下的含义)
        ← 评估是否已被市场定价 (not_priced_in)
        ↓
Step 4: 生成可操作建议
        ← 必须包含具体 A 股代码
        ← 优先从链中已有的 ts_codes 选取
        ↓
Step 5: 评估风险因素
        ← 从链数据中提取反面证据

输出: 结构化 JSON
{
  thesis: "因果论点",
  confidence: 0.0-1.0,
  key_findings: [{finding, evidence_ids, reasoning}],
  hidden_signals: [{signal, implication, not_priced_in}],
  risk_factors: [...],
  actionable_items: [{action, urgency, targets, reason}]
}
```

---

## 九、状态机与持久化

### 9.1 状态流转图

```
IDLE ──→ PLANNING ──→ EXECUTING ──→ EVALUATING ──→ COMPLETE
                                          │
                                          ├──→ REFINE ──→ EXECUTING (循环)
                                          │
                                          └──→ FAILED
                                               ↑
                    (任何阶段异常都可能进入) ─────┘
```

合法状态转换表：

| 当前状态 | 可转换到 |
|---------|---------|
| IDLE | PLANNING |
| PLANNING | EXECUTING, FAILED |
| EXECUTING | EVALUATING, FAILED |
| EVALUATING | COMPLETE, REFINE, FAILED |
| REFINE | EXECUTING, FAILED |
| COMPLETE | (终态) |
| FAILED | (终态) |

### 9.2 原子持久化

每次状态变更都写入 JSON 文件，使用原子写入（先写临时文件再 rename）防止写到一半崩溃：

```
state/
├── a1b2c3d4e5f6.json    ← 每次 run 的完整状态
├── f7e8d9c0b1a2.json
└── ...
```

保存的内容包括：运行参数、链数据、洞察结果、评估分数、迭代历史、错误记录。

### 9.3 Resume 恢复

支持从任意中断点恢复：
1. 加载 JSON 文件还原 RunContext
2. 检查当前状态
3. 从断点继续执行（例如从 EXECUTING 继续）

---

## 十、配置参数一览

所有链构建参数都集中在 `AnalystConfig` 中，可通过 `analyst.yaml` 覆盖：

### 链构建参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `chain_timeline_strength` | 0.5 | 时间链连边强度 |
| `chain_sector_strength` | 0.6 | 板块链连边强度 |
| `chain_anomaly_strength` | 0.8 | 异常链连边强度 |
| `chain_cross_strength` | 0.7 | 交叉链连边强度 |
| `chain_anomaly_significance` | 0.8 | 异常链固定显著性 |
| `chain_cross_base_significance` | 0.6 | 交叉链基础显著性 |
| `chain_cross_overlap_bonus` | 0.1 | 交叉链共现加分 |
| `chain_cross_max_overlap` | 4 | 共现加分上限 |
| `chain_burst_density_threshold` | 0.5 | 爆发密度阈值 |
| `min_cluster_size` | 3 | 最小聚类节点数 |
| `chain_significance_filter` | 0.3 | 显著性过滤阈值 |

### 显著性权重

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `chain_weight_source_priority` | 0.3 | 源优先级权重 |
| `chain_weight_sentiment_polarity` | 0.3 | 情绪极性权重 |
| `chain_weight_urgency` | 0.2 | 紧急度权重 |
| `chain_weight_node_count` | 0.2 | 节点数量权重 |

### Agent 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_iterations` | 3 | ReAct 最大迭代次数 |
| `expansion_factor` | 1.5 | expand_context 的时间窗口扩展系数 |
| `quality_threshold` | 0.65 | 评估通过阈值 |
| `hybrid_alpha` | 0.7 | 向量搜索权重 (0=纯关键词, 1=纯向量) |

### 链数量上限参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_timeline_chains` | 20 | timeline 链总数上限 |
| `max_sector_chains` | 8 | sector_propagation 链总数上限 |
| `max_anomaly_chains` | 1 | anomaly 链数量 |
| `max_entity_cross_chains` | 1 | entity_cross 链数量 |
| `max_entity_expand_chains` | 5 | --entity 模式关联实体扩展上限 |
| `max_sector_expand_chains` | 3 | --entity 模式关联板块链扩展上限 |
| `max_auto_sector_chains` | 3 | 非 auto 模式行业推断板块链上限 |

### 常驻跟踪关键词

| 参数 | 说明 |
|------|------|
| `tracking_keywords` | auto 模式下始终跟踪的关键词列表，命中后自动建 timeline + sector 链 |

---

## 十一、完整数据流示意

以 `--entity` 模式为例：

```
用户: python main.py run --entity "比亚迪" --days 90
                    │
                    ▼
            ┌──────────────┐
            │   Harness    │ 初始化 RunContext
            │              │ ctx.focus_entity = "比亚迪"
            │              │ ctx.time_window_days = 90
            └──────┬───────┘
                   │
            ┌──────▼───────┐
            │     Plan     │
            │              │ 分层扫描: 快讯源 + 公告源
            │              │ 提取高频词 / 板块统计
            │              │ 行业识别: 新能源、汽车
            │              │ 实体扩展: 比亚迪→宁德时代(共现3次)
            │              │
            │              │ 生成建链计划:
            │              │  1. timeline(比亚迪)
            │              │  2. timeline(宁德时代)  ← 实体扩展
            │              │  3. sector(新能源:光伏,锂电,风电,储能) ← 实体扩展
            │              │  4. sector(汽车:整车,零部件)  ← 实体扩展
            │              │  5. sector(半导体:芯片,集成电路) ← 行业识别
            │              │  6. anomaly
            │              │  7. entity_cross
            └──────┬───────┘
                   │ iteration=1
            ┌──────▼───────┐
            │   Execute    │
            │              │ 构建 7 条链
            │              │ LLM 逐链分析 → 生成洞察
            │              │ 去重 + 过滤 + 股票验证
            │              │ 得到 8 条洞察
            └──────┬───────┘
                   │
            ┌──────▼───────┐
            │   Evaluate   │
            │              │ 六维评分 → overall: 0.68
            │              │ passed: NO
            │              │ critique: "可操作项过于笼统..."
            └──────┬───────┘
                   │
            ┌──────▼───────┐
            │    Refine    │ 策略: critique_revise
            └──────┬───────┘
                   │ iteration=2
            ┌──────▼───────┐
            │   Execute    │ (第二轮，带批评意见)
            │              │ LLM 给出更具体的推荐
            └──────┬───────┘
                   │
            ┌──────▼───────┐
            │   Evaluate   │ overall: 0.82, passed: YES
            └──────┬───────┘
                   │
            ┌──────▼───────┐
            │   COMPLETE   │ 生成报告
            └──────────────┘
```

以 `--auto` 模式为例：

```
用户: python main.py run --auto --days 30
                    │
                    ▼
            ┌──────▼───────┐
            │     Plan     │
            │              │ 分层扫描快讯+公告
            │              │ 高频词: 锂电(12), 光伏(8), 半导体(7), ...
            │              │ 板块统计: 新能源(25), 银行(15), 汽车(12)
            │              │ 行业识别: 新能源→[光伏,锂电,风电,储能]
            │              │            银行→[银行]
            │              │            半导体→[芯片,集成电路,封测]
            │              │
            │              │ 生成建链计划:
            │              │  timeline: 锂电, 光伏, 半导体, ... (Top10)
            │              │  sector:   新能源, 银行, 半导体 (自动推断)
            │              │  anomaly:  1 条
            │              │  entity_cross: 1 条
            │              │  高频词补链: 若干条
            └──────┬───────┘
                   │
              (后续同上)
```

---

## 十二、关键词提取与停用词机制

关键词提取是实体交叉链和异常链的关键前置步骤：

### 提取流程

```
标题: "比亚迪发布新一代刀片电池，能量密度提升50%"
          │
          ▼
jieba 分词:
["比亚迪", "发布", "新一代", "刀片", "电池", "，",
 "能量", "密度", "提升", "50", "%"]
          │
          ▼
过滤规则:
  - 长度 < 2 → 移除 ("发布")
  - 在停用词表中 → 移除
  - 纯数字/百分比 → 移除 ("50", "%")
          │
          ▼
结果: ["比亚迪", "新一代", "刀片", "电池", "能量", "密度", "提升"]
```

### 停用词分类

| 类别 | 示例 |
|------|------|
| 中文虚词 | 的、了、在、是、和、就、不 |
| 财经泛词 | 公司、集团、股东、减持、增持、公告、表示 |
| 公告泛词 | 关于、审议、批准、表决、独立、立案、调查 |
| 快讯泛词 | 日内、盘中、报道、续创、拉升、涨幅扩大 |
| 新闻泛词 | 市场、预期、增长、全球、经济、最新、近期 |

---

## 附录：模块依赖关系

```
main.py
  └── harness.py (Harness)
        ├── config.py (AnalystConfig)
        │     └── tracking_keywords   ← 常驻跟踪关键词列表 (可配置)
        │     └── max_*_chains        ← 各类链数量上限 (可配置)
        │     └── industry_alias      ← 行业别名映射 (可配置)
        ├── state.py (RunContext, AgentState, StateStore)
        ├── agent.py (AnalysisAgent)
        │     ├── _plan()                        ← 建链决策引擎
        │     │   ├── _match_tracking_keywords() ← 常驻关键词匹配
        │     │   ├── _keyword_to_sector_keywords() ← 关键词→行业映射
        │     │   ├── _infer_sectors_from_keywords() ← 行业自动识别
        │     │   └── _expand_entity()           ← 实体扩展
        │     ├── chain_builder.py (ChainBuilder)
        │     │     └── query.py (NewsQuery) → indexagent/sdk.py
        │     ├── insight_engine.py (InsightEngine, LLMClient)
        │     ├── evaluator.py (Evaluator)
        │     └── stock_verify.py (verify_insight_stocks)
        └── report.py (generate_report)
              └── stock_holders.py (fetch_top_holders)
```
