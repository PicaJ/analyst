"""
数据富化层 — 补全链节点中为空的结构化字段

分层策略:
  Tier 1: 规则匹配 (ts_codes → 公司名+行业, 免费毫秒级)
  Tier 2: LLM 批量富化 (情绪/板块/公司, 仅处理链中实际使用的节点)
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from .config import AnalystConfig


# ── 情绪判定词表 (Tier 1 规则兜底, 仅作为 fallback) ──
_POSITIVE_WORDS = frozenset(
    "涨停 大涨 暴涨 新高 利好 增长 突破 上涨 反弹 超预期 "
    "获批 中标 合作 扩产 订单 利润增 营收增 首次覆盖 上调 "
    "增持 回购 翻倍 飙升 强势 井喷".split()
)

_NEGATIVE_WORDS = frozenset(
    "跌停 大跌 暴跌 新低 利空 下降 亏损 违规 处罚 减持 "
    "退市 风险 预警 下滑 不及预期 破发 腰斩 崩盘 暴雷 "
    "立案 警示 停牌 下调".split()
)

# ── LLM 富化 Prompt ──
_ENRICH_SYSTEM_PROMPT = """你是一个金融新闻结构化信息提取器。对每条新闻，提取以下字段并严格按 JSON 数组输出:

对每条新闻提取:
- "sentiment": 情绪，取值 positive / negative / neutral
- "mentioned_companies": 涉及的 A 股公司全称列表 (如 ["宁德时代", "比亚迪"])
- "related_sectors": 所属行业/板块列表 (如 ["新能源", "汽车"])

规则:
1. sentiment 基于新闻对市场和个股的影响判断，不是标题字面情绪
2. mentioned_companies 只提取明确提到的 A 股上市公司，不猜测
3. related_sectors 从以下行业中选取: 新能源, 半导体, 汽车, 银行, 保险, 证券, 医药, 白酒, 房地产, 煤炭, 钢铁, 有色金属, 石油, 电力, 军工, 消费电子, 家电, 食品, 纺织, 化工, 建材, 机械, 通信, 传媒, 计算机, 期货, 航运, 航空
4. 如果新闻与投资无关，sentiment=neutral, mentioned_companies=[], related_sectors=[]

输出格式: JSON 数组，每个元素对应一条输入新闻，顺序一致
[{"sentiment":"...","mentioned_companies":[...],"related_sectors":[...]}, ...]"""

_ENRICH_USER_TEMPLATE = """请提取以下 {count} 条新闻的结构化信息:

{news_list}

