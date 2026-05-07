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


SYSTEM_PROMPT = """你是一位资深财经分析师，擅长从大量新闻中寻找隐蔽的因果逻辑和未被市场充分反映的信息。

你的分析原则:
1. 交叉验证: 多源信息互相印证才可信
2. 时间序列: 关注事件的时间先后顺序，寻找因果关系
3. 传导链条: 政策→行业→个股的传导路径
4. 异常信号: 情绪突然转变、消息密度异常、关联方异动
5. 隐蔽关联: 表面不相关的事件之间可能存在深层联系
6. 市场定价: 评估当前信息是否已被股价充分反映

输出格式要求（严格遵守 JSON）:
{
  "thesis": "核心论点（一句话）",
  "confidence": 0.0-1.0,
  "time_horizon": "短期(1-5天)/中期(1-4周)/长期(1-6月)",
  "key_findings": [
    {
      "finding": "发现描述",
      "evidence_ids": ["关联的新闻ID"],
      "reasoning": "推导逻辑"
    }
  ],
  "hidden_signals": [
    {
      "signal": "隐蔽信号描述",
      "implication": "潜在影响",
      "not_priced_in": true/false
    }
  ],
  "risk_factors": ["风险因素"],
  "actionable_items": [
    {
      "action": "建议关注/操作",
      "urgency": "high/medium/low",
      "targets": ["相关股票代码或板块"]
    }
  ]
}"""

CHAIN_ANALYSIS_PROMPT = """请分析以下线索链，推导隐蔽信息并生成洞察。

## 线索链信息
- 类型: {chain_type}
- 主题: {theme}
- 时间跨度: {time_span}
- 重要性评分: {significance}
- 已发现的隐蔽信号: {hidden_signals}

## 线索链中的新闻（按时间顺序）

{news_list}

{critique_section}

请严格按照 JSON 格式输出分析结果。"""


def _format_news_list(nodes: List[ChainNode], max_items: int = 30) -> str:
    """将新闻节点格式化为 LLM 可读的文本"""
    lines = []
    for i, n in enumerate(nodes[:max_items], 1):
        parts = [
            f"[{i}] ID: {n.news_id}",
            f"    时间: {n.publish_time[:16] if n.publish_time else '未知'}",
            f"    来源: {n.source} (优先级: {n.source_priority})",
            f"    标题: {n.title}",
        ]
        if n.sentiment:
            parts.append(f"    情绪: {n.sentiment}")
        if n.mentioned_companies:
            parts.append(f"    涉及公司: {', '.join(n.mentioned_companies[:5])}")
        if n.related_sectors:
            parts.append(f"    关联板块: {', '.join(n.related_sectors[:3])}")
        if n.urgency != "normal":
            parts.append(f"    紧急度: {n.urgency}")
        lines.append("\n".join(parts))

    if len(nodes) > max_items:
        lines.append(f"\n... 还有 {len(nodes) - max_items} 条新闻未显示")
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
        """调用 LLM 完成生成"""
        logger.debug("LLM request: provider={}, model={}, system={} chars, user={} chars",
                      self.provider, self.model, len(system), len(user))
        try:
            if self.provider in _OPENAI_COMPAT_PROVIDERS or self.provider == "openai":
                return await self._call_openai_compat(system, user)
            elif self.provider == "anthropic":
                return await self._call_anthropic(system, user)
            elif self.provider == "ollama":
                return await self._call_ollama(system, user)
            else:
                raise ValueError(f"Unknown LLM provider: {self.provider}")
        except httpx.HTTPStatusError as e:
            body = e.response.text[:500]
            logger.error("LLM API error: provider={}, model={}, status={}, body={}",
                         self.provider, self.model, e.response.status_code, body)
            raise
        except httpx.TimeoutException:
            logger.error("LLM API timeout: provider={}, model={}", self.provider, self.model)
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
        return data["choices"][0]["message"]["content"]

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
        """批量分析线索链"""
        results = []
        for i, chain in enumerate(chains):
            logger.info("Analyzing chain {}/{}: {}", i + 1, len(chains), chain.theme)
            result = await self.analyze_chain(chain)
            results.append(result)
        results.sort(key=lambda r: r.get("confidence", 0), reverse=True)
        return results

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
