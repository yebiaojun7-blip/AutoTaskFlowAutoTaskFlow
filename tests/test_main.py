"""Agent 入口的日志配置。

这三条都不是风格问题：客户端按 stderr 判定 AgentError、按自己的时间列显示
时间、按行首的 `[LEVEL]` 决定这一条的级别列。配错了会出现「INFO 进度显示成
红色报错」「每条日志两段时间」「正文里多出一串没人解析的方括号」。
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

    def _render(self, handler: logging.Handler, message: str, level: int = logging.INFO) -> str:
        assert handler.formatter is not None
        return handler.formatter.format(
            logging.LogRecord("autotaskflow", level, __file__, 1, message, None, None)
        )

    def test_logs_go_to_stdout_not_stderr(self) -> None:
        """客户端把 stderr 一律当 AgentError，正常进度不能走那里。"""
        handler = self._install()
        self.assertIs(handler.stream, sys.stdout)

    def test_body_carries_no_timestamp(self) -> None:
        """时间由客户端单独渲染，正文再带一遍就是两段时间。"""
        handler = self._install()
        rendered = self._render(handler, "任务开始")
        self.assertNotRegex(rendered, r"\d{4}-\d{2}-\d{2}")
        self.assertNotRegex(rendered, r"\d{2}:\d{2}:\d{2}")

    def test_level_stays_at_the_line_head(self) -> None:
        """客户端靠行首的 `[LEVEL]` 决定级别列，不能换格式也不能挪位置。"""
        handler = self._install()
        self.assertTrue(self._render(handler, "任务开始").startswith("[INFO] "))
        self.assertTrue(
            self._render(handler, "任务中止", level=logging.ERROR).startswith("[ERROR] ")
        )
        self.assertTrue(
            self._render(handler, "没能执行", level=logging.WARNING).startswith("[WARNING] ")
        )

    def test_body_does_not_repeat_the_logger_name(self) -> None:
        """只有一个 logger，名称每行重复一遍既占宽又没信息量。"""
        handler = self._install()
        self.assertEqual(self._render(handler, "任务开始"), "[INFO] 任务开始")


if __name__ == "__main__":
    unittest.main()
