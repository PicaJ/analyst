# 线索链构建实现原理详解

> 源码位置: `analyst/chain_builder.py`
> 配置位置: `analyst.yaml` → `chain_*` 前缀参数
> 调用位置: `analyst/agent.py` → `_execute()` → `_build_chain()`

---

## 一、总体设计思想

线索链系统要解决的核心问题是：**从海量碎片化新闻中，发现隐蔽的因果关联和趋势演变**。

单条新闻的信息量有限，但当多条新闻以正确的方式被串联起来后，就能暴露出单条新闻无法揭示的信号。例如：

- 同一家公司连续 3 天被不同来源报道（时间链 → 事件正在升温）
- 政策先在 A 行业引发反应，B 行业尚未跟进（板块传导链 → 滞后机会）
- 某个关键词突然在 2 小时内出现 10 条新闻（异常链 → 可能存在未公开信息）
- 两个看似无关的公司频繁同时出现在不同新闻中（实体交叉链 → 隐蔽关联）

为实现这些目标，系统定义了 4 种链类型，每种对应一种**关联发现策略**：

| 链类型 | 发现策略 | 连接方式 | 核心算法 |
|--------|---------|---------|---------|
| 时间链 (timeline) | 同一实体的时间演变 | 按时间顺序首尾相连 | 混合检索 + 时间排序 + 情绪漂移检测 |
| 板块传导链 (sector_propagation) | 跨行业的政策/事件传导 | 板块间按首次出现时间串联 | 板块分组 + 时序排列 + 传导路径检测 |
| 异常链 (anomaly) | 消息频率/密度异常 | 同一实体的爆发新闻串联 | 密度检测（消息数/时间跨度） |
| 实体交叉链 (entity_cross) | 不同实体的隐蔽关联 | 共现新闻节点按时间串联 | 实体共现计数 + 集合交集 |

---

## 二、核心数据结构

### 2.1 ChainNode — 新闻节点

每条新闻被抽象为一个节点，携带多维结构化信息：

```
ChainNode:
    news_id            → 唯一标识（用于去重和连接）
    title              → 新闻标题（自然语言）
    publish_time       → 发布时间（排序依据）
    source             → 数据源（cls/jin10/eastmoney_notice 等）
    source_priority    → 来源权威度（1-5，越高越可靠）
    category           → 新闻分类
    sentiment          → 情绪标注（positive/negative/neutral）
    urgency            → 紧急度（normal/urgent/important）
    ts_codes           → 关联股票代码列表
    mentioned_companies → 提及的公司实体列表
    mentioned_persons  → 提及的人物列表
    related_sectors    → 关联行业板块列表
    impact_scope       → 影响范围
```

**关键设计点**：节点不是简单的"一条新闻"，而是**结构化实体信息**的载体。`mentioned_companies`、`related_sectors` 这些字段是链构建算法的核心输入——算法通过这些字段识别"哪些新闻属于同一实体"。

### 2.2 ChainLink — 连接边

每两个节点之间的关联被描述为一条有向边：

```
ChainLink:
    from_id   → 源节点 ID
    to_id     → 目标节点 ID
    link_type → 连接类型（temporal/entity/sector/anomaly）
    strength  → 连接强度（0.0~1.0，值越高表示关联越紧密）
    reason    → 连接原因（自然语言描述，供 LLM 理解）
```

`strength` 是可配置参数，不同链类型有不同的默认强度：

- 时间链: 0.5（时间相邻本身不代表强因果关系）
- 板块传导链: 0.6（板块间传导有一定滞后性）
- 实体交叉链: 0.7（共现关联较有价值）
- 异常链: 0.8（密度异常本身就是强信号）

### 2.3 ClueChain — 完整链

```
ClueChain:
    chain_id        → 链唯一 ID
    chain_type      → 链类型标识
    theme           → 主题描述（供 LLM 理解）
    nodes           → 节点列表
    links           → 边列表
    significance    → 重要性评分（0.0~1.0）
    hidden_signals  → 算法检测到的隐蔽信号列表
```

