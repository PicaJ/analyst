# analyst — 财经新闻深度分析引擎

从 indexagent 构建的向量库中检索财经新闻，通过算法构建四种线索链，交给 LLM 分析推导隐蔽信息，**自动评估输出质量，不达标则自我修正并重试**，直到生成高质量的分析报告。

---

## 一、架构总览

```
collectagent          indexagent              analyst
(采集新闻)    →     (构建索引/向量)    →     (深度分析荐股)

analyst 内部架构:

┌──────────────────────────────────────────────────────────┐
│  CLI (main.py)                                           │
│    run / chain / status / resume / runs                  │
└──────────────┬───────────────────────────────────────────┘
               │
┌──────────────▼───────────────────────────────────────────┐
│  Harness (harness.py)                                    │
│  调度器: 初始化上下文、熔断保护、持久化、报告生成        │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  AnalysisAgent (agent.py)                         │  │
│  │  ReAct 闭环引擎                                   │  │
│  │                                                    │  │
│  │  Phase 1: Plan    ─→ 混合检索扫描, 生成分析策略    │  │
│  │  Phase 2: Execute ─→ 构建线索链 + LLM 分析         │  │
│  │  Phase 3: Evaluate ─→ 5 维评分 + 幻觉检测          │  │
│  │  Phase 4: Refine  ─→ 选策略修正 → 回到 Phase 2     │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  ┌─────────────────────────────────────────────────┐    │
│  │  NewsQuery (query.py)                           │    │
│  │  通过 indexagent SDK 检索:                       │    │
│  │    · search_hybrid()  向量+关键词混合检索        │    │
│  │    · get_by_entity()  实体检索                   │    │
│  │    · get_by_time_range() 结构化时间查询          │    │
│  │    · get_urgent()     紧急/高优先级新闻          │    │
│  │    · get_timeline()   关键词时间线               │    │
│  └────────────────┬────────────────────────────────┘    │
│                   │                                      │
│  ┌────────┐  ┌────────┐  ┌──────┐  ┌───────┐          │
│  │Chain   │  │Insight │  │Eval- │  │Report │          │
│  │Builder │  │Engine  │  │uator │  │       │          │
│  └────────┘  └────────┘  └──────┘  └───────┘          │
└──────────────────┬───────────────────────────────────────┘
                   │
          indexagent Python SDK
                   │
    ┌──────────────▼──────────────────┐
    │  IndexAgent (sdk.py)            │
    │  HybridSearch 混合检索引擎      │
    │                                 │
    │  ┌──────┐  ┌──────┐  ┌───────┐ │
    │  │FAISS │  │ FTS5 │  │SQLite │ │
    │  │向量  │  │全文  │  │结构化 │ │
    │  └──────┘  └──────┘  └───────┘ │
    └─────────────────────────────────┘
```

### 闭环流程

```
Plan → Execute → Evaluate → (passed?) → COMPLETE → Report
                         → (failed?) → 找最弱维度 → 选修正策略
                           ├─ expand_context : 时间窗口 × 1.5
                           ├─ add_chains    : 补充 anomaly / entity_cross 链
                           └─ critique_revise: 批评意见注入 LLM 重写
                         → Refine → 回到 Execute (最多 N 次)
```

---

## 二、模块说明

### 2.1 state.py — 状态机与持久化

管理 Agent 运行的状态流转和崩溃恢复。

**状态机:**

```
IDLE → PLANNING → EXECUTING → EVALUATING → COMPLETE
                                  │
                                  ├→ REFINE → EXECUTING (重试)
                                  └→ FAILED (重试耗尽)
```

- `RunContext`: 单次运行的全量上下文 (输入参数、分析计划、线索链、洞察结果、评估分数、迭代历史)
- `StateStore`: 原子写入 JSON 文件，支持 `resume` 从中断处恢复

### 2.2 config.py — 统一配置管理

所有可调参数集中在 `AnalystConfig` 中，通过 `analyst.yaml` 配置:

| 分组 | 参数数 | 关键参数 |
|------|-------|---------|
| data | 4 | data_dir, db_path, report_dir |
| llm | 9 | provider, model, temperature, timeout |
| chain | 21 | 各链类型 strength, significance, burst 密度阈值 |
| eval | 12 | 5 维权重, 幻觉惩罚系数, 覆盖率乘数 |
| search | 2 | search_mode, hybrid_alpha |
| query | 6 | 各类查询 limit |
| agent | 2 | max_iterations, expansion_factor |
| harness | 1 | circuit_breaker_threshold |
| log | 5 | level, retention |

### 2.3 query.py — 数据检索层

analyst **不直接操作 SQLite 或 FAISS**，而是通过 indexagent 的 Python SDK (`IndexAgent`) 检索数据。

#### 数据是怎么存储的

collectagent 采集的每条新闻，经 indexagent 处理后会存成两种形态：

| 存储方式 | 文件 | 存了什么 | 类比 |
|---------|------|---------|------|
| **SQLite** | `news.db` (447MB) | 完整的结构化记录：标题、正文、来源、时间、提到的公司名、所属板块、情绪标签等 29 个字段 | 一张 Excel 表，每列一个属性 |
| **FAISS** | `news.index` (689MB) | 一条 512 维的浮点向量，由 Embedding 模型将新闻文本"压缩"而成 | 把一篇新闻的"意思"变成一个坐标点 |

两者存储的是**同一条新闻**，只是表达方式不同。SQLite 是主存储（先写入），FAISS 从 SQLite 读取文本后生成向量（后构建）。

#### 三种检索方式各是什么意思

**1. SQLite 结构化查询 — 按字段精确过滤**

就像在 Excel 里筛选：时间在最近 30 天 AND 来源是财联社 AND 紧急度高。适合精确条件过滤，速度极快（毫秒级）。

```
能找到: "比亚迪发布新款电动车"     ← mentioned_companies 字段包含"比亚迪"
找不到: "新能源汽车龙头月销暴涨"   ← 没有明确提到"比亚迪"三个字
```

**2. FTS5 全文搜索 — 按关键词匹配**

SQLite 内置的全文索引。搜索"降息 银行"会找到标题或正文中包含这些词的新闻。比 SQLite LIKE 更快更灵活（支持分词、排序），但仍然是基于字面匹配。

```
能找到: "央行降息利好银行股"       ← 包含"降息"和"银行"
找不到: "货币政策宽松推高金融板块"   ← 意思一样，但没有那几个字
```

**3. FAISS 向量搜索 — 按语义（意思）匹配**

新闻先经过 Embedding 模型变成一个 512 维的向量（可以理解为一个坐标）。语义越接近的两条新闻，坐标越近。搜索时，把查询文本也变成向量，然后找"距离最近"的新闻。

```
能找到: "央行降息利好银行股"       ← 语义匹配
能找到: "货币政策宽松推高金融板块"   ← 意思相近，虽然没有关键词重叠
能找到: "贷款利率下行利好地产"       ← 逻辑链条相关
```

#### 三种方式的优缺点

| | SQLite 结构化 | FTS5 全文 | FAISS 向量 |
|---|---|---|---|
| 精确度 | 高（字段级精确匹配） | 中（字面匹配） | 低一些（可能找来"意思相近"的） |
| 召回率 | 低（只找到明确提及的） | 中 | 高（能找到用词不同但意思相关的） |
| 速度 | 毫秒级 | 毫秒级 | 毫秒~百毫秒级 |
| 适合场景 | 按公司名、时间、紧急度过滤 | 按关键词搜索 | 模糊意图搜索、发现隐蔽关联 |

#### 为什么需要合并使用

没有一种方式是完美的。单独用任何一种都会漏掉新闻：

- 只用 SQLite → 漏掉"没提比亚迪但语义高度相关"的新闻
- 只用 FAISS → 可能漏掉"明确提到比亚迪但向量距离稍远"的新闻
- 只用 FTS5 → 漏掉"关键词不同但意思一样"的新闻

**合并使用 = SQLite 保证不漏 + FAISS 扩展语义相关 + FTS5 补充关键词匹配**，然后按 ID 去重，合并成一个完整的结果集。

