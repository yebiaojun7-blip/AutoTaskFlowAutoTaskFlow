"""MaaFramework 自定义动作：淘金币日常任务主循环。

这里只做"设备交互 + 流程编排"，所有判断逻辑都放在 agent/panel.py，
那样没有真机也能跑单元测试。

注意：本模块顶部不导入 maa，maatools 未安装时仍可被导入，
真正的注册发生在 register() 内。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from agent import panel
from agent.custom import app_user_id, load_config, start_app_command

LOGGER = logging.getLogger("autotaskflow")

# 单次运行最多处理多少个任务，防止页面异常时无限循环。
DEFAULT_MAX_ROUNDS = 20
# 主动安全停止的返回标记：让 Pipeline 走 on_error → StopTask。
STOP_SIGNAL = False


def _controller(context: Any) -> Any:
    return context.tasker.controller


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _screencap(context: Any) -> Any:
    """截一张图。缩放到 display_short_side 后，坐标与 OCR box 同一坐标系。"""
    job = _controller(context).post_screencap()
    job.wait()
    return job.get()


def _ocr_items(context: Any, image: Any, node: str) -> list[panel.OcrItem]:
    """在整屏上跑一次 OCR，拿到全部文字块。"""
    detail = context.run_recognition(node, image)
    if not detail:
        LOGGER.warning("OCR 节点 %s 未返回结果", node)
        return []
    return panel.parse_ocr_items(getattr(detail, "all_results", []) or [])


def _click(context: Any, x: int, y: int, *, delay: float = 1.0) -> None:
    LOGGER.info("点击 (%d, %d)", x, y)
    _controller(context).post_click(x, y).wait()
    _sleep(delay)


def _click_item(context: Any, item: panel.OcrItem, *, delay: float = 1.0) -> None:
    x, y = item.center
    _click(context, x, y, delay=delay)


def _back(context: Any, *, delay: float = 1.2) -> None:
    """Android KEYCODE_BACK。Pipeline 里等价于 ClickKey key:[4]。"""
    LOGGER.info("返回")
    _controller(context).post_click_key(4).wait()
    _sleep(delay)


def _swipe_up(context: Any, *, delay: float = 1.0) -> None:
    """从下方往上滑，用于把任务面板滚回顶部。"""
    width, height = _screen_size(context)
    x = width // 2
    _controller(context).post_swipe(x, int(height * 0.75), x, int(height * 0.25), 400).wait()
    _sleep(delay)


def _screen_size(context: Any) -> tuple[int, int]:
    """取控制器分辨率；取不到时退回 1280x720 基准。"""
    try:
        resolution = _controller(context).resolution
        if isinstance(resolution, (list, tuple)) and len(resolution) == 2:
            return int(resolution[0]), int(resolution[1])
    except Exception:  # noqa: BLE001 - 取不到就用基准值，不应因此中断任务
        pass
    return 1280, 720


def _run_search(context: Any, keyword: str, config: dict[str, Any], image: Any) -> None:
    """搜索任务：当前页找输入框 → 输入关键词 → 点搜索 → 停留 → 返回。

    淘宝搜索入口的文案与位置随版本变化，因此这里只在找到疑似输入框时才输入，
    否则退化为"停留 + 返回"，不会因为找不到输入框而卡死。
    """
    items = _ocr_items(context, image, "SearchBoxOcr")
    hint = panel.find_text_by_keywords(items, ("搜索", "搜一搜", "请输入"))
    if hint and keyword:
        _click_item(context, hint, delay=0.8)
        LOGGER.info("输入搜索关键词: %s", keyword)
        _controller(context).post_input_text(keyword).wait()
        _sleep(0.6)
        for item in _ocr_items(context, _screencap(context), "SearchBoxOcr"):
            if panel.normalize_text(item.text) in ("搜索", "搜索一下", "搜"):
                _click_item(context, item, delay=1.5)
                break
    else:
        LOGGER.info("未定位到搜索框，仅停留后返回")

    _sleep(panel.action_duration("jump", config))
    _back(context)


def _run_task(context: Any, pick: panel.PanelPick, config: dict[str, Any], button: panel.OcrItem) -> None:
    """点击任务按钮并按任务类型等待，然后返回任务面板。"""
    kind = pick.decision.kind
    _click_item(context, button, delay=1.5)

    if kind == "search":
        keyword = pick.decision.search_keyword or panel.search_keyword(config)
        _run_search(context, keyword, config, _screencap(context))
        return

    duration = panel.action_duration(kind, config)
    LOGGER.info("任务类型 %s，停留 %.1f 秒", kind, duration)
    _sleep(duration)
    _back(context)


def _scan(context: Any, node: str = "PanelFullOcr") -> list[panel.OcrItem]:
    return _ocr_items(context, _screencap(context), node)


def start_app(context: Any, config: dict[str, Any]) -> bool:
    """按配置启动淘宝，支持应用分身所在的独立 Android 用户。

    为什么不用 Pipeline 的 StartApp 动作：协议里的 StartApp 只有 package 参数，
    由框架固定以当前用户（通常是 user 0）启动。应用分身是另一个 Android 用户
    （本机为 user 999），用 StartApp 永远只会打开主用户那个淘宝。
    """
    app = config.get("app") or {}
    package = str(app.get("package") or "com.taobao.taobao")
    activity = str(app.get("activity") or "com.taobao.tao.welcome.Welcome")
    try:
        user_id = app_user_id(config)
        command = start_app_command(package, activity, user_id)
    except ValueError as exc:
        LOGGER.error("启动参数不合法：%s", exc)
        return False

    LOGGER.info("启动 %s（Android 用户 %d）", package, user_id)
    job = _controller(context).post_shell(command)
    job.wait()
    result = job.get()
    if result is None or "Error" in str(result):
        LOGGER.warning("启动命令返回异常：%s", result)

    return True


def _recover_to_panel(context: Any, config: dict[str, Any], *, tries: int = 6) -> bool:
    """把界面拉回淘金币任务面板。

    只按返回键是回不去的，四种局面必须分开处理：
      1. 已在任务面板 —— 直接返回；
      2. 停在双11 组队活动页 —— 这是广告页，没有任何日常任务，按返回退出；
      3. 停在淘金币赛车首页 —— 这里按返回会直接退出淘金币，
         必须点左下角的「赚金币」才能重新打开任务面板；
      4. 停在淘宝首页 —— 点「领淘金币」图标进入；
      5. 停在促销浮层（如「双11提前领好券」）—— 封面挡住了入口，按返回关掉。
    """
    for _ in range(tries):
        items = _scan(context)
        if panel.task_panel_visible(items):
            return True
        if panel.daily_done_visible(items):
            LOGGER.info("今日任务已全部完成，无需回到任务面板")
            return True

        if panel.team_page_visible(items):
            LOGGER.info("误入组队活动页，返回退出（该页面没有日常任务）")
            _back(context)
            continue

        if panel.coin_home_visible(items):
            entry = panel.find_text_by_keywords(items, panel.COIN_HOME_ENTRY)
            if entry is not None:
                LOGGER.info("在淘金币首页，点击「%s」打开任务面板", entry.text)
                _click_item(context, entry, delay=2.0)
                continue

        entry = panel.coin_entry_point(items)
        if entry is not None:
            # 首页那一排图标常被 OCR 合并成一个文字块（例如
            # 「领淘金币充值中心试用领取常买超市」），块中心落在旁边的「充值中心」上，
            # 直接点块中心会进错页面，所以按关键词在块内的字符位置折算。
            clicked = False
            for keyword in panel.COIN_ENTRY_KEYWORDS:
                point = panel.keyword_center(entry, keyword)
                if point is not None:
                    LOGGER.info("在淘宝首页点击「%s」进入淘金币 %s", keyword, point)
                    _click(context, point[0], point[1], delay=3.0)
                    clicked = True
                    break
            if clicked:
                continue

        _back(context)

    return panel.task_panel_visible(_scan(context))


def open_coin_page(context: Any, config: dict[str, Any]) -> bool:
    """从任意起始页面进入淘金币任务面板，供 Pipeline 入口节点调用。"""
    if _recover_to_panel(context, config):
        LOGGER.info("已进入淘金币任务面板")
        return True
    LOGGER.error("无法进入淘金币任务面板")
    return False



def run_daily_loop(context: Any, config: dict[str, Any], *, max_rounds: int = DEFAULT_MAX_ROUNDS) -> bool:
    """主循环：扫描 → 安全词检测 → 挑一个任务 → 执行 → 回来 → 重复。

    返回值语义：
        True  —— 正常结束（没有可执行任务了，或达到轮次上限）
        False —— 主动安全停止（命中安全验证），调用方应终止任务
    """
    max_empty = int(config.get("timing", {}).get("max_empty_rounds", 3))
    max_attempts = int(config.get("timing", {}).get("max_attempts_per_task", 2))
    empty_rounds = 0
    completed = 0
    attempt = 0
    # 同一个任务该执行几次就够。淘宝的任务面板会把没做完的任务一直摆在原位，
    # 若每次扫描都挑中同一个（例如「点击去逛+30」），停留再久也不会推进，
    # 就会把轮次全部耗在它身上。这里按任务名记执行次数，超过上限就放弃。
    tried: dict[str, int] = {}
    abandoned: set[str] = set()

    for round_index in range(1, max_rounds + 1):
        items = _scan(context)

        safety = panel.find_safety(items, config.get("safety_keywords", []))
        if safety:
            LOGGER.error("命中安全验证「%s」，立即停止任务", safety.text)
            return STOP_SIGNAL

        # 任务全部做完时淘宝会弹"今日奖励已拿完"，这是正常结束而非异常。
        if panel.daily_done_visible(items):
            LOGGER.info("检测到「今日奖励已拿完」提示，今日任务已完成，正常结束")
            return True

        if not panel.task_panel_visible(items):
            LOGGER.info("第 %d 轮：不在任务面板，尝试回到面板", round_index)
            if not _recover_to_panel(context, config):
                LOGGER.error("无法回到淘金币任务面板，停止任务")
                return STOP_SIGNAL
            # 成功找回面板不算空轮次：否则 max_empty_rounds=1 时
            # 刚恢复就被判定为"今日任务已完成"，直接结束整轮任务。
            continue

        pick = panel.pick_task(items, config, exclude=abandoned)
        if pick.item is None:
            if pick.skipped:
                LOGGER.info("本轮不可执行项：%s", ", ".join(pick.skipped))
            if abandoned:
                LOGGER.info("剩余任务都已尝试过 %d 次，无法继续推进，结束", max_attempts)
                return True
            LOGGER.info("第 %d 轮：当前屏无可执行任务，滑回顶部复查", round_index)
            _swipe_up(context)
            items = _scan(context)
            safety = panel.find_safety(items, config.get("safety_keywords", []))
            if safety:
                LOGGER.error("复查时命中安全验证「%s」，立即停止任务", safety.text)
                return STOP_SIGNAL
            pick = panel.pick_task(items, config, exclude=abandoned)
            if pick.item is None:
                empty_rounds += 1
                if empty_rounds >= max_empty:
                    LOGGER.info("连续 %d 轮无任务，判定今日任务已处理完", empty_rounds)
                    return True
                continue

        name = panel.normalize_text(pick.item.text)
        button = panel.find_action_button(items, pick.item) or pick.item
        LOGGER.info(
            "第 %d 轮：执行「%s」（%s），按钮「%s」",
            round_index,
            pick.item.text,
            pick.decision.kind,
            button.text,
        )
        _run_task(context, pick, config, button)
        completed += 1
        empty_rounds = 0
        attempt += 1

        if not _recover_to_panel(context, config):
            LOGGER.warning("任务后未能回到任务面板")

        tried[name] = tried.get(name, 0) + 1
        if _progress_stuck(context, name, tried[name], max_attempts):
            abandoned.add(name)
            LOGGER.warning(
                "任务「%s」已尝试 %d 次仍无进展，本次跳过它", name, tried[name]
            )

        _sleep(_random_delay(config))

    LOGGER.info("达到轮次上限 %d，已执行 %d 个任务", max_rounds, completed)
    return True


def _progress_stuck(context: Any, name: str, attempts: int, max_attempts: int) -> bool:
    """判断某个任务是否该放弃。

    任务名里带 (x/y) 进度时以进度为准：涨了就继续，没涨且已达上限就放弃。
    不带进度时（例如「点击去逛+30」）只能看执行次数，到上限就放弃。
    """
    progress = _find_progress(context, name)
    if progress is not None and progress[0] > 0:
        return False
    return attempts >= max_attempts


def _find_progress(context: Any, name: str) -> tuple[int, int] | None:
    """在任务面板上找到同名任务，读它的进度。"""
    key = name[:6]
    for item in _scan(context):
        if panel.normalize_text(item.text).startswith(key):
            progress = panel.task_progress(item.text)
            if progress is not None:
                return progress
    return None


def _random_delay(config: dict[str, Any]) -> float:
    import random

    timing = config.get("timing", {})
    low = float(timing.get("min_task_delay_seconds", 1))
    high = float(timing.get("max_task_delay_seconds", 3))
    if high <= low:
        return low
    return random.uniform(low, high)


def _parse_params(argv: Any) -> dict[str, Any]:
    """把自定义动作的 custom_action_param 解析成 dict。

    框架透传该字段时可能给出 dict，也可能给出 JSON 字符串；而调用方如果先
    json.dumps 过再塞进 pipeline_override，字符串里还会嵌一层 JSON。
    这里一律解到 dict 为止，避免下游再出现 'str' has no attribute 'get'。
    """
    raw = getattr(argv, "custom_action_param", "") or ""
    for _ in range(3):
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.warning("custom_action_param 不是合法 JSON，忽略：%s", raw)
            return {}
    return raw if isinstance(raw, dict) else {}


def register(agent_server: Any) -> None:
    """把动作注册到 AgentServer。分离出来便于在没有 maa 时也能导入本模块。"""

    from maa.context import Context
    from maa.custom_action import CustomAction

    @agent_server.custom_action("taobao_start_app")
    class TaobaoStartApp(CustomAction):
        """启动淘宝，可指定应用分身所在的 Android 用户。"""

        def run(self, context: Context, argv: Any) -> bool:
            try:
                config = load_config()
            except Exception as exc:  # noqa: BLE001 - 配置读不出来必须显式失败
                LOGGER.error("读取任务配置失败：%s", exc)
                return False
            return start_app(context, config)

    @agent_server.custom_action("taobao_open_coin_page")
    class TaobaoOpenCoinPage(CustomAction):
        """从当前页面进入淘金币任务面板。"""

        def run(self, context: Context, argv: Any) -> bool:
            try:
                config = load_config()
            except Exception as exc:  # noqa: BLE001 - 配置读不出来必须显式失败
                LOGGER.error("读取任务配置失败：%s", exc)
                return False
            return open_coin_page(context, config)

    @agent_server.custom_action("taobao_daily_loop")
    class TaobaoDailyLoop(CustomAction):
        """跑完一整轮淘金币日常任务。custom_action_param 支持 max_rounds 覆盖。"""

        def run(self, context: Context, argv: Any) -> bool:
            try:
                max_rounds = int(_parse_params(argv).get("max_rounds", DEFAULT_MAX_ROUNDS))
            except (TypeError, ValueError):
                LOGGER.warning("max_rounds 不是整数，改用默认值 %d", DEFAULT_MAX_ROUNDS)
                max_rounds = DEFAULT_MAX_ROUNDS

            try:
                config = load_config()
            except Exception as exc:  # noqa: BLE001 - 配置读不出来必须显式失败
                LOGGER.error("读取任务配置失败：%s", exc)
                return False

            LOGGER.info("===== 开始淘金币日常任务（最多 %d 轮）=====", max_rounds)
            return run_daily_loop(context, config, max_rounds=max_rounds)


def register_all(agent_server: Any) -> None:
    """保留一个统一入口，后续新增动作/识别器都从这里挂。"""
    register(agent_server)