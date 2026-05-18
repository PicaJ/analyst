"""
线索链构建器

从新闻时间线中发现隐蔽的因果/关联链条:
  1. 时间链 — 同一主题/实体随时间演变
  2. 实体链 — 不同实体通过共同关联被串联
  3. 板块传导链 — 政策/事件从上游传导到下游行业
  4. 异常链 — 情绪/频率突然变化，可能暗示未公开信息
"""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from loguru import logger

from .query import NewsQuery
from .config import AnalystConfig


@dataclass
class ChainNode:
    """线索链节点"""
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
    impact_scope: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ChainNode":
        return cls(
            news_id=d["id"],
            title=d["title"],
            publish_time=d.get("publish_time", ""),
            source=d.get("source", ""),
            source_priority=d.get("source_priority", 2),
            category=d.get("category", "general"),
            sentiment=d.get("sentiment"),
            urgency=d.get("urgency", "normal"),
            ts_codes=d.get("ts_codes", []),
            mentioned_companies=d.get("mentioned_companies", []),
            mentioned_persons=d.get("mentioned_persons", []),
            related_sectors=d.get("related_sectors", []),
            impact_scope=d.get("impact_scope"),
        )


@dataclass
class ChainLink:
    """线索链边 — 两个节点之间的关联"""
    from_id: str
    to_id: str
    link_type: str    # temporal / entity / sector / anomaly
    strength: float   # 0.0 ~ 1.0
    reason: str = ""


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

    @property
    def time_span(self) -> str:
        if not self.nodes:
            return ""
        times = [n.publish_time for n in self.nodes if n.publish_time]
        return f"{min(times)[:10]} ~ {max(times)[:10]}" if times else ""

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "chain_type": self.chain_type,
            "theme": self.theme,
            "significance": self.significance,
            "time_span": self.time_span,
            "node_count": self.node_count,
            "hidden_signals": self.hidden_signals,
            "nodes": [
                {
                    "id": n.news_id,
                    "title": n.title,
                    "time": n.publish_time[:16] if n.publish_time else "",
                    "source": n.source,
                    "sentiment": n.sentiment,
                    "companies": n.mentioned_companies[:3],
                    "sectors": n.related_sectors[:3],
                }
                for n in self.nodes
            ],
            "links": [
                {
                    "from": l.from_id,
                    "to": l.to_id,
                    "type": l.link_type,
                    "strength": round(l.strength, 2),
                    "reason": l.reason,
                }
                for l in self.links
            ],
        }