`significance` 是链的"质量分数"，决定这条链是否值得送给 LLM 分析。低于 `chain_significance_filter`（默认 0.3）的链会被直接丢弃。

---

## 三、数据检索层：混合检索策略

4 种链共享同一套数据检索基础设施，理解检索层是理解链构建的前提。

### 3.1 双路检索 + 合并去重

时间链和板块传导链都采用**双路检索**策略：

```
路径 1: search_hybrid()  → FAISS 向量语义搜索 + FTS5 全文关键词搜索
路径 2: get_timeline()   → SQLite 结构化查询（标题 LIKE 匹配）
          ↓
     _merge_dedup()       → 按 news_id 去重合并
```

**为什么要双路？**

- `search_hybrid` 通过向量语义理解能找到"意思相近但用词不同"的新闻（例如搜"特斯拉"能找到"马斯克旗下电动车公司"），但可能遗漏标题中直接包含关键词但语义距离较远的新闻
- `get_timeline` 通过 SQL `LIKE` 能精确匹配关键词，但无法理解语义
- 两者合并后，**召回率最大化**——既不漏字面匹配，又能捕获语义关联

### 3.2 降级机制

当 `search_hybrid` 因为 FAISS 索引未就绪或向量服务异常而失败时，系统会自动降级为纯 SQLite 查询：

```python
try:
    hybrid_items = await self.query.search_hybrid(...)
except Exception:
    hybrid_items = []  # 降级：只用 SQLite 结果
```

这确保了即使向量搜索不可用，链构建仍能基于结构化数据完成。

### 3.3 查询参数控制

每种链类型有独立的查询上限配置，避免数据量爆炸：

| 参数 | 默认值 | 用途 |
|------|--------|------|
| `query_limit_entity` | 500 | 时间链 / 实体查询上限 |
| `query_limit_timeline` | 500 | 板块传导链 / 时间线查询上限 |
| `query_limit_urgent` | 500 | 异常链 / 紧急新闻查询上限 |
| `query_limit_cross` | 5000 | 交叉链 / 时间范围查询上限 |

交叉链上限最高（5000），因为它需要更大的数据集才能发现实体间的共现关系。

---

## 四、4 种线索链的实现原理

### 4.1 时间链 (Timeline Chain)

**目的**：追踪某个实体（公司、关键词、行业）在一段时间内的事件演变过程。

#### 构建流程

```
输入: entity="特斯拉", days=90

Step 1: 双路检索
  ├─ search_hybrid("特斯拉", top_k=500, days=90, alpha=0.7)
  └─ get_timeline(keywords=["特斯拉"], days=90, limit=500)

Step 2: 合并去重
  └─ _merge_dedup(路径1结果, 路径2结果) → items

Step 3: 节点不足则放弃
  └─ if len(items) < 2: return []

Step 4: 转换为 ChainNode 并按时间排序
  └─ nodes.sort(key=lambda n: n.publish_time)

Step 5: 构建时间连接
  └─ 对每对相邻节点 (nodes[i], nodes[i+1]):
      创建 ChainLink(type="temporal", strength=0.5,
                     reason="同一entity的时间演变")

Step 6: 检测情绪漂移
  └─ _detect_sentiment_shifts(nodes)

Step 7: 计算重要性评分
  └─ _calc_significance(nodes)

输出: [ClueChain(theme="特斯拉 事件时间线 (90天)")]
```

#### 情绪漂移检测算法

这是时间链的核心信号检测逻辑。系统遍历排序后的节点序列，对比相邻节点的情绪标注：

```
对每一对相邻节点 (i, i+1):
    if 节点i情绪 != 节点i+1情绪:
        记录一个"情绪转变"信号

特殊模式识别:
  neutral → positive/negative  → "从沉默到表态，值得关注"
  positive → negative          → "利好转利空，重大反转信号"
```

**原理**：在金融市场中，情绪转变往往预示着趋势拐点。一个公司从"无消息"突然变成"正面消息"意味着有新的积极因素出现；而从"正面"急转为"负面"则可能是重大利空信号的前兆。