#### analyst 的检索策略

| 阶段 | 检索方式 | 原因 |
|------|---------|------|
| **Plan (扫描)** | FAISS + FTS5 混合检索 | 扫描哪些实体/板块活跃，需要语义理解 |
| **timeline 链** | FAISS + FTS5 混合 **合并** SQLite 实体匹配 | 向量找语义相关 + 实体匹配保证不漏，去重合并 |
| **sector 链** | FAISS + FTS5 混合 **合并** SQLite 关键词匹配 | 向量找语义相关 + 关键词保证不漏，去重合并 |
| **anomaly 链** | SQLite (urgency + priority) | 结构化过滤，不需要语义搜索 |
| **entity_cross 链** | SQLite (实体精确匹配) | 需要精确的实体重叠关系，语义搜索会引入噪音 |

timeline 和 sector 链的合并去重流程：

```
1. search_hybrid("比亚迪")           → 结果集 A (FAISS + FTS5)
2. get_by_entity(company="比亚迪")   → 结果集 B (SQLite)
3. A ∪ B, 按 id 去重                  → 合并结果
4. 送入链构建逻辑 → 送入 LLM 分析
```

#### 混合检索算法细节

`search_hybrid()` 内部的 FAISS + FTS5 融合过程：

```
输入: query="比亚迪新能源销量", alpha=0.7

1. FAISS 向量搜索 → 取 top_k×2 条, 按余弦相似度排序
2. FTS5 关键词搜索 → 取 top_k×2 条, 按 rank 排序
3. 分数归一化到 [0, 1]:
   - 向量分: cosine_score / max
   - 关键词分: 1 - |rank| / max
4. 加权融合: final = 0.7 × 向量分 + 0.3 × 关键词分
5. 结构化过滤: 时间范围 / 来源 / 分类
6. 取 top_k 条返回
```

`alpha` 控制向量权重: 0 = 纯关键词, 1 = 纯向量, 0.7 = 混合 (默认)。

#### 调用链

```
analyst NewsQuery
  → indexagent.IndexAgent (sdk.py)
    → HybridSearch (hybrid_search.py)
      → VectorStore (faiss)    向量语义搜索
      → FTS5                    关键词全文搜索
      → SQLite                  结构化查询
```

FAISS 索引和 Embedding 模型均为**懒加载**，首次调用搜索方法时才初始化。

### 2.4 chain_builder.py — 四种线索链

| 链类型 | 发现能力 | 数据来源 | 典型隐蔽信号 |
|--------|---------|---------|-------------|
| **timeline** | 同一实体的事件演变轨迹 | 混合检索 + SQLite 实体匹配 (合并去重) | 情绪反转 (沉默→表态)、利好→利空 |
| **sector_propagation** | 跨板块传导路径 | 混合检索 + SQLite 关键词匹配 (合并去重) | 下游板块滞后反应机会 |
| **entity_cross** | 实体间隐蔽关联 | `get_by_time_range()` | 未被市场定价的交叉关系 |
| **anomaly** | 短期消息爆发 | `get_urgent()` | 未公开信息即将释放 |

每条链计算 `significance` 重要性评分 (基于来源权威度、情绪极性、紧急度、节点数，权重均可配)。

### 2.5 insight_engine.py — LLM 洞察引擎

将线索链交给 LLM 分析，输出结构化 JSON:

```json
{
  "thesis": "核心论点",
  "confidence": 0.85,
  "time_horizon": "中期(1-4周)",
  "key_findings": [{"finding": "...", "evidence_ids": [...], "reasoning": "..."}],
  "hidden_signals": [{"signal": "...", "implication": "...", "not_priced_in": true}],
  "risk_factors": ["..."],
  "actionable_items": [{"action": "...", "urgency": "high", "targets": ["002594.SZ"]}]
}
```

支持的 LLM 提供商:

| 提供商 | API 格式 | 默认 base_url |
|--------|---------|--------------|
| openai | OpenAI | https://api.openai.com |
| deepseek | OpenAI 兼容 | https://api.deepseek.com |
| siliconflow | OpenAI 兼容 | 需配置 |
| moonshot | OpenAI 兼容 | 需配置 |
| qwen | OpenAI 兼容 | 需配置 |
| anthropic | Anthropic | https://api.anthropic.com |
| ollama | Ollama | http://localhost:11434 |

特性: HTTP 连接池复用、JSON 解析容错 (支持 markdown 包裹)、critique 注入 (用于 critique_revise 策略)。

### 2.6 evaluator.py — 自评估器

**五维度评分** (权重可配):

| 维度 | 默认权重 | 评估内容 |
|------|---------|---------|
| evidence_coverage | 25% | 结论引用了多少链中节点 |
| reasoning_quality | 20% | finding→reasoning 逻辑链条是否完整 |
| specificity | 20% | 可操作项是否有具体股票代码和时间 |
| signal_novelty | 20% | 隐蔽信号是否标记为 not_priced_in |
| self_consistency | 15% | 论点、置信度、发现是否方向一致 |

**幻觉检测**: 验证 evidence_ids 是否存在、操作目标是否在源数据实体中。每个幻觉标记扣 0.1 分 (上限 0.3，均可配)。

**批量评估**: 对多条洞察取加权平均，通过条件为 `平均分 ≥ 阈值 AND 通过率 ≥ 50%`。

**策略选择**: 基于最低维度分数驱动 (而非关键词匹配):

```
最低维度 = evidence_coverage → expand_context
最低维度 = signal_novelty    → add_chains
最低维度 = reasoning/specificity → critique_revise
存在幻觉标记                 → critique_revise
```

### 2.7 agent.py — 闭环 Agent

Plan → Execute → Evaluate → Refine 循环的核心驱动。

**Plan 阶段**:
- 有 entity/keywords 且 `search_mode=hybrid` 时，使用**混合检索** (向量+关键词) 扫描相关新闻
- 无明确主题时，使用时间范围扫描
- 统计活跃实体/板块，根据输入参数决定构建哪些链 (指定 entity → timeline, 指定 keywords → sector_propagation, 自动模式选热门实体)

**三种修正策略**:

| 策略 | 触发条件 | 动作 |
|------|---------|------|
| expand_context | evidence_coverage 最低 | 时间窗口 × 1.5 (系数可配)，同步更新所有链 |
| add_chains | signal_novelty 最低 | 补充 anomaly + entity_cross 链 |
| critique_revise | reasoning/specificity 最低 或 幻觉 | 批评意见注入 LLM prompt 重写 |

### 2.8 harness.py — 调度器

| 职责 | 说明 |
|------|------|
| 初始化 | 创建 RunContext, 注入参数 |
| 熔断保护 | 连续失败 N 次 (可配) 后暂停，防止无限重试 |
| 持久化 | 运行结束后原子写入 JSON，支持 resume |
| 指标记录 | 运行次数、成功率、平均质量分、LLM 调用量、平均耗时 |
| 报告 | 成功时生成 Markdown + JSON, 失败时仅 JSON (含诊断信息) |

### 2.9 report.py — 报告生成

- **Markdown**: 人类阅读，含分析概要表 + 逐条洞察详情 + 评估维度分数 + 错误记录
- **JSON**: 程序化消费，包含完整 RunContext 信息

### 2.10 logging_config.py — 日志配置

| 输出 | 级别 | 格式 | 保留 |
|------|------|------|------|
| 控制台 | INFO (可配) | 彩色，含时间/级别/位置/消息 | — |
| analyst_YYYY-MM-DD.log | DEBUG | 纯文本，含时间/级别/位置 | 7 天 (可配) |
| analyst_error_YYYY-MM-DD.log | WARNING+ | 纯文本 | 14 天 (可配) |

---

## 三、使用说明

### 3.1 安装

```bash
cd analyst
pip install -e ".[openai]"      # OpenAI / DeepSeek 等
# 或
pip install -e ".[anthropic]"   # Anthropic Claude
# 或
pip install -e .                # 仅 Ollama 本地

# 测试
pip install -e ".[test]"
pytest tests/ -v
```