请严格按 JSON 数组输出，顺序与输入一致。"""


class NewsEnricher:
    """数据富化器"""

    def __init__(self, config: AnalystConfig):
        self.config = config
        self._ts_code_map: Optional[Dict[str, str]] = None
        self._ts_code_industry: Optional[Dict[str, str]] = None

    # ========== 公开接口 ==========

    def enrich_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Tier 1: 规则富化 (全量 items, 免费)"""
        code_map = self._load_ts_code_map()
        enriched = 0
        for item in items:
            changed = False

            # mentioned_companies: ts_codes → 公司名
            if not item.get("mentioned_companies"):
                companies = self._companies_from_ts_codes(item, code_map)
                if companies:
                    item["mentioned_companies"] = companies
                    changed = True

            # related_sectors: ts_codes → 行业 + 标题匹配
            if not item.get("related_sectors"):
                sectors = self._sectors_from_ts_codes(item)
                if not sectors:
                    sectors = self._sectors_from_title(item)
                if sectors:
                    item["related_sectors"] = sectors
                    changed = True

            # sentiment: 简单关键词兜底
            if not item.get("sentiment"):
                sentiment = self._sentiment_from_keywords(item)
                if sentiment:
                    item["sentiment"] = sentiment
                    changed = True

            # urgency: source_priority + 关键词
            if not item.get("urgency") or item.get("urgency") == "":
                item["urgency"] = self._urgency_from_source(item)
                changed = True

            if changed:
                enriched += 1

        if enriched:
            logger.debug("Tier1 enriched {}/{} items", enriched, len(items))
        return items

    async def enrich_chain_nodes(
        self,
        nodes: List[Any],
        llm_client: Any,
    ) -> None:
        """Tier 2: LLM 批量富化链节点 (仅处理字段为空的节点)

        对每条链中缺失 sentiment/companies/sectors 的节点,
        分批调用 LLM 提取, 原地修改 nodes。
        """
        need_enrich = []
        for n in nodes:
            if not n.sentiment and not n.mentioned_companies and not n.related_sectors:
                need_enrich.append(n)
        if not need_enrich:
            return

        batch_size = 30
        total_enriched = 0
        for start in range(0, len(need_enrich), batch_size):
            batch = need_enrich[start:start + batch_size]
            try:
                results = await self._llm_enrich_batch(batch, llm_client)
                for node, result in zip(batch, results):
                    if result.get("sentiment"):
                        node.sentiment = result["sentiment"]
                    if result.get("mentioned_companies"):
                        node.mentioned_companies = result["mentioned_companies"]
                    if result.get("related_sectors"):
                        node.related_sectors = result["related_sectors"]
                    total_enriched += 1
            except Exception as e:
                logger.warning("LLM enrich batch failed (start={}): {}", start, e)
                break

        if total_enriched:
            logger.info("Tier2 LLM enriched {}/{} nodes",
                        total_enriched, len(need_enrich))

    # ========== Tier 1: 规则方法 ==========

    def _companies_from_ts_codes(
        self, item: Dict, code_map: Dict[str, str],
    ) -> List[str]:
        ts_codes = item.get("ts_codes") or []
        if isinstance(ts_codes, str):
            try:
                ts_codes = json.loads(ts_codes)
            except (json.JSONDecodeError, TypeError):
                return []
        companies = []
        for code in ts_codes:
            name = code_map.get(code)
            if name:
                companies.append(name)
        return companies

    def _sectors_from_ts_codes(self, item: Dict) -> List[str]:
        ts_codes = item.get("ts_codes") or []
        if isinstance(ts_codes, str):
            try:
                ts_codes = json.loads(ts_codes)
            except (json.JSONDecodeError, TypeError):
                return []
        industry_map = self._load_ts_code_industry()
        sectors = []
        for code in ts_codes:
            industry = industry_map.get(code)
            if industry and industry not in sectors:
                sectors.append(industry)
        return sectors

    def _sectors_from_title(self, item: Dict) -> List[str]:
        title = item.get("title", "")
        if not title:
            return []
        matched = []
        for industry, aliases in self.config.industry_alias.items():
            for alias in aliases:
                if alias in title:
                    if industry not in matched:
                        matched.append(industry)
                    break
        return matched

    def _sentiment_from_keywords(self, item: Dict) -> Optional[str]:
        title = (item.get("title") or "") + " " + (item.get("content") or "")
        pos = sum(1 for w in _POSITIVE_WORDS if w in title)
        neg = sum(1 for w in _NEGATIVE_WORDS if w in title)
        if pos > neg:
            return "positive"
        if neg > pos:
            return "negative"
        if pos > 0 or neg > 0:
            return "neutral"
        return None

    def _urgency_from_source(self, item: Dict) -> str:
        priority = item.get("source_priority", 3)
        title = item.get("title", "")
        if priority <= 1 or "突发" in title or "紧急" in title:
            return "urgent"
        if priority <= 2 or "重要" in title:
            return "important"
        return "normal"

    # ========== Tier 2: LLM 方法 ==========

    async def _llm_enrich_batch(
        self, nodes: List[Any], llm_client: Any,
    ) -> List[Dict[str, Any]]:
        news_lines = []
        for i, n in enumerate(nodes, 1):
            title = n.title or ""
            content = ""
            if hasattr(n, "_raw_content"):
                content = n._raw_content[:200]
            line = f"[{i}] {title}"
            if content:
                line += f" | {content}"
            news_lines.append(line)

        user_prompt = _ENRICH_USER_TEMPLATE.format(
            count=len(nodes),
            news_list="\n".join(news_lines),
        )

        raw = await llm_client.complete(_ENRICH_SYSTEM_PROMPT, user_prompt)
        return self._parse_enrich_response(raw, len(nodes))

    def _parse_enrich_response(
        self, raw: str, expected_count: int,
    ) -> List[Dict[str, Any]]:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines[1:] if not (l.strip() == "```" and l is lines[-1])]
            text = "\n".join(lines)
            if text.endswith("```"):
                text = text[:-3]

        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start:end])
                if isinstance(parsed, list):
                    while len(parsed) < expected_count:
                        parsed.append({})
                    return parsed[:expected_count]
            except json.JSONDecodeError:
                pass

        return [{}] * expected_count

    # ========== 缓存 ==========

    def _load_ts_code_map(self) -> Dict[str, str]:
        if self._ts_code_map is not None:
            return self._ts_code_map

        cache_path = Path(self.config.data_dir) / "cache" / "ts_code_name.json"
        if cache_path.exists():
            try:
                self._ts_code_map = json.loads(cache_path.read_text(encoding="utf-8"))
                logger.debug("Loaded ts_code map: {} entries", len(self._ts_code_map))
                return self._ts_code_map
            except Exception as e:
                logger.warning("Failed to load ts_code map: {}", e)

        self._ts_code_map = self._build_ts_code_map(cache_path)
        return self._ts_code_map

    def _build_ts_code_map(self, cache_path: Path) -> Dict[str, str]:
        code_map: Dict[str, str] = {}
        try:
            import sqlite3
            conn = sqlite3.connect(self.config.db_path)
            rows = conn.execute(
                "SELECT ts_codes, title FROM news_items "
                "WHERE ts_codes IS NOT NULL AND ts_codes != '[]' "
                "LIMIT 100000"
            ).fetchall()
            conn.close()

            import re
            for raw_codes, title in rows:
                try:
                    codes = json.loads(raw_codes) if isinstance(raw_codes, str) else raw_codes
                except (json.JSONDecodeError, TypeError):
                    continue
                for code in codes:
                    if code in code_map:
                        continue
                    match = re.match(r"^([^:：]+?)[:：]", title)
                    if match:
                        name = match.group(1).strip()
                        if 2 <= len(name) <= 10 and not name.startswith("关于"):
                            code_map[code] = name

            if code_map:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps(code_map, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.info("Built ts_code map: {} entries → {}",
                            len(code_map), cache_path)
        except Exception as e:
            logger.warning("Failed to build ts_code map: {}", e)

        return code_map

    def _load_ts_code_industry(self) -> Dict[str, str]:
        if self._ts_code_industry is not None:
            return self._ts_code_industry

        cache_path = Path(self.config.data_dir) / "cache" / "ts_code_industry.json"
        if cache_path.exists():
            try:
                self._ts_code_industry = json.loads(
                    cache_path.read_text(encoding="utf-8")
                )
                return self._ts_code_industry
            except Exception:
                pass

        self._ts_code_industry = {}
        return self._ts_code_industry
