"""
LLM 驱动的洞察引擎

将线索链交给 LLM 分析:
  - 推导隐蔽的因果逻辑
  - 发现市场尚未充分反映的信息
  - 评估潜在风险和机会
  - 生成结构化分析报告

支持多种 LLM 提供商:
  - openai (含 OpenAI 兼容的 deepseek / 硅基流动 等)
  - anthropic
  - ollama (本地)
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from .config import AnalystConfig
from .chain_builder import ChainNode, ClueChain


SYSTEM_PROMPT = """你是一位专注于 A 股市场的资深投资分析师，擅长从新闻中挖掘具体可执行的投资线索。

核心原则:
1. 投资导向: 只分析与 A 股投资直接相关的信息，排除纯政治/社会/文化新闻
2. 具体标的: 每条可操作建议必须指向具体的 A 股股票代码（如 000333.SZ、601318.SH）
3. 因果链条: 只输出有明确因果传导路径的分析（事件→行业→个股），禁止"可能影响相关板块"等空话
4. 交叉验证: 多源信息互相印证才可信
5. 时间序列: 关注事件的时间先后顺序
6. 市场定价: 评估信息是否已被股价充分反映

⚠️ 严格禁止以下输出:
- "相关股票代码或板块"、"相关个股代码" 等泛泛之词
- "关注XX行业" 而不给出具体股票代码
- 纯政治事件（领导人活动、外交礼节）的投资解读
- 没有直接因果链的牵强关联（如"情绪影响→消费→板块"）
- 如果新闻中没有涉及任何具体 A 股公司，不要强行给出可操作项

输出格式（严格遵守 JSON）:
{
  "thesis": "核心投资论点（一句话，必须包含因果关系）",
  "confidence": 0.0-1.0,
  "investment_relevance": "high/medium/low",
  "time_horizon": "短期(1-5天)/中期(1-4周)/长期(1-6月)",
  "logic_score": 0-100,
  "chain_score": 0-100,
  "chain_improvement": "对输入线索链的评价与优化建议",
  "key_findings": [
    {
      "finding": "发现描述",
      "evidence_ids": ["关联的新闻ID"],
      "reasoning": "推导逻辑（必须说明因果传导路径）"
    }
  ],
  "hidden_signals": [
    {
      "signal": "隐蔽信号描述",
      "implication": "对具体标的的潜在影响",
      "not_priced_in": true/false
    }
  ],
  "risk_factors": ["风险因素"],
  "actionable_items": [
    {
      "action": "具体操作建议",
      "urgency": "high/medium/low",
      "targets": ["000333.SZ", "601318.SH"],
      "target_reasons": [
        {
          "code": "000333.SZ",
          "reason": "推荐原因（简要总结）",
          "main_business": "主营业务（公司核心业务是什么）",
          "core_advantage": "核心竞争力（技术/渠道/成本等独特优势）",
          "industry_position": "行业地位（是否为行业龙头，市场份额排名）",
          "financial_highlight": "财报要点（最近一期财报的营收/净利润/增速等关键数据）",
          "holder_structure": "股东结构（大股东持股比例、散户占比、近期是否有增减持）"
        }
      ]
    }
  ]
}

logic_score 评分标准（0-100）:
  - 因果链条是否完整清晰（事件→行业→个股的传导路径是否每一步都有证据支撑）
  - 证据是否来自新闻中的具体事实（而非推测或泛泛而谈）
  - 逻辑是否自洽（各发现之间是否存在矛盾）
  - 投资论点是否具有可操作性（而非"可能影响"等空话）
  - 90+: 逻辑严密、证据充分、因果链完整
  - 70-89: 逻辑基本合理、部分环节需要补充证据
  - 50-69: 逻辑有断裂、但方向大致正确
  - <50: 逻辑牵强、缺乏因果证据

