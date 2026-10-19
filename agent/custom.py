"""AutoTaskFlow 的名单读取与任务分类辅助模块。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "tasks.json"


@dataclass(frozen=True)
class TaskDecision:
    """Pipeline 执行任务前使用的分类结果。

    三个字符串字段语义严格分开，不要互换：
        keyword        —— 命中的名单词，就是要点击的文字
        reason         —— 写进日志的人类可读说明
        search_keyword —— 搜索类任务真正输入到搜索框的关键词
    """

    kind: str
    keyword: str = ""
    reason: str = ""
    search_keyword: str = ""


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    """读取名单配置，并检查其根节点是对象。"""
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as config_file:
        config = json.load(config_file)
    if not isinstance(config, dict):
        raise ValueError("任务配置根节点必须是 JSON 对象")
    return config


def _contains_any(text: str, candidates: Iterable[str]) -> str:
    for candidate in candidates:
        if candidate and candidate in text:
            return candidate
    return ""


def classify_task(text: str, config: dict[str, Any] | None = None) -> TaskDecision:
    """按黑名单、已完成、搜索、跳转、普通浏览的优先级分类任务。"""
    config = config or load_config()
    normalized_text = "".join(text.split())

    blacklist_hit = _contains_any(normalized_text, config.get("blacklist", []))
    if blacklist_hit:
        return TaskDecision("skip", blacklist_hit, "命中黑名单")

    completed = _contains_any(normalized_text, ("已完成", "已领取"))
    if completed:
        return TaskDecision("skip", completed, "任务已完成")

    search_hit = _contains_any(normalized_text, config.get("search_tasks", []))
    if search_hit:
        keywords = config.get("search_keywords", [])
        return TaskDecision(
            "search",
            search_hit,
            f"搜索任务，命中「{search_hit}」",
            str(keywords[0]) if keywords else "",
        )

    jump_hit = _contains_any(normalized_text, config.get("jump_tasks", []))
    if jump_hit:
        return TaskDecision("jump", jump_hit, "跨应用或会场任务")

    browse_hit = _contains_any(normalized_text, config.get("browse_tasks", []))
    if browse_hit:
        return TaskDecision("browse", browse_hit, "普通浏览任务")

    return TaskDecision("unknown", reason="未匹配到可执行名单")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="分类一条 OCR 任务文本")
    parser.add_argument("text", help="OCR 识别出的任务文本")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    print(classify_task(args.text, load_config(args.config)))