#### 重要性评分公式

```
significance = (
    source_priority_weight × max_priority / 5     // 来源权威度贡献
  + sentiment_polarity_weight × 极性新闻占比       // 情绪极性贡献
  + urgency_weight × 紧急新闻占比                  // 紧急度贡献
  + node_count_weight × node_count / 10            // 节点数量贡献
)

默认权重:
  source_priority: 0.3   → 高权威来源的链更有价值
  sentiment_polarity: 0.3  → 情绪越鲜明的链越值得关注
  urgency: 0.2           → 紧急新闻多的链更重要
  node_count: 0.2         → 节点越多的链信息量越大
```

最终 `significance = min(score, 1.0)`，上限为 1.0。

---

### 4.2 板块传导链 (Sector Propagation Chain)

**目的**：发现政策/事件如何从上游行业传导到下游行业，寻找**滞后反应的投资机会**。

#### 构建流程

```
输入: policy_keywords=["半导体", "芯片", "集成电路"], days=90

Step 1: 双路检索（与时间链相同的检索策略）
  ├─ search_hybrid("半导体 芯片 集成电路", ...)
  └─ get_timeline(keywords=["半导体", "芯片", "集成电路"], ...)

Step 2: 合并去重 → items (需要 ≥ 3 条才继续)

Step 3: 转换为 ChainNode 并按时间排序

Step 4: 按板块分组 ← 核心步骤
  └─ 遍历每个节点的 related_sectors 字段
     └─ 将节点归入对应板块组
     └─ 无板块标签的节点归入"未分类"

Step 5: 计算每个板块的"首次出现时间"
  └─ 对每个板块组，取其中最早的 publish_time

Step 6: 按首次出现时间排序板块序列
  └─ sector_timeline = [(时间, 板块名, 节点列表), ...] sorted by 时间

Step 7: 构建跨板块传导连接
  └─ 对每对相邻板块 (sector_a, sector_b):
      取 sector_a 中最晚的节点 → sector_b 中最早的节点
      创建 ChainLink(type="sector", strength=0.6,
                     reason="板块传导: {sector_a} → {sector_b}")

Step 8: 检测传导信号
Step 9: 计算重要性评分
```

#### 板块分组的核心逻辑

```python
sector_groups: Dict[str, List[ChainNode]] = defaultdict(list)
for n in nodes:
    for s in n.related_sectors:
        sector_groups[s].append(n)
    if not n.related_sectors:
        sector_groups["未分类"].append(n)
```

**原理**：每条新闻在入库时已经被 `collectagent` 标注了 `related_sectors` 字段。板块传导链利用这个结构化字段，将新闻按行业维度重新组织。如果一个节点关联了多个板块，它会同时出现在多个分组中——这反映了"一条新闻同时影响多个行业"的现实。

#### 传导路径检测算法

```python
for i in range(len(sector_timeline) - 1):
    t1, sector_a, _ = sector_timeline[i]
    t2, sector_b, _ = sector_timeline[i + 1]
    signals.append(
        f"传导路径: {sector_a}({t1[:10]}) → {sector_b}({t2[:10]}), "
        f"{sector_b}可能存在滞后反应机会"
    )
```

**原理**：如果行业 A 在 5月1日 开始出现相关新闻，行业 B 在 5月3日 才开始出现，那么行业 B 的相关股票可能有 2 天的**滞后反应窗口**。这正是板块传导链要捕获的信号——在行业 B 的市场尚未完全反应之前，提前布局。

#### 连接方向的设计

连接从 sector_a 的**最新节点**指向 sector_b 的**最早节点**，而非简单的首尾连接。这个设计选择的原因是：

- sector_a 的最新节点代表了"上游行业对该事件最成熟的理解"
- sector_b 的最早节点代表了"下游行业刚开始做出反应"
- 这个方向反映了**信息传导的真实路径**

---

### 4.3 异常链 (Anomaly Chain)

**目的**：通过消息密度异常检测，发现可能存在未公开信息（内幕消息、即将发布的重大事件等）。

