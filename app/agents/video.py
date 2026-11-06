"""05 短视频脚本 Agent —— 前 3 秒决定生死，所以钩子只能有一个。

结构固定为四段（不是三幕，因为电商视频必须有 CTA）：
Hook(0-3s) → 痛点/场景(3-8s) → 卖点证明(8-20s) → CTA(20-25s)

时长由脚本测算，不靠估：语速按每秒 4 个音节算，超时直接标红。
"""

from __future__ import annotations

from app.agents.base import StageAgent, action, insight
from app.core.contracts import StageResult
from app.core.platforms import get_platform

# 每秒口播音节数（葡语/英语快节奏口播的经验值）
SYLLABLES_PER_SEC = 4.0
# 中文口播语速：每秒 4.5 字（短视频快节奏）
CHARS_PER_SEC_ZH = 4.5


class VideoAgent(StageAgent):
    key = "video"

    def _run(self, payload: dict) -> StageResult:
        platform = get_platform(payload.get("platform"))
        name = str(payload["product_name"]).strip()
        main_point = str(payload["main_point"]).strip()
        duration = int(payload.get("duration") or 25)
        audience = str(payload.get("audience") or "注重实用的日常用户")

        storyboard = self._build_storyboard(name, main_point, audience, duration)
        total_seconds = sum(s["seconds"] for s in storyboard)
        narration = " ".join(s["narration"] for s in storyboard if s["narration"])

        # 语速口径必须区分语种：中文按「字/秒」，拉丁语系按「音节/秒」。
        # 用同一把尺子量两种语言，会得出「中文永远超时」的假结论。
        cjk_chars = sum(1 for c in narration if "\u4e00" <= c <= "\u9fff")
        latin_chars = sum(1 for c in narration if c.isalpha() and not ("\u4e00" <= c <= "\u9fff"))
        needed = cjk_chars / CHARS_PER_SEC_ZH + latin_chars / SYLLABLES_PER_SEC

        over = total_seconds > duration
        too_fast = needed > total_seconds * 1.05

        insights = [
            insight(
                f"口播 {cjk_chars} 个汉字（按 {CHARS_PER_SEC_ZH:.0f} 字/秒）+ {latin_chars} 个拉丁字符"
                f"（按 {SYLLABLES_PER_SEC:.0f} 音节/秒）≈ {needed:.1f} 秒，"
                f"分镜总时长 {total_seconds} 秒"
                + ("，语速偏快需要删词" if too_fast else "，语速正常"),
                "warn" if too_fast else "good",
                "video.timing",
            ),
            insight(
                f"Hook 只讲「{main_point}」这一件事：前 3 秒讲两个卖点等于一个都没讲进去。",
                "info",
                "storyboard[0].narration",
            ),
            insight(
                f"画面比例 {platform.name} 优先 9:16 竖版，首帧必须出现产品本体，"
                f"否则信息流里会被当成无关内容划走。",
                "warn",
                f"platform:{platform.key}",
            ),
        ]

        actions = [
            action(
                "P0",
                "Hook",
                f"首帧直接上产品 + 大字「{main_point}」，不做片头 Logo 动画",
                "信息流前 3 秒流失超过 50%，片头是最贵的浪费",
                "看 3 秒完播率是否 >60%",
                "中",
            ),
            action(
                "P1",
                "字幕",
                "全程硬字幕，关键词加粗，静音也能看懂",
                "多数用户默认静音浏览",
                "看静音场景下的完播率",
            ),
            action(
                "P1",
                "素材复用",
                "同一条口播换 3 个 Hook 各剪一条，小预算并行测试",
                "Hook 是最大变量，测试它比测试卖点性价比高",
                "两周后保留 CTR 最高的一条做主力",
            ),
        ]

        headline = (
            f"{duration}s 竖版短视频：四段结构 + {len(storyboard)} 个分镜，"
            f"口播约需 {needed:.1f}s"
            + ("，超时长需压缩" if over or too_fast else "，时长匹配")
        )

        return StageResult(
            stage=self.key,
            title=self.title,
            status="ok" if not (over or too_fast) else "partial",
            headline=headline,
            metrics={
                "storyboard": storyboard,
                "narration": narration,
                "timing": {
                    "target_seconds": duration,
                    "storyboard_seconds": total_seconds,
                    "cjk_chars": cjk_chars,
                    "latin_chars": latin_chars,
                    "needed_seconds": round(needed, 1),
                    "over_duration": over,
                    "too_fast": too_fast,
                },
                "main_point": main_point,
            },
            insights=insights,
            actions=actions,
            evidence=["video.timing", "storyboard"],
            next_stage="ads",
        )

    @staticmethod
    def _build_storyboard(name: str, main_point: str, audience: str, duration: int) -> list[dict]:
        # 四段时长按比例分配，再按目标时长缩放。
        # 文案刻意写短：25 秒视频容不下长句，产品名交给画面，口播不念完整型号。
        base = [
            ("Hook", 3, f"还在为续航焦虑？{main_point}。",
             f"{name} 产品特写 + 大字幕「{main_point}」", "抓住 3 秒注意力"),
            ("痛点", 5, f"{audience}最烦的三件事：麻烦、不稳、占地方。",
             "真实场景还原，人物出镜演示问题", "让用户对号入座"),
            ("证明", min(12, max(8, duration - 10)), "实测给你看，不是参数吹出来的。",
             "连续三个使用镜头 + 参数角标", "用画面证明卖点，不靠形容词"),
            ("CTA", max(3, duration - 3 - 5 - max(8, duration - 10)),
             "链接在下方，今天下单送全套配件。",
             "产品全家福 + 价格角标 + 购物车指引", "给一个立刻行动的理由"),
        ]
        scale = duration / sum(b[1] for b in base)
        out = []
        cursor = 0.0
        for name_, seconds, narration, visual, purpose in base:
            sec = round(seconds * scale)
            out.append(
                {
                    "scene": name_,
                    "start": round(cursor, 1),
                    "end": round(cursor + sec, 1),
                    "seconds": sec,
                    "narration": narration,
                    "visual": visual,
                    "purpose": purpose,
                }
            )
            cursor += sec

        # 取整误差补到最后一段，保证分镜总时长精确等于目标时长
        diff = duration - sum(s["seconds"] for s in out)
        if diff and out:
            out[-1]["seconds"] += diff
            out[-1]["end"] = out[-1]["start"] + out[-1]["seconds"]

        return out
