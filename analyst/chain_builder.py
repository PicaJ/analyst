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
    "显示 根据 收到 事项 是否 需要 提供".split()
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

    def __init__(self, config: AnalystConfig):
        self.config = config
        self.query = NewsQuery(config)

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

        if len(items) < 2:
            return []

        nodes = [ChainNode.from_dict(it) for it in items]
        links = []

        nodes.sort(key=lambda n: n.publish_time or "")
        for i in range(len(nodes) - 1):
            n1, n2 = nodes[i], nodes[i + 1]
            links.append(ChainLink(
                from_id=n1.news_id,
                to_id=n2.news_id,
                link_type="temporal",
                strength=self.config.chain_timeline_strength,
                reason=f"同一{entity_type}({entity})的时间演变",
            ))

        sentiment_shifts = self._detect_sentiment_shifts(nodes)

        chain = ClueChain(
            chain_id=f"timeline_{entity}_{datetime.utcnow().strftime('%Y%m%d')}",
            chain_type="timeline",
            theme=f"{entity} 事件时间线 ({days}天)",
            nodes=nodes,
            links=links,
            significance=self._calc_significance(nodes),
            hidden_signals=sentiment_shifts,
        )
        return [chain]

    async def build_sector_propagation_chain(
        self,
        policy_keywords: List[str],
        days: int = 90,
    ) -> List[ClueChain]:
        """构建板块传导链 — 政策/事件从上游传导到下游行业

        混合检索 + SQLite 关键词匹配合并，按 ID 去重:
          - search_hybrid: FAISS 语义相关 + FTS5 关键词 → 高召回
          - get_timeline:  SQLite 关键词 LIKE 匹配       → 不漏
        search_hybrid 失败时降级为纯 SQLite 查询。
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

        if len(items) < 3:
            return []

        nodes = [ChainNode.from_dict(it) for it in items]
        nodes.sort(key=lambda n: n.publish_time or "")

        sector_groups: Dict[str, List[ChainNode]] = defaultdict(list)
        for n in nodes:
            for s in n.related_sectors:
                sector_groups[s].append(n)
            if not n.related_sectors:
                sector_groups["未分类"].append(n)

        sector_timeline = []
        for sector, sector_nodes in sector_groups.items():
            first_time = min(n.publish_time for n in sector_nodes if n.publish_time)
            sector_timeline.append((first_time, sector, sector_nodes))
        sector_timeline.sort()

        links = []
        for i in range(len(sector_timeline) - 1):
            _, sector_a, nodes_a = sector_timeline[i]
            _, sector_b, nodes_b = sector_timeline[i + 1]
            latest_a = max(nodes_a, key=lambda n: n.publish_time or "")
            earliest_b = min(nodes_b, key=lambda n: n.publish_time or "")
            links.append(ChainLink(
                from_id=latest_a.news_id,
                to_id=earliest_b.news_id,
                link_type="sector",
                strength=self.config.chain_sector_strength,
                reason=f"板块传导: {sector_a} → {sector_b}",
            ))

        propagation_signals = self._detect_propagation_signals(sector_timeline)

        chain = ClueChain(
            chain_id=f"sector_prop_{datetime.utcnow().strftime('%Y%m%d%H%M')}",
            chain_type="sector_propagation",
            theme=f"板块传导: {'/'.join(policy_keywords[:3])}",
            nodes=nodes,
            links=links,
            significance=self._calc_significance(nodes),
            hidden_signals=propagation_signals,
        )
        return [chain]

    async def build_anomaly_chains(
        self,
        days: int = 30,
    ) -> List[ClueChain]:
        """构建异常链 — 情绪/频率异常，可能暗示未公开信息"""
        items = await self.query.get_urgent(
            days=days,
            limit=self.config.query_limit_urgent,
        )
        if not items:
            return []

        nodes = [ChainNode.from_dict(it) for it in items]
        nodes.sort(key=lambda n: n.publish_time or "")

        entity_bursts = self._detect_entity_bursts(nodes)

        chains = []
        for entity, burst_nodes in entity_bursts.items():
            if len(burst_nodes) < self.config.min_cluster_size:
                continue

            links = []
            for i in range(len(burst_nodes) - 1):
                links.append(ChainLink(
                    from_id=burst_nodes[i].news_id,
                    to_id=burst_nodes[i + 1].news_id,
                    link_type="anomaly",
                    strength=self.config.chain_anomaly_strength,
                    reason=f"异常聚集: {entity} 在短时间内出现{len(burst_nodes)}条相关消息",
                ))

            chain = ClueChain(
                chain_id=f"anomaly_{entity}_{datetime.utcnow().strftime('%Y%m%d%H%M')}",
                chain_type="anomaly",
                theme=f"异常信号: {entity} 消息聚集",
                nodes=burst_nodes,
                links=links,
                significance=self.config.chain_anomaly_significance,
                hidden_signals=[
                    f"{entity} 在{days}天内出现{len(burst_nodes)}条消息，密度异常",
                    "可能存在未被市场充分反映的信息",
                ],
            )
            chains.append(chain)

        return chains

    async def build_entity_cross_chains(
        self,
        days: int = 60,
    ) -> List[ClueChain]:
        """构建实体交叉链 — 不同实体/主题通过共同关联被串联

        优先用实体字段，为空时从标题关键词提取。
        """
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        items = await self.query.get_by_time_range(
            start=cutoff,
            end=datetime.utcnow().isoformat(),
            limit=self.config.query_limit_cross,
        )

        if len(items) < 3:
            return []

        nodes = [ChainNode.from_dict(it) for it in items]

        entity_map: Dict[str, List[ChainNode]] = defaultdict(list)
        for n in nodes:
            has_entity = False
            for c in n.mentioned_companies:
                entity_map[c].append(n)
                has_entity = True
            for s in n.related_sectors:
                entity_map[s].append(n)
                has_entity = True
            # 实体字段为空时从标题提取
            if not has_entity and n.title:
                for kw in _extract_title_keywords(n.title):
                    entity_map[kw].append(n)

        chains = []
        processed: Set[str] = set()

        for entity, enodes in entity_map.items():
            if len(enodes) < 2:
                continue

            related_entities: Dict[str, int] = defaultdict(int)
            for n in enodes:
                for c in n.mentioned_companies:
                    if c != entity:
                        related_entities[c] += 1
                for s in n.related_sectors:
                    if s != entity:
                        related_entities[s] += 1
                # 标题关键词交叉
                if n.title:
                    for kw in _extract_title_keywords(n.title):
                        if kw != entity:
                            related_entities[kw] += 1

            for rel_entity, overlap in sorted(related_entities.items(), key=lambda x: -x[1]):
                if overlap < 2:
                    continue

                pair_key = tuple(sorted([entity, rel_entity]))
                if pair_key in processed:
                    continue
                processed.add(pair_key)

                rel_nodes = entity_map.get(rel_entity, [])
                common_ids = set(n.news_id for n in enodes) & set(n.news_id for n in rel_nodes)
                if not common_ids:
                    continue

                chain_nodes = [n for n in enodes if n.news_id in common_ids]
                chain_nodes.sort(key=lambda n: n.publish_time or "")

                links = []
                for i in range(len(chain_nodes) - 1):
                    links.append(ChainLink(
                        from_id=chain_nodes[i].news_id,
                        to_id=chain_nodes[i + 1].news_id,
                        link_type="entity",
                        strength=self.config.chain_cross_strength,
                        reason=f"实体交叉: {entity} ∩ {rel_entity}",
                    ))

                cfg = self.config
                sig = (cfg.chain_cross_base_significance
                       + cfg.chain_cross_overlap_bonus * min(overlap, cfg.chain_cross_max_overlap))

                chain = ClueChain(
                    chain_id=f"cross_{entity}_{rel_entity}_{datetime.utcnow().strftime('%Y%m%d%H%M')}",
                    chain_type="entity_cross",
                    theme=f"实体交叉: {entity} × {rel_entity}",
                    nodes=chain_nodes,
                    links=links,
                    significance=sig,
                    hidden_signals=[
                        f"{entity} 与 {rel_entity} 出现{overlap}次共同报道",
                        "两个实体的关联可能尚未被市场充分定价",
                    ],
                )
                chains.append(chain)

        chains.sort(key=lambda c: c.significance, reverse=True)
        return chains[:10]

    # ========== 内部方法 ==========

    def _detect_sentiment_shifts(self, nodes: List[ChainNode]) -> List[str]:
        signals = []
        sentiments = [(n.publish_time, n.sentiment, n.title[:40]) for n in nodes if n.sentiment]

        for i in range(len(sentiments) - 1):
            _, s1, _ = sentiments[i]
            _, s2, title2 = sentiments[i + 1]
            if s1 != s2 and s1 and s2:
                shift = f"情绪转变: {s1}→{s2}"
                if s1 == "neutral" and s2 in ("positive", "negative"):
                    shift += " (从沉默到表态，值得关注)"
                elif s1 == "positive" and s2 == "negative":
                    shift += " (利好转利空，重大反转信号)"
                signals.append(shift)

        return signals[:5]

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
                time_gap = t2[:10] if len(t2) >= 10 else t2
                signals.append(
                    f"传导路径: {sector_a}({t1[:10]}) → {sector_b}({time_gap}), "
                    f"{sector_b}可能存在滞后反应机会"
                )

        return signals[:5]

    def _detect_entity_bursts(self, nodes: List[ChainNode]) -> Dict[str, List[ChainNode]]:
        """检测实体/主题爆发 (优先用实体字段，为空时从标题关键词提取)"""
        entity_nodes: Dict[str, List[ChainNode]] = defaultdict(list)
        for n in nodes:
            # 优先用实体字段
            has_entity = False
            for c in n.mentioned_companies:
                entity_nodes[c].append(n)
                has_entity = True
            for s in n.related_sectors:
                entity_nodes[s].append(n)
                has_entity = True
            # 实体字段为空时，从标题提取关键词
            if not has_entity and n.title:
                for kw in _extract_title_keywords(n.title):
                    entity_nodes[kw].append(n)

        bursts = {}
        for entity, enodes in entity_nodes.items():
            if len(enodes) < self.config.min_cluster_size:
                continue
            times = []
            for n in enodes:
                if n.publish_time:
                    pt = n.publish_time
                    if isinstance(pt, str):
                        pt = datetime.fromisoformat(pt)
                    times.append(pt)
            if len(times) >= 2:
                times.sort()
                span_hours = (times[-1] - times[0]).total_seconds() / 3600
                density = len(times) / max(span_hours, 1)
                if density >= self.config.chain_burst_density_threshold:
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