#### 构建流程

```
输入: days=30

Step 1: 获取高优先级新闻
  └─ get_urgent(days=30, limit=500)

Step 2: 过滤噪音源
  └─ 排除 eastmoney_notice、cninfo（公告源）
  └─ 只保留快讯源（cls/jin10/thx 等）
  └─ 保留节点 ≥ 3 才继续

Step 3: 实体爆发检测 ← 核心算法
  └─ _detect_entity_bursts(nodes)

Step 4: 为每个爆发实体构建独立链
  └─ 过滤: 节点数 ≥ min_cluster_size, 非停用词
  └─ 链内相邻节点用 anomaly 类型连接

Step 5: 按节点数排序，取 Top 5

输出: [ClueChain(theme="异常信号: {entity} 消息聚集"), ...]
```

#### 实体爆发检测算法（核心）

这是异常链最关键的算法：

```
1. 构建实体→节点映射:
   对每个节点:
     优先用 mentioned_companies 字段
     其次用 related_sectors 字段
     都为空时，从标题分词提取关键词

2. 对每个实体，计算消息密度:
   times = 该实体所有节点的发布时间（排序后）
   span_hours = (最晚时间 - 最早时间) / 3600
   density = 消息数 / max(span_hours, 1)

3. 判定爆发:
   if density >= chain_burst_density_threshold (0.5 条/小时):
     标记为"爆发实体"
```

**density 计算示例**：

```
实体 "特斯拉" 在过去 30 天内:
  出现在 12 条新闻中
  时间跨度: 最早 2024-05-01 08:00, 最晚 2024-05-01 20:00
  span_hours = 12
  density = 12 / 12 = 1.0 条/小时
  threshold = 0.5
  1.0 >= 0.5 → 判定为爆发 ✓
```

**原理**：在正常情况下，同一实体在数小时内的新闻密度是有限的。如果密度突然飙升，通常意味着：
- 有重大事件即将或刚刚发生
- 市场正在激烈博弈
- 可能存在未公开信息被部分投资者感知

#### 噪音过滤

异常链特别注重噪音过滤，因为公告源（如 eastmoney_notice、cninfo）会在特定时间集中发布大量格式化公告，这些"密度高"的公告是正常业务节奏，不是真正的异常信号。

```python
_FILING = {"eastmoney_notice", "cninfo"}
nodes = [ChainNode.from_dict(it) for it in items if it.get("source") not in _FILING]
```

此外，标题分词提取的关键词会经过停用词过滤（`_STOP_WORDS`），避免"公司"、"集团"、"亿元"等高频泛词被误判为爆发实体。

#### 异常链的固定高重要性

异常链的 `significance` 固定为 0.8（`chain_anomaly_significance`），不使用通用的 `_calc_significance` 方法。原因：异常本身就是强信号，不需要依赖来源权威度等指标来评估。

---

### 4.4 实体交叉链 (Entity Cross Chain)

**目的**：发现两个看似无关的实体频繁共同出现在新闻中，暗示存在隐蔽的市场关联。

#### 构建流程

```
输入: days=60

Step 1: 大范围时间查询
  └─ get_by_time_range(start=60天前, end=现在, limit=5000)

Step 2: 过滤公告源噪音
  └─ 排除 eastmoney_notice、cninfo

Step 3: 构建实体→节点映射 ← 核心数据结构
  对每个节点:
    优先: mentioned_companies 字段中的公司
    其次: related_sectors 字段中的板块
    兜底: 从标题分词提取关键词
  → entity_map: Dict[str, List[ChainNode]]

Step 4: 计算实体间共现计数
  对每个实体 E:
    遍历 E 的所有节点
    在每个节点中收集其他实体（公司/板块/标题关键词）
    → related_entities: Dict[str, int]  (共现次数)

Step 5: 筛选有效交叉对
  └─ 共现次数 ≥ 2
  └─ 去重（sorted pair key）
  └─ 两个实体必须有共同新闻节点

Step 6: 为每个交叉对构建链
  └─ 共同节点按时间排序，用 entity 类型连接

Step 7: 计算交叉链重要性
  └─ significance = base(0.6) + bonus(0.1 × min(overlap, 4))

Step 8: 按重要性排序，取 Top 10
```

