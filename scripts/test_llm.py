"""LLM 连通性自检 + 效果对比。

用法：
    python scripts/test_llm.py            # 只测连通性
    python scripts/test_llm.py --demo     # 连通后跑一次 Listing / 视频，看实际效果

这个脚本不依赖 HTTP 服务，直接走代码层，用来快速定位「key 错 / 模型名错 / 网络不通」。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.llm import (  # noqa: E402
    RuleProvider,
    get_provider,
    load_env,
    provider_info,
    try_generate,
)


def mask(secret: str) -> str:
    if not secret:
        return "（未设置）"
    if len(secret) <= 10:
        return secret[:2] + "***"
    return f"{secret[:6]}…{secret[-4:]}"


def main() -> int:
    load_env()

    import os

    info = provider_info()
    print("=" * 60)
    print("当前 LLM 配置")
    print("=" * 60)
    print(f"模式      : {info['mode']}")
    print(f"供应商    : {info['label']}")
    print(f"模型      : {info['model'] or '—'}")
    print(f"端点      : {info['base_url'] or '—'}")
    print(f"API Key   : {mask(os.getenv('DASHSCOPE_API_KEY') or os.getenv('ECOM_LLM_API_KEY', ''))}")

    env_file = ROOT / ".env"
    print(f".env 文件 : {'已加载' if env_file.exists() else '不存在'}")

    if info["mode"] == "offline":
        print("\n[!] 当前是离线规则模式，不会调用任何外部服务。")
        print("    接百炼请在项目根目录建 .env：")
        print("      ECOM_LLM_PROVIDER=dashscope")
        print("      DASHSCOPE_API_KEY=sk-你的key")
        return 1

    provider = get_provider()
    if isinstance(provider, RuleProvider):
        print("\n[x] provider 构造失败")
        return 1

    print("\n" + "=" * 60)
    print("连通性测试")
    print("=" * 60)
    result = provider.generate(
        "你是连通性测试助手。", "只回复两个字：可用", max_tokens=16, temperature=0
    )
    if result.ok:
        print(f"[OK] 模型返回：{result.text[:40]}")
    else:
        print(f"[FAIL] {result.error}")
        print("\n常见原因：")
        print("  HTTP 401  → key 错误或未开通该模型")
        print("  HTTP 404  → 模型名不对，百炼可用 qwen-plus / qwen-max / qwen-turbo")
        print("  超时      → 网络问题，检查代理")
        return 1

    if "--demo" in sys.argv:
        from app.demo import build_demo_payload
        from app.orchestrator import run_single

        payload = build_demo_payload()

        print("\n" + "=" * 60)
        print("Listing 效果（模型写的标题，仍需过脚本校验）")
        print("=" * 60)
        res = run_single("listing", payload)
        for t in res["metrics"]["titles"]:
            lint = t["lint"]
            tag = "LLM" if t.get("source") == "llm" else "规则"
            print(f"[{tag}·{t['variant']}] {lint['chars']}字符 覆盖{lint['keyword_coverage']:.0%} 通过={lint['passed']}")
            print(f"    {t['text']}")

        print("\n" + "=" * 60)
        print("视频口播（四段结构与预算由脚本给定）")
        print("=" * 60)
        res = run_single("video", payload)
        used = res["metrics"].get("llm", {}).get("used")
        print(f"是否使用大模型: {used}")
        for seg in res["metrics"]["storyboard"]:
            print(f"  [{seg['scene']} {seg['seconds']}s] {seg['narration']}")
        tm = res["metrics"]["timing"]
        print(f"  时长 {tm['storyboard_seconds']}s / 口播需 {tm['needed_seconds']}s / 语速偏快={tm['too_fast']}")

    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
