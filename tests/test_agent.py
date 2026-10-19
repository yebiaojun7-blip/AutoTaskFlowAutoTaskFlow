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
        self.assertEqual(controller.actions, ["click:浏览商品", "wait:3", "back"])


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()