### 3.2 配置

编辑 `analyst.yaml`:

```yaml
# 必须配置的项:
llm_provider: deepseek
llm_model: deepseek-v4
llm_base_url: "https://api.deepseek.com"
llm_api_key: ""               # 或设置环境变量 LLM_API_KEY

# 数据目录 (与 indexagent 共享):
data_dir: ~/github_tradingpro/trading/data

# 检索模式:
search_mode: hybrid            # hybrid = 向量+关键词混合
hybrid_alpha: 0.7              # 向量权重 (0=纯关键词, 1=纯向量)
```

也可以通过环境变量:

```bash
export LLM_API_KEY="sk-xxx"
export LLM_BASE_URL="https://api.deepseek.com"
export TRADING_DATA_DIR="~/github_tradingpro/trading/data"
```

### 3.3 前置条件

analyst 通过 indexagent SDK 检索数据，依赖 indexagent 已构建的索引:

```
data/                            ← indexagent 和 analyst 共享
├── news.db                      ← SQLite 结构化数据 (33 万+ 条)
├── vectors/                     ← FAISS 向量索引
│   ├── news.index               ← 语义搜索索引 (33 万+ 向量)
│   └── id_map.json              ← FAISS ID ↔ news_id 映射
├── reports/                     ← analyst 输出的报告
├── logs/analyst/                ← 日志文件
└── state/                       ← 运行状态 (用于 resume)
```

确保 indexagent 已完成数据导入和索引构建:

```bash
cd ../indexagent
python main.py run    # 导入 + 索引一步完成
```

### 3.4 运行

---

## 四、指令集合

### 4.1 闭环 Agent 分析 (主入口)

```bash
# 分析指定公司
python main.py run --entity "比亚迪" --days 90

# 分析指定主题
python main.py run --keywords "芯片,制裁" --days 60

# 自动模式 — 自动选择热门实体分析
python main.py run --auto --days 30

# 控制迭代次数
python main.py run --entity "宁德时代" --days 90 --max-iter 5

# 不生成报告文件 (仅控制台输出)
python main.py run --auto --days 30 --no-report
```

**选项:**

| 选项 | 缩写 | 默认值 | 说明 |
|------|------|--------|------|
| `--entity` | `-e` | — | 聚焦实体 (公司名) |
| `--keywords` | `-k` | — | 关键词，逗号分隔 |
| `--auto` | — | — | 自动选择热门实体 |
| `--days` | — | 90 | 回溯天数 |
| `--max-iter` | — | 3 | 最大 Refine 迭代次数 |
| `--no-report` | — | — | 不生成报告文件 |

### 4.2 线索链构建 (不需要 LLM)

```bash
# 时间线链 — 某实体的事件演变
python main.py chain timeline --entity "比亚迪" --days 90

# 指定实体类型
python main.py chain timeline --entity "新能源" --type sector --days 60

# 板块传导链
python main.py chain sector --keywords "新能源,补贴" --days 90

# 异常链 — 短期消息爆发
python main.py chain anomaly --days 30

# 实体交叉链 — 隐蔽关联
python main.py chain cross --days 60
```

**chain timeline 选项:**

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `--entity` | (必填) | 实体名称 |
| `--type` | company | 实体类型: company / sector / person |
| `--days` | 90 | 回溯天数 |

**chain sector 选项:**

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `--keywords` | (必填) | 关键词，逗号分隔 |
| `--days` | 90 | 回溯天数 |

**chain anomaly / cross 选项:**

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `--days` | 30 / 60 | 回溯天数 |

### 4.3 生命周期管理

```bash
# 查看 Harness 状态 (指标 + 熔断器 + 最近运行)
python main.py status

# 列出所有运行记录
python main.py runs

# 从中断处恢复运行
python main.py resume <run_id>
```

**status 输出示例:**

```
=== Harness 状态 ===
总运行: 5  成功: 4  失败: 1
成功率: 80.0%  平均质量: 0.782
LLM 调用: 12  总迭代: 8
熔断器: CLOSED (正常)  (连续失败: 0/3)

最近运行:
  abc123def456  state=complete  score=0.875  iter=1  updated=2024-01-15 10:30:00
```

---

## 五、配置参数参考

### analyst.yaml 完整参数

```yaml
# ── data: 数据路径 ──
data_dir: ~/github_tradingpro/trading/data
db_path: ""                 # 留空自动使用 data_dir/news.db
index_dir: ""               # 留空自动使用 data_dir/vectors
report_dir: ""              # 留空自动使用 data_dir/reports

# ── llm: 大模型连接 ──
llm_provider: deepseek       # openai / deepseek / anthropic / ollama
llm_model: deepseek-v4
llm_base_url: "https://api.deepseek.com"
llm_api_key: ""              # 或环境变量 LLM_API_KEY
llm_max_tokens: 4096
llm_temperature: 0.3
llm_timeout: 120.0           # 请求超时 (秒)
llm_connect_timeout: 30.0    # 连接超时 (秒)

# ── search: 检索模式 ──
search_mode: hybrid           # hybrid(向量+关键词) / keyword / entity / time
hybrid_alpha: 0.7             # 向量权重 (0=纯关键词, 1=纯向量)

# ── chain: 线索链构建 ──
chain_max_depth: 5
chain_time_window_days: 90
chain_significance_filter: 0.3       # 低于此值的链被过滤
chain_timeline_strength: 0.5         # 时间链连接强度
chain_sector_strength: 0.6           # 板块传导链连接强度
chain_anomaly_strength: 0.8          # 异常链连接强度
chain_cross_strength: 0.7            # 实体交叉链连接强度
chain_anomaly_significance: 0.8      # 异常链默认重要性
chain_cross_base_significance: 0.6   # 交叉链基础重要性
chain_cross_overlap_bonus: 0.1       # 交叉链重叠加分
chain_cross_max_overlap: 4           # 交叉链重叠上限
chain_burst_density_threshold: 0.5   # 消息爆发密度阈值 (条/小时)
min_cluster_size: 3                  # 最小聚类大小
insight_max_news: 50                 # 单次 LLM 分析最大新闻数

# ── chain significance weights: 链重要性评分权重 ──
chain_weight_source_priority: 0.3    # 来源权威度
chain_weight_sentiment_polarity: 0.3 # 情绪极性
chain_weight_urgency: 0.2            # 紧急度
chain_weight_node_count: 0.2         # 节点数量

# ── eval: 自评估器 ──
quality_threshold: 0.65              # 质量通过阈值 (0~1)
eval_weight_evidence: 0.25           # 证据覆盖率权重
eval_weight_reasoning: 0.20          # 推理质量权重
eval_weight_specificity: 0.20        # 具体性权重
eval_weight_signal: 0.20             # 信号新颖性权重
eval_weight_consistency: 0.15        # 自洽性权重
eval_hallucination_penalty_per_flag: 0.1
eval_hallucination_max_penalty: 0.3
eval_coverage_multiplier: 2.0
eval_max_hallucination_flags: 5
eval_pass_rate_threshold: 0.5

# ── query: 查询限制 ──
query_limit_time_range: 200
query_limit_entity: 100
query_limit_urgent: 50
query_limit_timeline: 200
query_limit_cross: 300
query_plan_limit: 500

# ── agent: 闭环 Agent ──
max_iterations: 3
expansion_factor: 1.5

# ── harness: 调度器 ──
circuit_breaker_threshold: 3

# ── log: 日志 ──
log_dir: ""
log_level: INFO
log_retention_days: "7 days"
log_error_retention_days: "14 days"
```

---

## 六、输出报告

### Markdown 报告

每次闭环运行后自动生成，包含:

1. **分析概要表**: 所有洞察的核心论点、置信度、时间维度、未定价信号数
2. **逐条洞察详情**: 核心发现 + 推导逻辑 + 隐蔽信号 + 风险因素 + 可操作项
3. **评估详情**: 五维度分数、综合评分、通过率、幻觉标记数、修正策略
4. **错误记录**: 运行过程中的错误 (如有)