chain_score 评分标准（0-100）— 对输入线索链本身的质量评价:
  - 新闻与主题的相关度（是否混入大量无关新闻）
  - 时间线的连贯性（新闻之间是否有逻辑关联，还是单纯按时间堆叠）
  - 信息密度（是否有价值的投资信号，还是充斥行情播报/公告噪声）
  - 线索链主题是否准确（主题是否真实反映了新闻内容）
  - 90+: 线索链质量优秀，新闻高度相关，逻辑连贯
  - 70-89: 线索链质量良好，偶有噪声，整体可用
  - 50-69: 线索链质量一般，相关度不足或噪声较多，需要优化
  - <50: 线索链质量差，主题偏移或噪声占主导，建议重建

chain_improvement 要求:
  - 如果 chain_score < 90，必须给出具体的优化建议
  - 指出线索链中存在的问题（如：哪些新闻不应出现在此链中、缺少哪些关键新闻、主题应如何调整）
  - 建议应具体可执行，而非泛泛而谈

每个 actionable_item 的 targets 数量不设上限，只要是"可用股票代码及公司信息"列表中与分析论点相关的股票都可以推荐，优先推荐产业链上下游或同板块中受益程度不同的标的。targets 必须按推荐强度从强到弱排列（最值得买入的排在最前面），target_reasons 也按相同顺序排列。
根据你的专业知识，推荐与投资论点最相关的 A 股标的，确保股票代码和公司名称准确。

target_reasons 是必须字段，对 targets 中的每个股票代码必须按以下5个维度详细说明:
  1. main_business: 主营业务
  2. core_advantage: 核心竞争力（技术/渠道/成本等独特优势）
  3. industry_position: 行业地位（是否为行业龙头，市场份额排名）
  4. financial_highlight: 财报要点（最近一期财报的营收/净利润/增速等关键数据）
  5. holder_structure: 股东结构（大股东持股比例、散户占比、近期是否有增减持）
如果新闻中不涉及任何 A 股公司，或没有与投资论点直接相关的股票，actionable_items 设为空数组 []。"""

CHAIN_ANALYSIS_PROMPT = """请分析以下线索链，聚焦 A 股投资机会，生成具体可执行的投资建议。

## 线索链信息
- 类型: {chain_type}
- 主题: {theme}
- 时间跨度: {time_span}
- 重要性评分: {significance}
- 已发现的隐蔽信号: {hidden_signals}

## 线索链中的新闻（按时间顺序）

{news_list}

{critique_section}

重要提醒:
- 只分析与 A 股投资直接相关的内容
- 如果新闻内容与 A 股投资无关（如纯国际政治、社会新闻），给出 confidence=0.1, actionable_items=[]
- 禁止输出"相关板块代码"、"相关个股代码"等泛泛之词
- 推荐的股票必须与投资论点直接相关
- 每个 actionable_item 应推荐多只相关股票（2-5 只），涵盖产业链上下游或同板块不同环节
- logic_score 必须给出，评分基于因果链条的完整性、证据充分性和逻辑自洽性
- chain_score 必须给出，评分基于线索链中新闻的相关度、连贯性和信息密度
- 如果 chain_score < 90，chain_improvement 必须给出具体优化建议

