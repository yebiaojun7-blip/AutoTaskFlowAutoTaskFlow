"""AutoTaskFlow 的名单读取与任务分类辅助模块。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "tasks.json"

# 覆盖 app.user_id 的环境变量，供不想改配置文件时使用。
APP_USER_ENV = "AUTOTASKFLOW_APP_USER"

# 界面上「自定义规则」配置项写进 custom_action_param 的字段名 → 对应的名单键。
# 字段名用 ASCII 是为了 pipeline_override 里的 {占位符} 好读，中文只出现在界面标签上。
EXTRA_RULE_FIELDS: dict[str, str] = {
    "extra_skip": "blacklist",
    "extra_jump": "jump_tasks",
    "extra_search": "search_tasks",
    "extra_quiz": "quiz_tasks",
    "extra_browse": "browse_tasks",
}

# 一个输入框里可以写多个词，逗号、顿号、分号、空白都当分隔符。
_RULE_SEPARATORS = re.compile(r"[,，、;；\s]+")


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


def split_rules(raw: Any) -> list[str]:
    """把界面上一个输入框的内容拆成词表。

    用户在手机上打字，分隔符不可控：可能用逗号、顿号，也可能一行一个。
    空串、纯空白输入都返回空表，绝不能让一个空词进名单 —— 空词会被
    _contains_any 判成「命中」，那样所有任务都会被跳过。
    """
    if not isinstance(raw, str):
        return []
    words: list[str] = []
    for piece in _RULE_SEPARATORS.split(raw):
        word = piece.strip()
        if word and word not in words:
            words.append(word)
    return words


def apply_extra_rules(config: dict[str, Any], params: dict[str, Any]) -> list[str]:
    """把界面上手动加的规则并进名单，返回实际新增的「词(类别)」。

    就地改 config：主循环、pick_task、classify_task 共用同一份配置对象，
    在这里并一次就够，不必给每条调用链都加一个参数。

    新词并按进对应名单，因此继承该类别原有的优先级（跳过最先判，浏览最后判）。
    注意匹配是**包含**匹配：往跳过里写「快手」，任务名里带「快手」的都会跳过。
    """
    added: list[str] = []
    for field, list_name in EXTRA_RULE_FIELDS.items():
        words = split_rules(params.get(field))
        if not words:
            continue
        existing = config.get(list_name)
        if not isinstance(existing, list):
            existing = []
            config[list_name] = existing
        for word in words:
            if word in existing:
                continue
            existing.append(word)
            added.append(f"{word}({list_name})")
    return added


def _contains_any(text: str, candidates: Iterable[str]) -> str:
    for candidate in candidates:
        if candidate and candidate in text:
            return candidate
    return ""


def classify_task(
    text: str,
    config: dict[str, Any] | None = None,
    *,
    hint: str = "",
) -> TaskDecision:
    """按黑名单、已完成、搜索、跳转、答题、普通浏览的优先级分类任务。

    hint 是任务名下方那行说明文字（「点击去逛」「参与答题」「浏览15秒」）。
    任务名本身认不出来时才用它兜底：面板上有的任务名完全是自造词
    （「去神奇海洋逛逛吧」「领闪购红包下单直接省」），名单里永远匹配不到，
    而类型其实写在副标题上。优先级仍以任务名为准 —— 副标题只是泛化描述，
    例如「去芭芭农场逛一逛」名字里就带「逛一逛」，不该被副标题改判。
    """
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

    return _classify_by_hint(hint, config)


def _classify_by_hint(hint: str, config: dict[str, Any]) -> TaskDecision:
    """任务名认不出来时，按副标题判类型。"""
    normalized_hint = "".join((hint or "").split())
    if not normalized_hint:
        return TaskDecision("unknown", reason="未匹配到可执行名单")

    # 「点击去逛」「去逛逛」这类副标题代表跳到外部页面或会场，
    # 与 jump_tasks 里的任务同样处理：点进去、确认弹框、待够时长。
    if _contains_any(normalized_hint, config.get("jump_tasks", [])):
        return TaskDecision("jump", hint, "按任务说明判定为跳转类")

    if _contains_any(normalized_hint, config.get("search_tasks", [])):
        keywords = config.get("search_keywords", [])
        return TaskDecision(
            "search",
            hint,
            "按任务说明判定为搜索类",
            str(keywords[0]) if keywords else "",
        )

    if _contains_any(normalized_hint, config.get("quiz_tasks", [])) or "答题" in normalized_hint:
        return TaskDecision("quiz", hint, "按任务说明判定为答题类")

    # 「浏览15秒」「浏览5秒」：任务名认不出来，但说明写明了是浏览。
    # 只认带「浏览」的说明文字，不把任何副标题都当浏览任务处理。
    if "浏览" in normalized_hint:
        return TaskDecision("browse", hint, "按任务说明判定为浏览类")

    return TaskDecision("unknown", reason="未匹配到可执行名单")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="分类一条 OCR 任务文本")
    parser.add_argument("text", help="OCR 识别出的任务文本")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    print(classify_task(args.text, load_config(args.config)))
