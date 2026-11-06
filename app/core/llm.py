"""LLM 接入层 —— 可插拔，且**默认离线可用**。

设计取舍：
- 分析类结论（市场判断、定价、广告分级）由规则引擎生成 —— 因为它们必须可复现，
  同一份数据跑两次必须得出同一句话，否则没法做回归测试；
- 创意类产出（标题、卖点、脚本）才交给 LLM —— 这部分本来就没有唯一正确答案。

没有 API Key 时自动降级到 rule 模式，全链路依然跑得通。
这不是「阉割版」：规则模式产出的结论是确定性结论，比 LLM 更该被当基准。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class LLMResult:
    text: str
    provider: str
    ok: bool
    error: str = ""


class RuleProvider:
    """离线规则模式：不调用任何外部服务。"""

    name = "rule"

    def available(self) -> bool:
        return True

    def generate(self, system: str, user: str, max_tokens: int = 900) -> LLMResult:
        return LLMResult(text="", provider=self.name, ok=False, error="rule 模式不生成自由文本")


class OpenAICompatProvider:
    """任意 OpenAI 兼容端点（OpenAI / DeepSeek / 通义 / 本地 vLLM 均可）。"""

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.name = f"openai:{model}"

    def available(self) -> bool:
        return bool(self.api_key)

    def generate(self, system: str, user: str, max_tokens: int = 900) -> LLMResult:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.4,
            "max_tokens": max_tokens,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"].strip()
            return LLMResult(text=text, provider=self.name, ok=bool(text))
        except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError) as exc:
            return LLMResult(text="", provider=self.name, ok=False, error=str(exc)[:200])


_PROVIDER: object | None = None


def get_provider():
    """按环境变量选 provider，进程内缓存。

    ECOM_LLM_PROVIDER=rule|openai
    ECOM_LLM_API_KEY / ECOM_LLM_BASE_URL / ECOM_LLM_MODEL
    """
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER

    mode = (os.getenv("ECOM_LLM_PROVIDER") or "rule").lower()
    if mode in ("openai", "compat", "deepseek"):
        key = os.getenv("ECOM_LLM_API_KEY", "")
        if key:
            _PROVIDER = OpenAICompatProvider(
                base_url=os.getenv("ECOM_LLM_BASE_URL", "https://api.openai.com/v1"),
                api_key=key,
                model=os.getenv("ECOM_LLM_MODEL", "gpt-4o-mini"),
            )
            return _PROVIDER

    _PROVIDER = RuleProvider()
    return _PROVIDER


def provider_info() -> dict:
    p = get_provider()
    return {"name": p.name, "mode": "offline" if isinstance(p, RuleProvider) else "online"}
