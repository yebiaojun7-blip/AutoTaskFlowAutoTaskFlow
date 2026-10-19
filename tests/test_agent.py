"""Agent 分类和安全停止行为测试。"""

import logging
import unittest

from agent.custom import classify_task, load_config
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


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()