def _merge_dedup(*result_lists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """合并多个查询结果，按 id 去重"""
    seen: Set[str] = set()
    merged: List[Dict[str, Any]] = []
    for items in result_lists:
        for item in items:
            item_id = item.get("id")
            if item_id and item_id not in seen:
                seen.add(item_id)
                merged.append(item)
    return merged


# 中文停用词 (常见虚词/副词/代词/泛词)
_STOP_WORDS = frozenset(
    "的 了 在 是 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 "
    "自己 这 那 他 她 它 们 把 被 从 对 与 向 为 以 之 而 或 但 如果 因为 所以 那么 "
    "可以 这个 那个 什么 怎么 为什么 多少 哪 里 谁 时 中 后 前 又 将 已 还 再 更 "
    "最 该 其 此 每 各 该 本 该 等 被 让 给 用 比 按 据 "
    # 财经新闻常见泛词 (不是有效实体)
    "公司 集团 股份 有限公司 控股 股东 减持 增持 公告 表示 目前 可能 导致 变更 "
    "预计 计划 相关 继续 发布 实施情况 说明 关注 进行 通过 影响 年度 记者报道 "
    "此前 未来 期间 持股 数量 合计 不超 人民币 万元 亿元 美元 报告 通知 决议 "
    "显示 根据 收到 事项 是否 需要 提供 "
    # 公告/快讯标题高频泛词
    "关于 工作 情况 进展 投资者 说明会 股东会 回购 集体 业绩 证券 科技 "
    "委员会 董事会 监事会 审议 批准 表决 独立 立案 调查 处罚 处分 "
    "之日起 交易日 收盘 价格 行使 权利 期权 激励 对象 限制性 股票 "
    "首次 公开 发行 上市 辅导 管理 制度 规则 办法 指引 指南 "
    "召开 会议 表决 投票 结果 有效 出席 委托 "
    # 公司治理/人事泛词 (不应作为投资子主题)
    "董事 监事 高管 董秘 法人 代表 副总 总经理 董事长 秘书 "
    "国投 中投 持有 子公司 分公司 控股股东 实际控制人 "
    "议案 提案 决议 任命 免职 辞职 任职 离任 变动 调整 "
    # 快讯通用词（非实体）
    "日内 后者 前者 已停 短线 盘中 报道 消息人士 据报道 "
    "续创 创新高 快速 拉升 涨幅扩大 跌幅扩大 直线 拉升 "
    # 异常链/交叉链常见垃圾实体（从标题分词中混入的泛词）
    "研究 五一 假期 加强 举措 历史 新高 涨幅 扩大 股价 "
    "亿美元 走高 走低 大涨 大跌 反弹 回落 冲高 震荡 "
    "上行 下行 走强 走弱 突破 站上 跌破 触及 收益 "
    # 公告标题高频泛词（不应构成投资子主题）
    "资金 募集 往来 鉴证 核查 汇总 专项 存放 使用情况 "
    "管理办法 审计 报告 披露 证监会 深交所 上交所 "
    # 新闻标题高频泛词（非投资关键词）
    "联社 日电 中国 美国 市场 预期 增长 全球 经济 国内 "
    "其中 当日 当周 同比 环比 年率 季调 终值 初值 修正 "
    "最新 今日 本周 近期 上周 上月 下月 下周 "
    # jieba 分词常见碎片（不应作为投资实体）
    "投资 研报 高端 商业 布局 核心 持续 加速 推进 推动 "
    "引领 赋能 融合 转型 升级 赋能 领军 头部 深度 广度 "
    "中信 建投 国泰 海通 华泰 招商 广发 申万 银河 中金 "
    "伟达 英伟 谷歌 苹果 微软 特斯 亚马 奥多 拉里 "
    "高质量 青年 引擎 致贺 时代 世纪 梦想 未来 世界 "
    "观点 分析师 评论 解读 建议 洞察 视角 角度 "
    "大幅 显著 明显 较快 稳步 强劲 亮眼 优异 "
    "第一 第二 第三 每日 每周 每月 双周 单月 "
    "重点 关键 热点 焦点 核心要点 重要 重大 "
    "机构 券商 基金 保险 银行 私募 公募 "
    "板块 概念 题材 赛道 主题 方向 风口 "
    "产能 产量 出货 出货量 需求 供给 供应 "
    "项目 工程 基地 园区 中心 平台 系统 "
    "技术 方案 方案 产品 方案 解决方案 应用 场景 "
    "标准 规范 指标 数据 信息 系统 平台 "
    "合作 战略 伙伴 关系 协议 框架 谅解 "
    "发展 创新 创新 创造 突破 领先 前沿 "
    "服务 解决 支持 帮助 促进 提升 优化 "
    "收入 利润 估值 市值 规模 份额 占比 "
    "计划 规划 目标 愿景 战略 方向 路线 "
    "指数 指标 基准 权重 成分 样本 调整 "
    # 财务指标泛词 (匹配所有公司财报，不是投资主题)
    "净利润 净利 营业收入 营收 毛利率 净利率 每股收益 "
    "净资产 资产负债 现金流 同比增长 环比增长 "
    "盈利 亏损 扭亏 减亏 增盈 派息 分红 送转 "
    "财报 一季报 年报 半年报 季报 业绩快报 业绩预告 "
    "财报季 披露期 业绩 业绩公告 业绩报告 季度报告 "
    "Q1 Q2 Q3 Q4 "
    # 快讯行情泛词 (不应独立建链)
    "回应 涨停 跌停 涨停板 跌停板 "
    "特朗普 美股 港股 日经 台交所 恒生 "
    "成交额 两市 涨超 跌超 "
    "期货 主力 合约 保证金 "
    "第一季度 第二季度 第三季度 第四季度 "
    "我国 一季度 二季度 三季度 四季度 "
    "超过 用于 拟向 募资 "
    "宣布 表示 回应 澄清 "
    "纳斯达克 标普 道琼斯 "
    # 二次过滤: 更多泛词 (从 Round 2 测试结果中发现)
    "国家 企业 下降 上涨 风险 ETF 上海 "
    "研究院 亿日元 公积金 烟花爆竹 洪迪厄斯 "
    "宣布 指出 认为 预计 预期 可能 "
    # 三次过滤: 更多泛词 (从 Round 3 测试结果)
    "行动 俄罗斯 美联储 下跌 盘前 欧盟 "
    "制裁 反倾销 补贴 "
    # 四次过滤 (从 Round 4)
    "以色列 央行 韩国 航运 船舶 运价 "
    "霍尔木兹 海峡 军事 战争 冲突 "
    # 五次过滤 (从 Round 5)
    "总统 视频 日本 建设 全国 石油 "
    "COMEX 伊朗 伊拉克 阿联酋 卡塔尔 "
    # 六次过滤 (从 Round 6)
    "上调 外交部 完成 官员 美军 安全 "
    "批准 批复 获批 "
    # 七次过滤 (从 Round 7 - 动词/形容词泛词)
    "谈判 连续 正在 成立 开盘 谷歌 "
    "进行 实施 启动 推进 推动 开展 "
    "计划 预期 目标 签署 "
    "上市 发行 上会 辅导 "
    "招标 中标 中标结果 获批 "
    # 八次过滤 (从 Round 8)
    "警示 袭击 前值 出口 进口 贸易 "
    "签订 到期 到账 到位 "
    # 九次过滤 (从 Round 9)
    "发生 英国 TO 交易 附近 "
    "那里 这里 那个 这个 ".split()
)


def _extract_title_keywords(title: str) -> List[str]:
    """从标题提取关键词 (优先 jieba 分词, 没有则按标点拆分)"""
    try:
        import jieba
        words = list(jieba.cut(title))
    except ImportError:
        # 没有 jieba 时按常见标点拆分
        import re
        parts = re.split(r'[：:，,。！!？?、；;\s]+', title)
        words = [p for p in parts if p]
    return [w for w in words if len(w) >= 2 and w not in _STOP_WORDS
            and not all(c in "0123456789.%％万亿元角分" for c in w)]


def _count_title_keywords(items: List[Dict[str, Any]], top_n: int = 10) -> List[Tuple[str, int]]:
    """从新闻列表的标题中统计高频关键词"""
    from collections import Counter
    counter: Counter = Counter()
    for item in items:
        title = item.get("title", "")
        if title:
            counter.update(_extract_title_keywords(title))
    return counter.most_common(top_n)


class ChainBuilder:
    """线索链构建器"""

    # 公告源: 合规文件，不含市场信号，不参与链构建
    _FILING_SOURCES = frozenset({"eastmoney_notice", "cninfo"})

    def __init__(self, config: AnalystConfig):
        self.config = config
        self.query = NewsQuery(config)
        # 合并配置文件中的停用词到全局停用词集合
        if config.chain_stop_words:
            global _STOP_WORDS
            _STOP_WORDS = _STOP_WORDS | frozenset(config.chain_stop_words)
        # 股票名称 → ts_code 映射，用于补提取 ts_codes
        self._stock_name_map: Dict[str, str] = {}
        self._load_stock_name_map()

    def _load_stock_name_map(self):
        """加载 股票名称→ts_code 映射"""
        from pathlib import Path as _P

        data_dir = _P(self.config.data_dir)

        # ts_code_name.json (主要来源: ~5000 条)
        name_path = data_dir / "cache" / "ts_code_name.json"
        if name_path.exists():
            try:
                d = json.loads(name_path.read_text(encoding="utf-8"))
                for ts_code, name in d.items():
                    if name:
                        self._stock_name_map[name] = ts_code
            except Exception:
                pass

        # stock_industry_cache.json (补充)
        industry_path = data_dir / "stock_industry_cache.json"
        if industry_path.exists():
            try:
                d = json.loads(industry_path.read_text(encoding="utf-8"))
                for code, info in d.get("data", {}).items():
                    name = info.get("name", "")
                    if name and name not in self._stock_name_map:
                        suffix = ".SH" if code.startswith(("6", "5")) else ".SZ"
                        self._stock_name_map[name] = f"{code}{suffix}"
            except Exception:
                pass

        logger.debug("Loaded stock name map: {} entries", len(self._stock_name_map))

        # 构建公司名匹配正则 (用于实体抽取)
        self._compile_company_pattern()

        # 行业别名关键词 (用于板块推断)
        self._sector_keywords: List[Tuple[str, str]] = []
        for sector, aliases in getattr(self.config, "industry_alias", {}).items():
            for alias in aliases:
                if len(alias) >= 2:
                    self._sector_keywords.append((alias, sector))

    def _compile_company_pattern(self):
        """构建公司名匹配正则 — 按名称长度降序，优先匹配长名"""
        import re as _re
        if not self._stock_name_map:
            self._company_pattern = None
            return
        # 只取 >= 3 字符的公司名，避免两字泛词误匹配
        names = sorted(
            [n for n in self._stock_name_map if len(n) >= 3],
            key=len, reverse=True,
        )
        if not names:
            self._company_pattern = None
            return
        # 分批构建正则（避免单条正则过长）
        batch_size = 500
        self._company_patterns = []
        for i in range(0, len(names), batch_size):
            batch = names[i:i + batch_size]
            pat = _re.compile("|".join(_re.escape(n) for n in batch))
            self._company_patterns.append((pat, batch))
        self._sorted_names = names

    def enrich_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """批量富化新闻条目：从标题抽取公司名/行业板块/股票代码

        解决非公告源 (thx/cls/eastmoney等) 结构化字段全部为空的问题。
        在链构建和规划阶段之前调用，使下游可使用 mentioned_companies、
        related_sectors、ts_codes 等字段。
        """
        if not self._stock_name_map:
            return items

        for item in items:
            title = item.get("title", "")
            if not title:
                continue

            # ── 抽取公司名 → ts_codes ──
            companies: List[str] = []
            ts_set: Set[str] = set()
            if self._company_patterns:
                for pat, batch in self._company_patterns:
                    for m in pat.finditer(title):
                        name = m.group()
                        if name in self._stock_name_map:
                            companies.append(name)
                            ts_set.add(self._stock_name_map[name])

            if companies:
                # 合并已有的 ts_codes
                existing_ts = item.get("ts_codes", [])
                if isinstance(existing_ts, str):
                    try:
                        existing_ts = json.loads(existing_ts)
                    except (json.JSONDecodeError, TypeError):
                        existing_ts = []
                item["mentioned_companies"] = companies
                item["ts_codes"] = list(ts_set | set(existing_ts))

            # ── 推断行业板块 ──
            sectors: List[str] = []
            for alias, sector in self._sector_keywords:
                if alias in title and sector not in sectors:
                    sectors.append(sector)
            if sectors:
                existing_sec = item.get("related_sectors", [])
                if isinstance(existing_sec, str):
                    try:
                        existing_sec = json.loads(existing_sec)
                    except (json.JSONDecodeError, TypeError):
                        existing_sec = []
                item["related_sectors"] = list(set(sectors + existing_sec))

        return items

    def _enrich_ts_codes(self, nodes: List[ChainNode]) -> None:
        """对 ts_codes 为空的节点，从标题中提取公司名并映射为股票代码"""
        if not self._stock_name_map:
            return

        # 按名称长度降序排列，优先匹配长名称 (如"中国平安"优先于"平安")
        sorted_names = sorted(self._stock_name_map.keys(), key=len, reverse=True)

        for node in nodes:
            if node.ts_codes:
                continue
            title = node.title
            found_codes = []
            matched_spans = []
            for name in sorted_names:
                if len(found_codes) >= 5:
                    break
                idx = title.find(name)
                if idx >= 0:
                    # 避免重叠匹配 (如"中国平安"匹配后不再匹配"平安")
                    span = (idx, idx + len(name))
                    if any(s <= span[0] < e or s < span[1] <= e for s, e in matched_spans):
                        continue
                    found_codes.append(self._stock_name_map[name])
                    matched_spans.append(span)
            if found_codes:
                node.ts_codes = found_codes

    async def build_timeline_chain(
        self,
        entity: str,
        entity_type: str = "company",
        days: int = 90,
    ) -> List[ClueChain]:
        """构建时间线索链 — 同一实体/主题的事件演变

        混合检索 + SQLite 标题匹配合并，按 ID 去重:
          - search_hybrid: FAISS 语义相关 + FTS5 关键词 → 高召回
          - get_timeline:  SQLite 标题 LIKE 匹配         → 不漏
        search_hybrid 失败时降级为纯 SQLite 查询。

        当匹配结果超过 chain_split_threshold 时，自动按共现实体
        拆分为多条子主题链，每条聚焦一个投资方向。
        """
        limit = self.config.query_limit_entity

        # 1. 混合检索 (FAISS + FTS5)，失败则降级
        try:
            hybrid_items = await self.query.search_hybrid(
                query=entity, top_k=limit, days=days,
                alpha=self.config.hybrid_alpha,
            )
        except Exception as e:
            logger.warning("search_hybrid 失败, 降级为 SQLite: {}", e)
            hybrid_items = []

        # 2. SQLite 标题匹配 (entity 字段可能为空，用 title LIKE 保底)
        entity_items = await self.query.get_timeline(
            keywords=[entity], days=days, limit=limit,
        )

        # 3. 合并去重
        items = _merge_dedup(hybrid_items, entity_items)

        # 3.5 富化: 从标题抽取公司名/行业 (非公告源无结构化字段)
        items = self.enrich_items(items)

        # 4. 统一过滤: 排除公告源 + 合规文件 + 关键词不相关
        before_filter = len(items)
        items = self._filter_for_chain(items, entity=entity, entity_type=entity_type)
        if len(items) < before_filter:
            logger.info("Timeline '{}': filtered {} → {} items",
                        entity, before_filter, len(items))

        if len(items) < 2:
            return []

        # 6. 子主题分裂: 匹配结果过多时按共现实体拆分
        cfg = self.config
        if len(items) > cfg.chain_split_threshold and cfg.max_subtopic_chains > 0:
            subtopics = self._split_into_subtopics(entity, items, cfg.max_subtopic_chains)
            if len(subtopics) >= 2:
                logger.info("Timeline chain '{}' splitting: {} items → {} sub-topics",
                            entity, len(items), len(subtopics))
                return self._build_split_chains(entity, items, subtopics, entity_type, days)

        # 5. 正常: 构建单条链
        return [self._make_timeline_chain(entity, items, entity_type, days)]

    # ========== timeline 链辅助方法 ==========

    def _make_timeline_chain(
        self,
        entity: str,
        items: List[Dict[str, Any]],
        entity_type: str,
        days: int,
    ) -> ClueChain:
        """从 items 构建单条 timeline 链 — 基于事件线索聚类

        不再把所有新闻平铺为一条线，而是:
          1. 用关键词重叠+时间窗口将新闻分为事件线索
          2. 同一事件线索内的相邻节点才相连
          3. link.reason 从数据推导而非写死
        """
        nodes = [ChainNode.from_dict(it) for it in items]
        nodes.sort(key=lambda n: n.publish_time or "")
        self._enrich_ts_codes(nodes)

        # 事件线索聚类
        threads = self._cluster_event_threads(nodes)

        links = []
        thread_labels = []  # 用于 hidden_signals
        for thread_idx, thread_nodes in enumerate(threads):
            thread_label = self._label_event_thread(thread_nodes, entity)
            thread_labels.append(thread_label)
            for i in range(len(thread_nodes) - 1):
                n1, n2 = thread_nodes[i], thread_nodes[i + 1]
                reason = self._derive_link_reason(n1, n2, thread_label)
                links.append(ChainLink(
                    from_id=n1.news_id,
                    to_id=n2.news_id,
                    link_type="temporal",
                    strength=self.config.chain_timeline_strength,
                    reason=reason,
                ))

        sentiment_shifts = self._detect_sentiment_shifts(nodes)

        # 如果检测到多条事件线索，报告为 hidden_signal
        if len(thread_labels) > 1:
            sentiment_shifts.insert(
                0,
                f"检测到{len(thread_labels)}条事件线索: {'; '.join(thread_labels[:3])}"
            )

        safe_id = entity.replace(" ", "_").replace("×", "_")[:40]
        return ClueChain(
            chain_id=f"timeline_{safe_id}_{datetime.utcnow().strftime('%Y%m%d')}",
            chain_type="timeline",
            theme=f"{entity} 事件时间线 ({days}天, {len(threads)}条线索)",
            nodes=nodes,
            links=links,
            significance=self._calc_significance(nodes),
            hidden_signals=sentiment_shifts[:5],
        )

    # ── 事件线索聚类辅助方法 ──

    def _cluster_event_threads(
        self,
        nodes: List[ChainNode],
        gap_days: int = 3,
        min_overlap: int = 2,
    ) -> List[List[ChainNode]]:
        """将节点按事件线索聚类

        规则: 时间差 <= gap_days AND 标题关键词重叠 >= min_overlap → 同一事件线索
        否则在两者之间断开，后续节点开始新线索。
        """
        if len(nodes) <= 2:
            return [nodes]

        # 预计算每个节点的标题关键词集合
        node_keywords: List[Set[str]] = []
        for n in nodes:
            kws = set(_extract_title_keywords(n.title)) if n.title else set()
            # 补充结构化字段作为关键词
            for c in n.mentioned_companies:
                if c not in _STOP_WORDS and len(c) >= 2:
                    kws.add(c)
            for s in n.related_sectors:
                if s not in _STOP_WORDS and len(s) >= 2:
                    kws.add(s)
            node_keywords.append(kws)

        # 事件断裂点检测
        break_points = []
        for i in range(len(nodes) - 1):
            try:
                t1 = datetime.fromisoformat(nodes[i].publish_time)
                t2 = datetime.fromisoformat(nodes[i + 1].publish_time)
                days_gap = (t2 - t1).days
            except (ValueError, TypeError):
                days_gap = 999

            overlap = len(node_keywords[i] & node_keywords[i + 1])

            # 断裂条件: 时间差太大 OR 关键词无重叠
            if days_gap > gap_days and overlap < min_overlap:
                break_points.append(i + 1)
            elif days_gap > gap_days * 3:
                # 超大间隔，强制断开
                break_points.append(i + 1)

        # 按断裂点切分
        if not break_points:
            return [nodes]

        threads = []
        prev = 0
        for bp in break_points:
            segment = nodes[prev:bp]
            if segment:
                threads.append(segment)
            prev = bp
        if prev < len(nodes):
            threads.append(nodes[prev:])
        return threads

    def _label_event_thread(
        self,
        thread_nodes: List[ChainNode],
        entity: str,
    ) -> str:
        """为事件线索生成简短标签

        从线索中提取高频非停用关键词（排除 entity 本身）作为事件标签。
        """
        if not thread_nodes:
            return "未知事件"

        kw_counter: Dict[str, int] = {}
        entity_lower = entity.lower()
        for n in thread_nodes:
            for kw in _extract_title_keywords(n.title or ""):
                if kw.lower() != entity_lower and len(kw) >= 2:
                    kw_counter[kw] = kw_counter.get(kw, 0) + 1

        if not kw_counter:
            # 用标题前 15 字作为兜底
            return (thread_nodes[0].title or "未知事件")[:15]

        top_kw = sorted(kw_counter, key=kw_counter.get, reverse=True)[:2]
        return "+".join(top_kw)

    def _derive_link_reason(
        self,
        n1: ChainNode,
        n2: ChainNode,
        thread_label: str,
    ) -> str:
        """从两个节点的标题推导连接原因"""
        t1 = n1.title[:25] if n1.title else "?"
        t2 = n2.title[:25] if n2.title else "?"
        return f"[{thread_label}] {t1} → {t2}"

    def _split_into_subtopics(
        self,
        entity: str,
        items: List[Dict[str, Any]],
        max_subtopics: int,
    ) -> List[Tuple[str, List[Dict[str, Any]]]]:
        """从大量匹配结果中提取共现实体，拆分为子主题

        策略:
          1. 优先从结构化字段 (mentioned_companies / related_sectors) 提取 — 精确可靠
          2. 结构化字段不足时，用标题关键词补充
          3. 对候选子主题去重: 覆盖新闻重叠度 >80% 的只保留一个

        返回: [(子主题实体名, 匹配的新闻列表), ...]
        """
        from collections import Counter

        entity_lower = entity.lower()

        # ── Pass 1: 结构化字段 (高置信) ──
        cooccur_structured: Counter = Counter()
        entity_items_structured: Dict[str, List[Dict]] = defaultdict(list)

        for item in items:
            for c in (item.get("mentioned_companies") or []):
                if c.lower() == entity_lower or c in _STOP_WORDS or len(c) < 2:
                    continue
                if entity_lower in c.lower() or c.lower() in entity_lower:
                    continue
                cooccur_structured[c] += 1
                entity_items_structured[c].append(item)
            for s in (item.get("related_sectors") or []):
                if s.lower() == entity_lower or s in _STOP_WORDS or len(s) < 2:
                    continue
                if entity_lower in s.lower() or s.lower() in entity_lower:
                    continue
                cooccur_structured[s] += 1
                entity_items_structured[s].append(item)

        # ── Pass 2: 标题关键词 (补充) ──
        cooccur_title: Counter = Counter()
        entity_items_title: Dict[str, List[Dict]] = defaultdict(list)

        for item in items:
            title = item.get("title", "")
            for kw in _extract_title_keywords(title):
                kl = kw.lower()
                if kl == entity_lower or kw in _STOP_WORDS or len(kw) < 2:
                    continue
                if entity_lower in kl or kl in entity_lower:
                    continue
                # 已在结构化字段中出现的不再重复
                if kw in cooccur_structured:
                    continue
                cooccur_title[kw] += 1
                entity_items_title[kw].append(item)

        # ── 合并候选: 结构化优先，标题关键词补充 ──
        all_candidates: List[Tuple[str, List[Dict[str, Any]]]] = []
        for c, count in cooccur_structured.most_common():
            if count >= 3:
                all_candidates.append((c, entity_items_structured[c]))
        for c, count in cooccur_title.most_common():
            if count >= 3:
                all_candidates.append((c, entity_items_title[c]))

        # ── 去重: 覆盖新闻重叠度 >80% 的只保留一个 ──
        selected: List[Tuple[str, List[Dict[str, Any]]]] = []
        for sub_entity, sub_items in all_candidates:
            sub_ids = set(it.get("id", "") for it in sub_items)
            is_dup = False
            for _, existing_items in selected:
                existing_ids = set(it.get("id", "") for it in existing_items)
                overlap = len(sub_ids & existing_ids) / min(len(sub_ids), len(existing_ids))
                if overlap > 0.8:
                    is_dup = True
                    break
            if not is_dup:
                selected.append((sub_entity, sub_items))
            if len(selected) >= max_subtopics:
                break

        return selected

    def _build_split_chains(
        self,
        entity: str,
        items: List[Dict[str, Any]],
        subtopics: List[Tuple[str, List[Dict[str, Any]]]],
        entity_type: str,
        days: int,
    ) -> List[ClueChain]:
        """将大链拆为子主题链 + 剩余链

        - 每个子主题: 主关键词 × 共现实体 → 独立 timeline 链
        - 剩余: 未被子主题覆盖的新闻 → 保留在原链中
        - 同一条新闻可以被多个子主题链包含 (不同投资视角)
        """
        chains: List[ClueChain] = []
        covered_ids: Set[str] = set()

        for sub_entity, sub_items in subtopics:
            chain = self._make_timeline_chain(
                entity=f"{entity}×{sub_entity}",
                items=sub_items,
                entity_type=entity_type,
                days=days,
            )
            chains.append(chain)
            covered_ids.update(it.get("id", "") for it in sub_items)
            logger.debug("Sub-topic chain: {}×{} → {} nodes", entity, sub_entity, len(sub_items))

        # 未被任何子主题覆盖的新闻 → 保留在原链
        remaining = [it for it in items if it.get("id", "") not in covered_ids]
        if len(remaining) >= 2:
            chain = self._make_timeline_chain(
                entity=entity,
                items=remaining,
                entity_type=entity_type,
                days=days,
            )
            chains.append(chain)
            logger.debug("Remaining chain: {} → {} nodes", entity, len(remaining))

        logger.info("Split '{}' ({} items) → {} chains",
                    entity, len(items), len(chains))
        return chains

    async def build_sector_propagation_chain(
        self,
        policy_keywords: List[str],
        days: int = 90,
    ) -> List[ClueChain]:
        """构建板块传导链 — 基于产业链关系推导传导方向，用新闻数据验证

        1. 从 policy_keywords 匹配 supply_chain_map 找到上下游关系
        2. 在新闻数据中验证传导路径（上下游板块都有相关新闻）
        3. 只保留有数据证据支持的传导路径
        """
        limit = self.config.query_limit_timeline

        # 1. 混合检索 (FAISS + FTS5)，失败则降级
        try:
            hybrid_items = await self.query.search_hybrid(
                query=" ".join(policy_keywords), top_k=limit, days=days,
                alpha=self.config.hybrid_alpha,
            )
        except Exception as e:
            logger.warning("search_hybrid 失败, 降级为 SQLite: {}", e)
            hybrid_items = []

        # 2. SQLite 关键词匹配
        timeline_items = await self.query.get_timeline(
            keywords=policy_keywords, days=days, limit=limit,
        )

        # 3. 合并去重
        items = _merge_dedup(hybrid_items, timeline_items)

        # 3.5 富化: 从标题抽取公司名/行业
        items = self.enrich_items(items)

        # 4. 统一过滤
        items = self._filter_for_chain(items)

        if len(items) < 3:
            return []

        nodes = [ChainNode.from_dict(it) for it in items]
        nodes.sort(key=lambda n: n.publish_time or "")
        self._enrich_ts_codes(nodes)

        # 4. 板块分组
        sector_groups: Dict[str, List[ChainNode]] = defaultdict(list)
        for n in nodes:
            for s in n.related_sectors:
                sector_groups[s].append(n)

        if len(sector_groups) < 2:
            return []

        # 5. 从 supply_chain_map 推导传导路径
        scm = getattr(self.config, 'supply_chain_map', {})
        propagation_paths = self._derive_propagation_paths(
            sector_groups, scm, policy_keywords,
        )

        if not propagation_paths:
            # 无产业链依据，退回基于时间排序（但加 warning）
            logger.info("Sector chain: no supply_chain_map match, using time-based fallback")
            return self._build_time_based_sector_chain(nodes, sector_groups, policy_keywords)

        # 6. 构建基于产业链的传导链
        links = []
        signals = []
        for upstream, downstream, lag_days, reason in propagation_paths:
            upstream_nodes = sector_groups.get(upstream, [])
            downstream_nodes = sector_groups.get(downstream, [])
            if not upstream_nodes or not downstream_nodes:
                continue

            latest_up = max(upstream_nodes, key=lambda n: n.publish_time or "")
            earliest_down = min(downstream_nodes, key=lambda n: n.publish_time or "")
            if latest_up.news_id == earliest_down.news_id:
                continue

            links.append(ChainLink(
                from_id=latest_up.news_id,
                to_id=earliest_down.news_id,
                link_type="sector",
                strength=self.config.chain_sector_strength,
                reason=reason,
            ))
            signals.append(
                f"产业链传导: {upstream}(上游) → {downstream}(下游), "
                f"预计滞后{lag_days}天, {downstream}存在滞后反应机会"
            )

        if not links:
            return self._build_time_based_sector_chain(nodes, sector_groups, policy_keywords)

        chain = ClueChain(
            chain_id=f"sector_prop_{datetime.utcnow().strftime('%Y%m%d%H%M')}",
            chain_type="sector_propagation",
            theme=f"板块传导: {'/'.join(policy_keywords[:3])} (产业链驱动)",
            nodes=nodes,
            links=links,
            significance=self._calc_significance(nodes),
            hidden_signals=signals[:5],
        )
        return [chain]

    # ── 板块传导链辅助方法 ──

    def _derive_propagation_paths(
        self,
        sector_groups: Dict[str, List[ChainNode]],
        supply_chain_map: Dict[str, List[str]],
        policy_keywords: List[str],
    ) -> List[Tuple[str, str, int, str]]:
        """从 supply_chain_map 推导传导路径并用新闻数据验证

        返回: [(上游板块, 下游板块, 滞后天数, 原因描述), ...]
        """
        paths = []
        matched_upstreams: Set[str] = set()

        # Step 1: policy_keywords 匹配 supply_chain_map 中的上游行业
        for keyword in policy_keywords:
            for upstream, downstreams in supply_chain_map.items():
                # 关键词匹配上游行业名或其别名
                aliases = self.config.industry_alias.get(upstream, [upstream])
                if keyword in aliases or keyword == upstream:
                    matched_upstreams.add(upstream)

        # Step 2: 对每个上游，推导下游并验证
        for upstream in matched_upstreams:
            if upstream not in sector_groups:
                continue
            downstreams = supply_chain_map.get(upstream, [])
            up_nodes = sector_groups[upstream]
            up_first = self._earliest_time(up_nodes)
            if not up_first:
                continue

            for downstream in downstreams:
                if downstream not in sector_groups:
                    continue
                down_nodes = sector_groups[downstream]
                down_first = self._earliest_time(down_nodes)
                if not down_first:
                    continue

                # 计算滞后天数
                try:
                    t_up = datetime.fromisoformat(up_first)
                    t_down = datetime.fromisoformat(down_first)
                    lag_days = (t_down - t_up).days
                except (ValueError, TypeError):
                    lag_days = 0

                # 只保留有方向性的传导（下游晚于上游，或同日但不同时）
                if lag_days < 0:
                    continue

                # 找触发新闻标题
                trigger_title = self._best_trigger_title(up_nodes)

                reason = (
                    f"产业链传导: {upstream}(上游)→{downstream}(下游), "
                    f"触发[{trigger_title[:25]}]"
                )
                paths.append((upstream, downstream, max(lag_days, 0), reason))

        return paths

    def _build_time_based_sector_chain(
        self,
        nodes: List[ChainNode],
        sector_groups: Dict[str, List[ChainNode]],
        policy_keywords: List[str],
    ) -> List[ClueChain]:
        """退路: 无 supply_chain_map 匹配时，用时间排序（旧逻辑）"""
        sector_timeline = []
        for sector, sector_nodes in sector_groups.items():
            valid_times = [n.publish_time for n in sector_nodes if n.publish_time]
            if not valid_times:
                continue
            first_time = min(valid_times)
            sector_timeline.append((first_time, sector, sector_nodes))
        sector_timeline.sort()

        if len(sector_timeline) < 2:
            return []

        links = []
        for i in range(len(sector_timeline) - 1):
            _, sector_a, nodes_a = sector_timeline[i]
            _, sector_b, nodes_b = sector_timeline[i + 1]
            latest_a = max(nodes_a, key=lambda n: n.publish_time or "")
            earliest_b = min(nodes_b, key=lambda n: n.publish_time or "")
            if latest_a.news_id == earliest_b.news_id:
                continue
            links.append(ChainLink(
                from_id=latest_a.news_id,
                to_id=earliest_b.news_id,
                link_type="sector",
                strength=self.config.chain_sector_strength,
                reason=f"时序传导: {sector_a} → {sector_b} (无产业链依据)",
            ))

        propagation_signals = self._detect_propagation_signals(sector_timeline)
        chain = ClueChain(
            chain_id=f"sector_prop_{datetime.utcnow().strftime('%Y%m%d%H%M')}",
            chain_type="sector_propagation",
            theme=f"板块传导: {'/'.join(policy_keywords[:3])} (时序推断)",
            nodes=nodes,
            links=links,
            significance=self._calc_significance(nodes),
            hidden_signals=propagation_signals,
        )
        return [chain]

    @staticmethod
    def _earliest_time(nodes: List[ChainNode]) -> str:
        times = [n.publish_time for n in nodes if n.publish_time]
        return min(times) if times else ""

    @staticmethod
    def _best_trigger_title(nodes: List[ChainNode]) -> str:
        """在板块节点中找到最佳触发新闻标题（优先级最高+最早的）"""
        by_priority = sorted(nodes, key=lambda n: (n.source_priority, n.publish_time or ""))
        return by_priority[0].title if by_priority else "未知事件"

    async def build_anomaly_chains(
        self,
        days: int = 30,
    ) -> List[ClueChain]:
        """构建异常链 — 触发点→爆发→扩散 三段式结构

        1. 检测密度爆发（复用现有逻辑）
        2. 在爆发窗口内定位触发点（最早的高优先级实质性新闻）
        3. 区分爆发期和扩散期
        4. 过滤掉无实质触发点的假异常（纯行情播报密度高）
        """
        items = await self.query.get_urgent(
            days=days,
            limit=self.config.query_limit_urgent,
        )
        if not items:
            return []

        # 富化 + 过滤
        items = self.enrich_items(items)
        items = self._filter_for_chain(items)

        nodes = [ChainNode.from_dict(it) for it in items]
        if len(nodes) < 3:
            return []
        nodes.sort(key=lambda n: n.publish_time or "")
        self._enrich_ts_codes(nodes)

        entity_bursts = self._detect_entity_bursts(nodes, window_days=days)

        chains = []
        for entity, burst_nodes in entity_bursts.items():
            if len(burst_nodes) < self.config.min_cluster_size:
                continue
            if entity in _STOP_WORDS or len(entity) < 2:
                continue

            # 定位触发点
            catalyst = self._find_catalyst(burst_nodes)
            if not catalyst:
                logger.debug("Anomaly '{}': no valid catalyst found, skipping", entity)
                continue

            # 构建三段式 links
            links, signals = self._build_anomaly_links(entity, burst_nodes, catalyst, days)

            chain = ClueChain(
                chain_id=f"anomaly_{entity}_{datetime.utcnow().strftime('%Y%m%d%H%M')}",
                chain_type="anomaly",
                theme=f"异常信号: {entity} 密度爆发",
                nodes=burst_nodes,
                links=links,
                significance=self.config.chain_anomaly_significance,
                hidden_signals=signals,
            )
            chains.append(chain)

        chains.sort(key=lambda c: c.node_count, reverse=True)
        return chains[:15]

    # ── 异常链辅助方法 ──

    _TICKER_WORDS = frozenset(
        "盘中 涨幅扩大 跌幅扩大 直线拉升 快速拉升 拉升 "
        "续创 创新高 站上 跌破 触及 "
        "涨幅 跌幅 涨停 跌停 冲高 回落 震荡 走高 走低".split()
    )

    def _find_catalyst(self, burst_nodes: List[ChainNode]) -> Optional[ChainNode]:
        """在爆发窗口内定位触发点

        条件: 时间最早的前3条中，source_priority 最高且标题不是纯行情播报
        """
        if not burst_nodes:
            return None

        candidates = sorted(burst_nodes, key=lambda n: n.publish_time or "")[:5]

        # 找非行情播报的高优先级节点
        best = None
        best_priority = 99
        for n in candidates:
            if any(w in (n.title or "") for w in self._TICKER_WORDS):
                continue
            if n.source_priority < best_priority:
                best_priority = n.source_priority
                best = n

        return best

    def _build_anomaly_links(
        self,
        entity: str,
        burst_nodes: List[ChainNode],
        catalyst: ChainNode,
        window_days: int,
    ) -> Tuple[List[ChainLink], List[str]]:
        """构建 触发→爆发→扩散 三段式 links 和 signals"""
        links = []
        signals = []

        try:
            catalyst_time = datetime.fromisoformat(catalyst.publish_time)
        except (ValueError, TypeError):
            catalyst_time = None

        # 划分阶段
        catalyst_id = catalyst.news_id
        burst_ids = []
        diffusion_ids = []

        for n in burst_nodes:
            if n.news_id == catalyst_id:
                continue
            if not catalyst_time or not n.publish_time:
                burst_ids.append(n.news_id)
                continue
            try:
                n_time = datetime.fromisoformat(n.publish_time)
                hours_from_catalyst = (n_time - catalyst_time).total_seconds() / 3600
                if hours_from_catalyst <= 6:
                    burst_ids.append(n.news_id)
                else:
                    diffusion_ids.append(n.news_id)
            except (ValueError, TypeError):
                burst_ids.append(n.news_id)

        # 触发点信号
        signals.append(
            f"触发事件: {catalyst.title[:40]} ({catalyst.source})"
        )
        if burst_ids:
            signals.append(
                f"爆发期: {len(burst_ids)}条快讯在6小时内集中出现"
            )
        if diffusion_ids:
            signals.append(
                f"扩散期: {len(diffusion_ids)}条消息在爆发后继续扩散"
            )

        # 构建 links: catalyst → burst → diffusion
        prev_id = catalyst_id
        for n in sorted(
            [n for n in burst_nodes if n.news_id != catalyst_id],
            key=lambda n: n.publish_time or "",
        ):
            if n.news_id in burst_ids:
                phase = "爆发"
            elif n.news_id in diffusion_ids:
                phase = "扩散"
            else:
                phase = "跟进"

            links.append(ChainLink(
                from_id=prev_id,
                to_id=n.news_id,
                link_type="anomaly",
                strength=self.config.chain_anomaly_strength,
                reason=f"{phase}: {entity} ({n.title[:25]})",
            ))
            prev_id = n.news_id

        return links, signals

    async def build_semantic_theme_chains(
        self,
        days: int = 30,
        max_chains: int = 5,
        min_cluster_size: int = 5,
    ) -> List[ClueChain]:
        """语义主题发现链 — 从未被 tracking_keywords 覆盖的新闻中发现新兴投资主题

        原理:
          1. 获取非公告源新闻的向量嵌入
          2. 对向量做 K-Means 聚类，每簇代表一个投资主题
          3. 剔除已被 tracking_keywords 覆盖的簇
          4. 对每个新簇构建 timeline 链
        """
        import numpy as np

        # 1. 获取非公告新闻 ID + 向量
        try:
            vs = self.query._agent.search_engine.vector_store
        except Exception:
            logger.warning("语义主题发现: vector_store 不可用，跳过")
            return []

        if vs.index is None or vs.index.ntotal == 0:
            return []

        # 2. 从 DB 获取非公告新闻 ID
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        import aiosqlite
        db = await aiosqlite.connect(self.config.db_path)
        try:
            async with db.execute(
                "SELECT id FROM news_items "
                "WHERE publish_time >= ? AND source NOT IN ('eastmoney_notice','cninfo') "
                "ORDER BY publish_time DESC",
                (cutoff,),
            ) as cur:
                rows = await cur.fetchall()
        finally:
            await db.close()

        non_filing_ids = set(r[0] for r in rows)
        if len(non_filing_ids) < min_cluster_size * 3:
            return []

        # 3. 从 FAISS 取出这些新闻的向量
        # id_map: faiss_index → news_id
        id_map = vs.id_map
        target_indices = []
        target_ids = []
        for faiss_idx, news_id in enumerate(id_map):
            if news_id in non_filing_ids:
                target_indices.append(faiss_idx)
                target_ids.append(news_id)

        if len(target_indices) < min_cluster_size * 3:
            return []

        # 提取向量 (FAISS reconstruct)
        try:
            all_vecs = np.array([vs.index.reconstruct(i) for i in target_indices])
        except Exception:
            # IndexIVFFlat 不支持 reconstruct，退回到搜索方式
            logger.debug("FAISS 不支持 reconstruct，跳过语义聚类")
            return []

        # 4. K-Means 聚类 — 控制簇数量，避免过多低质簇
        from sklearn.cluster import KMeans
        min_cluster_size = max(min_cluster_size, 10)  # 提高最小簇大小
        n_clusters = min(max_chains, len(target_indices) // min_cluster_size)
        n_clusters = max(n_clusters, 2)
        n_clusters = min(n_clusters, 15)  # 上限 15 个簇

        # 归一化向量 (内积 → 余弦)
        norms = np.linalg.norm(all_vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1
        all_vecs_norm = all_vecs / norms

        kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=5, max_iter=100)
        labels = kmeans.fit_predict(all_vecs_norm)

        # 5. 筛选有效簇: 大小 >= min_cluster_size 且不被 tracking_keywords 覆盖
        tracking_kws = set(getattr(self.config, "tracking_keywords", []))
        chains: List[ClueChain] = []

        # 批量获取新闻详情
        news_by_id = {}
        if target_ids:
            items = await self.query.get_by_ids(target_ids)
            for it in items:
                news_by_id[it.get("id", "")] = it

        for cluster_id in range(n_clusters):
            member_indices = [i for i, l in enumerate(labels) if l == cluster_id]
            if len(member_indices) < min_cluster_size:
                continue

            # 提取簇内新闻
            cluster_ids = [target_ids[i] for i in member_indices]
            cluster_items = [news_by_id[nid] for nid in cluster_ids if nid in news_by_id]

            if len(cluster_items) < min_cluster_size:
                continue

            # 检查是否已被 tracking_keywords 覆盖 (>30% 的标题包含任一 tracking keyword)
            covered_count = 0
            for it in cluster_items:
                title = it.get("title", "")
                if any(kw.lower() in title.lower() for kw in tracking_kws):
                    covered_count += 1
            coverage_ratio = covered_count / len(cluster_items)

            if coverage_ratio > 0.3:
                continue  # 已被现有链覆盖，跳过

            # 检查簇是否由停用词主导 (财务指标等非投资主题)
            from collections import Counter
            title_words = []
            for it in cluster_items:
                for w in _extract_title_keywords(it.get("title", "")):
                    title_words.append(w)
            top_words = [w for w, _ in Counter(title_words).most_common(5)]
            stop_count = sum(1 for w in top_words if w in _STOP_WORDS)
            if stop_count >= 2:
                logger.debug("语义簇 #{}: 跳过 (停用词主导: {})", cluster_id, top_words)
                continue

            # 检查簇内主题连贯性: 簇中心与成员的平均余弦相似度
            cluster_center = kmeans.cluster_centers_[cluster_id]
            cluster_center_norm = cluster_center / (np.linalg.norm(cluster_center) + 1e-10)
            member_vecs = all_vecs_norm[member_indices]
            similarities = member_vecs @ cluster_center_norm
            avg_similarity = float(np.mean(similarities))
            if avg_similarity < 0.20:
                logger.debug("语义簇 #{}: 跳过 (内聚性不足: {:.3f})", cluster_id, avg_similarity)
                continue

            # 检查是否为英文主导簇 (翻译新闻，非 A 股投资相关)
            eng_count = sum(1 for it in cluster_items
                            if sum(1 for c in it.get("title", "") if c.isascii() and c.isalpha())
                            > len(it.get("title", "")) * 0.3)
            if eng_count > len(cluster_items) * 0.4:
                logger.debug("语义簇 #{}: 跳过 (英文主导: {}/{})", cluster_id, eng_count, len(cluster_items))
                continue

            # 检查簇内新闻是否与 A 股投资相关 (至少 30% 标题包含公司名、行业关键词或股票代码)
            investment_keywords = set()
            for aliases in getattr(self.config, "industry_alias", {}).values():
                for a in aliases:
                    if len(a) >= 2:
                        investment_keywords.add(a)
            a_stock_count = 0
            for it in cluster_items:
                title = it.get("title", "")
                if (it.get("ts_codes") or it.get("mentioned_companies") or
                        any(kw in title for kw in investment_keywords)):
                    a_stock_count += 1
            if a_stock_count < len(cluster_items) * 0.3:
                logger.debug("语义簇 #{}: 跳过 (A股相关性不足: {}/{})",
                             cluster_id, a_stock_count, len(cluster_items))
                continue

            # 富化
            cluster_items = self.enrich_items(cluster_items)

            # 排序 + 截断: 最多取 50 条 (按时间倒序取最新)
            cluster_items.sort(key=lambda x: x.get("publish_time", ""), reverse=True)
            cluster_items = cluster_items[:50]
            cluster_items.sort(key=lambda x: x.get("publish_time", ""))

            # 生成主题: 从簇内高频标题词提取 (用更长、更有意义的词)
            from collections import Counter
            title_words = []
            for it in cluster_items:
                for w in _extract_title_keywords(it.get("title", "")):
                    title_words.append(w)
            top_words = [w for w, _ in Counter(title_words).most_common(8)]
            # 优先选 >=3 字符的词作为主题标签 (避免两字碎片)
            long_words = [w for w in top_words if len(w) >= 3]
            theme_words = long_words[:3] if long_words else top_words[:3]
            theme_label = "·".join(theme_words) if theme_words else "未命名主题"
            theme = f"新兴主题: {theme_label}"

            # 构建链
            nodes = [ChainNode.from_dict(it) for it in cluster_items]
            self._enrich_ts_codes(nodes)

            chain_id = f"semantic_{cluster_id}_{datetime.utcnow().strftime('%Y%m%d')}"
            chain = ClueChain(
                chain_id=chain_id,
                chain_type="semantic_cluster",
                theme=theme,
                significance=0.7,
                nodes=nodes,
            )

            # 计算链的链接
            for i in range(len(nodes) - 1):
                chain.links.append(ChainLink(
                    from_id=nodes[i].news_id,
                    to_id=nodes[i + 1].news_id,
                    link_type="semantic",
                    strength=0.5,
                    reason="语义相似",
                ))

            chains.append(chain)
            logger.info("语义主题链 #{}: {} items, theme={}",
                        cluster_id, len(cluster_items), theme[:40])

            if len(chains) >= max_chains:
                break

        logger.info("语义主题发现: {} clusters → {} new chains", n_clusters, len(chains))
        return chains

    async def build_entity_cross_chains(
        self,
        days: int = 60,
    ) -> List[ClueChain]:
        """构建实体交叉链 — 基于先导-滞后方向性检测

        1. 共现检测（现有逻辑）
        2. 时序方向性检测: A 的新闻是否系统性领先 B
        3. 关系类型推断: 供应链/竞争/政策受益
        4. 无方向性的共现丢弃
        """
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        items = await self.query.get_by_time_range(
            start=cutoff,
            end=datetime.utcnow().isoformat(),
            limit=self.config.query_limit_cross,
        )

        if len(items) < 3:
            return []

        # 富化 + 过滤
        items = self.enrich_items(items)
        items = self._filter_for_chain(items)
        if len(items) < 3:
            return []

        nodes = [ChainNode.from_dict(it) for it in items]
        self._enrich_ts_codes(nodes)

        entity_map: Dict[str, List[ChainNode]] = defaultdict(list)
        # 仅使用结构化字段，不使用标题关键词 (标题分词碎片太多噪声)
        for n in nodes:
            for c in n.mentioned_companies:
                if c not in _STOP_WORDS and len(c) >= 2:
                    entity_map[c].append(n)
            for s in n.related_sectors:
                if s not in _STOP_WORDS and len(s) >= 2:
                    entity_map[s].append(n)

        # 预计算每个实体→新闻时间的映射（用于方向性检测）
        entity_times: Dict[str, List[Tuple[str, datetime]]] = {}
        for entity, enodes in entity_map.items():
            times = []
            for n in enodes:
                if n.publish_time:
                    try:
                        times.append((n.news_id, datetime.fromisoformat(n.publish_time)))
                    except (ValueError, TypeError):
                        pass
            if times:
                entity_times[entity] = sorted(times, key=lambda x: x[1])

        chains = []
        processed: Set[str] = set()

        for entity, enodes in entity_map.items():
            if len(enodes) < 3:
                continue

            related_entities: Dict[str, int] = defaultdict(int)
            for n in enodes:
                for c in n.mentioned_companies:
                    if c != entity and c not in _STOP_WORDS and len(c) >= 2:
                        related_entities[c] += 1
                for s in n.related_sectors:
                    if s != entity and s not in _STOP_WORDS and len(s) >= 2:
                        related_entities[s] += 1

            for rel_entity, overlap in sorted(related_entities.items(), key=lambda x: -x[1]):
                if overlap < 4:
                    continue

                pair_key = tuple(sorted([entity, rel_entity]))
                if pair_key in processed:
                    continue
                processed.add(pair_key)

                rel_nodes = entity_map.get(rel_entity, [])
                common_ids = set(n.news_id for n in enodes) & set(n.news_id for n in rel_nodes)
                if not common_ids:
                    continue

                # 方向性检测
                direction_info = self._detect_lead_lag(
                    entity, rel_entity, entity_times, common_ids,
                )

                # 无方向性的共现丢弃（只是被媒体打包报道）
                if not direction_info:
                    continue

                leader, follower, avg_lead_hours, direction_ratio = direction_info

                # 关系类型推断
                rel_type = self._infer_relationship_type(
                    entity, rel_entity, enodes,
                )

                chain_nodes = []
                seen = set()
                for n in enodes:
                    if n.news_id in common_ids and n.news_id not in seen:
                        chain_nodes.append(n)
                        seen.add(n.news_id)
                chain_nodes.sort(key=lambda n: n.publish_time or "")

                links = []
                for i in range(len(chain_nodes) - 1):
                    links.append(ChainLink(
                        from_id=chain_nodes[i].news_id,
                        to_id=chain_nodes[i + 1].news_id,
                        link_type="entity",
                        strength=self.config.chain_cross_strength,
                        reason=f"{rel_type}: {leader}(先导)→{follower}(滞后)",
                    ))

                cfg = self.config
                sig = (cfg.chain_cross_base_significance
                       + cfg.chain_cross_overlap_bonus * min(overlap, cfg.chain_cross_max_overlap))

                # 用可读性更好的时间描述
                if avg_lead_hours >= 24:
                    lead_desc = f"{avg_lead_hours / 24:.1f}天"
                else:
                    lead_desc = f"{avg_lead_hours:.1f}小时"

                chain = ClueChain(
                    chain_id=f"cross_{leader}_{follower}_{datetime.utcnow().strftime('%Y%m%d%H%M')}",
                    chain_type="entity_cross",
                    theme=f"实体交叉: {leader}(先导) → {follower}(滞后)",
                    nodes=chain_nodes,
                    links=links,
                    significance=sig,
                    hidden_signals=[
                        f"{rel_type}: {leader} 平均领先 {follower} {lead_desc} (方向性比率{direction_ratio:.1f})",
                        f"{follower} 可能存在滞后反应的投资窗口",
                    ],
                )
                chains.append(chain)

        chains.sort(key=lambda c: c.significance, reverse=True)
        return chains[:20]

    # ── 实体交叉链辅助方法 ──

    def _detect_lead_lag(
        self,
        entity_a: str,
        entity_b: str,
        entity_times: Dict[str, List[Tuple[str, datetime]]],
        common_ids: Set[str],
    ) -> Optional[Tuple[str, str, float, float]]:
        """检测两个实体之间的先导-滞后关系

        返回: (先导者, 滞后者, 平均领先小时数, 方向性比率) 或 None (无方向性)
        """
        times_a = entity_times.get(entity_a, [])
        times_b = entity_times.get(entity_b, [])
        if len(times_a) < 2 or len(times_b) < 2:
            return None

        # 对每个 common_id 的时间，统计 A 领先 B 的次数
        a_leads = 0
        b_leads = 0
        lead_hours_list: List[float] = []

        # 构建时间映射
        id_time_a = {nid: t for nid, t in times_a if nid in common_ids}
        id_time_b = {nid: t for nid, t in times_b if nid in common_ids}

        # 对共同新闻之外，用最近邻匹配 A 和 B 的新闻
        all_times_a = [t for _, t in times_a]
        all_times_b = [t for _, t in times_b]

        # 简化方法: 对 A 的每条新闻，找 B 中时间最近且更晚的新闻
        for t_a in all_times_a:
            for t_b in all_times_b:
                diff_hours = (t_b - t_a).total_seconds() / 3600
                if 0 < diff_hours <= 72:  # B 在 A 之后 0-72 小时内
                    a_leads += 1
                    lead_hours_list.append(diff_hours)
                    break  # 只取最近的一个 B

        for t_b in all_times_b:
            for t_a in all_times_a:
                diff_hours = (t_a - t_b).total_seconds() / 3600
                if 0 < diff_hours <= 72:
                    b_leads += 1
                    break

        total = a_leads + b_leads
        if total == 0:
            return None

        ratio = max(a_leads, b_leads) / total

        # 方向性阈值: 需要至少 60% 的方向一致
        if ratio < 0.6:
            return None

        avg_lead = sum(lead_hours_list) / len(lead_hours_list) if lead_hours_list else 0

        if a_leads > b_leads:
            return (entity_a, entity_b, avg_lead, ratio)
        else:
            return (entity_b, entity_a, avg_lead, ratio)

    _REL_SUPPLY_WORDS = frozenset("供应 采购 原材料 上游 下游 供应商 订单 产能".split())
    _REL_COMPETE_WORDS = frozenset("竞争 对标 市占率 份额 替代 抢占".split())
    _REL_POLICY_WORDS = frozenset("政策 补贴 扶持 利好 规划 指导意见 支持".split())

    def _infer_relationship_type(
        self,
        entity_a: str,
        entity_b: str,
        nodes_a: List[ChainNode],
    ) -> str:
        """从共现新闻标题推断关系类型"""
        supply_hits = 0
        compete_hits = 0
        policy_hits = 0

        for n in nodes_a[:10]:
            title = n.title or ""
            if any(w in title for w in self._REL_SUPPLY_WORDS):
                supply_hits += 1
            if any(w in title for w in self._REL_COMPETE_WORDS):
                compete_hits += 1
            if any(w in title for w in self._REL_POLICY_WORDS):
                policy_hits += 1

        scores = {
            "供应链": supply_hits,
            "竞争": compete_hits,
            "政策受益": policy_hits,
        }
        best = max(scores, key=scores.get)
        if scores[best] >= 1:
            return best
        return "关联"

    # ========== 内部方法 ==========

    # ── 行情播报过滤关键词 ──
    # 纯行情播报标题模式，无投资分析价值
    _QUOTE_BROADCAST_PATTERNS = [
        "站上", "跌破", "收涨", "收跌", "报收", "收报",
        "涨幅扩大", "跌幅扩大", "涨超", "跌超",
        "再创历史新高", "盘中创", "日内涨", "日内跌",
        "直线拉升", "快速拉升", "直线跳水", "快速回落",
        "行情直播", "行情中", "技术分析",
        "筹码峰", "压力位", "支撑位",
    ]

    # 仅在标题中出现的行情播报短语（不在深度分析中出现的纯行情描述）
    _BROADCAST_TITLE_PHRASES = [
        # 期货行情
        "转跌", "转涨", "跌幅收窄", "涨幅收窄",
        "盘中波动", "期货涨", "期货跌",
        "WTI", "布伦特", "现货",
        "持仓更新", "持仓变化", "ETF持仓",
        "交易量统计", "成交量放大",
        # ETF 行情
        "ETF涨", "ETF跌", "ETF资金",
        "基金发行", "基金规模",
        # 短线行情描述
        "冲高回落", "探底回升",
        "窄幅震荡", "宽幅震荡",
        "主力资金", "北向资金", "资金流出", "资金流入",
        "半日主力", "半日成交",
        # 盘中简讯
        "盘中异动", "盘中涨", "盘中跌",
        "券商晨报", "券商研报", "晨会纪要",
    ]

    # 商品关键词的比喻用法排除模式
    # 黄金/白银等词常作形容词使用(黄金通道/黄金五月/白银时代),
    # 只有标题同时出现资产相关词才视为真正的商品/投资匹配
    _COMMODITY_METAPHOR_RULES = {
        "黄金": {
            "must_have": ["金价", "黄金价格", "黄金期货", "黄金ETF", "黄金T+D",
                          "购金", "售金", "金矿", "黄金股", "黄金珠宝",
                          "黄金储备", "黄金交易", "黄金需求", "黄金供给",
                          "国际金价", "上海金", "COMEX", "盎司",
                          "黄金产量", "黄金开采", "黄金冶炼"],
            "never_if": ["黄金通道", "黄金五月", "黄金时段", "黄金周", "黄金海岸",
                         "黄金时间", "黄金档", "黄金法则", "黄金窗口", "黄金交叉",
                         "黄金期", "黄金岁月", "黄金十年", "黄金一代"],
        },
        "白银": {
            "must_have": ["银价", "白银价格", "白银期货", "白银ETF", "沪银",
                          "银矿", "白银股", "国际银价", "COMEX白银",
                          "白银产量", "白银需求", "白银供给"],
            "never_if": ["白银时代", "白银利润"],
        },
        "火箭": {
            "must_have": ["商业航天", "可回收", "火箭发动机", "火箭回收",
                          "火箭发射", "火箭试飞", "运力", "卫星",
                          "蓝箭", "星河", "天兵", "力箭", "快舟",
                          "长征", "发射场", "发射台", "助推器"],
            "never_if": ["火箭弹", "火箭炮", "导弹", "也门", "胡塞",
                         "袭击", "轰炸", "拦截", "发射火箭弹"],
        },
        "矿": {
            "must_have": ["锂矿", "稀土", "锂辉石", "碳酸锂", "氢氧化锂",
                          "锂盐", "找矿", "探矿权", "采矿权",
                          "锂价", "稀土价格", "钨价", "锡价",
                          "矿权", "矿业", "矿产", "采选",
                          "矿企", "矿企", "有色金属"],
            "never_if": ["钙钛矿", "煤矿", "煤矿事故", "煤矿爆炸",
                         "煤矿智能化", "煤矿安全", "矿产纠纷",
                         "印度", "越南", "澳大利亚", "莫迪",
                         "外交", "访问", "合作开发",
                         "挖矿", "比特币", "加密货币"],
        },
    }
    # 澄清公告关键词
    _CLARIFICATION_KEYWORDS = [
        "澄清", "不涉及", "无关联", "未开展", "未有",
        "与本公司无关", "无关的", "未与",
        "无关业务", "不存在关联", "未参与",
        "未生产", "未销售", "不生产", "不销售",
        "未用于", "未供应", "无业务",
    ]

    def _filter_for_chain(
        self,
        items: List[Dict[str, Any]],
        entity: Optional[str] = None,
        entity_type: str = "keyword",
    ) -> List[Dict[str, Any]]:
        """统一过滤: 为链构建清洗数据

        过滤层级:
          1. 排除公告源 (eastmoney_notice/cninfo)
          2. 排除常规合规文件
          3. 排除澄清公告 (公司说"不涉及XX业务")
          4. 排除纯行情播报 (只有价格数字，无投资分析)
          5. 关键词相关性 (entity 非空时)
          6. 同事件多源去重 (保留最高优先级源)
        """
        if not items:
            return items

        filter_kw = getattr(self.config, 'filing_filter_keywords', [])
        kept = []

        for item in items:
            # Layer 1: 公告源直接排除
            if item.get("source") in self._FILING_SOURCES:
                continue

            title = item.get("title", "")

            # Layer 2: 常规合规文件过滤
            if filter_kw:
                if any(kw in title for kw in filter_kw):
                    continue

            # Layer 3: 澄清公告过滤 (公司说"不涉及XX"，零投资价值)
            if any(kw in title for kw in self._CLARIFICATION_KEYWORDS):
                continue

            # Layer 4: 纯行情播报过滤
            # 模式: 标题同时包含行情词和价格数字，但不包含"分析"/"解读"/"观点"等深度词
            if self._is_quote_broadcast(title):
                continue

            kept.append(item)

        # Layer 5: 关键词相关性过滤 (entity 非空时)
        if entity and kept:
            kept = self._filter_by_keyword_relevance(entity, kept, entity_type)

        # Layer 6: 同事件多源去重
        if kept:
            kept = self._dedup_same_event(kept)

        # Layer 7: 短时间窗口同源碎片去重 (同一事件30分钟内>3条只保留最佳)
        if kept:
            kept = self._dedup_time_clustered(kept, window_minutes=30)

        return kept

    def _is_quote_broadcast(self, title: str) -> bool:
        """判断是否为纯行情播报 (只有价格/行情描述，无投资分析)"""
        import re

        # 深度分析词 — 有这些的不是行情播报
        depth_words = [
            "分析", "解读", "观点", "影响", "原因", "逻辑", "策略",
            "投资", "机会", "风险", "推荐", "预期", "催化", "驱动",
            "受益", "利好", "利空", "行业", "产业链", "传导",
            "财报", "业绩", "营收", "净利", "订单", "合同",
            "政策", "发布", "批准", "获批", "中标", "签署", "合作",
            "突破", "研发", "量产", "交付", "投产", "扩产", "签约",
            "增持", "回购", "定增", "收购", "并购",
            "产能", "招标", "供货", "供应", "需求", "供给",
        ]
        if any(w in title for w in depth_words):
            return False

        # 纯行情播报模式词 (无深度分析价值)
        broadcast_phrases = [
            # 价格行情
            "站上", "跌破", "收涨", "收跌", "报收", "收报",
            "涨幅扩大", "跌幅扩大", "涨超", "跌超",
            "再创历史新高", "盘中创", "日内涨", "日内跌",
            "直线拉升", "快速拉升", "直线跳水", "快速回落",
            "行情直播", "行情中", "技术分析",
            "筹码峰", "压力位", "支撑位",
            # 板块行情播报
            "板块走强", "板块走弱", "板块拉升", "板块跳水",
            "板块大涨", "板块大跌", "板块活跃", "板块异动",
            "全线大涨", "全线大跌", "全线飘红", "全线飘绿",
            "集体涨停", "批量涨停", "掀涨停潮",
            "ETF领涨", "ETF领跌", "ETF涨幅", "ETF跌幅",
            # 指数行情
            "指数涨", "指数跌", "指数高开", "指数低开",
            "高开高走", "高开低走", "低开高走", "低开低走",
            "午后拉升", "尾盘拉升", "尾盘跳水", "午后异动",
            "两市成交", "成交额", "成交破",
            # 通用的"行情快讯"标题
            "行情播报", "午评", "收评", "盘前", "盘后快讯",
            "异动拉升", "异动下跌", "短线拉升", "短线跳水",
            # 交易所行情播报
            "金交所", "上金所", "上海黄金交易所",
            "开盘涨", "开盘跌", "开盘价",
            # 涨跌停/资金流行情
            "涨停板", "跌停板", "封板", "开板",
            "主力净流入", "主力净流出", "净流入", "净流出",
            "龙虎榜", "机构买入", "机构卖出",
            # 趋势描述行情 (无增量信息)
            "震荡上扬", "震荡下行", "震荡走高", "震荡走低",
            "持续上行", "持续下行", "持续走高", "持续走低",
            "股价创", "股价突破", "创阶段",
            "板块回调", "板块分化",
            # 涨跌连板
            "连阳", "连阴", "连板", "三连板", "四连板",
            "飙升", "暴涨", "暴跌", "闪崩",
            # 概念股行情
            "概念股拉升", "概念股活跃", "概念股大涨", "概念股大跌",
            "概念股掀", "概念股批量", "概念股涨停", "概念股走强",
        ]
        if any(p in title for p in broadcast_phrases):
            return True

        # 仅标题中的行情短语 (不在深度分析中出现)
        if any(p in title for p in self._BROADCAST_TITLE_PHRASES):
            return True

        # 典型行情播报: "XX涨X%"/"XX跌X%"
        if re.search(r'[涨跌]\d+\.?\d*[％%]', title):
            return True

        # 有价格数字 + 无深度词 = 行情播报
        has_price = bool(re.search(r'\d+\.?\d*[％%美元元点]', title))
        if has_price and not any(w in title for w in depth_words):
            # 但排除包含公司名/产品名的标题 (可能是业绩数据)
            company_indicators = ["公司", "集团", "股份", "科技", "发布", "公告"]
            if not any(w in title for w in company_indicators):
                return True

        return False

    @staticmethod
    def _dedup_same_event(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """同事件多源去重 — 保留最高优先级源

        判断规则: 标题相似度>80% 视为同一事件，只保留 source_priority 最小的
        """
        if len(items) <= 1:
            return items

        result = []
        for item in items:
            title = item.get("title", "")
            merged = False
            for existing in result:
                existing_title = existing.get("title", "")
                # 简单相似度: 较短标题被较长标题包含的字符比例
                shorter, longer = (title, existing_title) if len(title) <= len(existing_title) else (existing_title, title)
                if len(shorter) < 6:
                    continue
                # 计算重叠字符数
                overlap = sum(1 for c in shorter if c in longer)
                similarity = overlap / len(shorter) if shorter else 0
                if similarity > 0.8:
                    # 同一事件: 保留优先级更高的
                    if (item.get("source_priority", 5) < existing.get("source_priority", 5)):
                        result.remove(existing)
                        result.append(item)
                    merged = True
                    break
            if not merged:
                result.append(item)

        return result

    @staticmethod
    def _dedup_time_clustered(items: List[Dict[str, Any]], window_minutes: int = 30) -> List[Dict[str, Any]]:
        """短时间窗口内的同源新闻聚合去重

        问题: 同一事件(如美联储官员讲话)在30分钟内被同一源拆成20+条碎片新闻,
              导致链中充斥重复信息。
        策略: 对同一 source, 在 window_minutes 内的新闻,
              保留 source_priority 最高的那条, 其余去除。
        """
        if len(items) <= 1:
            return items

        from itertools import groupby as _groupby

        # 按 source 分组
        by_source: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for item in items:
            by_source[item.get("source", "")].append(item)

        result = []
        for source, group in by_source.items():
            if len(group) <= 2:
                result.extend(group)
                continue

            # 按时间排序
            def _sort_key(x):
                try:
                    return datetime.fromisoformat(x.get("publish_time", "")[:19])
                except (ValueError, TypeError):
                    return datetime.min

            group.sort(key=_sort_key)

            # 滑动窗口: 收集 window_minutes 内的新闻, 只保留 priority 最好的
            i = 0
            while i < len(group):
                cluster = [group[i]]
                try:
                    t_start = datetime.fromisoformat(group[i].get("publish_time", "")[:19])
                    j = i + 1
                    while j < len(group):
                        try:
                            t_j = datetime.fromisoformat(group[j].get("publish_time", "")[:19])
                            if (t_j - t_start).total_seconds() <= window_minutes * 60:
                                cluster.append(group[j])
                                j += 1
                            else:
                                break
                        except (ValueError, TypeError):
                            j += 1

                    if len(cluster) > 3:
                        # 保留 priority 最好的 (数字越小越好)
                        best = min(cluster, key=lambda x: x.get("source_priority", 5))
                        result.append(best)
                        i = j
                    else:
                        result.append(group[i])
                        i += 1
                except (ValueError, TypeError):
                    result.append(group[i])
                    i += 1

        return result

    def _filter_by_keyword_relevance(
        self,
        entity: str,
        items: List[Dict[str, Any]],
        entity_type: str,
    ) -> List[Dict[str, Any]]:
        """关键词相关性过滤 — 用词边界匹配，避免子串误匹配

        核心问题: 对 "AI" 用简单的 `in` 匹配会导致 "AIMCU"/"Chain" 误匹配。
        改用正则词边界 \b 或字符类型边界检测。
        """
        import re

        entity_lower = entity.lower()

        # 收集关键词别名 (从 industry_alias 中查找)
        keywords = {entity_lower}
        for industry, alias_list in self.config.industry_alias.items():
            if entity in alias_list or entity == industry:
                keywords.update(a.lower() for a in alias_list)

        # 为每个关键词构建匹配正则
        # 对纯英文短词 (< 4 字符): 用非ASCII字母边界匹配，避免 "AIMCU"/"Chain" 误匹配
        #   匹配条件: 关键词前面不是大写字母, 后面不是任何字母
        #   这样 "AI" 匹配 "推动AI价格"(✓) "OpenAI发布"(✓) 但不匹配 "AIMCU"(✗) "Chain"(✗)
        # 对中文/长词: 直接用 in 匹配
        strict_patterns: List["re.Pattern"] = []
        loose_keywords: List[str] = []

        for kw in keywords:
            if kw.isascii() and len(kw) <= 4:
                # 关键词前面是小写字母(如OpenAI的'n') → 允许
                # 关键词前面是大写字母(如AIMCU的'A') → 拒绝
                # 关键词后面是任何字母 → 拒绝
                pattern = r'(?:(?<=[a-z])|(?<![a-zA-Z]))' + re.escape(kw) + r'(?![a-zA-Z])'
                strict_patterns.append(re.compile(pattern, re.IGNORECASE))
            else:
                loose_keywords.append(kw)

        kept = []
        for item in items:
            title = item.get("title", "")

            # 0. 商品比喻用法过滤 (如"黄金通道"不是投资新闻)
            if entity in self._COMMODITY_METAPHOR_RULES:
                rules = self._COMMODITY_METAPHOR_RULES[entity]
                # 排除明确比喻用法的标题
                if any(p in title for p in rules["never_if"]):
                    continue
                # 必须包含至少一个资产相关词
                if not any(p in title for p in rules["must_have"]):
                    # 但也检查结构化字段
                    companies = " ".join(item.get("mentioned_companies") or [])
                    sectors = " ".join(item.get("related_sectors") or [])
                    ts_codes = item.get("ts_codes") or []
                    if not any(p in companies or p in sectors for p in rules["must_have"]):
                        if not ts_codes:
                            continue

            # 1. 严格匹配: 英文短词必须词边界匹配
            if any(p.search(title) for p in strict_patterns):
                kept.append(item)
                continue

            # 2. 宽松匹配: 中文/长词直接 in 匹配
            title_lower = title.lower()
            if any(kw in title_lower for kw in loose_keywords):
                kept.append(item)
                continue

            # 3. 结构化字段匹配
            companies = " ".join(item.get("mentioned_companies") or []).lower()
            sectors = " ".join(item.get("related_sectors") or []).lower()
            if entity_lower in companies or entity_lower in sectors:
                kept.append(item)
                continue

            # 4. ts_codes 字段匹配 (仅 company 类型)
            if entity_type == "company":
                ts_codes = item.get("ts_codes") or []
                if any(entity in tc for tc in ts_codes):
                    kept.append(item)

        return kept

    def _detect_sentiment_shifts(self, nodes: List[ChainNode]) -> List[str]:
        """检测情绪漂移和沉默间隔信号

        1. 滑动窗口情绪分布变化检测: 窗口内极性比例突变才是真信号
        2. 沉默间隔检测: 两个相邻节点之间有 ≥7 天空白，暗示事件中断或重启
        """
        signals: List[str] = []

        # ── 沉默间隔检测 ──
        sorted_nodes = sorted(
            [n for n in nodes if n.publish_time],
            key=lambda n: n.publish_time or "",
        )
        silence_threshold_days = 7
        for i in range(len(sorted_nodes) - 1):
            try:
                t1 = datetime.fromisoformat(sorted_nodes[i].publish_time)
                t2 = datetime.fromisoformat(sorted_nodes[i + 1].publish_time)
                gap_days = (t2 - t1).days
                if gap_days >= silence_threshold_days:
                    signals.append(
                        f"沉默间隔: {gap_days}天无相关报道 "
                        f"({t1.strftime('%m-%d')}→{t2.strftime('%m-%d')}), "
                        f"事件可能中断后重启"
                    )
            except (ValueError, TypeError):
                pass

        # ── 滑动窗口情绪漂移检测 ──
        sentiments = [
            (n.publish_time, n.sentiment)
            for n in sorted_nodes if n.sentiment
        ]
        if len(sentiments) >= 4:
            window_size = max(3, len(sentiments) // 4)
            step = max(1, window_size // 2)
            prev_polar_rate = -1.0
            for start in range(0, len(sentiments) - window_size + 1, step):
                window = sentiments[start:start + window_size]
                polar_count = sum(
                    1 for _, s in window if s in ("positive", "negative")
                )
                polar_rate = polar_count / len(window)
                if prev_polar_rate >= 0 and abs(polar_rate - prev_polar_rate) > 0.4:
                    direction = "加剧" if polar_rate > prev_polar_rate else "缓和"
                    w_start = window[0][0][:10] if window[0][0] else "?"
                    w_end = window[-1][0][:10] if window[-1][0] else "?"
                    signals.append(
                        f"情绪{direction}: 极性比例 {prev_polar_rate:.0%}→{polar_rate:.0%} "
                        f"({w_start}~{w_end})"
                    )
                prev_polar_rate = polar_rate

        # 保留最重要的信号，按优先级: 硬反转 > 沉默间隔 > 缓变
        # 先标注硬反转 (positive→negative 这类关键模式)
        for i in range(len(sentiments) - 1):
            _, s1 = sentiments[i]
            _, s2 = sentiments[i + 1]
            if s1 == "positive" and s2 == "negative":
                signals.insert(0, "情绪反转: positive→negative (利好转利空，重大反转信号)")
            elif s1 == "neutral" and s2 in ("positive", "negative"):
                signals.insert(0, f"情绪激活: neutral→{s2} (从沉默到表态，值得关注)")

        # 去重并截断
        seen = set()
        unique = []
        for s in signals:
            if s not in seen:
                seen.add(s)
                unique.append(s)
        return unique[:5]

    def _detect_propagation_signals(
        self,
        sector_timeline: List[Tuple[str, str, List[ChainNode]]],
    ) -> List[str]:
        signals = []
        if len(sector_timeline) < 2:
            return signals

        for i in range(len(sector_timeline) - 1):
            t1, sector_a, _ = sector_timeline[i]
            t2, sector_b, _ = sector_timeline[i + 1]

            if t1 and t2:
                t1_date = t1[:10]
                t2_date = t2[:10] if len(t2) >= 10 else t2
                # 同日不算传导，跳过
                if t1_date == t2_date:
                    continue
                signals.append(
                    f"传导路径: {sector_a}({t1_date}) → {sector_b}({t2_date}), "
                    f"{sector_b}可能存在滞后反应机会"
                )

        return signals[:5]

    def _detect_entity_bursts(
        self,
        nodes: List[ChainNode],
        window_days: int = 30,
    ) -> Dict[str, List[ChainNode]]:
        """检测实体/主题爆发 — 基于相对密度 (当前密度 / 基线密度)

        优先用实体字段，为空时从标题关键词提取。
        基线密度 = 实体在整个时间窗口内的日均密度 (条/小时)。
        爆发判定: relative_density >= chain_burst_density_threshold (默认 0.5,
          但此时含义变为"当前密度是基线的 0.5 倍以上"这一阈值,
          实际配置中建议 >= 3.0 表示"密度是平时 3 倍")。
        """
        entity_nodes: Dict[str, List[ChainNode]] = defaultdict(list)
        for n in nodes:
            has_entity = False
            for c in n.mentioned_companies:
                entity_nodes[c].append(n)
                has_entity = True
            for s in n.related_sectors:
                entity_nodes[s].append(n)
                has_entity = True
            if not has_entity and n.title:
                for kw in _extract_title_keywords(n.title):
                    # 仅接受 3+ 字符的标题关键词 (2字符碎片太多噪声)
                    if len(kw) >= 3:
                        entity_nodes[kw].append(n)

        bursts = {}
        window_hours = max(window_days * 24, 1)
        cfg = self.config

        for entity, enodes in entity_nodes.items():
            if len(enodes) < cfg.min_cluster_size:
                continue
            times: List[datetime] = []
            for n in enodes:
                if n.publish_time:
                    pt = n.publish_time
                    if isinstance(pt, str):
                        pt = datetime.fromisoformat(pt)
                    times.append(pt)
            if len(times) < 2:
                continue

            times.sort()
            span_hours = (times[-1] - times[0]).total_seconds() / 3600

            # 基线密度: 实体在整个时间窗口内的平均密度
            baseline_density = len(times) / window_hours
            # 实际爆发密度: 聚集时间段内的密度
            actual_density = len(times) / max(span_hours, 1)

            # 相对密度: 当前密度是基线的多少倍
            # 避免除零: 基线极低时用固定阈值兜底
            if baseline_density > 0.001:
                relative_density = actual_density / baseline_density
            else:
                relative_density = actual_density  # 冷门实体直接用绝对密度

            if relative_density >= cfg.chain_burst_density_threshold:
                bursts[entity] = enodes

        return bursts

    def _calc_significance(self, nodes: List[ChainNode]) -> float:
        """计算线索链重要性评分"""
        if not nodes:
            return 0.0
        cfg = self.config
        score = 0.0

        max_priority = max(n.source_priority for n in nodes)
        score += max_priority / cfg.chain_max_priority_divisor * cfg.chain_weight_source_priority

        sentiments = [n.sentiment for n in nodes if n.sentiment]
        if sentiments:
            polar_count = sum(1 for s in sentiments if s in ("positive", "negative"))
            score += (polar_count / len(sentiments)) * cfg.chain_weight_sentiment_polarity

        urgent_count = sum(1 for n in nodes if n.urgency in ("urgent", "important"))
        score += min(urgent_count / len(nodes), 1.0) * cfg.chain_weight_urgency

        score += min(len(nodes) / cfg.chain_node_count_normalizer, 1.0) * cfg.chain_weight_node_count

        return min(score, 1.0)