### JSON 报告

供其他 agent / 程序消费:

```json
{
  "generated_at": "2024-01-15T10:30:00",
  "run_id": "abc123def456",
  "state": "complete",
  "iteration": 2,
  "quality_score": 0.875,
  "evaluation": { ... },
  "total_llm_calls": 2,
  "total_latency_ms": 3500,
  "refinement_strategy": "critique_revise",
  "insights": [ ... ],
  "errors": []
}
```

### 运行状态

每次运行的状态保存到 `data/state/{run_id}.json`，包含完整上下文，支持 `resume`。

---

## 七、项目结构

```
analyst/
├── README.md                    # 本文档
├── analyst.yaml                 # 配置文件 (所有可调参数)
├── main.py                      # CLI 入口
├── pyproject.toml               # 项目依赖
│
├── analyst/                     # 核心模块
│   ├── __init__.py              # 公开 API 导出
│   ├── config.py                # 统一配置管理
│   ├── state.py                 # 状态机 + 原子持久化
│   ├── query.py                 # 数据检索层 (通过 indexagent SDK)
│   ├── chain_builder.py         # 四种线索链构建器
│   ├── insight_engine.py        # LLM 洞察引擎 (多提供商)
│   ├── evaluator.py             # 五维评估器 + 幻觉检测
│   ├── agent.py                 # ReAct 闭环 Agent
│   ├── harness.py               # 调度器 (熔断 + 指标 + 报告)
│   ├── report.py                # Markdown + JSON 报告生成
│   └── logging_config.py        # 日志配置
│
└── tests/                       # 97 项测试
    ├── test_state.py             # 状态机 + 持久化
    ├── test_evaluator.py         # 评估器 + 幻觉检测
    ├── test_chain_builder.py     # 四种链构建
    ├── test_insight_engine.py    # LLM 客户端 + 洞察引擎
    ├── test_agent_loop.py        # Agent 闭环循环
    ├── test_harness.py           # Harness + 熔断 + Resume
    ├── test_config_report.py     # 配置 + 报告
    └── test_self_eval_closed_loop.py  # 自评估闭环专项
```

---

## 八、与上下游工程的接口

```
collectagent (采集新闻)
    ↓ 输出 JSONL 文件
indexagent (构建索引)
    ↓ 输出 SQLite + FAISS 向量
    ↓ 暴露 Python SDK (IndexAgent)
analyst (深度分析)  ← 本工程
    ↑ 通过 indexagent SDK 检索数据
    ↓ 输出 reports/ (Markdown + JSON)
```

analyst **通过 indexagent SDK 检索数据**，不直接操作底层存储:

| 调用方式 | 底层引擎 | 用途 |
|---------|---------|------|
| `IndexAgent.search()` | FAISS + FTS5 + SQL | 混合/向量/关键词检索 |
| `IndexAgent.search_by_time()` | SQLite | 结构化时间查询 |
| `IndexAgent.search_by_entity()` | SQLite | 实体匹配查询 |
| `IndexAgent.get_by_ids()` | SQLite | ID 精确查询 |
| `IndexAgent.get_urgent()` | SQLite | 紧急新闻查询 |
| `IndexAgent.get_timeline()` | SQLite | 关键词时间线 |

两个工程通过共享 `data_dir` 下的数据文件解耦，可独立部署、独立运行。indexagent 的 FAISS 索引和 Embedding 模型由 SDK 懒加载，仅在首次搜索时初始化。

---

## 九、实现原理详解

### 9.1 核心设计理念：ReAct 闭环推理

analyst 的核心设计借鉴了 **ReAct (Reasoning + Acting)** 范式，但做了面向财经分析的特化：

```
传统 ReAct:  Thought → Action → Observation → Thought → ...
analyst:     Plan → Execute → Evaluate → Reflect → Refine → Execute → ...
```

**关键区别：**

1. **有状态的迭代**：传统 ReAct 每轮独立，analyst 的 `RunContext` 在迭代间积累状态（链、洞察、评估分数、批评意见），每次迭代都在前一轮基础上改进
2. **面向质量而非任务**：传统 ReAct 直到任务完成，analyst 直到**质量达标**——即使用 LLM 生成了结果，如果自评估不通过，仍会修正后重试
3. **策略化修正**：不是简单的"再做一遍"，而是根据**最弱维度**选择不同的修正策略（扩大数据、补充链、批评重写），每次修正都有针对性

### 9.2 线索链构建算法

#### 9.2.1 ChainNode / ChainLink / ClueChain 数据结构

```
ClueChain (一条线索链)
├── chain_id: 唯一标识 (如 "timeline_比亚迪_20260508")
├── chain_type: 类型 (timeline / sector_propagation / anomaly / entity_cross)
├── theme: 主题描述 (如 "比亚迪 事件时间线 (90天)")
├── significance: 重要性评分 (0.0~1.0)
├── nodes: List[ChainNode]  ← 新闻节点列表
│   └── ChainNode
│       ├── news_id: 新闻 ID
│       ├── title: 标题
│       ├── publish_time: 发布时间
│       ├── source / source_priority: 来源及其权威度
│       ├── sentiment: 情绪标签 (positive / negative / neutral)
│       ├── urgency: 紧急度 (normal / urgent / important)
│       ├── ts_codes: 涉及的 A 股代码
│       ├── mentioned_companies: 提及的公司名
│       ├── mentioned_persons: 提及的人物
│       └── related_sectors: 关联板块
├── links: List[ChainLink]  ← 节点间关联
│   └── ChainLink
│       ├── from_id / to_id: 关联的两个节点
│       ├── link_type: 关联类型 (temporal / entity / sector / anomaly)
│       ├── strength: 连接强度 (0.0~1.0)
│       └── reason: 关联原因描述
└── hidden_signals: List[str]  ← 算法检测到的隐蔽信号
```

#### 9.2.2 Timeline 链构建算法

```
输入: entity="比亚迪", days=90

Step 1: 双路数据获取
  ├── search_hybrid(query="比亚迪", top_k=100, alpha=0.7)
  │   └── FAISS 语义 + FTS5 关键词 → 结果集 A (语义相关新闻)
  ├── get_timeline(keywords=["比亚迪"], days=90)
  │   └── SQLite LIKE 匹配 → 结果集 B (标题精确包含的新闻)
  └── _merge_dedup(A, B)  → 按 id 去重合并

Step 2: 时间排序 + 链接
  nodes.sort(by publish_time)
  for i in 0..n-2:
      links.append(nodes[i] → nodes[i+1], type="temporal", strength=0.5)

Step 3: 情绪转变检测 (_detect_sentiment_shifts)
  遍历相邻节点的 sentiment 变化:
    neutral → positive/negative  → "从沉默到表态，值得关注"
    positive → negative          → "利好转利空，重大反转信号"

Step 4: 计算重要性 (_calc_significance)
  score = source_priority权重×(最高优先级/5)
        + sentiment_polarity权重×(极性新闻占比)
        + urgency权重×(紧急新闻占比)
        + node_count权重×(节点数/10)
  → clamp to [0, 1]

输出: [ClueChain(theme="比亚迪 事件时间线 (90天)", nodes=..., links=...)]
```

#### 9.2.3 Sector Propagation 链构建算法

```
输入: policy_keywords=["新能源", "补贴"], days=90

Step 1: 双路数据获取 (同 timeline)

Step 2: 按板块分组
  for node in nodes:
      for sector in node.related_sectors:
          sector_groups[sector].append(node)
  没有板块标签的 → "未分类" 组

Step 3: 按时间排序板块
  sector_timeline = sorted(sector_groups, by 各板块最早新闻时间)

Step 4: 构建板块间传导链接
  for i in 0..n-2:
      sector_A → sector_B, type="sector", strength=0.6
      (链接: sector_A 最晚新闻 → sector_B 最早新闻)

Step 5: 传导信号检测 (_detect_propagation_signals)
  板块A(t1) → 板块B(t2): "传导路径: A(t1) → B(t2), B可能存在滞后反应机会"
```

