"""07 评论归因 Agent —— 把「差评多」翻译成可排期的工作量。

核心指标是可修复率：
改文案/补图就能消掉的差评分出来，剩下的才是供应链问题。
这个区分直接决定接下来两周该派活给运营还是派给工厂。
"""

from __future__ import annotations

from app.agents.base import StageAgent, action, feedback, fmt_pct, insight
from app.core import metrics as M
from app.core.contracts import StageResult

# 八类归因 → 修补动作（这是运营 SOP 里最值钱的一张表）
FIX_PLAYBOOK = {
    "误解型": ("详情页 + 使用步骤图", "把误解点写进「注意事项」段，并在第 9 张图做三步图示", "listing"),
    "参数不符合预期": ("参数表 + 卖点收敛", "参数图数字必须溯源实物；卖点只讲一条，避免预期错配", "listing"),
    "配件缺失": ("包装清单图", "九宫格第 8 张改为全配件平铺俯拍并逐件标注", "image"),
    "使用困难": ("使用步骤图 + 短视频", "三步图示 + 15 秒操作视频放在详情页第二屏", "image"),
    "描述不符": ("主图/文案一致性", "主图不得美化尺寸与材质，文案删除形容性夸张", "listing"),
    "价格偏高": ("套装感知价值", "不改价，改组合：加赠配件或做两件装", "strategy"),
    "发货慢": ("物流说明前置", "不可通过内容修复，详情页前置发货时效并评估海外仓", ""),
    "破损/故障/退货": ("包装与质检", "不可通过内容修复，需升级包装与出厂检", ""),
}


class ReviewAgent(StageAgent):
    key = "review"

    def _run(self, payload: dict) -> StageResult:
        agg = M.review_aggregate(payload["reviews"])

        insights = []
        actions = []
        feedbacks = []
        evidence = ["metrics.review_aggregate", "bad_by_category[*]"]

        insights.append(
            insight(
                f"共 {agg['total']} 条评论，好评率 {fmt_pct(agg['good_rate'])}，"
                f"差评 {agg['bad_count']} 条（{fmt_pct(agg['bad_rate'])}），平均分 {agg['avg_rating']}。",
                "good" if agg["good_rate"] >= 0.85 else "warn",
                "review_aggregate.good_rate",
            )
        )

        if agg["bad_count"]:
            insights.append(
                insight(
                    f"差评中 {agg['fixable_count']} 条属于「改文案/补图就能消掉」的类型，"
                    f"可修复率 {fmt_pct(agg['fixable_rate'])}——这部分几乎零成本，"
                    f"剩下 {agg['bad_count'] - agg['fixable_count']} 条才是供应链要处理的。",
                    "good" if agg["fixable_rate"] >= 0.5 else "warn",
                    "review_aggregate.fixable_rate",
                )
            )

            top = agg["bad_by_category"][0]
            insights.append(
                insight(
                    f"最大归因是「{top['category']}」，占差评 {fmt_pct(top['share'])}（{top['count']} 条）。"
                    + ("这类属于认知错配，改详情页即可。" if top["fixable"] else "这类属于履约/质量问题，内容层面修不掉。"),
                    "warn",
                    "bad_by_category[0]",
                )
            )

        if agg["with_image_rate"] < 0.15:
            insights.append(
                insight(
                    f"带图评论仅 {fmt_pct(agg['with_image_rate'])}：买家秀稀缺会拉低转化，"
                    f"可在包裹卡上做「晒图返券」，但不得诱导好评。",
                    "info",
                    "review_aggregate.with_image_rate",
                )
            )

        if agg["unlabeled_bad"]:
            insights.append(
                insight(
                    f"有 {agg['unlabeled_bad']} 条差评未标注归因，无法进入动作池。"
                    f"先补标注再谈改进，否则改进对象是模糊的。",
                    "warn",
                    "review_aggregate.unlabeled_bad",
                )
            )

        # ---- 动作：每条可修复归因对应一个具体改动 ----
        for item in agg["bad_by_category"]:
            cat = item["category"]
            if not item["fixable"]:
                continue
            fix_target, fix_how, back_to = FIX_PLAYBOOK.get(cat, ("详情页", "补充说明", "listing"))
            actions.append(
                action(
                    "P0" if item["share"] >= 0.3 else "P1",
                    fix_target,
                    fix_how,
                    f"该类差评 {item['count']} 条，占 {fmt_pct(item['share'])}，且属于内容可修复类型",
                    f"改版后 30 天内「{cat}」差评占比是否下降一半",
                    "中" if cat in ("使用困难", "描述不符") else "低",
                )
            )
            if back_to:
                feedbacks.append(
                    feedback(back_to, f"「{cat}」差评占 {fmt_pct(item['share'])}，内容未覆盖该预期", fix_target)
                )

        unfixable = [i for i in agg["bad_by_category"] if not i["fixable"]]
        for item in unfixable:
            fix_target, fix_how, _ = FIX_PLAYBOOK.get(item["category"], ("供应链", "人工评估", ""))
            actions.append(
                action(
                    "P1",
                    fix_target,
                    fix_how,
                    f"「{item['category']}」{item['count']} 条，属于内容不可修复类",
                    f"90 天内「{item['category']}」差评数量是否下降",
                    "高",
                )
            )

        headline = (
            f"{agg['total']} 条评论 / 好评率 {fmt_pct(agg['good_rate'])}，"
            f"差评 {agg['bad_count']} 条其中 {fmt_pct(agg['fixable_rate'])} 可内容修复"
        )

        return StageResult(
            stage=self.key,
            title=self.title,
            status="ok",
            headline=headline,
            metrics={"aggregate": agg, "playbook": FIX_PLAYBOOK},
            insights=insights,
            actions=actions,
            feedback=feedbacks,
            evidence=evidence,
            next_stage=None,
        )
