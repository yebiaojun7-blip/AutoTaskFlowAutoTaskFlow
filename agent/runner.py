"""AutoTaskFlow 的本地任务调度器和设备控制器接口。"""

from __future__ import annotations

import argparse
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

from agent.custom import TaskDecision, classify_task, load_config


class DeviceController(Protocol):
    """真实设备控制器需要实现的最小接口。"""

    def click(self, text: str) -> None: ...

    def back(self) -> None: ...

    def wait(self, seconds: float) -> None: ...


class SimulationController:
    """不连接设备的演示控制器，用于验证任务调度逻辑。"""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.actions: list[str] = []

    def click(self, text: str) -> None:
        self.actions.append(f"click:{text}")
        self.logger.info("点击任务: %s", text)

    def back(self) -> None:
        self.actions.append("back")
        self.logger.info("返回任务面板")

    def wait(self, seconds: float) -> None:
        self.actions.append(f"wait:{seconds:g}")
        self.logger.info("等待 %.1f 秒", seconds)


@dataclass(frozen=True)
class RunResult:
    completed: int
    skipped: int
    stopped: bool
    stop_reason: str = ""


def create_logger(log_file: Path | None = None) -> logging.Logger:
    logger = logging.getLogger("autotaskflow")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    return logger


def is_safety_text(text: str, keywords: Iterable[str]) -> str:
    for keyword in keywords:
        if keyword and keyword in text:
            return keyword
    return ""


def run_tasks(
    tasks: Iterable[str],
    controller: DeviceController,
    config: dict,
    logger: logging.Logger,
    *,
    sleep: bool = True,
) -> RunResult:
    timing = config.get("timing", {})
    safety_keywords = config.get("safety_keywords", ["安全验证", "拖动滑块", "请完成验证"])
    completed = 0
    skipped = 0

    for index, task_text in enumerate(tasks, start=1):
        safety_hit = is_safety_text(task_text, safety_keywords)
        if safety_hit:
            reason = f"识别到安全验证: {safety_hit}"
            logger.error("[%d] %s，立即停止", index, reason)
            return RunResult(completed, skipped, True, reason)

        decision: TaskDecision = classify_task(task_text, config)
        logger.info("[%d] 分类=%s 文本=%s", index, decision.kind, task_text)
        if decision.kind == "skip":
            skipped += 1
            logger.info("跳过: %s (%s)", decision.keyword, decision.reason)
            continue
        if decision.kind == "unknown":
            skipped += 1
            logger.warning("跳过未识别任务: %s", task_text)
            continue

        controller.click(decision.keyword)
        if decision.kind == "search" and decision.search_keyword:
            logger.info("输入搜索词: %s", decision.search_keyword)
        duration_key = {
            "browse": "browse_duration_seconds",
            "jump": "jump_duration_seconds",
        }.get(decision.kind)
        if duration_key:
            controller.wait(float(timing.get(duration_key, 3)))
        controller.back()
        completed += 1
        if sleep:
            minimum = float(timing.get("min_task_delay_seconds", 1))
            maximum = float(timing.get("max_task_delay_seconds", 3))
            time.sleep(random.uniform(minimum, max(minimum, maximum)))

    logger.info("任务运行结束: 完成=%d, 跳过=%d", completed, skipped)
    return RunResult(completed, skipped, False)


def demo_tasks() -> list[str]:
    return [
        "浏览商品 去完成",
        "搜索商品 去完成",
        "绑定银行卡 去完成",
        "看直播 去完成",
        "安全验证",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 AutoTaskFlow 本地调度模拟")
    parser.add_argument("tasks", nargs="*", help="模拟 OCR 文本；不传则运行内置演示")
    parser.add_argument("--log", type=Path, help="追加写入日志文件")
    parser.add_argument("--no-sleep", action="store_true", help="跳过任务间随机等待")
    args = parser.parse_args()

    logger = create_logger(args.log)
    config = load_config()
    controller = SimulationController(logger)
    result = run_tasks(args.tasks or demo_tasks(), controller, config, logger, sleep=not args.no_sleep)
    logger.info("动作数=%d", len(controller.actions))
    return 1 if result.stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
