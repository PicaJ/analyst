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

## 九、依赖

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
```

Python >= 3.10