请严格按照 JSON 格式输出分析结果。"""


def _format_news_list(nodes: List[ChainNode], max_items: int = 30) -> str:
    """将新闻节点格式化为 LLM 可读的文本

    超出 max_items 的部分不会简单丢弃，而是按天分组生成摘要，
    确保有价值的消息不丢失。
    """
    # 按 news_id 去重，保留首次出现的
    seen_ids = set()
    unique_nodes = []
    for n in nodes:
        if n.news_id not in seen_ids:
            seen_ids.add(n.news_id)
            unique_nodes.append(n)
    nodes = unique_nodes

    lines = []
    show_count = min(len(nodes), max_items)

    # 展示前面的新闻 (完整信息)
    for i, n in enumerate(nodes[:show_count], 1):
        parts = [
            f"[{i}] ID: {n.news_id}",
            f"    时间: {n.publish_time[:16] if n.publish_time else '未知'}",
            f"    来源: {n.source} (优先级: {n.source_priority})",
            f"    标题: {n.title}",
        ]
        if n.ts_codes:
            parts.append(f"    股票代码: {', '.join(n.ts_codes[:5])}")
        if n.sentiment:
            parts.append(f"    情绪: {n.sentiment}")
        if n.mentioned_companies:
            parts.append(f"    涉及公司: {', '.join(n.mentioned_companies[:5])}")
        if n.related_sectors:
            parts.append(f"    关联板块: {', '.join(n.related_sectors[:3])}")
        if n.urgency != "normal":
            parts.append(f"    紧急度: {n.urgency}")
        lines.append("\n".join(parts))

    # 超出部分: 按天分组摘要，而非丢弃
    remaining = nodes[show_count:]
    if remaining:
        from collections import Counter
        day_groups: dict[str, list] = {}
        for n in remaining:
            day = (n.publish_time or "未知")[:10]
            day_groups.setdefault(day, []).append(n)

        lines.append(f"\n--- 以下 {len(remaining)} 条新闻按天摘要 ---")
        for day in sorted(day_groups.keys()):
            group = day_groups[day]
            # 提取该天的高优先级标题
            titles = [f"「{n.title[:50]}」" for n in group[:8]]
            summary = f"[{day}] {len(group)} 条: " + "、".join(titles)
            if len(group) > 8:
                summary += f"... 等{len(group)}条"
            lines.append(summary)

    return "\n\n".join(lines)


# OpenAI 兼容的提供商列表 (共用同一个 API 格式)
_OPENAI_COMPAT_PROVIDERS = {"openai", "deepseek", "siliconflow", "moonshot", "qwen", "glm"}


class LLMClient:
    """统一的 LLM 客户端

    复用 httpx.AsyncClient 连接池，避免每次请求创建新连接。
    """

    def __init__(self, config: AnalystConfig):
        self.config = config
        self.provider = config.llm_provider.lower().strip()
        self.model = config.llm_model
        self.base_url = config.llm_base_url
        self.api_key = config.llm_api_key
        self.max_tokens = config.llm_max_tokens
        self.temperature = config.llm_temperature
        self.max_retries = config.llm_max_retries
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 HTTP 连接"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.llm_timeout, connect=self.config.llm_connect_timeout)
            )
        return self._client

    async def close(self):
        """关闭连接池"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def complete(self, system: str, user: str) -> str:
        """调用 LLM 完成生成

        重试策略:
          - 超时 (TimeoutException): 无限重试, 退避 2→4→8→16→30s
          - 服务端错误 (5xx) / 限流 (429): 无限重试, 退避 5→10→20→30s
          - 认证失败 (401/403) / 余额不足 (402) / 网络断开: 立即终止
          - 单条链安全阀: 总重试时间超过 llm_per_chain_timeout 时放弃
        """
        import asyncio
        import time

        logger.debug("LLM request: provider={}, model={}, system={} chars, user={} chars",
                      self.provider, self.model, len(system), len(user))

        per_chain_timeout = self.config.llm_per_chain_timeout
        start_time = time.monotonic()
        attempt = 0

        while True:
            attempt += 1
            elapsed = time.monotonic() - start_time

            # 安全阀: 单条链总耗时超限
            if elapsed > per_chain_timeout:
                logger.error("LLM per-chain timeout ({:.0f}s exceeded), giving up after {} attempts: "
                             "provider={}, model={}",
                             per_chain_timeout, attempt, self.provider, self.model)
                raise httpx.TimeoutException(
                    f"Per-chain timeout ({per_chain_timeout}s) exceeded after {attempt} attempts"
                )

            try:
                if self.provider in _OPENAI_COMPAT_PROVIDERS or self.provider == "openai":
                    return await self._call_openai_compat(system, user)
                elif self.provider == "anthropic":
                    return await self._call_anthropic(system, user)
                elif self.provider == "ollama":
                    return await self._call_ollama(system, user)
                else:
                    raise ValueError(f"Unknown LLM provider: {self.provider}")

            except httpx.TimeoutException:
                # 超时: 无限重试, 指数退避 2→4→8→16→30s
                wait = min(2 ** attempt, 30)
                logger.warning("LLM timeout (attempt #{}, elapsed {:.0f}s), retrying in {}s: "
                               "provider={}, model={}",
                               attempt, elapsed, wait, self.provider, self.model)
                await asyncio.sleep(wait)
                continue

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                body = e.response.text[:500]

                # 不可恢复: 认证失败 / 余额不足 → 立即终止
                if status in (401, 402, 403):
                    logger.error("LLM fatal error ({}): provider={}, model={}, body={}",
                                 status, self.provider, self.model, body)
                    raise

                # 可恢复: 服务端错误 / 限流 → 无限重试
                if status >= 500 or status == 429:
                    wait = min(5 * attempt, 30)
                    logger.warning("LLM server error {} (attempt #{}, elapsed {:.0f}s), "
                                   "retrying in {}s: provider={}, model={}, body={}",
                                   status, attempt, elapsed, wait,
                                   self.provider, self.model, body)
                    await asyncio.sleep(wait)
                    continue

                # 其他客户端错误 (4xx) → 不重试
                logger.error("LLM API error: provider={}, model={}, status={}, body={}",
                             self.provider, self.model, status, body)
                raise

            except (httpx.ConnectError, httpx.ConnectTimeout):
                # 网络断开 → 立即终止
                logger.error("LLM connection failed (network down?): provider={}, model={}",
                             self.provider, self.model)
                raise

            except Exception as e:
                logger.error("LLM call failed: provider={}, model={}, error={}: {}",
                             self.provider, self.model, type(e).__name__, e)
                raise

    def _resolve_base_url(self) -> str:
        """根据 provider 解析默认 base_url"""
        if self.base_url:
            return self.base_url
        if self.provider == "openai":
            return "https://api.openai.com"
        elif self.provider == "deepseek":
            return "https://api.deepseek.com"
        elif self.provider == "anthropic":
            return "https://api.anthropic.com"
        elif self.provider == "ollama":
            return "http://localhost:11434"
        elif self.provider == "glm":
            return "https://open.bigmodel.cn/api/paas/v4"
        return self.base_url or "https://api.openai.com"

    async def _call_openai_compat(self, system: str, user: str) -> str:
        """OpenAI 兼容 API (覆盖 deepseek / 硅基流动 / moonshot / glm 等)"""
        base = self._resolve_base_url().rstrip("/")
        # base_url 已包含版本路径 (如 /api/paas/v4/) 时直接拼接 chat/completions
        # 否则添加 /v1 前缀 (标准 OpenAI 格式)
        if "/v1" in base or "/v4" in base or "/v3" in base:
            url = f"{base}/chat/completions"
        else:
            url = f"{base}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        client = await self._get_client()
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        message = data["choices"][0]["message"]
        content = message.get("content", "")
        reasoning = message.get("reasoning_content", "")
        if reasoning:
            return f"<thinking>\n{reasoning}\n</thinking>\n\n{content}"
        return content

    async def _call_anthropic(self, system: str, user: str) -> str:
        base = self._resolve_base_url()
        url = f"{base}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": self.temperature,
        }
        client = await self._get_client()
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]

    async def _call_ollama(self, system: str, user: str) -> str:
        base = self._resolve_base_url()
        url = f"{base}/api/chat"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            },
        }
        client = await self._get_client()
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["message"]["content"]


