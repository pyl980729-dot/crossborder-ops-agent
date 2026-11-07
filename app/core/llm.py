"""LLM 接入层 —— 可插拔，默认离线可用。

职责边界（这条边界是整个系统的设计底线）：
- **分析类结论永不经过 LLM**：定价、广告分级、评论归因必须确定性，否则无法回归测试；
- **创意类产出才调用 LLM**：标题、口播稿这类本来就没有唯一正确答案的内容。

调用失败、超时、返回格式不合规 —— 一律静默回落到规则模板，绝不让整条流水线挂掉。
模型生成的文本还必须再过一遍脚本校验（字符数、堆砌、语速），
不合规就丢弃 —— 生成与校验共用同一套标准。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# .env 加载（零依赖，不引 python-dotenv）
# ---------------------------------------------------------------------------

def load_env() -> None:
    """把项目根目录的 .env 读进环境变量。已存在的环境变量优先，不被覆盖。"""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 供应商预设
# ---------------------------------------------------------------------------

DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"

PRESETS: dict[str, dict] = {
    "dashscope": {
        "base": DASHSCOPE_BASE,
        "model": "qwen-plus",
        "key_env": ("DASHSCOPE_API_KEY", "ECOM_LLM_API_KEY"),
        "label": "阿里云百炼",
    },
    "bailian": {
        "base": DASHSCOPE_BASE,
        "model": "qwen-plus",
        "key_env": ("DASHSCOPE_API_KEY", "ECOM_LLM_API_KEY"),
        "label": "阿里云百炼",
    },
    "qwen": {
        "base": DASHSCOPE_BASE,
        "model": "qwen-plus",
        "key_env": ("DASHSCOPE_API_KEY", "ECOM_LLM_API_KEY"),
        "label": "阿里云百炼",
    },
    "openai": {
        "base": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "key_env": ("OPENAI_API_KEY", "ECOM_LLM_API_KEY"),
        "label": "OpenAI",
    },
    "deepseek": {
        "base": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "key_env": ("DEEPSEEK_API_KEY", "ECOM_LLM_API_KEY"),
        "label": "DeepSeek",
    },
}


@dataclass
class LLMResult:
    text: str
    provider: str
    ok: bool
    error: str = ""


class RuleProvider:
    """离线规则模式：不调用任何外部服务。"""

    name = "rule"
    label = "离线规则模式"
    model = ""
    base_url = ""
    last_error = ""

    def available(self) -> bool:
        return True

    def generate(self, system: str, user: str, max_tokens: int = 900) -> LLMResult:
        return LLMResult(text="", provider=self.name, ok=False, error="rule 模式不生成自由文本")


class OpenAICompatProvider:
    """任意 OpenAI 兼容端点（百炼 / OpenAI / DeepSeek / 本地 vLLM）。"""

    def __init__(self, base_url: str, api_key: str, model: str, label: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.label = label or model
        self.name = f"{label or 'compat'}:{model}"
        self.last_error = ""

    def available(self) -> bool:
        return bool(self.api_key)

    def generate(
        self, system: str, user: str, max_tokens: int = 900, temperature: float = 0.5
    ) -> LLMResult:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
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
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"].strip()
            self.last_error = ""
            return LLMResult(text=text, provider=self.name, ok=bool(text))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8")[:300]
            except Exception:  # noqa: BLE001
                pass
            self.last_error = f"HTTP {exc.code}: {body or exc.reason}"
            return LLMResult(text="", provider=self.name, ok=False, error=self.last_error)
        except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            return LLMResult(text="", provider=self.name, ok=False, error=self.last_error)

    def ping(self) -> LLMResult:
        """连通性自检：发一个最小请求，确认 key 与模型都可用。"""
        return self.generate("你是连通性测试助手。", "只回复两个字：可用", max_tokens=16, temperature=0)


_PROVIDER: object | None = None


def get_provider(force_reload: bool = False):
    """按 .env / 环境变量选 provider，进程内缓存。

    优先级：
      1. ECOM_LLM_PROVIDER=dashscope|bailian|qwen  → 阿里云百炼（默认 qwen-plus）
      2. ECOM_LLM_PROVIDER=openai|deepseek         → 对应预设
      3. ECOM_LLM_PROVIDER=rule 或未设置           → 离线规则模式
    任何情况下都可用 ECOM_LLM_BASE_URL / ECOM_LLM_MODEL 覆盖预设。
    """
    global _PROVIDER
    if _PROVIDER is not None and not force_reload:
        return _PROVIDER

    load_env()

    mode = (os.getenv("ECOM_LLM_PROVIDER") or "rule").strip().lower()

    # 兜底：只填了百炼 key 但没写 provider，也自动识别（少一步配置就少一个出错点）
    if mode == "rule" and os.getenv("DASHSCOPE_API_KEY"):
        mode = "dashscope"

    preset = PRESETS.get(mode)
    if not preset:
        _PROVIDER = RuleProvider()
        return _PROVIDER

    api_key = ""
    for env_name in preset["key_env"]:
        api_key = os.getenv(env_name, "")
        if api_key:
            break

    if not api_key:
        _PROVIDER = RuleProvider()
        return _PROVIDER

    _PROVIDER = OpenAICompatProvider(
        base_url=os.getenv("ECOM_LLM_BASE_URL") or preset["base"],
        api_key=api_key,
        model=os.getenv("ECOM_LLM_MODEL") or preset["model"],
        label=preset["label"],
    )
    return _PROVIDER


def provider_info() -> dict:
    p = get_provider()
    return {
        "name": p.name,
        "mode": "offline" if isinstance(p, RuleProvider) else "online",
        "label": getattr(p, "label", p.name),
        "model": getattr(p, "model", ""),
        "base_url": getattr(p, "base_url", ""),
        "last_error": getattr(p, "last_error", ""),
    }


def try_generate(system: str, user: str, max_tokens: int = 900) -> str | None:
    """统一入口：能用就返回文本，不能用/失败就返回 None。

    调用方拿到 None 时必须回落到规则模板，不许抛错、不许中断流水线。
    """
    provider = get_provider()
    if isinstance(provider, RuleProvider):
        return None
    result = provider.generate(system, user, max_tokens=max_tokens)
    return result.text if result.ok and result.text else None