#### 9.2.4 Anomaly 链构建算法

```
输入: days=30

Step 1: 获取高优先级新闻
  get_urgent(days=30) → 按 urgency 和 source_priority 过滤

Step 2: 实体爆发检测 (_detect_entity_bursts)
  for node in nodes:
      优先用 mentioned_companies / related_sectors
      为空时从标题提取关键词 (_extract_title_keywords)

  for entity, enodes in entity_map:
      if len(enodes) < min_cluster_size (默认 3): 跳过
      计算消息密度 = 消息数 / 时间跨度(小时)
      if 密度 >= burst_density_threshold (默认 0.5 条/小时): 标记为爆发

Step 3: 为每个爆发实体构建链
  significance = 固定 0.8 (高优先级)
  hidden_signals = ["X 在N天内出现M条消息，密度异常", "可能存在未被市场充分反映的信息"]
```

#### 9.2.5 Entity Cross 链构建算法

```
输入: days=60

Step 1: 获取时间范围内所有新闻 (get_by_time_range, limit=300)

Step 2: 构建实体→节点映射
  for node in nodes:
      优先用 mentioned_companies / related_sectors
      为空时从标题提取关键词

Step 3: 寻找实体交叉
  for entity, enodes in entity_map:
      if len(enodes) < 2: 跳过

      for node in enodes:
          统计 node 中出现的其他实体 → related_entities 计数

      for rel_entity, overlap in related_entities (按 overlap 降序):
          if overlap < 2: 跳过
          if pair 已处理: 跳过

          找到 common_ids = enodes ∩ rel_nodes
          if 无共同节点: 跳过

          significance = 0.6 + 0.1 × min(overlap, 4)
          构建 ClueChain(theme=f"实体交叉: {entity} × {rel_entity}")

Step 4: 按 significance 降序，取 top 10
```

#### 9.2.6 关键词提取与停用词过滤

`_extract_title_keywords()` 是实体交叉链和异常链的关键辅助函数：

```
输入: "比亚迪发布新款电动车，销量预计翻倍"

Step 1: 分词
  优先 jieba 分词 (安装时)
  退化: 按标点符号拆分

Step 2: 过滤
  排除: 长度 < 2 的词
  排除: _STOP_WORDS 中的虚词/泛词 (约 100+ 个)
  排除: 纯数字/百分比/金额格式

输出: ["比亚迪", "新款", "电动车", "销量", "预计", "翻倍"]
```

停用词表包含两类：
- 通用虚词/副词/代词（的、了、在、是...）
- 财经新闻常见泛词（公司、集团、公告、表示、目前...）

### 9.3 LLM 洞察引擎工作原理

#### 9.3.1 Prompt 工程

LLM 分析由两个 Prompt 组成：

**System Prompt** (固定，约 75 行):
- 角色定义：A 股资深投资分析师
- 6 条核心原则：投资导向、具体标的、因果链条、交叉验证、时间序列、市场定价
- 严格禁止项：泛泛之词、强行关联、无因果链的推论
- 输出格式：严格的 JSON schema 定义

**User Prompt** (动态，由 CHAIN_ANALYSIS_PROMPT 模板生成):
```
## 线索链信息
- 类型: timeline / sector_propagation / anomaly / entity_cross
- 主题: 链的主题描述
- 时间跨度 / 重要性评分 / 已发现的隐蔽信号

## 可用股票代码（本链新闻中出现的 ts_codes）
→ 限制 LLM 只能推荐这些代码

## 线索链中的新闻（按时间顺序，最多 50 条）
→ 格式化后的新闻列表

## [可选] 上一轮评估的批评意见
→ 仅在 critique_revise 策略时注入
```

#### 9.3.2 LLM 客户端架构

```
LLMClient
├── 复用 httpx.AsyncClient 连接池 (避免每次请求创建新连接)
├── 三种 API 格式:
│   ├── _call_openai_compat()  → openai/deepseek/siliconflow/moonshot/qwen/glm
│   ├── _call_anthropic()      → Anthropic Claude
│   └── _call_ollama()         → Ollama 本地
├── base_url 自动解析 (_resolve_base_url)
│   └── 根据 provider 推断默认 URL
└── URL 路径拼接
    ├── 已含 /v1 /v3 /v4 → 直接拼接 /chat/completions
    └── 否则 → 添加 /v1 前缀
```

#### 9.3.3 LLM 响应解析

`_parse_llm_response()` 有三层容错：

```
Layer 1: 去掉 markdown 代码块 (```json ... ```)
Layer 2: 直接 json.loads()
Layer 3: 找到第一个 { 和最后一个 }，json.loads(子串)
Layer 4: 全部失败 → 返回 {"thesis": "LLM 返回格式异常", "confidence": 0.0}
```

#### 9.3.4 洞察去重

`_deduplicate_insights()` 使用 Jaccard 相似度去重：

```
for each insight:
    提取论点中的中文关键词 (正则: [一-鿿]{2,})
    计算与已保留洞察的 Jaccard 相似度
    if Jaccard > 0.5:
        → 重复，保留 confidence 更高的
    else:
        → 保留
```

### 9.4 自评估器评分算法

#### 9.4.1 五维度评分详解

**1. evidence_coverage (证据覆盖率) — 权重 25%**

```
输入: insight.key_findings[].evidence_ids, chain_nodes[].id

node_ids = {所有链节点的 id}
cited_ids = {所有 finding 引用的 evidence_ids}

if 无引用: return 0.1
coverage = len(cited_ids ∩ node_ids) / max(len(node_ids), 1)
return min(coverage × coverage_multiplier(2.0), 1.0)

含义: LLM 的结论有多少确实引用了链中的新闻
```

**2. reasoning_quality (推理质量) — 权重 20%**

```
输入: insight.key_findings[]

has_reasoning = 有 reasoning 字段的 finding 数量
has_finding = 有 finding 字段的 finding 数量

return 0.5 × (has_finding / total) + 0.5 × (has_reasoning / total)

含义: 每条发现是否都有描述和推导逻辑
```

**3. specificity (具体性) — 权重 20%**

```
输入: insight.actionable_items[]

for each item:
    +0.2  if action 非空
    +0.5  if targets 含标准股票代码 (如 000333.SZ, 正则 \d{6}\.[A-Z]{2})
    +0.1  if targets 有内容但不是标准代码
    +0.1  if urgency 有效
    +0.1  if reason 非空

return min(total / items_count, 1.0)

含义: 可操作项是否包含具体的股票代码、操作方向和推荐理由
```

**4. signal_novelty (信号新颖性) — 权重 20%**

```
输入: insight.hidden_signals[]

not_priced = 标记为 not_priced_in 的信号数
has_implication = 有 implication 描述的信号数

return 0.5 × (not_priced / total) + 0.5 × (has_implication / total)

含义: 隐蔽信号是否标注了"尚未被市场定价"并说明了潜在影响
```

**5. self_consistency (自洽性) — 权重 15%**

```
输入: thesis, confidence, key_findings, risk_factors

base = 0.5
+0.15  if thesis 非空
+0.10  if 0 < confidence <= 1
+0.15  if 同时有 findings 和 risks
+0.10  if confidence > 0.7 且 findings >= 2

return min(score, 1.0)

含义: 论点、置信度、发现、风险是否方向一致
```

#### 9.4.2 幻觉检测

```
检测两类幻觉:

1. 引用不存在的证据
   for evidence_id in finding.evidence_ids:
       if id 不在 chain_node_ids 中:
           flag("引用了不存在的证据ID: {id}")

2. 操作目标不在源数据中
   for target in actionable_item.targets:
       if 是标准股票代码格式: 跳过 (股票代码不受限)
       if 不在 mentioned_companies 且不在 related_sectors:
           flag("操作目标 '{target}' 不在源数据实体中")

每个幻觉标记扣 0.1 分，上限 0.3 分
```

#### 9.4.3 批量评估与通过条件

