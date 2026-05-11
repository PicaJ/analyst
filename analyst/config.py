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
from typing import Any, Dict, List, Optional


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
    llm_timeout: float = 180.0
    llm_connect_timeout: float = 30.0
    llm_max_retries: int = 2
    llm_per_chain_timeout: int = 300    # 单条链 LLM 分析总耗时安全阀 (秒)

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
    chain_burst_density_threshold: float = 3.0
    min_cluster_size: int = 3
    chain_split_threshold: int = 100      # 超过此节点数时按共现实体拆分为子主题链
    max_subtopic_chains: int = 5          # 每个大链最多拆出的子链数

    # ── LLM 分析时的新闻展示策略 ──
    # insight_max_news: 传给 LLM 的最大新闻条数
    #   链节点数量不受此限制 (由 query_limit_* 控制)
    #   超出部分会用摘要替代，确保不丢失有价值消息
    insight_max_news: int = 80
    # insight_summary_threshold: 当链节点数超过此值时,
    #   前 insight_max_news 条正常展示, 剩余部分按时间窗口分组摘要
    insight_summary_threshold: int = 80

    # ── chain significance weights ──
    chain_weight_source_priority: float = 0.3
    chain_weight_sentiment_polarity: float = 0.3
    chain_weight_urgency: float = 0.2
    chain_weight_node_count: float = 0.2
    chain_max_priority_divisor: float = 5.0
    chain_node_count_normalizer: float = 10.0

    # ── eval ──
    quality_threshold: float = 0.65
    eval_weight_evidence: float = 0.15
    eval_weight_reasoning: float = 0.15
    eval_weight_specificity: float = 0.25
    eval_weight_signal: float = 0.15
    eval_weight_consistency: float = 0.10
    eval_weight_investment_relevance: float = 0.20
    eval_hallucination_penalty_per_flag: float = 0.1
    eval_hallucination_max_penalty: float = 0.3
    eval_coverage_multiplier: float = 2.0
    eval_max_hallucination_flags: int = 5
    eval_pass_rate_threshold: float = 0.5

    # ── search ──
    search_mode: str = "hybrid"      # hybrid / keyword / entity / time
    hybrid_alpha: float = 0.7         # 向量权重 (0=纯关键词, 1=纯向量)

    # ── query limits ──
    query_limit_time_range: int = 50000
    query_limit_entity: int = 500
    query_limit_urgent: int = 500
    query_limit_timeline: int = 500
    query_limit_cross: int = 5000
    query_plan_limit: int = 50000

    # ── agent ──
    max_iterations: int = 3
    expansion_factor: float = 1.5
    hot_keyword_threshold: int = 80      # 高频词最低出现次数 (出现次数≥此值才视为高频词)

    # ── chain count limits (各模式下各类链的最大数量) ──
    # auto 模式 timeline 链上限 (高频词自动实体 + tracking_keywords 实体合计)
    max_timeline_chains: int = 20
    # sector_propagation 链上限 (行业推断 + 实体扩展 + tracking_keywords 合计)
    max_sector_chains: int = 8
    # anomaly 链上限 (通常 1 条即可，包含所有爆发实体的检测)
    max_anomaly_chains: int = 1
    # entity_cross 链上限
    max_entity_cross_chains: int = 1
    # semantic_cluster 链上限
    max_semantic_chains: int = 15
    # 公司级链上限
    max_company_chains: int = 2
    # --entity 模式下扩展关联实体的最大数量
    max_entity_expand_chains: int = 5
    # --entity 模式下扩展关联板块链的最大数量
    max_sector_expand_chains: int = 3
    # --entity 模式下扩展板块链最大数量 (非 auto 模式的行业推断)
    max_auto_sector_chains: int = 3

    # ── tracking keywords: 常驻跟踪关键词 (auto 模式始终建链) ──
    # 这些关键词一旦在新闻中出现，系统会自动为其建立:
    #   - timeline 链 (追踪该关键词的事件演变)
    #   - sector_propagation 链 (如匹配到 industry_alias 则建板块链)
    #   - anomaly 链中的密度检测会自动覆盖
    # 可在 analyst.yaml 中随时增删
    tracking_keywords: List[str] = field(default_factory=lambda: [
        "AI", "机器人", "特斯拉", "商业航天", "航天",
        "半导体", "存储", "电池", "矿", "算力",
        "服务器", "黄金", "白银", "原油",
    ])

    # ── harness ──
    circuit_breaker_threshold: int = 3

    # ── filter keywords (可配置的关键词列表) ──
    political_keywords: List[str] = field(default_factory=lambda: [
        "回信", "慰问", "贺信", "致辞", "讲话", "会见", "署名文章", "访问", "抵达", "出席",
        "发表演讲", "联合声明", "友好访问", "签署谅解备忘录", "元首峰会",
        "共青团", "妇联", "工会", "人大代表", "政协委员", "精神文明", "文明创建",
        "荣誉称号", "表彰大会", "劳动模范", "先进工作者", "道德模范",
        "作出重要指示", "作出批示", "重要批示", "抓好", "排查整治", "确保人民",
        "加强基础研究", "加强公共安全", "爆炸事故", "烟花厂", "生命财产安全", "安全生产",
    ])
    no_value_keywords: List[str] = field(default_factory=lambda: [
        "回信", "慰问", "贺信", "致辞", "讲话", "会见", "署名文章",
        "共青团", "妇联", "工会", "精神文明", "荣誉称号", "表彰大会",
        "劳动模范", "先进工作者", "道德模范",
    ])
    # 链构建停用词: 出现在新闻标题/实体中的泛词，不构成投资实体，不应建链
    chain_stop_words: List[str] = field(default_factory=lambda: [
        # 市场行情泛词
        "研究", "五一", "假期", "加强", "举措", "历史", "新高", "涨幅", "扩大", "股价",
        "走高", "走低", "大涨", "大跌", "反弹", "回落", "冲高", "震荡",
        "上行", "下行", "走强", "走弱", "突破", "站上", "跌破", "触及", "收益",
        # 公告标题泛词
        "资金", "募集", "往来", "鉴证", "核查", "汇总", "专项", "存放",
        "管理办法", "审计", "披露", "证监会", "深交所", "上交所",
        # 公司治理泛词
        "董事", "监事", "高管", "董秘", "法人", "董事长",
        "国投", "中投", "议案", "提案", "任命", "辞职",
        # 合规公告泛词
        "内部控制", "自我评价", "自查", "评价报告", "内部审计",
        # 快讯行情泛词
        "回应", "涨停", "跌停", "涨停板", "跌停板",
        "特朗普", "美股", "港股", "日经", "台交所", "恒生",
        "成交额", "两市", "涨超", "跌超",
        "期货", "主力", "合约", "保证金",
        "第一季度", "第二季度", "第三季度", "第四季度",
        "一季度", "二季度", "三季度", "四季度",
        "超过", "用于", "拟向", "募资",
        "宣布", "澄清",
        "纳斯达克", "标普", "道琼斯",
        # 二次过滤: 更多泛词
        "国家", "企业", "下降", "上涨", "风险", "ETF", "上海",
        "研究院", "亿日元", "公积金", "烟花爆竹",
        "宣布", "指出", "认为", "预计", "预期", "可能",
        # 三次过滤
        "行动", "俄罗斯", "美联储", "下跌", "盘前", "欧盟",
        "制裁", "反倾销", "补贴",
        # 四次过滤
        "以色列", "央行", "韩国", "航运", "船舶", "运价",
        "霍尔木兹", "海峡", "军事", "战争", "冲突",
        # 五次过滤
        "总统", "视频", "日本", "建设", "全国", "石油",
        "COMEX", "伊朗", "伊拉克", "阿联酋", "卡塔尔",
        # 六次过滤
        "上调", "外交部", "完成", "官员", "美军", "安全",
        "批准", "批复", "获批",
        # 七次过滤
        "谈判", "连续", "正在", "成立", "开盘", "谷歌",
        "进行", "实施", "启动", "推进", "推动", "开展",
        "计划", "预期", "目标", "签署",
        "上市", "发行", "上会", "辅导",
        "招标", "中标", "中标结果",
        # 八次过滤
        "警示", "袭击", "前值", "出口", "进口", "贸易",
        "签订", "到期", "到账", "到位",
        # 九次过滤
        "发生", "英国", "TO", "交易", "附近",
        "那里", "这里", "那个", "这个",
    ])
    # 公告过滤关键词: 标题包含这些词的公告被视为常规合规文件，不参与链构建
    filing_filter_keywords: List[str] = field(default_factory=lambda: [
        "内部控制", "审计报告", "自我评价", "自查表",
        "募集资金管理", "关联资金往来", "非经营性资金占用",
        "管理办法", "工作制度", "鉴证报告", "核查意见",
        "内部审计制度", "内部报告制度", "信息披露管理制度",
    ])
    industry_alias: Dict[str, List[str]] = field(default_factory=lambda: {
        "航运": ["航运", "港口", "水上运输", "远洋", "海运", "船"],
        "航空": ["航空", "机场", "民航"],
        "银行": ["银行"],
        "保险": ["保险"],
        "证券": ["证券", "券商", "期货"],
        "医药": ["医药", "生物", "制药", "医疗", "中药", "化学制药", "医疗器械"],
        "半导体": ["半导体", "芯片", "集成电路", "封测"],
        "新能源": ["新能源", "光伏", "锂电", "风电", "储能", "充电桩"],
        "汽车": ["汽车", "整车", "零部件", "新能源车"],
        "白酒": ["白酒", "酒"],
        "房地产": ["房地产", "地产", "园区开发"],
        "煤炭": ["煤炭", "煤"],
        "钢铁": ["钢铁", "钢"],
        "有色金属": ["有色金属", "铜", "铝", "锂", "稀土"],
        "石油": ["石油", "石化", "油气"],
        "电力": ["电力", "电网", "发电"],
        "军工": ["军工", "国防", "航天", "航空装备"],
        "消费电子": ["消费电子", "电子", "面板", "显示"],
        "家电": ["家电", "白色家电", "小家电"],
        "食品": ["食品", "饮料", "乳制品", "调味品"],
        "纺织": ["纺织", "服装", "服饰"],
        "化工": ["化工", "化学", "化纤", "塑料"],
        "建材": ["建材", "水泥", "玻璃"],
        "机械": ["机械", "工程机械", "专用设备", "通用设备"],
        "通信": ["通信", "5G", "光通信"],
        "传媒": ["传媒", "游戏", "影视", "广告"],
        "计算机": ["计算机", "软件", "IT", "信创", "人工智能", "AI"],
        "期货": ["期货", "白银", "沪银", "集运", "欧线"],
    })
    # 产业链传导关系图: 上游 → [下游行业列表]
    # 用于板块传导链推导传导方向, 而非靠时间先后猜测
    supply_chain_map: Dict[str, List[str]] = field(default_factory=lambda: {
        "半导体": ["消费电子", "汽车", "通信", "计算机"],
        "石油": ["化工", "航运", "汽车"],
        "煤炭": ["电力", "钢铁", "化工"],
        "钢铁": ["建材", "机械", "汽车"],
        "有色金属": ["新能源", "汽车", "电子"],
        "锂电": ["新能源", "汽车", "储能"],
        "电力": ["有色", "化工", "钢铁"],
        "化工": ["纺织", "医药", "农业"],
        "机械": ["汽车", "建材", "军工"],
        "粮食": ["食品", "白酒"],
    })

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
                       + self.eval_weight_consistency
                       + self.eval_weight_investment_relevance)
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