#### 实体共现计数的详细算法

```python
for entity, enodes in entity_map.items():
    related_entities: Dict[str, int] = defaultdict(int)

    for n in enodes:
        # 在该节点的公司列表中找其他实体
        for c in n.mentioned_companies:
            if c != entity:
                related_entities[c] += 1

        # 在该节点的板块列表中找其他实体
        for s in n.related_sectors:
            if s != entity:
                related_entities[s] += 1

        # 在该节点的标题关键词中找其他实体
        for kw in _extract_title_keywords(n.title):
            if kw != entity:
                related_entities[kw] += 1
```

**原理**：如果实体 A 和实体 B 在同一条新闻中出现，说明它们在某个事件中被同时提及。如果这种共现发生 ≥ 2 次，就不再是偶然，而是存在某种结构性关联——可能是：
- 同一供应链的上下游关系
- 同一政策的不同受益方
- 同一大股东控制的不同公司
- 竞争对手关系

#### 去重机制

两个实体的交叉关系是双向的（A×B 等于 B×A），因此使用排序后的元组作为去重键：

```python
pair_key = tuple(sorted([entity, rel_entity]))
if pair_key in processed:
    continue
processed.add(pair_key)
```

#### 共同节点筛选

两个实体必须有**实际的共同新闻节点**（不只是共现计数），才构成有效交叉：

```python
common_ids = set(n.news_id for n in enodes) & set(n.news_id for n in rel_nodes)
if not common_ids:
    continue
```

这一步确保交叉链中的每条新闻确实同时涉及两个实体，而非仅仅是"在同一时间段出现"。

#### 交叉链的独特评分公式

```python
significance = chain_cross_base_significance                                    # 0.6
             + chain_cross_overlap_bonus × min(overlap, chain_cross_max_overlap) # 0.1 × min(overlap, 4)
```

| 共现次数 | significance |
|---------|-------------|
| 2 | 0.6 + 0.1×2 = 0.8 |
| 3 | 0.6 + 0.1×3 = 0.9 |
| 4+ | 0.6 + 0.1×4 = 1.0 |

**设计意图**：共现次数越多，关联越强，评分越高。但封顶在 4 次（`chain_cross_max_overlap`），避免某个高频实体因为出现在所有新闻中而获得虚高的评分。

---

## 五、共享的关键词提取与停用词过滤

### 5.1 标题关键词提取

异常链和实体交叉链在实体字段为空时，需要从标题中提取关键词：

```python
def _extract_title_keywords(title: str) -> List[str]:
    try:
        import jieba
        words = list(jieba.cut(title))       # 优先 jieba 分词
    except ImportError:
        parts = re.split(r'[：:，,。！!？?、；;\s]+', title)  # 降级：按标点拆分
        words = [p for p in parts if p]

    return [w for w in words
            if len(w) >= 2                        # 至少 2 个字符
            and w not in _STOP_WORDS              # 非停用词
            and not all(c in "0123456789.%％万亿元角分" for c in w)]  # 非纯数字/金额
```

### 5.2 停用词表

系统维护了一个覆盖 150+ 词的停用词表，分为 4 类：

1. **通用虚词**：的、了、在、是...（自然语言基本成分，无信息量）
2. **财经泛词**：公司、集团、公告、减持、亿元...（所有新闻都会出现，无区分度）
3. **公告标题泛词**：董事会、监事会、审议、批准...（公告格式化用语）
4. **快讯通用词**：盘中、报道、拉升、涨幅扩大...（行情播报用语）

---

## 六、链在 Agent 闭环中的角色

线索链不是孤立存在的，它是 `AnalysisAgent` 闭环推理引擎的核心数据结构：