```
evaluate_batch():
  for each insight:
      evaluate(insight, chain_nodes) → EvaluationResult

  avg_score = 所有洞察的 overall_score 加权平均
  pass_rate = 通过的洞察数 / 总洞察数

  通过条件: avg_score >= quality_threshold(0.65)
         AND pass_rate >= eval_pass_rate_threshold(0.5)

  聚合批评:
      if 不通过:
          拼接: 平均分不足 / 通过率不足 / 幻觉标记 / 逐条批评
```

#### 9.4.4 修正策略选择算法

```
_select_refinement_strategy():

  1. 收集所有未通过洞察的各维度分数
  2. 取每个维度的最低分 (在所有未通过洞察中)
  3. 检查是否有幻觉标记 → 有则直接返回 "critique_revise"
  4. 找最低维度:

     evidence_coverage 最低 → "expand_context"
       │ 扩大时间窗口 × expansion_factor(1.5)
       │ 同步更新 plan 中所有链的 days
       │
     signal_novelty 最低 → "add_chains"
       │ 补充 anomaly + entity_cross 链 (如果尚无)
       │
     reasoning/specificity 最低 → "critique_revise"
       │ 不改参数，将批评意见注入下一轮 LLM prompt
       │ InsightEngine.set_critique(ctx.critique)

  兜底 → "expand_context"
```

### 9.5 状态机与持久化

#### 9.5.1 状态转换图

```
IDLE ──transition()──→ PLANNING ──transition()──→ EXECUTING ──transition()──→ EVALUATING
  │                      │                         │                        │
  │                      └──(失败)──→ FAILED       └──(失败)──→ FAILED     │
  │                                                                         │
  │                                              ←──transition()── REFINE ←─┤
  │                                                  ↑          │            │
  │                                                  │          ↓            │
  │                                              EXECUTING ←───┘            │
  │                                                                           │
  │                                                       ┌──(通过)──→ COMPLETE
  │                                                       └──(重试用尽)──→ FAILED
```

合法转换表 (TRANSITIONS 字典):

| 当前状态 | 允许的目标状态 |
|---------|-------------|
| IDLE | PLANNING |
| PLANNING | EXECUTING, FAILED |
| EXECUTING | EVALUATING, FAILED |
| EVALUATING | COMPLETE, REFINE, FAILED |
| REFINE | EXECUTING, FAILED |
| COMPLETE | (终态，不可转换) |
| FAILED | (终态，不可转换) |

#### 9.5.2 RunContext 数据结构

```python
RunContext:
  # 标识
  run_id: str               # UUID 前 12 位
  state: AgentState         # 当前状态
  created_at / updated_at   # 时间戳

  # 输入参数 (Harness 注入)
  focus_entity: str         # 聚焦实体 (如 "比亚迪")
  focus_keywords: List[str] # 关键词列表 (如 ["芯片", "制裁"])
  time_window_days: int     # 时间窗口 (初始 90, expand_context 时 ×1.5)

  # 执行过程中积累
  analysis_plan: Dict       # Plan 阶段生成的分析计划
  chains: List[Dict]        # 构建的线索链 (序列化为 dict)
  insights: List[Dict]      # LLM 分析结果
  evaluation: Dict          # 评估结果
  critique: str             # 评估器生成的批评意见
  refinement_strategy: str  # 当前修正策略

  # 迭代控制
  iteration: int            # 当前迭代次数 (从 0 开始)
  max_iterations: int       # 最大迭代次数 (默认 3)

  # 指标
  total_llm_calls: int      # LLM 调用次数
  total_latency_ms: float   # 总耗时

  # 错误
  errors: List[str]         # 运行过程中记录的错误
```

#### 9.5.3 原子持久化

```python
StateStore.save(ctx):
  1. json.dumps(ctx.to_dict(), indent=2)
  2. 写入临时文件 {run_id}.json.tmp
  3. tmp.replace(target)  ← 原子操作 (OS rename)
  4. 如果写失败: 删除临时文件

StateStore.load(run_id):
  1. 读取 {run_id}.json
  2. json.loads() → RunContext.from_dict()
  3. 返回完整 RunContext (含所有历史状态)
```

### 9.6 Harness 调度器工作原理

```
Harness.run_analysis(entity, keywords, days, ...):

  1. 熔断检查
     if consecutive_failures >= threshold(3):
         → 直接返回 FAILED (不再启动 Agent)

  2. 初始化 RunContext
     ctx = RunContext()
     ctx.focus_entity = entity
     ctx.focus_keywords = keywords
     ctx.time_window_days = days
     ctx.max_iterations = max_iterations

  3. 启动 Agent
     agent = AnalysisAgent(config)
     ctx = await agent.run(ctx)  ← 内部完成全部闭环

  4. 持久化
     state_store.save(ctx)

  5. 生成报告
     if output_report:
         generate_json_report()  → 始终生成
         if COMPLETE and insights:
             generate_report()   → 生成 Markdown

  6. 更新指标
     metrics.record_run(ctx)
     if COMPLETE: consecutive_failures = 0
     else: consecutive_failures += 1
```

---

## 十、数据流向详解

### 10.1 端到端数据流

```
用户输入                      Plan                        Execute                     Evaluate                   Output
───┬───                    ───┬───                    ───┬───                    ───┬───                   ───┬───
    │                         │                           │                           │                        │
    │  entity="比亚迪"       │                           │                           │                        │
    │  days=90                │                           │                           │                        │
    ├──────────────────────→ RunContext                   │                           │                        │
    │                         │                           │                           │                        │
    │                         │ search_hybrid("比亚迪")  │                           │                        │
    │                         │  → FAISS + FTS5           │                           │                        │
    │                         │  → 500 条新闻             │                           │                        │
    │                         │                           │                           │                        │
    │                         │ 统计活跃实体/板块         │                           │                        │
    │                         │ → analysis_plan:          │                           │                        │
    │                         │   chains: [               │                           │                        │
    │                         │     {type: timeline,      │                           │                        │
    │                         │      entity: "比亚迪"},   │                           │                        │                        │
    │                         │     {type: anomaly},      │                           │                        │
    │                         │     {type: entity_cross}  │                           │                        │
    │                         │   ]                       │                           │                        │
    │                         │                           │                           │                        │
    │                         │                      build_chains()                  │                        │
    │                         │                      ┌─────────────┐                 │                        │
    │                         │                      │ timeline链: │                 │                        │
    │                         │                      │  混合检索+  │                 │                        │
    │                         │                      │  SQLite匹配 │                 │                        │
    │                         │                      │  → 45条新闻 │                 │                        │
    │                         │                      │  → 44条链接 │                 │                        │
    │                         │                      │  → 2个信号  │                 │                        │
    │                         │                      ├─────────────┤                 │                        │
    │                         │                      │ anomaly链:  │                 │                        │
    │                         │                      │  get_urgent │                 │                        │
    │                         │                      │  → 3个爆发  │                 │                        │
    │                         │                      ├─────────────┤                 │                        │
    │                         │                      │ cross链:    │                 │                        │
    │                         │                      │  时间范围查询│                 │                        │
    │                         │                      │  → 5个交叉  │                 │                        │
    │                         │                      └──────┬──────┘                 │                        │
    │                         │                             │                        │                        │
    │                         │                      过滤 significance < 0.3        │                        │
    │                         │                             │                        │                        │
    │                         │                    LLM 分析每条链                    │                        │
    │                         │                    ┌──────────────────┐              │                        │
    │                         │                    │ System Prompt +   │              │                        │
    │                         │                    │ User Prompt (     │              │                        │
    │                         │                    │   链信息+新闻列表 │              │                        │
    │                         │                    │   +可用股票代码   │              │                        │
    │                         │                    │ )                  │              │                        │
    │                         │                    │ → LLM API call    │              │                        │
    │                         │                    │ → JSON 解析       │              │                        │
    │                         │                    │ → insight dict    │              │                        │
    │                         │                    └────────┬──────────┘              │                        │
    │                         │                             │                        │                        │
    │                         │                      去重 (Jaccard > 0.5)             │                        │
    │                         │                      → 3 条洞察                       │                        │
    │                         │                             │                        │                        │
    │                         │                             │                  evaluate_batch()               │
    │                         │                             │                  ┌────────────────┐             │
    │                         │                             │                  │ 每条洞察:       │             │
    │                         │                             │                  │ evidence: 0.6   │             │
    │                         │                             │                  │ reasoning: 0.8  │             │
    │                         │                             │                  │ specificity: 0.7│             │
    │                         │                             │                  │ signal: 0.5     │             │
    │                         │                             │                  │ consistency: 0.9│             │
    │                         │                             │                  │ → overall: 0.70 │             │
    │                         │                             │                  │ → passed: true  │             │
    │                         │                             │                  └───────┬────────┘             │
    │                         │                             │                          │                      │
    │                         │                             │                     avg_score ≥ 0.65?           │
    │                         │                             │                     pass_rate ≥ 0.5?            │
    │                         │                             │                          │                      │
    │                         │                             │                    ┌──YES──┘──NO──┐             │
    │                         │                             │                    │             │             │
    │                         │                             │              COMPLETE      选择修正策略       │
    │                         │                             │                  │         ┌──┴──┐          │
    │                         │                             │                  │    expand  add  critique   │
    │                         │                             │                  │    context chains revise    │
    │                         │                             │                  │         └──┬──┘          │
    │                         │                             │                  │            │             │
    │                         │                             │                  │      _apply_refine()       │
    │                         │                             │                  │            │             │
    │                         │                             │                  │      回到 Execute ─────────┘
    │                         │                             │                  │
    │                         │                             │                  │
    │                         │                             │                  ├──→ generate_json_report()
    │                         │                             │                  └──→ generate_report(MD)
    │                         │                             │                       │
    ↓                         ↓                             ↓                       ↓
  CLI 输出 ←─────── RunContext (含报告路径) ──────────────────┘
```

