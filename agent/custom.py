"""AutoTaskFlow 的名单读取与任务分类辅助模块。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "tasks.json"

# 覆盖 app.user_id 的环境变量，供不想改配置文件时使用。
APP_USER_ENV = "AUTOTASKFLOW_APP_USER"


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


def app_user_id(config: dict[str, Any] | None = None) -> int:
    """返回要操作的 Android 用户 ID。

    应用分身（小米「手机分身」、一加「应用分身」等）用的是独立 Android 用户，
    常见 ID 是 999。主用户是 0。Pipeline 的 StartApp 只会拉起 user 0，
    所以操作分身必须显式指定用户。
    """
    override = os.environ.get(APP_USER_ENV, "").strip()
    if override:
        try:
            return int(override)
        except ValueError:
            raise ValueError(f"{APP_USER_ENV} 必须是整数，当前为 {override!r}") from None

    app = (config or {}).get("app") or {}
    raw = app.get("user_id", 0)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"config/tasks.json 的 app.user_id 必须是非负整数，当前为 {raw!r}")
    return raw


def start_app_command(package: str, activity: str, user_id: int) -> str:
    """构造启动应用的 adb shell 命令。

    user 0 与分身用户都是同一条命令，只有 --user 不同；刻意不走 StartApp 动作，
    因为协议里的 StartApp 没有 user 参数，永远只能启动主用户。

    这里必须显式写 Activity：`am start` 的通用 LAUNCHER intent 在本机分身用户下
    解析不到淘宝（Error: Activity not started, unable to resolve Intent），
    而 `monkey -p ... --user 999` 在 Android 14 上不支持 --user。
    """
    if not package or not activity:
        raise ValueError("package 与 activity 都不能为空")
    return f"am start --user {int(user_id)} -n {package}/{activity}"


def _contains_any(text: str, candidates: Iterable[str]) -> str:
    for candidate in candidates:
        if candidate and candidate in text:
            return candidate
    return ""


def classify_task(text: str, config: dict[str, Any] | None = None) -> TaskDecision:
    """按黑名单、已完成、搜索、跳转、答题、普通浏览的优先级分类任务。"""
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

    # 答题类必须排在浏览之前：这类任务点进去是一道二选一的单选题，
    # 当浏览任务处理只会停留一会儿再返回，进度永远不涨。
    quiz_hit = _contains_any(normalized_text, config.get("quiz_tasks", []))
    if quiz_hit:
        return TaskDecision("quiz", quiz_hit, "答题任务")

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