```
Plan（规划）
  → 扫描数据，决定建哪些类型的链、建多少条
  → 输出: chains_to_build 列表

Execute（执行）
  → 调用 ChainBuilder 构建链
  → 按 significance 过滤低质量链
  → 过滤无投资价值的链（政治类主题无股票代码）
  → 将链送入 LLM 进行分析

Evaluate（评估）
  → 对 LLM 的分析结果进行 6 维度质量评估
  → 检测幻觉

Refine（修正）
  → 质量不够时，选择修正策略:
    - expand_context: 扩大时间窗口（影响链的数据范围）
    - add_chains: 补充更多类型的链（补充 anomaly/entity_cross）
    - critique_revise: 将批评反馈给 LLM 重写
```

### 建链策略

Plan 阶段根据运行模式选择建链策略：

**auto 模式**（全自动）：
1. 从快讯标题分词中提取高频实体 → 建 timeline 链
2. 将高频词与 `industry_alias` 匹配 → 建 sector_propagation 链
3. 固定建 anomaly 链（检测全局异常）
4. 固定建 entity_cross 链（检测全局交叉）

**entity 模式**（指定实体）：
1. 为指定实体建 timeline 链
2. 从指定实体的新闻中提取关联实体 → 扩展 timeline 链
3. 从指定实体的新闻中提取关联板块 → 建 sector_propagation 链
4. 固定建 anomaly + entity_cross 链

**keywords 模式**（指定关键词）：
1. 为指定关键词建 sector_propagation 链
2. 固定建 anomaly + entity_cross 链

### 链数量控制

为防止 LLM 调用爆炸，每种链类型都有数量上限：

```yaml
max_timeline_chains: 30       # 时间链最多 30 条
max_sector_chains: 30         # 板块传导链最多 30 条
max_anomaly_chains: 20        # 异常链最多 20 条
max_entity_cross_chains: 20   # 交叉链最多 20 条
```

---

## 七、配置参数速查表

| 参数 | 默认值 | 影响范围 |
|------|--------|---------|
| `chain_timeline_strength` | 0.5 | 时间链连接强度 |
| `chain_sector_strength` | 0.6 | 板块传导链连接强度 |
| `chain_anomaly_strength` | 0.8 | 异常链连接强度 |
| `chain_cross_strength` | 0.7 | 交叉链连接强度 |
| `chain_significance_filter` | 0.3 | 重要性过滤阈值 |
| `chain_anomaly_significance` | 0.8 | 异常链固定重要性 |
| `chain_cross_base_significance` | 0.6 | 交叉链基础重要性 |
| `chain_cross_overlap_bonus` | 0.1 | 交叉链重叠加分 |
| `chain_cross_max_overlap` | 4 | 交叉链重叠上限 |
| `chain_burst_density_threshold` | 0.5 | 异常链爆发密度阈值（条/小时） |
| `min_cluster_size` | 3 | 异常链最小聚类大小 |
| `chain_weight_source_priority` | 0.3 | 评分: 来源权威度权重 |
| `chain_weight_sentiment_polarity` | 0.3 | 评分: 情绪极性权重 |
| `chain_weight_urgency` | 0.2 | 评分: 紧急度权重 |
| `chain_weight_node_count` | 0.2 | 评分: 节点数量权重 |
| `hybrid_alpha` | 0.7 | 混合检索的向量权重 |

---

## 八、算法复杂度分析

| 链类型 | 时间复杂度 | 空间复杂度 | 瓶颈 |
|--------|-----------|-----------|------|
| 时间链 | O(N log N) | O(N) | 排序（N=检索结果数） |
| 板块传导链 | O(N log N + S²) | O(N+S) | 板块分组 + 板块间两两连接 |
| 异常链 | O(N × K) | O(N × E) | 实体映射（K=每条新闻的关键词数，E=实体数） |
| 交叉链 | O(N × K + E² × K) | O(N × E) | 实体间共现计数（E=实体数，K=关键词数） |

其中交叉链在实际运行中是最重的操作，因为需要遍历所有实体对计算共现计数。这也是为什么交叉链使用更大的查询上限（5000）——数据越多，越可能发现隐蔽的交叉关系。