class InsightEngine:
    """洞察引擎 — 用 LLM 分析线索链"""

    def __init__(self, config: AnalystConfig):
        self.config = config
        self.llm = LLMClient(config)
        self._critique: Optional[str] = None
        self._stock_info_cache: Optional[Dict[str, Dict[str, str]]] = None

    def set_critique(self, critique: str):
        """设置批评意见 (用于 critique_revise 策略)"""
        self._critique = critique

    async def analyze_chain(self, chain: ClueChain) -> Dict[str, Any]:
        """分析单条线索链"""
        critique_section = ""
        if self._critique:
            critique_section = (
                f"## 上一轮评估的批评意见\n"
                f"请针对以下批评改进你的分析:\n{self._critique}\n"
            )

        # 收集链中所有 ts_codes（仅用于报告展示，不发给 LLM）
        all_ts_codes: set = set()
        for n in chain.nodes:
            for c in n.ts_codes:
                all_ts_codes.add(c)

        # 加载股票真实信息，格式化为 "代码 → 公司名（行业）"
        stock_info = self._load_stock_info_map()
        ts_codes_lines = []
        for code in sorted(all_ts_codes):
            info = stock_info.get(code, {})
            name = info.get("name", "")
            industry = info.get("industry", "")
            if name and industry:
                ts_codes_lines.append(f"  {code} → {name}（{industry}）")
            elif name:
                ts_codes_lines.append(f"  {code} → {name}")
            else:
                ts_codes_lines.append(f"  {code}")
        detected_stocks = "\n".join(ts_codes_lines) if ts_codes_lines else ""

        user_prompt = CHAIN_ANALYSIS_PROMPT.format(
            chain_type=chain.chain_type,
            theme=chain.theme,
            time_span=chain.time_span,
            significance=f"{chain.significance:.2f}",
            hidden_signals="; ".join(chain.hidden_signals) if chain.hidden_signals else "无",
            news_list=_format_news_list(chain.nodes, self.config.insight_max_news),
            critique_section=critique_section,
        )

        try:
            raw = await self.llm.complete(SYSTEM_PROMPT, user_prompt)
            logger.debug("LLM response for {}: {} chars", chain.chain_id, len(raw) if raw else 0)
            result = self._parse_llm_response(raw)
            result["chain_id"] = chain.chain_id
            result["chain_type"] = chain.chain_type
            result["node_count"] = chain.node_count
            result["time_span"] = chain.time_span
            result["chain_theme"] = chain.theme
            result["llm_raw"] = raw
            result["llm_input"] = user_prompt
            result["detected_stocks"] = detected_stocks  # 报告展示用
            # 校验推荐股票信息准确性
            result = self._verify_recommendations(result)
            return result
        except Exception as e:
            logger.error("LLM analysis failed for chain {}: {}", chain.chain_id, e)
            logger.debug("Failed prompt: system={} chars, user={} chars",
                         len(SYSTEM_PROMPT), len(user_prompt))
            return {
                "chain_id": chain.chain_id,
                "error": str(e),
                "thesis": "分析失败",
                "confidence": 0.0,
            }

    async def analyze_chains(self, chains: List[ClueChain]) -> List[Dict[str, Any]]:
        """批量分析线索链 (并发执行)"""
        import asyncio
        import time

        semaphore = asyncio.Semaphore(8)
        completed = asyncio.Event()
        done_count = 0
        total = len(chains)

        async def _analyze_with_limit(idx: int, chain: ClueChain) -> Dict[str, Any]:
            nonlocal done_count
            async with semaphore:
                logger.info("Analyzing chain {}/{}: {}", idx + 1, total, chain.theme)
                result = await self.analyze_chain(chain)
                done_count += 1
                if done_count < total:
                    logger.info("Completed {}/{}, waiting for remaining {}...",
                                done_count, total, total - done_count)
                else:
                    logger.info("All {} chains analyzed.", total)
                return result

        tasks = [_analyze_with_limit(i, c) for i, c in enumerate(chains)]
        results = await asyncio.gather(*tasks)
        results = list(results)
        results.sort(key=lambda r: r.get("confidence", 0), reverse=True)
        return results

    def _load_stock_info_map(self) -> Dict[str, Dict[str, str]]:
        """加载股票代码→{name, industry}映射 (从缓存文件)"""
        if self._stock_info_cache is not None:
            return self._stock_info_cache

        from pathlib import Path as _P
        data_dir = _P(self.config.data_dir)
        result: Dict[str, Dict[str, str]] = {}

        # 1. 行业缓存 (有 name + industry)
        industry_path = data_dir / "stock_industry_cache.json"
        if industry_path.exists():
            try:
                d = json.loads(industry_path.read_text(encoding="utf-8"))
                for code, info in d.get("data", {}).items():
                    suffix = ".SH" if code.startswith(("6", "5")) else ".SZ"
                    ts_code = f"{code}{suffix}"
                    result[ts_code] = {
                        "name": info.get("name", ""),
                        "industry": info.get("industry", ""),
                    }
            except Exception:
                pass

        # 2. ts_code_name 缓存 (补充名称)
        name_path = data_dir / "cache" / "ts_code_name.json"
        if name_path.exists():
            try:
                d = json.loads(name_path.read_text(encoding="utf-8"))
                for ts_code, name in d.items():
                    if ts_code not in result:
                        result[ts_code] = {"name": name, "industry": ""}
                    elif not result[ts_code].get("name"):
                        result[ts_code]["name"] = name
            except Exception:
                pass

        self._stock_info_cache = result
        logger.debug("Loaded stock info map: {} entries", len(result))
        return result

    def _verify_recommendations(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """校验 LLM 推荐的股票信息是否与实际一致，过滤不匹配推荐

        过滤规则:
          1. 实际行业与分析论点不相关 → 过滤 (如白色家电 vs AI芯片)
        """
        stock_info = self._load_stock_info_map()
        if not stock_info:
            return result

        thesis = result.get("thesis", "")
        findings_text = " ".join(
            f.get("finding", "") for f in result.get("key_findings", [])
        )
        analysis_text = f"{thesis} {findings_text}"

        # 构建 industry_alias 关键词列表，用于模糊匹配
        industry_alias = getattr(self.config, 'industry_alias', {})

        for item in result.get("actionable_items", []):
            valid_targets = []
            valid_reasons = []
            removed = []

            for tr in item.get("target_reasons", []):
                code = tr.get("code", "")

                info = stock_info.get(code)
                if not info or not info.get("name"):
                    valid_reasons.append(tr)
                    continue

                actual_name = info["name"]
                actual_industry = info.get("industry", "")
                claimed_business = tr.get("main_business", "")

                # 注入真实信息
                tr["actual_name"] = actual_name
                if actual_industry:
                    tr["actual_industry"] = actual_industry

                # 检查实际行业是否与分析论点相关
                industry_relevant = False
                if actual_industry:
                    # 收集该行业的所有关联关键词 (行业名 + alias)
                    keywords = set()
                    keywords.add(actual_industry)
                    for ind, aliases in industry_alias.items():
                        if ind == actual_industry or actual_industry in aliases:
                            keywords.update(aliases)
                            keywords.add(ind)

                    for kw in keywords:
                        kw = kw.strip()
                        if kw and kw in analysis_text:
                            industry_relevant = True
                            break

                if not industry_relevant and actual_industry:
                    # 实际行业在分析论点中未被提及，标记为不匹配
                    tr["business_mismatch"] = True
                    tr["business_mismatch_note"] = (
                        f"LLM声称「{claimed_business}」，"
                        f"实际为「{actual_name}」（{actual_industry}），与投资论点不匹配"
                    )
                    removed.append(tr)
                    continue

                valid_reasons.append(tr)
                valid_targets.append(code)

            if removed:
                logger.warning(
                    "Filtered {} mismatched stock recommendations: {}",
                    len(removed),
                    [f"{r['code']}({r.get('actual_name', '?')})" for r in removed],
                )

            # 更新为仅保留有效推荐
            item["target_reasons"] = valid_reasons
            if valid_targets:
                item["targets"] = valid_targets
            elif removed and not valid_reasons:
                item["targets"] = []

        return result

    def _parse_llm_response(self, raw: str) -> Dict[str, Any]:
        """解析 LLM 返回的 JSON (带容错)"""
        text = raw.strip()

        # 去掉 markdown 代码块
        if text.startswith("```"):
            lines = text.split("\n")
            # 去掉首行 ```json 和末行 ```
            lines = [l for l in lines[1:] if not (l.strip() == "```" and l is lines[-1])]
            text = "\n".join(lines)
            if text.endswith("```"):
                text = text[:-3]

        # 直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 找 JSON 部分
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass

        return {
            "thesis": "LLM 返回格式异常",
            "confidence": 0.0,
            "raw_text": text[:500],
        }