### 10.2 各阶段的数据结构变化

| 阶段 | 输入数据结构 | 输出数据结构 | 关键转换 |
|------|-----------|-----------|---------|
| CLI 输入 | `--entity "比亚迪" --days 90` | `RunContext(focus_entity="比亚迪", time_window_days=90)` | CLI 参数注入 |
| Plan 混合检索 | 查询字符串 | `List[Dict]` (新闻列表, 每条约 29 字段) | FAISS 向量 + FTS5 关键词 + SQLite 结构化 |
| Plan 分析 | 新闻列表 | `analysis_plan = {"chains": [{type, entity, days}, ...]}` | 统计实体/板块频次，决定构建哪些链 |
| Chain 构建 | chain_spec + 新闻数据 | `List[ClueChain]` (含 nodes, links, significance, hidden_signals) | 双路检索→合并去重→排序→链接→评分 |
| LLM 分析 | `ClueChain` | `Dict` (thesis, confidence, findings, signals, actions) | System + User Prompt → LLM API → JSON 解析 |
| 评估 | insight + chain_nodes | `EvaluationResult` (5 维分数 + overall + critique) | 规则评分 + 幻觉检测 + 批量聚合 |
| 修正 | evaluation + critique | 修改 RunContext (time_window / plan / critique) | 根据最弱维度选择策略 |
| 报告生成 | `RunContext` | Markdown 文件 + JSON 文件 | Jinja2 模板渲染 |

### 10.3 关键数据字段映射

新闻数据从 indexagent 到 analyst 报告的字段流转：

```
indexagent SQLite 字段          analyst ChainNode 字段       报告展示
─────────────────────         ──────────────────         ──────────
id                         →  news_id                  →  证据引用 / 消息来源
title                      →  title                    →  标题展示
publish_time               →  publish_time             →  时间排序 / 时间跨度
source                     →  source                   →  来源展示
source_priority            →  source_priority          →  重要性评分因子
category                   →  category                 →  分类过滤
sentiment                  →  sentiment                →  情绪转变检测
urgency                    →  urgency                  →  异常链 / 重要性评分
ts_codes                   →  ts_codes                 →  LLM "可用股票代码"
mentioned_companies        →  mentioned_companies       →  实体交叉 / 幻觉检测
mentioned_persons          →  mentioned_persons         →  实体识别
related_sectors            →  related_sectors           →  板块传导 / 实体交叉
impact_scope               →  impact_scope             →  影响范围评估
```

---

## 十一、分析过程链路详解

### 11.1 典型分析场景：以 "比亚迪" 为例的完整链路

```
命令: python main.py run --entity "比亚迪" --days 90
```

#### Phase 0: 初始化

```
main.py run()
  → load_config("analyst.yaml")  → AnalystConfig
  → setup_logging()
  → Harness(config)
  → harness.run_analysis(entity="比亚迪", days=90)
    → RunContext(focus_entity="比亚迪", time_window_days=90)
    → 熔断检查: consecutive_failures(0) < threshold(3) → 通过
    → AnalysisAgent(config)
    → agent.run(ctx)
```

#### Phase 1: Plan — 数据扫描与策略生成

```
agent._plan(ctx):
  │
  ├── 1. 选择搜索策略
  │   search_query = "比亚迪" (非空)
  │   search_mode = "hybrid"
  │   → search_hybrid(query="比亚迪", top_k=500, days=90, alpha=0.7)
  │     │
  │     └── indexagent SDK
  │         ├── FAISS 向量搜索: "比亚迪" → embedding → 余弦相似度排序 → top 1000
  │         ├── FTS5 全文搜索: "比亚迪" → rank 排序 → top 1000
  │         ├── 分数归一化到 [0,1]
  │         ├── 加权融合: 0.7×向量分 + 0.3×关键词分
  │         ├── 结构化过滤: 90 天内
  │         └── 取 top 500 → recent (实际返回约 200 条)
  │
  ├── 2. 统计活跃实体和板块
  │   遍历 recent 中每条新闻:
  │     mentioned_companies → entity_counts
  │     related_sectors → sector_counts
  │   → top_entities: [("比亚迪", 85), ("宁德时代", 12), ...]
  │   → top_sectors: [("新能源汽车", 45), ("锂电池", 30), ...]
  │
  ├── 3. 决定构建哪些链
  │   有 focus_entity="比亚迪" → + timeline 链
  │   无 focus_keywords → 不建 sector_propagation 链
  │   len(recent)=200 >= 5 → + anomaly 链
  │   len(recent)=200 >= 3 → + entity_cross 链
  │
  └── analysis_plan = {
        "chains": [
          {"type": "timeline", "entity": "比亚迪", "days": 90},
          {"type": "anomaly", "days": 90},
          {"type": "entity_cross", "days": 90},
        ],
        "scan_summary": {
          "search_mode": "hybrid",
          "total_recent": 200,
          "top_entities": [...],
          "top_sectors": [...]
        }
      }
```

#### Phase 2: Execute — 构建链 + LLM 分析

