"""04 主图与详情图 Agent —— 九张图不是九个装饰，是九个转化理由。

固定九宫格框架（顺序即用户滑动顺序）：
主图给「点进来」的理由 → 卖点图给「继续看」的理由 → 场景/参数/细节给「相信」的理由
→ 包装/步骤给「不犹豫」的理由。

校验用脚本：少一格就是少一个理由，规划不完整不许交付。
"""

from __future__ import annotations

from app.agents.base import StageAgent, action, fmt_pct, insight
from app.core import metrics as M
from app.core.contracts import StageResult
from app.core.platforms import get_platform

# (slot, 目的, 画面要求, 出图 prompt 模板)
NINE_GRID = [
    (
        "主图",
        "让人点进来",
        "纯白底、单品居中、主体占画面 85% 以上、无文字水印",
        "professional e-commerce product photo of {name}, pure white background, "
        "centered, soft studio lighting, high detail, no text, no watermark, 1:1",
    ),
    (
        "卖点图",
        "让人相信第一个理由",
        "只讲唯一主卖点：{main_point}，一行大字 + 产品局部特写",
        "product detail shot of {name} highlighting {prompt_point}, clean background, "
        "single bold headline space, commercial photography",
    ),
    (
        "场景图A",
        "让人对号入座",
        "真实使用场景，人在画面中，体现目标人群",
        "lifestyle photo, person using {name} in {scene_a}, natural light, candid",
    ),
    (
        "场景图B",
        "覆盖第二种需求",
        "第二个高频场景，与场景 A 人群互补",
        "lifestyle photo, {name} used in {scene_b}, warm ambient light, realistic",
    ),
    (
        "参数图",
        "让人比得清",
        "关键参数可视化，数字必须与实物一致",
        "clean infographic layout of {name} specifications, minimal flat design, "
        "no exaggerated claims",
    ),
    (
        "细节图",
        "让人看得到做工",
        "材质/工艺/接口特写，微距",
        "macro shot of {name} material texture and joints, sharp focus, studio light",
    ),
    (
        "尺寸对比",
        "让人判断合不合适",
        "与常见参照物同框，标注长宽高",
        "size comparison shot of {name} next to a hand and a common object, "
        "clean background, measurement labels",
    ),
    (
        "包装清单",
        "让人不担心缺件",
        "所有配件平铺俯拍，逐件标注",
        "flat lay top-down photo of {name} with all included accessories, "
        "each item labeled, white background",
    ),
    (
        "使用步骤",
        "让人不会用错",
        "三步图示 + 注意事项（覆盖误解型差评）",
        "three-step usage instruction visual for {name}, numbered steps, "
        "simple icons, clear and friendly",
    ),
]


class ImageAgent(StageAgent):
    key = "image"

    def _run(self, payload: dict) -> StageResult:
        platform = get_platform(payload.get("platform"))
        name = str(payload["product_name"]).strip()
        points = [str(p).strip() for p in payload["selling_points"] if str(p).strip()]
        scenes = payload.get("scenes") or ["居家日常", "户外出行"]

        main_point = points[0] if points else name
        # 出图模型对中英混杂的 prompt 表现很差，卖点是中文时用语义占位，
        # 中文原意保留在 visual 字段里给人看。
        prompt_point = main_point if not _has_cjk(main_point) else "the hero benefit shown in image"
        ctx = {
            "name": name,
            "main_point": main_point,
            "prompt_point": prompt_point,
            "scene_a": scenes[0] if scenes else "居家日常",
            "scene_b": scenes[1] if len(scenes) > 1 else "户外出行",
        }

        plan = []
        for slot, purpose, visual, prompt in NINE_GRID:
            plan.append(
                {
                    "slot": slot,
                    "purpose": purpose,
                    "visual": visual.format(**ctx),
                    "prompt": prompt.format(**ctx),
                    "compliance": self._compliance_note(slot, platform),
                }
            )

        check = M.image_plan_check(plan, [slot for slot, *_ in NINE_GRID])

        insights = [
            insight(
                f"九图规划 {check['planned']}/{check['required']} 完成，覆盖率 {fmt_pct(check['coverage'])}。"
                f"顺序按用户滑动路径排：主图给理由 → 卖点给信任 → 参数给比较 → 包装给安心。",
                "good" if check["complete"] else "risk",
                "image_plan_check.coverage",
            ),
            insight(
                f"唯一主卖点锁定为「{main_point}」：卖点图、视频钩子、标题后半段必须讲同一件事，"
                f"否则用户看完记不住任何一条。",
                "info",
                "selling_points[0]",
            ),
            insight(
                f"{platform.name} 主图硬要求：≥{platform.image_min_px}px、背景 {platform.image_bg}、"
                f"最多 {platform.image_max_count} 张。不合规的主图会被降权而不是拒审，更麻烦。",
                "warn",
                f"platform:{platform.key}",
            ),
        ]

        actions = [
            action(
                "P0",
                "主图",
                f"先出第 1、2 张（主图 + 卖点图），一版只改一个变量做 A/B",
                "前两张决定点击率，其余八张决定转化率，优先级不同",
                "两周后对比两版点击率，差异 >15% 才算显著",
                "中",
            ),
            action(
                "P1",
                "包装清单图",
                "拍摄全部配件平铺俯拍，逐件标注",
                "「配件缺失」是差评里改图就能消掉的一类，成本最低",
                "上线 30 天后该类差评是否归零",
            ),
            action(
                "P1",
                "合规",
                "出图后逐张核对主体占比、背景纯度、是否有文字水印",
                "平台对主图的判定是自动的，不合规不拒审但会限流",
                "用 check_main_image 类脚本做发布前校验",
            ),
        ]

        return StageResult(
            stage=self.key,
            title=self.title,
            status="ok" if check["complete"] else "partial",
            headline=f"生成 {len(plan)} 张图的规划与出图 Prompt，完整性 {fmt_pct(check['coverage'])}",
            metrics={
                "plan": plan,
                "check": check,
                "main_point": main_point,
                "platform": {
                    "key": platform.key,
                    "image_min_px": platform.image_min_px,
                    "image_bg": platform.image_bg,
                },
            },
            insights=insights,
            actions=actions,
            evidence=["metrics.image_plan_check", f"platform:{platform.key}"],
            next_stage="video",
        )

    @staticmethod
    def _compliance_note(slot: str, platform) -> str:
        notes = {
            "主图": f"主体 ≥85%，背景 {platform.image_bg}，≥{platform.image_min_px}px，禁止文字/水印/多品同图",
            "卖点图": "卖点必须有参数或画面可验证，禁止绝对化用语",
            "场景图A": "场景须真实可复现，禁止合成虚假使用效果",
            "场景图B": "不得出现未授权品牌标识与他人肖像",
            "参数图": "所有数字必须能溯源到实物规格，禁止虚标与极限词",
            "细节图": "保留真实材质纹理，禁止过度磨皮修图",
            "尺寸对比": "参照物不得放大或缩小以误导尺寸感知",
            "包装清单": "清单必须与实物完全一致，少一件就是一条差评",
            "使用步骤": "步骤须可实际执行，错误步骤会导致退货",
        }
        return notes.get(slot, "禁止绝对化用语与未授权品牌词")


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text))
