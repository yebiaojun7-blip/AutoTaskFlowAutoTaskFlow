"""Agent 入口的日志配置。

这两条都不是风格问题：客户端按 stderr 判定 AgentError，按自己的时间列显示
时间。配错了会出现「INFO 进度显示成红色报错」和「每条日志两段时间」。
"""

from __future__ import annotations

import logging
import sys
import unittest

from agent import main


class LoggingSetupTest(unittest.TestCase):
    def setUp(self) -> None:
        # basicConfig 在 root 已有 handler 时什么都不做，测试里要清掉再用，
        # 结束后原样恢复，免得影响其它用例的日志输出。
        self._saved = logging.root.handlers[:]
        logging.root.handlers.clear()

    def tearDown(self) -> None:
        logging.root.handlers.clear()
        logging.root.handlers.extend(self._saved)

    def _install(self) -> logging.Handler:
        main._setup_logging()
        self.assertTrue(logging.root.handlers, "未安装任何 handler")
        return logging.root.handlers[0]

    def test_logs_go_to_stdout_not_stderr(self) -> None:
        """客户端把 stderr 一律当 AgentError，正常进度不能走那里。"""
        handler = self._install()
        self.assertIs(handler.stream, sys.stdout)

    def test_body_carries_no_timestamp(self) -> None:
        """时间由客户端单独渲染，正文再带一遍就是两段时间。"""
        handler = self._install()
        assert handler.formatter is not None
        rendered = handler.formatter.format(
            logging.LogRecord("autotaskflow", logging.INFO, __file__, 1, "任务开始", None, None)
        )
        self.assertEqual(rendered, "[INFO] autotaskflow: 任务开始")
        self.assertNotRegex(rendered, r"\d{4}-\d{2}-\d{2}")


if __name__ == "__main__":
    unittest.main()
