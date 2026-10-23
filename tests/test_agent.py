"""Agent 分类和安全停止行为测试。"""

import logging
import unittest

from agent.custom import (
    apply_extra_rules,
    classify_task,
    load_config,
    split_rules,
)
from agent.runner import SimulationController, create_logger, run_tasks


class AgentBehaviorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config()
        cls.logger = create_logger()

    def test_blacklist_has_priority_over_browse(self) -> None:
        decision = classify_task("绑定银行卡 浏览商品", self.config)
        self.assertEqual(decision.kind, "skip")

    def test_unknown_task_is_not_clicked(self) -> None:
        decision = classify_task("无法识别的任务", self.config)
        self.assertEqual(decision.kind, "unknown")

    def test_alipay_tasks_are_still_executed_as_jumps(self) -> None:
        """跳支付宝的任务要做，不能因为弹框就放弃。"""
        for text in ("去蚂蚁庄园逛逛哟(0/1)", "去蚂蚁森林收能量(0/1)"):
            with self.subTest(text=text):
                decision = classify_task(text, self.config)
                self.assertEqual(decision.kind, "jump")

    def test_safety_verification_stops_before_click(self) -> None:
        controller = SimulationController(self.logger)
        result = run_tasks(
            ["浏览商品", "拖动滑块", "搜索商品"],
            controller,
            self.config,
            self.logger,
            sleep=False,
        )
        self.assertTrue(result.stopped)
        self.assertEqual(result.completed, 1)
        # 停留时长来自配置，不要在测试里写死具体秒数。
        browse_seconds = self.config["timing"]["browse_duration_seconds"]
        self.assertEqual(
            controller.actions,
            ["click:浏览商品", f"wait:{browse_seconds:g}", "back"],
        )


class ExtraRuleTest(unittest.TestCase):
    """界面上的「自定义规则」：用户在手机上填的词要真的进名单。"""

    def config(self) -> dict:
        """用一份最小配置，不依赖 config/tasks.json 的当前内容。"""
        return {"blacklist": ["绑定银行卡"], "browse_tasks": ["浏览商品"]}

    def test_splits_on_any_common_separator(self) -> None:
        """用户在手机上打字，分隔符不可控。"""
        self.assertEqual(split_rules("快手，微博、天猫; 京东\n拼多多"), ["快手", "微博", "天猫", "京东", "拼多多"])

    def test_blank_input_adds_nothing(self) -> None:
        """空词会被当成「任何任务名都包含」，一填就是全部跳过。"""
        for raw in ("", "   ", "，、；", None, 123):
            with self.subTest(raw=raw):
                self.assertEqual(split_rules(raw), [])

    def test_words_merge_into_the_matching_list(self) -> None:
        config = self.config()
        added = apply_extra_rules(
            config, {"extra_skip": "快手,微博", "extra_browse": "逛逛"}
        )
        self.assertEqual(config["blacklist"], ["绑定银行卡", "快手", "微博"])
        self.assertEqual(config["browse_tasks"], ["浏览商品", "逛逛"])
        self.assertCountEqual(added, ["快手(blacklist)", "微博(blacklist)", "逛逛(browse_tasks)"])

    def test_repeated_rules_are_not_duplicated(self) -> None:
        config = self.config()
        apply_extra_rules(config, {"extra_skip": "快手"})
        again = apply_extra_rules(config, {"extra_skip": "快手"})
        self.assertEqual(config["blacklist"].count("快手"), 1)
        self.assertEqual(again, [])

    def test_added_word_really_skips_a_task(self) -> None:
        """光进名单不够，得真的把任务判成跳过。"""
        config = load_config()
        text = "关注小红书领红包"
        self.assertNotEqual(classify_task(text, config).kind, "skip")
        apply_extra_rules(config, {"extra_skip": "小红书"})
        decision = classify_task(text, config)
        self.assertEqual(decision.kind, "skip")
        self.assertEqual(decision.keyword, "小红书")

    def test_unknown_field_is_ignored(self) -> None:
        """字段名写错时不能把无关内容当名单。"""
        config = self.config()
        self.assertEqual(apply_extra_rules(config, {"extra_skipp": "快手"}), [])
        self.assertEqual(config, self.config())


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()