```
agent._execute(ctx):

  ├── 1. 构建链 (_build_chain)
  │   │
  │   ├── Chain 1: timeline "比亚迪"
  │   │   ├── search_hybrid("比亚迪", top_k=100) → 85 条
  │   │   ├── get_timeline(["比亚迪"], days=90) → 92 条
  │   │   ├── _merge_dedup → 110 条 (去重后)
  │   │   ├── 按 publish_time 排序
  │   │   ├── 相邻节点建立 temporal 链接 (strength=0.5)
  │   │   ├── 检测情绪转变:
  │   │   │   → "情绪转变: neutral→positive (从沉默到表态，值得关注)"
  │   │   │   → "情绪转变: positive→negative (利好转利空，重大反转信号)"
  │   │   └── significance = 0.72
  │   │       (source_priority: 0.18, sentiment: 0.24, urgency: 0.10, nodes: 0.20)
  │   │
  │   ├── Chain 2: anomaly
  │   │   ├── get_urgent(days=90) → 35 条高优先级新闻
  │   │   ├── 按实体分组，计算消息密度
  │   │   ├── 发现爆发:
  │   │   │   "比亚迪": 12条/48小时 = 0.25 条/小时 < 0.5 阈值 → 不算爆发
  │   │   │   "宁德时代": 8条/6小时 = 1.33 条/小时 > 0.5 → 算爆发!
  │   │   │   "半导体": 6条/8小时 = 0.75 > 0.5 → 算爆发!
  │   │   └── 2 条 anomaly 链, significance=0.8
  │   │
  │   └── Chain 3: entity_cross
  │       ├── get_by_time_range(90天, limit=300) → 300 条
  │       ├── 构建实体→节点映射
  │       ├── 寻找交叉:
  │       │   "比亚迪" ∩ "宁德时代": overlap=5, common_ids=5 → significance=1.0
  │       │   "比亚迪" ∩ "新能源": overlap=3, common_ids=3 → significance=0.9
  │       │   "宁德时代" ∩ "锂电池": overlap=4, common_ids=4 → significance=1.0
  │       │   ...
  │       └── top 10 链 (按 significance 降序)
  │
  ├── 2. 过滤
  │   all_chains = [链 with significance >= 0.3]
  │   → 保留 12 条链 (过滤掉 3 条低重要性的)
  │
  ├── 3. LLM 分析
  │   engine = InsightEngine(config)
  │   for chain in chains:
  │       │
  │       ├── 格式化新闻列表 (_format_news_list, 最多 50 条/链)
  │       ├── 收集 ts_codes → "可用股票代码: 002594.SZ, 300750.SZ, ..."
  │       ├── 生成 User Prompt
  │       ├── llm.complete(SYSTEM_PROMPT, user_prompt)
  │       │   → POST https://open.bigmodel.cn/api/paas/v4/chat/completions
  │       │   → {"choices": [{"message": {"content": "{...JSON...}"}}]}
  │       ├── 解析 JSON (_parse_llm_response)
  │       └── 添加 chain_id, chain_type, node_count, time_span
  │
  │   → 12 条洞察 (每条链一个)
  │
  └── 4. 去重
      _deduplicate_insights()
      → 发现 2 条论点 Jaccard > 0.5, 保留 confidence 更高的
      → 最终 10 条洞察
```

#### Phase 3: Evaluate — 质量评估

```
evaluator.evaluate_batch(insights, chains_data):

  for insight in insights:
      evaluate(insight, chain_nodes):
        │
        ├── evidence_coverage:
        │   cited_ids = insight.key_findings 中引用的 evidence_ids
        │   node_ids = chain 中实际的新闻 id
        │   coverage = len(cited ∩ nodes) / len(nodes)
        │   → 0.55
        │
        ├── reasoning_quality:
        │   有 reasoning 的 finding 比例: 3/4 = 0.75
        │   有 finding 的 finding 比例: 4/4 = 1.0
        │   → 0.5 × 1.0 + 0.5 × 0.75 = 0.875
        │
        ├── specificity:
        │   2 个 actionable_items, 每个都有股票代码+reason:
        │   → 0.9
        │
        ├── signal_novelty:
        │   3 个 hidden_signals, 2 个 not_priced_in, 3 个有 implication:
        │   → 0.5 × (2/3) + 0.5 × (3/3) = 0.833
        │
        ├── self_consistency:
        │   thesis 非空 + confidence 有效 + 有 findings 和 risks + 高置信多发现:
        │   → 0.5 + 0.15 + 0.10 + 0.15 + 0.10 = 1.0
        │
        ├── hallucination:
        │   所有 evidence_ids 都在 node_ids 中 → 无幻觉
        │   所有 targets 是标准股票代码 → 无幻觉
        │   → flags: []
        │
        ├── overall = 0.55×0.25 + 0.875×0.20 + 0.9×0.20 + 0.833×0.20 + 1.0×0.15
        │           = 0.138 + 0.175 + 0.180 + 0.167 + 0.150 = 0.810
        │
        └── passed = 0.810 >= 0.65 → True

  聚合:
    avg_score = 0.72
    pass_rate = 7/10 = 0.70
    → avg_score(0.72) >= 0.65 AND pass_rate(0.70) >= 0.5
    → overall_passed = True
```

#### Phase 4: Output — 报告生成

```
假设通过 (COMPLETE):

  generate_json_report(ctx, report_dir):
    → analysis_20260508_143022.json
    {
      "run_id": "a1b2c3d4e5f6",
      "state": "complete",
      "iteration": 1,
      "quality_score": 0.72,
      "total_llm_calls": 10,
      "total_latency_ms": 45000,
      "insights": [...10 条...],
      ...
    }

  generate_report(ctx, report_dir):
    → analysis_20260508_143022.md
    (使用 Jinja2 模板渲染，包含概要表 + 逐条详情 + 评估分数)
```

#### CLI 输出

```
==================================================
运行 ID: a1b2c3d4e5f6
状态: complete
迭代: 1/3
质量分数: 0.720
LLM 调用: 10
耗时: 45000ms

洞察 (10 条):
  1. [85%] 比亚迪新能源销量持续增长，供应链机会值得关注...
  2. [78%] 宁德时代与比亚迪的电池供应关系变化...
  ...

评估: 0.720 (通过率: 70%)

报告: ~/github_tradingpro/trading/data/reports/analysis_20260508_143022.md
==================================================
```

### 11.2 修正场景：第一轮不通过的链路

```
假设第一轮评估: avg_score=0.58, pass_rate=0.3 → 未通过

Phase 4: Reflect + Refine

  _select_refinement_strategy():
    │
    ├── 收集未通过洞察 (7/10) 的各维度最低分:
    │   evidence_coverage:  min = 0.25  ← 最低!
    │   reasoning_quality:  min = 0.50
    │   specificity:        min = 0.45
    │   signal_novelty:     min = 0.55
    │
    ├── 无幻觉标记
    │
    └── 最弱维度 = evidence_coverage → "expand_context"

  _apply_refine("expand_context"):
    ctx.time_window_days = 90 × 1.5 = 135
    同步更新 plan.chains 中所有链的 days = 135
    ctx.transition(REFINE)

  ── 回到 Phase 2 (iteration=2) ──

  第二轮 Execute:
    链构建使用 135 天窗口 → 更多新闻 → 更高的 evidence_coverage
    LLM 分析 → 新的 insights
    Evaluate → avg_score=0.71, pass_rate=0.6 → 通过!
    → COMPLETE
```

### 11.3 critique_revise 场景

```
假设评估结果: reasoning_quality 最低且存在幻觉标记

Phase 4: Reflect + Refine

  _select_refinement_strategy():
    │
    ├── 检测到幻觉标记: "引用了不存在的证据ID: abc123"
    │
    └── 立即返回 "critique_revise"

  _apply_refine("critique_revise"):
    不改参数
    InsightEngine.set_critique(ctx.critique)
    # critique 内容: "证据引用不足，需更多关联到具体新闻 | 检测到幻觉: 引用了不存在的证据ID: abc123"

  ── 回到 Phase 2 (iteration=2) ──

  第二轮 Execute:
    LLM Prompt 中多了一段:
      "## 上一轮评估的批评意见
       请针对以下批评改进你的分析:
       证据引用不足，需更多关联到具体新闻 | 检测到幻觉: ..."

    → LLM 根据批评修正输出
    → 新的 insights 中 evidence_ids 更准确，幻觉减少
    → Evaluate → 通过 → COMPLETE
```

---

## 十二、依赖

```
aiosqlite>=0.20       # 异步 SQLite
pydantic>=2.0         # 数据验证
numpy>=1.24           # 数值计算
faiss-cpu>=1.7        # 向量搜索 (与 indexagent 共用)
httpx>=0.27           # 异步 HTTP (LLM 调用)
loguru>=0.7           # 日志
click>=8.0            # CLI 框架
pyyaml>=6.0           # YAML 配置
jinja2>=3.1           # 报告模板

[可选]
openai>=1.0           # OpenAI SDK
anthropic>=0.30       # Anthropic SDK
sentence-transformers # Embedding 模型 (与 indexagent 共用)
jieba>=0.42           # 中文分词 (标题关键词提取，无则退化)
```

Python >= 3.10
