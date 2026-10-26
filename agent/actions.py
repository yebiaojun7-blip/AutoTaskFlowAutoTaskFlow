"""MaaFramework 自定义动作：淘金币日常任务主循环。

这里只做"设备交互 + 流程编排"，所有判断逻辑都放在 agent/panel.py，
那样没有真机也能跑单元测试。

注意：本模块顶部不导入 maa，maatools 未安装时仍可被导入，
真正的注册发生在 register() 内。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, NamedTuple, Sequence, Callable, Sequence

from agent import panel
from agent.custom import apply_extra_rules, app_user_id, load_config, start_app_command

LOGGER = logging.getLogger("autotaskflow")

# 从 dumpsys 里取前台包名：
#   topResumedActivity=ActivityRecord{263017643 u999 com.taobao.taobao/...}
_FOREGROUND_RE = re.compile(r"topResumedActivity=ActivityRecord\{[^}]*?\s((?:[a-zA-Z0-9_]+\.)+[a-zA-Z0-9_]+)/")

# 单次运行的**安全兜底**轮数，不是「该做多少个任务」的目标值。
#
# 主循环真正该停的判据只有一条：面板上已经没有可执行任务了（黑名单、做满
# (x/y)、右侧没有按钮的行都不算可执行）。曾经把这里当目标值用（20），于是
# 双11能量面板上三个 (x/10) 任务加十来个跨应用任务做不完就报「任务完成」——
# 明明只推进了七格。任务量由面板决定，不由这个数字决定。
#
# 留这个上限只是为了不在页面异常时无限打转（例如某个任务点了不涨进度、
# 又永远不被计入放弃）。真跑满它说明判据出了新问题，日志会明确写出来。
DEFAULT_MAX_ROUNDS = 120

# 连续多少次「回不到目标面板」才放弃整个任务。
# 恢复到同一张面板要重开淘宝、等首页渲染、再点入口，偶发一次失败很常见；
# 一次失败就 StopTask 会把已经做了十几分钟的任务整轮作废（真机 09:35 那次
# 只做了 8 轮就中止，而屏上还有一多半任务）。
_MAX_LOST_STREAK = 3
# 任务名短于这个长度就只可能是 OCR 碎片（「看」「好物」「0」），不是任务。
# 这些碎片既匹配不到任何名单、也没法按名字去重，被挑中就是空耗一轮停留。
_MIN_TASK_NAME_LEN = 3

# 每多少轮打一条进度日志。一轮要一分多钟，没有周期日志时日志里全是失败
# 重试，看不出「其实还在往前推进」。
_PROGRESS_LOG_EVERY = 5

# 同一个任务「点了但页面没反应」最多重试几次。空点通常是浮层挡住了按钮，
# 或这一行刚被重建；重试几次就能中。但按钮彻底失效时要有封顶。
_MAX_NO_OP_RETRIES = 3

# 主动安全停止的返回标记：让 Pipeline 走 on_error → StopTask。
STOP_SIGNAL = False

# 「本轮不可执行项」日志里最多列几条。屏上认出的界面碎片可以有十几条，
# 全铺出来没人看得下去，剩下的只进 debug。
_SKIPPED_LOG_LIMIT = 4

# 一屏任务都做过时，最多再往下翻几屏去找没做过的任务。
# 面板一屏大约 6 行、整个列表十来行，翻 3 屏足够到底；不设上限会在到底之后
# 反复滑动，把轮次全耗在滑动手势上。
_PANEL_SCROLL_DEPTH = 3

# 点完首页入口后，等目标面板真正渲染出来的重试次数。
# 这个界面在虚拟屏上只有 2 FPS（日志里的「游戏画面帧率仅 2 FPS」），页面切换
# 要持续好几秒。点完立刻判一次，截到的是「上半屏已是面板、下半屏还是首页」的
# 过渡态，两边判定都不成立，于是误判成没进去而按了返回，把刚打开的面板关掉。
_ENTRY_RENDER_TRIES = 3


class PanelProfile(NamedTuple):
    """一个任务面板的差异点。

    双11「做任务赚能量」面板和淘金币任务面板的**行结构完全一样**：左边任务名
    带 (x/y)、下面一行说明、右侧「去完成」，所以挑选任务、点按钮、停留、
    返回、放弃无进展任务这一整套都能原样复用，只有三件事不一样：

      visible      —— 怎么确认「现在就在这个面板上」
      home_entry   —— 淘金币首页上打开本面板的入口文字
      has_overall  —— 面板顶部有没有可信的「完成进度 n/m」

    has_overall 必须严格：能量面板顶部写的是「当前进度 0/50」，那是活动累计
    计数（从 0 开始、做任务才涨），不是今日完成度。把它当完成度会让刚开局的
    任务在第一轮就被判为「今日已全部做完」。
    """

    name: str
    visible: Callable[[Sequence[panel.OcrItem]], bool]
    home_entry: tuple[str, ...]
    has_overall: bool
    daily_done: bool = True


COIN_PROFILE = PanelProfile(
    "淘金币任务面板",
    panel.task_panel_visible,
    panel.COIN_HOME_ENTRY,
    True,
)

# 能量面板没有「今日奖励已拿完」这类完成提示页（面板顶部是活动累计进度，
# 任务做完了也照样列在那里），所以 daily_done 关掉，靠连续空轮收尾。
ENERGY_PROFILE = PanelProfile(
    "双11赚能量面板",
    panel.energy_panel_visible,
    panel.ENERGY_HOME_ENTRY,
    False,
    daily_done=False,
)


def _controller(context: Any) -> Any:
    return context.tasker.controller


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


# 当前这轮截图的真实尺寸（宽, 高）。点击和滑动都必须用这个坐标系。
_FRAME_SIZE: tuple[int, int] | None = None


def _screencap(context: Any) -> Any:
    """截一张图。缩放到 display_short_side 后，坐标与 OCR box 同一坐标系。"""
    global _FRAME_SIZE
    job = _controller(context).post_screencap()
    job.wait()
    image = job.get()
    shape = getattr(image, "shape", None)
    if shape is not None and len(shape) >= 2:
        _FRAME_SIZE = (int(shape[1]), int(shape[0]))
    return image


def _ocr_items(context: Any, image: Any, node: str) -> list[panel.OcrItem]:
    """在整屏上跑一次 OCR，拿到全部文字块。"""
    detail = context.run_recognition(node, image)
    if not detail:
        LOGGER.warning("OCR 节点 %s 未返回结果", node)
        return []
    return panel.parse_ocr_items(getattr(detail, "all_results", []) or [])


def _click(context: Any, x: int, y: int, *, delay: float = 1.0) -> None:
    # 坐标是排查用的细节，不是给用户看的流程：调用方已经说清点了什么、为什么点。
    LOGGER.debug("点击 (%d, %d)", x, y)
    _controller(context).post_click(x, y).wait()
    _sleep(delay)


def _click_item(context: Any, item: panel.OcrItem, *, delay: float = 1.0) -> None:
    x, y = item.center
    _click(context, x, y, delay=delay)


def _back(context: Any, *, delay: float = 1.2) -> None:
    """Android KEYCODE_BACK。Pipeline 里等价于 ClickKey key:[4]。"""
    LOGGER.debug("返回")
    _controller(context).post_click_key(4).wait()
    _sleep(delay)


# Android KEYCODE_ENTER。搜索框没有提交按钮时用它提交，
# 但**不能**用它来误当输入完成信号：输入法弹窗里也能触发。
_KEYCODE_ENTER = 66


def _enter(context: Any, *, delay: float = 1.0) -> None:
    """按一次回车，用于提交搜索。"""
    LOGGER.debug("按下回车")
    _controller(context).post_click_key(_KEYCODE_ENTER).wait()
    _sleep(delay)


# 滑动手势必须落在这个纵向区间里。
# 下边留到 0.85：再往下是系统手势导航条和淘宝底部标签栏，手指按在那里会被
# 当成返回/切页手势，整次滑动直接被吞掉（实测 0.92 以下 diff 从 72 跌到 0.13）。
# 上边留到 0.18：再往上会盖到状态栏或页面顶部横幅。
_SCROLL_SAFE_TOP = 0.18
_SCROLL_SAFE_BOTTOM = 0.85


def _swipe_up(context: Any, *, delay: float = 1.0) -> None:
    """从下方往上滑：手指向上，页面向下走一屏，露出列表**下面**的任务。"""
    width, height = _screen_size(context)
    x = width // 2
    start = int(height * _SCROLL_SAFE_BOTTOM)
    end = int(height * _SCROLL_SAFE_TOP)
    _controller(context).post_swipe(x, start, x, end, 400).wait()
    _sleep(delay)


def _swipe_to_top(context: Any, *, delay: float = 1.0) -> None:
    """从上方往下拖：页面向回走一屏，露出列表**上面**的任务。

    方向和 _swipe_up 相反，两者不能混用。没做完的任务永远在面板顶部，
    而「滑回顶部复查」曾经写成 _swipe_up —— 手指向上，页面反而继续往下走，
    复查看到的是更下面那一批（真机 12:05:08 那一帧就是下面的任务），
    顶部没做完的行再也回不到屏幕上。
    """
    width, height = _screen_size(context)
    x = width // 2
    start = int(height * _SCROLL_SAFE_TOP)
    end = int(height * _SCROLL_SAFE_BOTTOM)
    _controller(context).post_swipe(x, start, x, end, 400).wait()
    _sleep(delay)


def _scroll_to_top(context: Any, times: int) -> None:
    """按「往下翻过几屏」原样拖回几屏，把面板恢复到顶部。"""
    for _ in range(max(int(times), 0)):
        _swipe_to_top(context)


# 清单 / 会场 / 课堂这类页面靠滑动触发加载，页面静止不动就不再计时长，
# 所以停留期间要定时上下滑一下。
_SCROLL_INTERVAL = 5.0
# 单次滑动只用屏幕中间一段，留在内容区里：贴边滑会触发下拉刷新或被当成返回手势。
_SCROLL_TOP = 0.32
_SCROLL_BOTTOM = 0.68


def _scroll_once(context: Any, *, upward: bool = True) -> None:
    """在页面内容区滑一小段。

    upward=True 是「看后面的内容」，停留浏览时用；upward=False 是手指往下拖，
    把页面拉回顶部，用来找被滚出屏幕的入口图标。
    """
    width, height = _screen_size(context)
    x = width // 2
    if upward:
        start, end = _SCROLL_BOTTOM, _SCROLL_TOP
    else:
        start, end = _SCROLL_TOP, _SCROLL_BOTTOM
    _controller(context).post_swipe(
        x, int(height * start), x, int(height * end), 500
    ).wait()
    _sleep(0.5)


def _dwell(context: Any, seconds: float, *, scroll: bool) -> None:
    """在目标页停留指定秒数；需要滑动的页面边滑边等。

    滑动不是「顺便做的」而是计时本身：淘宝的浏览任务按页面有没有在动累计时长，
    界面完全静止时不计时。所以停留期间必须持续滑动，而且不能只滑一次就停。
    """
    if seconds <= 0:
        return
    if not scroll:
        _sleep(seconds)
        return
    remaining = seconds
    while remaining > 0:
        chunk = min(_SCROLL_INTERVAL, remaining)
        _sleep(chunk)
        remaining -= chunk
        if remaining > 0:
            _scroll_once(context)


def _job_succeeded(job: Any) -> bool:
    """判断一个 maa 作业是否成功。

    `Job.succeeded` 在不同版本里是 property 也可能是方法，两种都要认。
    判不出来时按「未知 → 当作失败」处理：shell 能力是可选能力，把没执行当成
    执行成功会让上层拿到一个空字符串继续往下跑，错误被推到很远的地方才暴露。
    """
    value = getattr(job, "succeeded", None)
    if value is None:
        return False
    if callable(value):
        try:
            value = value()
        except Exception:  # noqa: BLE001 - 判定本身出错时按失败处理
            return False
    return bool(value)


def _shell(context: Any, command: str) -> str | None:
    """执行一条 shell 命令；控制器没有 shell 能力时返回 None。

    手机上用 MaaFwApp + Shizuku 那条链路跑时，控制器是 MaaFramework 的 Android
    原生控制器：它只实现了截屏和触摸，没继承 ShellableUnit，因此 post_shell
    不会抛异常，只是作业失败、输出为空（源码里是 "Shell is not supported for
    this controller type"）。所以要显式判一次成功与否，否则会把「没执行」
    当成「执行了但没输出」。
    """
    try:
        job = _controller(context).post_shell(command)
        job.wait()
    except Exception:  # noqa: BLE001 - 控制器不支持时可能直接抛，当作没有 shell
        return None
    # maa 的 Job.succeeded 是 property，不是方法：写成 succeeded() 会 TypeError，
    # 而判 callable 再调用会静默跳过这次检查，把「作业失败」当成「执行成功但没输出」，
    # 于是 am start 没生效也被当作启动成功，任务一路跑到截图才发现淘宝根本没在前台。
    if not _job_succeeded(job):
        return None
    try:
        return str(job.get() or "")
    except Exception:  # noqa: BLE001 - 作业失败时取结果可能抛，同上
        return None


# 拿不到截图尺寸时的兜底值，与 interface.json 的 display_short_side 不冲突。
_FALLBACK_FRAME = (1280, 720)


def _screen_size(context: Any) -> tuple[int, int]:
    """手势坐标系：必须是截图尺寸，不能用设备原始分辨率。

    post_click / post_swipe 收的是截图坐标系里的点（截图按 display_short_side
    缩放过）。设备分辨率是另一套数字：手机 1080x2376 上截图只有 720x1584。
    按设备分辨率算滑动起点会落到截图范围外，手势被当成从屏幕顶部按下，
    于是「往上滑」变成点顶部、页面纹丝不动；往下滑因为终点被截断到边界，
    碰巧还是一次合法拖动，所以只有往上滑出问题。
    """
    if _FRAME_SIZE is not None:
        return _FRAME_SIZE
    # 正常路径上每次循环都先截图再动手，不会走到这里；真走到了宁可用兜底值，
    # 也不能回头去读设备分辨率——那正是这个 bug 的来源。
    LOGGER.warning("尚未截到图，滑动手势暂用 %dx%d 兜底坐标", *_FALLBACK_FRAME)
    return _FALLBACK_FRAME


def _wait_for(predicate: Any, context: Any, *, tries: int, delay: float) -> list[panel.OcrItem]:
    """反复截图 + OCR，直到 predicate(items) 为真；返回最后一次的识别结果。

    「点完任务按钮立刻找搜索框」是行不通的：淘宝要先把搜索页渲染出来，
    那一刻 OCR 只拿到一个空白页，于是判定「找不到输入框」，搜索任务被静默
    退化成「停留 + 返回」—— 页面没搜过，奖励自然一分不拿。
    """
    items: list[panel.OcrItem] = []
    for attempt in range(tries):
        items = _scan(context)
        if predicate(items):
            return items
        if attempt + 1 < tries:
            _sleep(delay)
    return items


def _run_search(
    context: Any,
    keyword: str,
    config: dict[str, Any],
    *,
    required: float = 0.0,
) -> bool:
    """搜索任务：找到输入框 → 输入关键词 → 提交 → 在结果页停留 → 返回。

    返回是否真的提交了搜索。没提交时调用方要把这次算作失败而不是完成 ——
    页面没搜过，进度不会涨，当成成功只会反复重跑同一个任务。

    required 是任务自己写明的浏览秒数（「浏览15秒」），搜索后要按它停留，
    不能只用配置里的兜底值：搜索任务的奖励挂在「搜完再浏览」这一步上。
    """
    # 先等提交按钮出现，再按它的行去定位输入框：页面顶部的推广横幅也写着
    # 「搜索有福利」，只认「搜索」两个字会点到横幅上，输入框拿不到焦点，
    # 关键词敲进去就丢了（真机实测进度一直停在 1/3）。
    #
    # 重试次数按「页面加载要几秒」定，不是按「多试几次更保险」定：真机上一次
    # 全屏 OCR 要十几到三十几秒，多留的每一次重试都是实打实的白等。搜索页通常
    # 在第一次就绪，三次已经覆盖掉「点进去还没渲染完」这一种情况。
    items = _wait_for(
        lambda current: panel.search_submit_button(current) is not None,
        context,
        tries=3,
        delay=1.5,
    )
    submit = panel.search_submit_button(items)
    box = panel.search_box(items, submit)

    if box is None or not keyword:
        reason = "未定位到搜索框" if box is None else "没有可用的搜索关键词"
        LOGGER.warning("%s，只能停留后返回（这次搜索不计入完成）", reason)
        _dwell(context, panel.dwell_seconds("jump", config), scroll=True)
        _back(context)
        return False

    _click_item(context, box, delay=1.2)
    LOGGER.info("输入搜索关键词: %s", keyword)
    # post_input_text 底层是 adb `input text`，它只认 ASCII：中文会让进程直接
    # 抛 NullPointerException 失败。作业成败必须判，否则「输入失败 → 回车提交空
    # 搜索 → 原地停留」会被当成搜索成功，任务进度一分不涨。
    input_job = _controller(context).post_input_text(keyword)
    input_job.wait()
    if not _job_succeeded(input_job):
        LOGGER.warning("关键词「%s」没有输入成功（adbd 的 input text 不支持非 ASCII）", keyword)
        _back(context)
        return False
    _sleep(0.8)

    # 输入完成后弹出输入法，搜索栏位置会变，提交按钮要重新找一次。
    # 淘宝的搜索页有时根本没有按钮，那就发一次回车 —— 搜索框回车就是提交。
    submit = panel.search_submit_button(_scan(context))
    if submit is not None:
        LOGGER.info("点击「%s」提交搜索", submit.text)
        _click_item(context, submit, delay=1.8)
    else:
        LOGGER.info("没有搜索按钮，用回车提交")
        _enter(context, delay=1.8)

    # 结果页同样要滑着才算浏览，界面静止不计时长。
    total = panel.dwell_seconds("jump", config, required)
    LOGGER.info("搜索结果页停留 %.1f 秒", total)
    _dwell(context, total, scroll=True)
    _back(context)
    return True


def _run_quiz(context: Any, *, tries: int = 3) -> bool:
    """答题任务：选一个选项 → 提交 → 等结果页，然后返回任务面板。

    「淘金币趣味课堂」点进去是一道二选一的单选题，不是浏览页。当成浏览任务
    停留再久也不会推进：奖励挂在「提交答案」这一步上，页面上的选项不点、
    「我选好了」不按，任务永远停在原处，主循环就会一直重跑它。

    答案猜不出来也不需要猜 —— 任务文案是「参与答题」，不是「答对」。
    返回是否真的答完（看到结果页）。没答完时调用方必须把这次算作失败。

    tries 同样按「真机一次 OCR 十几到三十几秒」来定：答题页只有一屏内容，
    渲染完就一直在，多留的重试只会让失败路径白等两分钟。
    """
    items = _wait_for(lambda current: panel.quiz_option(current) is not None, context, tries=tries, delay=1.5)
    option = panel.quiz_option(items)
    if option is None:
        LOGGER.warning("答题页上没找到选项，退出这次答题")
        return False

    LOGGER.info("选择答案「%s」", option.text)
    _click_item(context, option, delay=1.5)

    # 「我选好了」在选中之前是灰的，点不动，所以必须选完再找它。
    submit = panel.quiz_submit(_scan(context))
    if submit is None:
        LOGGER.warning("没找到「我选好了」按钮，答案没有提交")
        return False

    LOGGER.info("点击「%s」提交答案", submit.text)
    _click_item(context, submit, delay=2.0)

    result = _wait_for(panel.quiz_answered, context, tries=tries, delay=1.5)
    if not panel.quiz_answered(result):
        LOGGER.warning("提交后没看到答题结果页，这次答题不算完成")
        return False
    LOGGER.info("答题完成")
    # 结果页底部是「已成功领取奖励 回到主页」，按返回直接退回任务面板。
    _back(context)
    return True


# 一次任务执行的结果。必须区分「做失败」与「根本没发生」：
#   OK      —— 流程走完，进度应该涨了
#   FAILED  —— 进去了但没做成（搜索框没找到、答案没提交），该计一次失败
#   NOTHING —— 连页面都没动（跳转空点、确认框没点到确认），这不是这个任务
#              的错，不能计入「试了几次没进展」，否则一个本来能做的任务会被
#              三次空点提前放弃。
TASK_OK = "ok"
TASK_FAILED = "failed"
TASK_NOTHING = "nothing"


def _run_task(
    context: Any,
    pick: panel.PanelPick,
    config: dict[str, Any],
    button: panel.OcrItem,
    items: list[panel.OcrItem],
) -> str:
    """点击任务按钮并按任务类型等待，然后返回任务面板。

    items 是面板上已识别出来的内容，用来读任务自己写明的停留要求，
    避免为了这个再截一次图。

    返回 TASK_OK / TASK_FAILED / TASK_NOTHING（语义见上面常量注释）。
    搜索类没提交成搜索时返回 TASK_FAILED：页面没搜过，进度不会涨，
    当成成功会让主循环反复重跑同一个任务。
    """
    kind = pick.decision.kind
    _click_item(context, button, delay=1.5)

    if kind == "search":
        keyword = pick.decision.search_keyword or panel.search_keyword(config)
        required = panel.required_seconds(items, pick.item)
        done = _run_search(context, keyword, config, required=required)
        return TASK_OK if done else TASK_FAILED

    # 答题任务点进去是单选题，必须选答案、点提交才有奖励；
    # 当成浏览任务停留只会空等，进度一动不动。
    if kind == "quiz":
        return TASK_OK if _run_quiz(context) else TASK_FAILED

    # 跳转类任务（蚂蚁庄园 / 蚂蚁森林 / 跳闲鱼 / 站外页）点下去会弹确认框，
    # 要真的点确认进去做，不能取消：取消就是放弃任务。
    # 这个框盖在面板上方且面板文字仍能被识别，不处理就会永远卡在这里。
    if kind == "jump":
        popped = _scan(context)
        if not panel.open_app_dialog_visible(popped) and panel.same_task_still_visible(
            popped, pick.item.text
        ):
            # 只截一帧就判空点会误伤：弹框和小程序首屏有时要两三秒才出来，
            # 正好扫在过渡帧上就会把一次成功的跳转判成空点、白白多跑一轮。
            # 所以再等两眼，等它要么弹出确认框、要么任务行已经不在屏上。
            popped = _wait_for(
                lambda current: panel.open_app_dialog_visible(current)
                or not panel.same_task_still_visible(current, pick.item.text),
                context,
                tries=3,
                delay=1.5,
            )
        if panel.open_app_dialog_visible(popped):
            if _open_app_dialog_action(context, popped) is None:
                # 弹框在、但没点到确认按钮：再停留只会停在弹框上白等。
                LOGGER.warning("确认框上没找到确认按钮，这次跳转不算完成")
                _back(context)
                return TASK_FAILED
            LOGGER.info("已确认打开外部小程序，进去做任务「%s」", pick.item.text)
        elif panel.same_task_still_visible(popped, pick.item.text):
            # 压根没出弹框，任务行却原封不动。真机上这一下常常是空点（按钮上
            # 还有别的浮层、或这一行刚被重建），然后停留十几秒、返回、计入一轮 ——
            # 10:20 那次第 10/11/12 轮就是这样白跑了三分多钟。
            # 空点不算失败：任务本身没毛病，只是这一下没点中，重试同一行即可。
            LOGGER.warning(
                "点「%s」后任务仍在屏上，这次跳转没有真正发生，不计入完成",
                button.text,
            )
            return TASK_NOTHING
        else:
            # 既没弹出确认框、原任务行也不在屏上了：界面确实换了地方，但认不出
            # 换到了哪。这一档原先什么都不记，日志里「执行」之后就是近一分钟的
            # 空白 —— 11:35 那次「去淘特赚钱抵20%」连着三轮都这样，事后完全看
            # 不出它跳没跳、停在哪。至少要留下这一行的判定依据。
            LOGGER.info(
                "点「%s」后未出现确认框，任务行也已不在屏上，按已跳出处理",
                button.text,
            )

    required = panel.required_seconds(items, pick.item)
    total = panel.dwell_seconds(kind, config, required)
    if required > 0:
        LOGGER.info("任务要求停留 %.0f 秒，实际停留 %.0f 秒", required, total)
    elif kind == "jump":
        # 跳转任务不写停留秒数，但奖励是在对方页面里待够时间才给。不记这一行，
        # 日志里「执行」之后就是近一分钟的空白，看不出这段时间到底在干什么。
        LOGGER.info("跳转到外部页面后停留 %.0f 秒", total)
    # 浏览类和跳转到的会场页都是靠页面在动才累计，停留期间必须持续滑动。
    _dwell(context, total, scroll=kind in ("browse", "jump"))
    _back(context)
    return TASK_OK


def _scan(context: Any, node: str = "PanelFullOcr") -> list[panel.OcrItem]:
    return _ocr_items(context, _screencap(context), node)


def start_app(context: Any, config: dict[str, Any], user_id: int | None = None) -> bool:
    """按配置启动淘宝，支持应用分身所在的独立 Android 用户。

    为什么不用 Pipeline 的 StartApp 动作：协议里的 StartApp 只有 package 参数，
    由框架固定以当前用户（通常是 user 0）启动。应用分身是另一个 Android 用户
    （本机为 user 999），用 StartApp 永远只会打开主用户那个淘宝。

    user_id 由 PI 配置项传入，优先级最高；没传时用环境变量 / config/tasks.json。
    """
    app = config.get("app") or {}
    package = str(app.get("package") or "com.taobao.taobao")
    activity = str(app.get("activity") or "com.taobao.tao.welcome.Welcome")
    try:
        target_user = app_user_id(config) if user_id is None else int(user_id)
        if target_user < 0:
            raise ValueError(f"user_id 不能为负：{target_user}")
        command = start_app_command(package, activity, target_user)
    except (TypeError, ValueError) as exc:
        LOGGER.error("启动参数不合法：%s", exc)
        return False

    LOGGER.info("启动 %s（Android 用户 %d）", package, target_user)
    output = _shell(context, command)
    if output is not None:
        if "Error" in output:
            LOGGER.warning("启动命令返回异常：%s", output)
        return True

    # 没有 shell 能力（Android 原生控制器）：只能退回框架的 StartApp。
    # 它没有 --user 参数，拉起的是当前用户那份，分身账号靠它启动不了。
    if target_user != 0:
        LOGGER.error(
            "当前控制器没有 shell 能力，无法启动 Android 用户 %d 上的淘宝；"
            "请在配置里改选「主账号」，或在电脑上用 adb 运行",
            target_user,
        )
        return False

    LOGGER.debug("控制器没有 shell 能力，改用框架 StartApp 启动 %s", package)
    try:
        job = _controller(context).post_start_app(package)
        job.wait()
    except Exception as exc:  # noqa: BLE001 - 起不来就显式失败，让 Pipeline 中止
        LOGGER.error("启动应用失败：%s", exc)
        return False
    if not _job_succeeded(job):
        LOGGER.error("框架 StartApp 启动 %s 失败，目标应用没有起来", package)
        return False
    return True


def _foreground_package(context: Any) -> str:
    """查当前前台应用包名，取不到就返回空串。

    任务里有几条要跳去支付宝（蚂蚁庄园 / 蚂蚁森林 / 支付宝农场）。支付宝会弹一个
    提示框拦下这次跳转，淘宝就被压到后台，之后无论按多少次返回都回不到任务面板。
    所以要能问出「现在最前面的是谁」，才能决定是不是该把淘宝重新拉到前台。
    """
    output = _shell(context, "dumpsys activity activities | grep -m 1 topResumedActivity")
    if output is None:
        return ""
    match = _FOREGROUND_RE.search(output)
    return match.group(1) if match else ""


def _ensure_taobao_foreground(context: Any, config: dict[str, Any]) -> bool:
    """不是淘宝在前台时，把淘宝拉回来。返回是否真的做了恢复。"""
    app = config.get("app") or {}
    package = str(app.get("package") or "com.taobao.taobao")
    current = _foreground_package(context)
    if not current or current == package:
        return False

    LOGGER.info("前台应用是 %s，不是淘宝，准备把淘宝拉回前台", current)
    if start_app(context, config):
        _sleep(2.0)
    return True


def _installed_packages(context: Any) -> set[str]:
    """取整台手机上装了哪些应用（按主账号 user 0 清点）。

    注意范围：查的是**手机**，不是当前运行的账号。应用分身只是把淘宝克隆了一份，
    本机应用是共用的，分身里点「去蚂蚁庄园」照样能拉起支付宝。
    所以只有整台手机都没装的才排除（实测本机没有今日头条），
    否则会把本来能做的任务当掉。
    """
    packages: set[str] = set()
    for command in ("pm list packages --user 0", "pm list packages"):
        output = _shell(context, command)
        if output is None:
            # 控制器没有 shell 能力（手机端原生控制器）：退回"全部可尝试"，
            # 由主循环按执行后有没有推进来决定放弃。
            continue
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                packages.add(line[len("package:"):])
        if packages:
            break
    return packages


def _open_app_dialog_action(
    context: Any, items: list[panel.OcrItem], *, tries: int = 3
) -> list[panel.OcrItem] | None:
    """碰到「想要打开其他应用」确认框就点确认，真的跳进去做任务。

    跳外部应用的任务（蚂蚁庄园 / 蚂蚁森林 / 跳闲鱼 / 浏览器打开站外页）点击后
    会出这个框。框盖在面板上方但面板文字仍能被 OCR 认出来，所以必须主动识别并
    处理，否则每轮都会重复点同一个任务、重复被同一个框拦住。

    这里选确认（而非「取消」）：目标任务就是要进去才能完成，取消等于放弃。
    进去做完再由 _recover_to_panel 把淘宝拉回来。

    items 传入调用方已经拿到的识别结果，避免为了判断弹框再多截一次图。

    返回点击之后重新识别到的界面内容；没点过确认框时返回 None。这份结果必须交给
    调用方：确认之后界面往往**已经**到了目标页面，再按一次返回会把它关掉。
    """
    after: list[panel.OcrItem] | None = None
    for _ in range(tries):
        if not panel.open_app_dialog_visible(items):
            break
        button = panel.open_app_dialog_button(items, panel.OPEN_APP_DIALOG_CONFIRM)
        if button is None:
            break
        LOGGER.info("确认框点「%s」进入外部小程序", button.text)
        _click_item(context, button, delay=1.5)
        items = _scan(context)
        after = items
    return after


def _recover_to_panel(
    context: Any,
    config: dict[str, Any],
    *,
    tries: int = 6,
    profile: PanelProfile = COIN_PROFILE,
    first: Sequence[panel.OcrItem] | None = None,
) -> list[panel.OcrItem] | None:
    """把界面拉回目标任务面板，成功时返回面板上的识别结果。

    返回识别结果而不是 bool，是为了让调用方省掉一次整屏 OCR：每轮本来就要
    判任务进度，重新截一张图再跑一遍全屏 OCR 是纯浪费（真机上一次要好几秒）。

    只按返回键是回不去的，几种局面必须分开处理：
      1. 已在任务面板 —— 直接返回；
      2. 停在双11 组队活动页 —— 这是广告页，没有任何日常任务，按返回退出；
      3. 停在淘金币赛车首页 —— 这里按返回会直接退出淘金币，
         必须点剖面下的「赚金币」/「去赚能量」才能重新打开目标面板；
      4. 停在淘宝首页 —— 点「领淘金币」图标进入；
      5. 停在促销浮层（如「双11提前领好券」）—— 封面挡住了入口，按返回关掉；
      6. 挡住「想要打开其他应用」确认框 —— 直接点「打开」继续这次跳转；
      7. 已经跳到别的应用 —— 重新打开淘宝。

    首页单独处理：那排图标（「领淘金币」）会随信息流一起滚出屏幕，启动动画期间
    也可能还没渲染出来。这两种情况下按返回都会直接退出淘宝，于是越退越找不到入口，
    所以只能往下滑找入口，实在不行重启淘宝，唯独不能按返回。

    profile 决定目标面板：能量任务走到赛车首页时要点「去赚能量」而不是「赚金币」。

    first 是调用方刚扫出来的识别结果。主循环判完「不在面板上」之后就调这里，
    而那次判定用的正是这一帧 —— 不传进来就得对同一张没变过的界面重扫一遍，
    真机上一次全屏 OCR 十几到三十几秒，白花的正好是最贵的那一步。

    但只有界面没被动过才敢复用：下面这一步可能把淘宝重新拉回前台，那之后
    屏幕已经不是 first 那一张了，必须重新识别。
    """
    if _ensure_taobao_foreground(context, config):
        LOGGER.info("已重新打开淘宝，继续找%s", profile.name)
        first = None

    home_tries = 0
    # 已经点空过的入口坐标。首页那排图标会被 OCR 合并成一整块，按字符占比
    # 折算出的点可能一直落在空白处 —— 真机 21:46 那次三次都点在 (636,194)，
    # 三次空点把一次本来能救回来的进入拖成了「无法进入面板」。所以同一个
    # 坐标只点一次：没反应就换手段（滑一下版式 → 重开淘宝 → 交出恢复），
    # 而不是把同一个动作重做三遍。
    failed_points: set[tuple[int, int]] = set()
    # 已经点过的入口文字。按文字而不是按坐标记，是因为赛车首页的版式会整体
    # 上下移 60 像素（同一个「去赚能量」实测出现在 y=383 和 y=443 两种位置），
    # 按坐标记会把同一个入口的第二次尝试当成全新入口，于是又点一遍同一个位置。
    failed_entries: set[str] = set()
    # 入口入口试尽后的升级档位，只升不降：滑一下 → 重开淘宝 → 放弃本次恢复。
    entry_escalation = 0
    # 上一次恢复动作的签名与它的重复次数。同一个动作重复出现说明它**没有改变
    # 界面**：一遍又一遍地重做同一件事，每次中间隔着一整轮全屏 OCR（真机 20 秒
    # 量级），能把一次失败放大成两三分钟的原地打转。重复到第二次就换更硬的手段。
    last_action: str | None = None
    repeats = 0

    def note(action: str) -> int:
        """记下这次要做的恢复动作，返回它连续重复了几遍。"""
        nonlocal last_action, repeats
        repeats = repeats + 1 if action == last_action else 1
        last_action = action
        return repeats

    for _ in range(tries):
        if first is not None:
            items, first = first, None
        else:
            items = _scan(context)

        # 确认框必须先于「在面板上」判断：它盖在面板上方，但面板文字依然能被识别，
        # 先判面板就会以为一切正常，然后不断点在弹框上。
        if panel.open_app_dialog_visible(items):
            after = _open_app_dialog_action(context, items)
            # 确认之后界面常常**已经**是目标页面了（点「去赚能量」时的跳转确认框
            # 背后就是能量面板本身）。这里再按一次返回会把刚拿到的面板关掉，
            # 一路退出淘宝。所以要先看确认后的界面，确实还没到才按返回。
            if after is not None and (
                profile.visible(after)
                or (profile.daily_done and panel.daily_done_visible(after))
            ):
                return after
            _back(context)
            continue

        if profile.visible(items):
            return items
        if profile.daily_done and panel.daily_done_visible(items):
            LOGGER.info("今日任务已全部完成，无需回到任务面板")
            return items

        # 「今日速赚…淘金币」完成提示页（真机 2026-10-26 10:11 那帧）盖在淘金币
        # 首页上方。它上面没有任何任务入口，标题里却带「淘金币」，不先处理它就会
        # 被当成首页那排图标的入口：按字符占比折算出的点正好落在标题文字上
        # （(636,195) 就是「今日速赚830淘金币」里那三个字的位置），点下去页面
        # 纹丝不动，于是连点三次、重启一次，最后报「进不去面板」。
        #
        # 对淘金币任务它是「今日已完成」的证据（上面那条已经接走了）；对能量任务
        # 它只是挡路：能量面板在淘金币首页的「赚能量」入口后面，必须先把这层收起来。
        if panel.daily_done_visible(items):
            if note("promo-close") >= 3:
                LOGGER.warning("「今日速赚」提示页收不起来，放弃本次恢复")
                return None
            close = panel.popup_close_button(items)
            if close is None:
                LOGGER.warning("「今日速赚」提示页上没找到关闭按钮，按返回收起")
                _back(context)
            else:
                LOGGER.info("收起「今日速赚」提示页，回到淘金币首页继续找入口")
                _click_item(context, close, delay=2.0)
            continue

        # 已经跳到淘宝以外（支付宝 / 浏览器 / 小程序）。手机端控制器没有 shell，
        # 问不出前台包名，_ensure_taobao_foreground 那头是瞎的，只能靠屏幕文字判断。
        # 这种局面上按返回只会退出当前应用，退多少次都回不到淘宝，必须重新拉起。
        if panel.foreign_app_visible(items):
            if note("foreign-app") >= 2:
                LOGGER.warning("连续两次仍在淘宝以外的应用里，放弃继续恢复")
                return None
            LOGGER.info("已跳到淘宝以外的应用，重新启动淘宝")
            start_app(context, config)
            _sleep(3.0)
            continue

        # 已进了淘金币任务面板但目标是能量面板：这个面板上没有「去赚能量」，
        # 而它里面又有「淘金币趣味课堂」这类含「淘金币」的文字，继续往下走会
        # 把任务行当成首页入口点下去。按返回回到淘金币首页再重找入口。
        if panel.task_panel_visible(items):
            LOGGER.info("当前在淘金币任务面板而非%s，按返回回到淘金币首页", profile.name)
            _back(context)
            continue

        if panel.team_page_visible(items):
            LOGGER.info("误入组队活动页，返回退出（该页面没有日常任务）")
            _back(context)
            continue

        if panel.promo_layer_visible(items):
            LOGGER.info("识别到淘宝促销弹层，返回关闭后重新扫描")
            _back(context)
            continue

        if panel.quiz_page_visible(items):
            # 答题页没有任务入口，按返回能直接退出（不能点标题里的「淘金币」）。
            LOGGER.info("误入淘金币答题页，返回退出")
            _back(context)
            continue

        if panel.coin_home_visible(items):
            entry = panel.find_text_by_keywords(items, profile.home_entry)
            key = panel.normalize_text(entry.text) if entry is not None else ""
            if entry is not None and key not in failed_entries:
                LOGGER.info("在淘金币首页，点击「%s」打开%s", entry.text, profile.name)
                failed_entries.add(key)
                failed_points.add(entry.center)
                _click_item(context, entry, delay=2.0)
                # 点完入口不能立刻扫一眼就下结论：这个界面在虚拟屏上只有 2 FPS，
                # 页面切换要持续好几秒，点击后 2 秒截到的那一帧是「上半屏已经是
                # 能量面板、下半屏还是赛车首页」的过渡态，两边的判定都不成立，
                # 于是落到内层页分支按了返回 —— 刚打开的面板被自己关掉。
                # 真机 15:02 那次就是这样一轮轮空转，最后报「无法进入面板」。
                # 这里必须给面板留出渲染时间，再判一次。
                appeared = _wait_for(
                    lambda current: profile.visible(current)
                    or (profile.daily_done and panel.daily_done_visible(current)),
                    context,
                    tries=_ENTRY_RENDER_TRIES,
                    delay=1.5,
                )
                if profile.visible(appeared):
                    LOGGER.info("已进入%s", profile.name)
                    return appeared
                if profile.daily_done and panel.daily_done_visible(appeared):
                    LOGGER.info("今日任务已全部完成，无需再进%s", profile.name)
                    return appeared
                continue
            if entry is not None:
                # 这个入口刚点过、没打开面板。重做同一个动作只会又一次停在首页，
                # 所以改成重开淘宝让页面回到干净状态；仍不行就退出淘金币，
                # 回到淘宝首页从入口那一排重新走一遍。
                entry_escalation += 1
                if entry_escalation == 1:
                    LOGGER.info("点「%s」没打开%s，重新启动淘宝", entry.text, profile.name)
                    start_app(context, config)
                    _sleep(3.0)
                elif entry_escalation == 2:
                    LOGGER.info(
                        "重开淘宝后「%s」仍打不开%s，退出淘金币重走入口",
                        entry.text,
                        profile.name,
                    )
                    _back(context)
                else:
                    LOGGER.info("淘金币首页入口怎么点都进不去%s，交给下一轮恢复", profile.name)
                    break
                continue

        # 已经翻进了某一层内页（标题带返回箭头）。内页正文里同样有「淘金币」，
        # 例如领券中心的「淘金币频道内，可叠加所有优惠」「需淘金币1500」，
        # 落到下面的首页入口分支就会点在纯文本上，页面纹丝不动 —— 真机连点
        # 四次同一处，最后报「进不去面板」。内页一律按返回退出，回首页再找入口。
        if panel.nav_title(items) is not None:
            LOGGER.info("当前在淘宝内层页面，返回退出后重新找入口")
            _back(context)
            continue

        entry = panel.coin_entry_point(items)
        if entry is not None:
            # 首页那一排图标常被 OCR 合并成一个文字块（例如
            # 「领淘金币充值中心试用领取常买超市」），块中心落在旁边的「充值中心」上，
            # 直接点块中心会进错页面，所以按关键词在块内的字符位置折算。
            clicked = False
            for keyword in panel.COIN_ENTRY_KEYWORDS:
                point = panel.keyword_center(entry, keyword)
                if point is None or point in failed_points:
                    continue
                LOGGER.info("在淘宝首页点击「%s」进入淘金币 (%d, %d)", keyword, *point)
                failed_points.add(point)
                _click(context, point[0], point[1], delay=3.0)
                clicked = True
                break
            if clicked:
                continue
            # 能折算出来的入口位置全点过了、都没进淘金币。同一排图标重复点
            # 只会得到同一个结果，所以逐级改手段：先滑一下让版式变（合并块
            # 会被拆开、坐标随之改变），再不行重开淘宝，最后交出本次恢复。
            entry_escalation += 1
            if entry_escalation == 1:
                LOGGER.info("首页「淘金币」入口点了没反应，滑动换版式再找")
                _scroll_once(context, upward=True)
            elif entry_escalation == 2:
                LOGGER.info("首页「淘金币」入口反复点不开，重新启动淘宝")
                start_app(context, config)
                _sleep(3.0)
            else:
                LOGGER.info("首页「淘金币」入口怎么点都进不去，交给下一轮恢复")
                break
            continue

        if panel.home_tab_bar_visible(items):
            # 首页上按返回等于退出淘宝，越退越回不到入口。先把信息流滚回顶部
            # 找那排图标；滚两趟还找不到就重开淘宝，让首页回到初始状态。
            home_tries += 1
            if home_tries <= 2:
                LOGGER.info("停在淘宝首页但没看到淘金币入口，向上滑动找图标行")
                _scroll_once(context, upward=False)
            elif home_tries == 3:
                LOGGER.info("淘宝首页找不到淘金币入口，重新启动淘宝")
                start_app(context, config)
                _sleep(2.0)
            else:
                # 刚重启的淘宝要几秒才渲染出图标行，等着别按键，也不能反复重启。
                _sleep(2.0)
            continue

        _back(context)

    # 收尾必须补扫一次：最后一轮很可能刚点完「去赚金币」或刚按过返回，
    # 界面是在那次动作之后才变成面板的，沿用动作前的识别结果会把这种刚好
    # 成功的恢复误判成失败。这一次 OCR 是判断「到底回没回来」的唯一依据。
    items = _scan(context)
    return items if profile.visible(items) else None


def open_coin_page(context: Any, config: dict[str, Any]) -> bool:
    """从任意起始页面进入淘金币任务面板，供 Pipeline 入口节点调用。

    「今日奖励已拿完」这一屏也算就位：任务面板换成了完成提示，本来就没有面板可进。
    它和「没进去」都是返回 True，区别只在对不对得上后续节点 —— 入口 Pipeline 的
    WaitCoinPanel 同时认完成提示，因此这里照实记录是哪种情况即可，不能当成失败。
    """
    items = _recover_to_panel(context, config)
    if items is None:
        LOGGER.error("无法进入淘金币任务面板")
        return False
    if panel.daily_done_visible(items):
        LOGGER.info("今日任务已完成，不需要再进任务面板")
    else:
        LOGGER.info("已进入淘金币任务面板")
    return True


def open_energy_page(context: Any, config: dict[str, Any]) -> bool:
    """从任意起始页面进入双11「做任务赚能量」面板，供 Pipeline 入口节点调用。

    入口就在淘金币首页上，与「去赚金币」并排，所以复用同一套恢复逻辑，
    只把目标面板和首页入口文字换成能量那边的。
    """
    items = _recover_to_panel(context, config, profile=ENERGY_PROFILE)
    if items is None:
        LOGGER.error("无法进入双11赚能量面板")
        return False
    LOGGER.info("已进入双11赚能量面板")
    return True


def report_daily_done() -> bool:
    """在「今日奖励已拿完」这一屏上收尾，不再进日常循环。

    这一屏说明今天的任务已经全部做完，走进循环只会再截一次整屏、再判一次
    同一屏，然后原样退出来 —— 用户看到的现象是「已经报了完成，后面又开始
    跑日常任务」。所以入口阶段就把这条支路截断。
    """
    LOGGER.info("今日任务已全部完成，跳过日常任务循环")
    return True


def run_daily_loop(context: Any, config: dict[str, Any], *, max_rounds: int = DEFAULT_MAX_ROUNDS) -> bool:
    """淘金币任务面板的主循环。返回值语义同 _run_panel_loop。"""
    return _run_panel_loop(context, config, profile=COIN_PROFILE, max_rounds=max_rounds)


def run_energy_loop(context: Any, config: dict[str, Any], *, max_rounds: int = DEFAULT_MAX_ROUNDS) -> bool:
    """双11「做任务赚能量」面板的主循环。

    和淘金币面板共用同一套流程，差别只在两处：
      - 目标面板不同（顶部是「做任务赚能量 / 当前进度」而不是「赚金币抵钱」）；
      - 没有可信的「完成进度 n/m」，只能靠「连续几轮都没任务可做」判定收尾。

    那个面板上做不了的任务（拉人助力、天猫积分兑换、快手打卡、微信）靠
    config/tasks.json 的黑名单挡掉，不靠完成度判断。
    """
    return _run_panel_loop(context, config, profile=ENERGY_PROFILE, max_rounds=max_rounds)


def _log_skipped(skipped: Sequence[str], *, prefix: str = "本轮不可执行项") -> None:
    """把「本轮没挑中的项」压缩成一行给人看的日志。

    skipped 里绝大多数是 OCR 认出来的界面碎片（「61679>」「×」「は」「10」），
    它们既不是任务也没法执行，原样铺出来会把日志冲成一大段看不懂的字符。
    这里只保留**有意义的跳过原因**（黑名单、已做完、已放弃、没装应用），
    并给条数封顶；完整清单降到 debug，排查时仍然拿得到。
    """
    if not skipped:
        return
    LOGGER.debug("本轮全部跳过项：%s", ", ".join(skipped))

    meaningful = [
        entry for entry in skipped if not entry.endswith("(未匹配到可执行名单)")
    ]
    if not meaningful:
        LOGGER.info("%s：%d 条界面文字都不像任务", prefix, len(skipped))
        return
    shown = ", ".join(meaningful[:_SKIPPED_LOG_LIMIT])
    more = len(meaningful) - _SKIPPED_LOG_LIMIT
    LOGGER.info("%s：%s%s", prefix, shown, f" 等 {len(meaningful)} 项" if more > 0 else "")


def _task_saturated(text: str, tried: dict[str, int]) -> bool:
    """这个任务是不是「做也白做」，该往下找别的了。

    「拍立淘(2/5)」这类带进度的任务，做过一次仍然要把剩下的格数做完，
    不能因为出现过就跳过；只有进度已经做满、或本来就读不到进度
    （「任务到访得金币」那种纯汇总行）时，才算它占住了这一屏。
    """
    if panel.dedupe_key(text) not in tried:
        return False
    progress = panel.task_progress(text)
    if progress is None:
        return True
    return progress[0] >= progress[1]


def _run_panel_loop(
    context: Any,
    config: dict[str, Any],
    *,
    profile: PanelProfile,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> bool:
    """主循环：扫描 → 安全词检测 → 挑一个任务 → 执行 → 回来 → 重复。

    返回值语义：
        True  —— 正常结束（面板上已经没有可执行任务了）
        False —— 主动安全停止（命中安全验证 / 连续多次回不到目标面板）
    """
    max_empty = int(config.get("timing", {}).get("max_empty_rounds", 3))
    max_attempts = int(config.get("timing", {}).get("max_attempts_per_task", 2))
    empty_rounds = 0
    completed = 0
    # 连续多少次没回到面板。单次恢复失败很常见（重启淘宝后首页还没渲染完、
    # 跳外部应用后回不来），一次就判死会把十几分钟的任务整轮作废。
    lost_streak = 0
    # 同一个任务该执行几次就够。淘宝的任务面板会把没做完的任务一直摆在原位，
    # 若每次扫描都挑中同一个（例如「点击去逛+30」），停留再久也不会推进，
    # 就会把轮次全部耗在它身上。这里按任务名记执行次数，超过上限就放弃。
    tried: dict[str, int] = {}
    abandoned: set[str] = set()
    # 每个任务「点了但页面没反应」的次数。空点不是任务的错，但也不能无限重试。
    no_ops: dict[str, int] = {}
    # 每个任务上一次看到的 (x/y) 进度。判断「有没有推进」必须跟上一轮比，
    # 只看 progress[0] > 0 会把所有带进度的任务当成永不放弃。
    last_progress: dict[str, tuple[int, int]] = {}
    # 整台手机上装了什么，用来挡掉「点了必然失败」的任务（例如本机没装今日头条）。
    # 每轮只查一次，取不到就退回空集合（等于不挡任何任务）。
    installed = _installed_packages(context)
    # 上一轮收尾时已经是「面板 + 识别完成」的状态，界面在这之后没有任何操作，
    # 直接交给下一轮用，省掉一次整屏 OCR（真机上一次要好几秒）。
    pregained: list[panel.OcrItem] | None = None
    # 本轮为了找屏外任务往下翻了几屏。面板会记住滚动位置：做完任务按返回回来时
    # 还停在下方，而没做完的任务都在顶部 —— 不先拖回去，它们一整轮都轮不到，
    # 最终被当成「没任务可做」收尾。
    scrolled = 0

    for round_index in range(1, max_rounds + 1):
        if scrolled:
            LOGGER.info("第 %d 轮：面板停在下方，拖回顶部再挑任务", round_index)
            _scroll_to_top(context, scrolled)
            scrolled = 0
            # 上一轮那份识别结果是下方那一屏的，位置已经变了，不能再沿用。
            pregained = None
        if pregained is not None:
            items, pregained = pregained, None
        else:
            items = _scan(context)

        safety = panel.find_safety(items, config.get("safety_keywords", []))
        if safety:
            LOGGER.error("命中安全验证「%s」，立即停止任务", safety.text)
            return STOP_SIGNAL

        # 任务全部做完时淘宝会弹"今日奖励已拿完"，这是正常结束而非异常。
        if profile.daily_done and panel.daily_done_visible(items):
            LOGGER.info("检测到「今日奖励已拿完」提示，今日任务已完成，正常结束")
            return True

        if not profile.visible(items):
            LOGGER.info("第 %d 轮：不在%s，尝试恢复", round_index, profile.name)
            # 把刚扫出来的这一帧交给恢复流程：它开头第一件事就是判「现在在哪」，
            # 而这一帧正是那个判断的依据。不传进去就得对同一张界面再扫一遍，
            # 真机上一次全屏 OCR 十几到三十几秒。
            if _recover_to_panel(context, config, profile=profile, first=items) is None:
                lost_streak += 1
                # 一次恢复失败不能终止整个任务：真机 09:35 那次只做了 8 轮就被
                # 判「回不到面板」中止，而屏上还有一多半任务没做。只有连续多次
                # 彻底回不去（每次都重启了淘宝仍然无效）才当作真失败。
                if lost_streak >= _MAX_LOST_STREAK:
                    LOGGER.error(
                        "连续 %d 次无法回到%s，停止任务", lost_streak, profile.name
                    )
                    return STOP_SIGNAL
                LOGGER.warning(
                    "第 %d 轮：没能回到%s（连续 %d 次），先等页面稳定再试",
                    round_index,
                    profile.name,
                    lost_streak,
                )
                pregained = None
                _sleep(3.0)
            else:
                lost_streak = 0
            # 成功找回面板不算空轮次：否则 max_empty_rounds=1 时
            # 刚恢复就被判定为"今日任务已完成"，直接结束整轮任务。
            continue

        # 面板本身能判断"做完了"时不必再扫：主账号没有进度行，走 pick_task 兜底；
        # 分身账号有「完成进度 n/m」，n>=m 即完成。
        # 能量面板没有这种可信进度行（「当前进度 n/50」是活动累计计数），
        # 对它强行套用会让开局第一轮就判成「今日已全部做完」，所以按 profile 关掉。
        if completed and profile.has_overall and panel.overall_done(items):
            progress = panel.overall_progress(items)
            if progress is not None:
                LOGGER.info("完成进度 %d/%d，今日任务已全部完成", *progress)
            else:
                LOGGER.info("面板上已无可执行任务，今日任务已全部完成")
            return True

        pick = panel.pick_task(
            items, config, exclude=abandoned, tried=tried, deprioritize=no_ops
        )
        if pick.item is not None and installed and panel.task_unreachable(pick.item.text, installed):
            package = panel.required_package(pick.item.text)
            LOGGER.warning(
                "任务「%s」要跳 %s，这台手机上没装这个应用，跳过",
                pick.item.text,
                package,
            )
            abandoned.add(panel.task_key(pick.item.text))
            continue

        if pick.item is None:
            _log_skipped(pick.skipped)
            LOGGER.info("第 %d 轮：当前屏无可执行任务，拖回顶部复查", round_index)
            # 必须拖到顶，不能只挪一小段：没做完的任务在面板最上方，
            # 一次 36% 屏高的微调根本够不到，复查看到的还是同一批。
            # 本轮往下翻过几屏，这里就拖回几屏再补一屏。
            _scroll_to_top(context, scrolled + 1)
            scrolled = 0
            items = _scan(context)
            safety = panel.find_safety(items, config.get("safety_keywords", []))
            if safety:
                LOGGER.error("复查时命中安全验证「%s」，立即停止任务", safety.text)
                return STOP_SIGNAL
            pick = panel.pick_task(
                items, config, exclude=abandoned, tried=tried, deprioritize=no_ops
            )
            if pick.item is None:
                _log_skipped(pick.skipped, prefix="复查后仍无可执行任务")
                # 「有任务被放弃」不等于「都没得做了」：放弃是按去重键记的，
                # 而复查后看到的往往是一批新的行。11:35 那次第 13 轮只剩下
                # 四个已放弃项就直接收尾，可屏幕上那些 (x/10) 任务根本没做满。
                if abandoned:
                    LOGGER.info("剩余任务都已尝试过 %d 次，无法继续推进，结束", max_attempts)
                    return True
                empty_rounds += 1
                if empty_rounds >= max_empty:
                    LOGGER.info("连续 %d 轮无任务，判定今日任务已处理完", empty_rounds)
                    return True
                continue

        # 面板一屏只显示几行任务，排在屏外的任务（例如列表下方的「去蚂蚁森林
        # 收能量」）OCR 根本看不到；不往下翻就永远轮不到它们，整轮反而一直重复
        # 做屏上那几行做过的。翻一屏继续找，翻到没做过（或还没做满）的就改做它。
        original, original_items = pick, items
        for _ in range(_PANEL_SCROLL_DEPTH):
            if not _task_saturated(pick.item.text, tried):
                break
            LOGGER.info("第 %d 轮：本屏都是做过的任务，往下翻一屏找新任务", round_index)
            _swipe_up(context)
            scrolled += 1
            items = _scan(context)
            safety = panel.find_safety(items, config.get("safety_keywords", []))
            if safety:
                LOGGER.error("翻页时命中安全验证「%s」，立即停止任务", safety.text)
                return STOP_SIGNAL
            candidate = panel.pick_task(
                items, config, exclude=abandoned, tried=tried, deprioritize=no_ops
            )
            if candidate.item is None:
                # 这一屏也没有能做的，再翻下去也是同样的结果。
                break
            if installed and panel.task_unreachable(candidate.item.text, installed):
                LOGGER.warning(
                    "任务「%s」要跳 %s，这台手机上没装这个应用，跳过",
                    candidate.item.text,
                    panel.required_package(candidate.item.text),
                )
                abandoned.add(panel.task_key(candidate.item.text))
                continue
            pick = candidate

        if _task_saturated(pick.item.text, tried):
            # 翻遍了也没找到新任务：退回原来那一个接着做，
            # 别让翻页把「唯一还能推进的任务」弄丢。
            # 那两个坐标来自翻页前那一帧，屏幕必须先拖回同一位置，
            # 否则点下去落在别的任务行上。
            if scrolled:
                _scroll_to_top(context, scrolled)
                scrolled = 0
            pick, items = original, original_items

        name = panel.dedupe_key(pick.item.text)
        button = panel.find_action_button(items, pick.item) or pick.item
        LOGGER.info(
            "第 %d 轮：执行「%s」（%s），按钮「%s」",
            round_index,
            pick.item.text,
            pick.decision.kind,
            button.text,
        )
        outcome = _run_task(context, pick, config, button, items)
        if outcome == TASK_OK:
            completed += 1
            no_ops.pop(name, None)
        elif outcome == TASK_FAILED:
            # 搜索没提交成功：页面根本没搜过，算完成只会让主循环不停地重跑同一个任务。
            LOGGER.warning("任务「%s」未能真正执行，不计入完成", pick.item.text)
        empty_rounds = 0

        if outcome == TASK_NOTHING:
            # 页面根本没动：这不是这个任务做不了，只是这一下没点中。
            # 既不算完成、也不计入「试了几次没进展」——否则一个本来能做的
            # 任务会被三次空点提前放弃。但仍然要有封顶，不然按钮彻底失效的
            # 那一行会在面板上无限重试。
            no_ops[name] = no_ops.get(name, 0) + 1
            if no_ops[name] >= _MAX_NO_OP_RETRIES:
                abandoned.add(name)
                LOGGER.warning(
                    "任务「%s」连点 %d 次都没反应，本次跳过它",
                    pick.item.text,
                    no_ops[name],
                )
            # 界面没变，上一轮的识别结果仍然有效，不必再扫一遍。
            # 能给这份结果的正是 TASK_NOTHING 的定义本身：它只在「任务行原封不动、
            # 也没弹出确认框」时返回，那时屏幕和点之前是同一张。
            pregained = items
            _sleep(_random_delay(config))
            continue

        recovered = _recover_to_panel(context, config, profile=profile)
        if recovered is None:
            LOGGER.warning("任务后未能回到%s", profile.name)

        tried[name] = tried.get(name, 0) + 1
        # 面板就在眼前时，用这一轮的识别结果判进度，不再单独截一张图：
        # _find_progress 要整屏 OCR 一次，每轮多这一次就是几十秒的总代价。
        if _progress_stuck(
            context, name, tried, max_attempts, last_progress, seen=recovered
        ):
            abandoned.add(name)
            stuck_at = last_progress.get(name)
            # 把读数写进日志：只说「没进展」，下一次还得重新翻日志找屏幕上是几比几。
            if stuck_at is None:
                LOGGER.warning(
                    "任务「%s」已尝试 %d 次仍无进展，本次跳过它", name, tried[name]
                )
            else:
                LOGGER.warning(
                    "任务「%s」已尝试 %d 次仍停在 %d/%d，本次跳过它",
                    name,
                    tried[name],
                    stuck_at[0],
                    stuck_at[1],
                )

        # 周期进度：一轮要一分多钟，只有失败日志的话看不出其实还在推进。
        if round_index % _PROGRESS_LOG_EVERY == 0:
            LOGGER.info(
                "已完成 %d 个任务（第 %d 轮），已放弃 %d 个做不动的任务",
                completed,
                round_index,
                len(abandoned),
            )

        # 到下一轮开始之间只有等待，界面不会再变，这份识别结果仍然有效。
        # recovered 为 None 时不能沿用上一轮的图：那一存现在很可能已经不在面板上了。
        pregained = recovered
        _sleep(_random_delay(config))

    # 走到这里说明「面板上一直有任务可做」这个假设没被打破，却把兜底轮数跑满了。
    # 这不是「任务做完了」，必须让日志能分清：否则下次又会把安全兜底当成目标值看。
    LOGGER.warning(
        "已跑满兜底轮数 %d（完成 %d 个任务）。任务仍可继续，"
        "请检查是否有任务反复点击但不推进进度",
        max_rounds,
        completed,
    )
    return True


def _progress_stuck(
    context: Any,
    name: str,
    tried: dict[str, int],
    max_attempts: int,
    last_progress: dict[str, tuple[int, int]],
    *,
    seen: list[panel.OcrItem] | None = None,
) -> bool:
    """判断某个任务是否该放弃。

    任务名里带 (x/y) 进度时以「进度有没有真正增长」为准：跟上一轮的值比，
    变大了算有进展、把这一任务的执行次数清回 1，然后再看次数。
    不清回的后果是做到一半的任务会被次数耗死：「拍立淘(0/5)」每次都推进一格，
    跑满 max_attempts 次后照样被放弃，而它明明只差最后一两格。
    不带进度时（例如「点击去逛+30」）只能看执行次数，到上限就放弃。

    tried 由调用方持有并在这里就地更新，因为「有进展」要重置的就是它。
    seen 是调用方刚拿到的面板识别结果，给了就不必再截一次图。
    """
    progress = _find_progress(context, name, seen=seen)
    if progress is None:
        return tried.get(name, 0) >= max_attempts
    previous = last_progress.get(name)
    last_progress[name] = progress
    if previous is None:
        return tried.get(name, 0) >= max_attempts
    if progress[0] > previous[0]:
        tried[name] = 1
        return False
    if progress[0] < previous[0]:
        # 读数倒退不是「没进展」。面板上的进度只会往上走，倒退只可能是两次识别
        # 读到了不同的行、或漏读了一位数字：11:35 那次「好物沉浸看」已经 9/10，
        # 第 7 轮被读成 1/10，紧接着就按「试了 3 次没进展」把一个只差一格的
        # 浏览任务放弃了。读数变了就说明这一任务在动，基线跟着挪、次数清零；
        # 进度真被重置的话，也从新基线继续算，不会漏掉后面的推进。
        tried[name] = 1
        LOGGER.info(
            "任务「%s」的进度从 %d/%d 变成 %d/%d，按新读数重新计算尝试次数",
            name,
            previous[0],
            previous[1],
            progress[0],
            progress[1],
        )
        return False
    return tried.get(name, 0) >= max_attempts


def _find_progress(
    context: Any,
    name: str,
    *,
    seen: list[panel.OcrItem] | None = None,
) -> tuple[int, int] | None:
    """在任务面板上找到同名任务，读它的进度。

    name 是去掉进度后缀的去重键，这里按前缀匹配：面板上那一条的进度后缀
    会随执行变化，拿完整文字去对是找不到的。

    seen 不为空时直接用它，省掉一次整屏 OCR。
    """
    key = name[:6]
    fallback: tuple[int, int] | None = None
    for item in seen if seen is not None else _scan(context):
        progress = panel.task_progress(item.text)
        if progress is None:
            continue
        text = panel.dedupe_key(item.text)
        if text == name:
            return progress
        # 优先要完全同名的行。前缀匹配只当备选：任务名可能被 OCR 和后一行并成
        # 一条，但也可能真的存在一个更长的同名任务（「好物沉浸看直播」之于
        # 「好物沉浸看」），只认前缀就会把更靠前那行的进度当成它的进度。
        if fallback is None and key and text.startswith(key):
            fallback = progress
    return fallback


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


def _apply_extra_rules(config: dict[str, Any], params: dict[str, Any]) -> None:
    """把界面上手动加的规则并进 config，并回显一行日志。

    用户是在手机界面上加的规则，日志是他唯一能确认「真的加进去了」的地方，
    否则规则写错只能从「任务没被跳过」这一个现象反推。
    """
    added = apply_extra_rules(config, params)
    if added:
        LOGGER.info("界面自定义规则已生效：%s", "，".join(added))
    else:
        LOGGER.debug("界面自定义规则：未填写")


def register(agent_server: Any) -> None:
    """把动作注册到 AgentServer。分离出来便于在没有 maa 时也能导入本模块。"""

    from maa.context import Context
    from maa.custom_action import CustomAction

    @agent_server.custom_action("taobao_start_app")
    class TaobaoStartApp(CustomAction):
        """启动淘宝，可指定应用分身所在的 Android 用户。

        user_id 由界面上的「运行账号」配置项经 pipeline_override 写进
        custom_action_param；不写时退回 config/tasks.json 与环境变量。
        """

        def run(self, context: Context, argv: Any) -> bool:
            try:
                config = load_config()
            except Exception as exc:  # noqa: BLE001 - 配置读不出来必须显式失败
                LOGGER.error("读取任务配置失败：%s", exc)
                return False

            raw_user = _parse_params(argv).get("user_id")
            if raw_user is None:
                return start_app(context, config)
            try:
                user_id = int(raw_user)
            except (TypeError, ValueError):
                LOGGER.error("配置项「运行账号」不是整数：%r", raw_user)
                return False
            LOGGER.debug("按界面配置启动，Android 用户 %d", user_id)
            return start_app(context, config, user_id=user_id)

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

    @agent_server.custom_action("taobao_daily_done")
    class TaobaoDailyDone(CustomAction):
        """今日任务全部完成时在完成提示页收尾，不进日常循环。"""

        def run(self, context: Context, argv: Any) -> bool:
            return report_daily_done()

    @agent_server.custom_action("taobao_abort")
    class TaobaoAbort(CustomAction):
        """显式中止，并把这次运行标记为失败。

        为什么要这个动作：`StopTask` 只是停下，任务收尾一律 `Succeeded`。
        于是「淘宝没拉起来、任务面板一次都没进」这种彻底空转，看起来和
        「今日任务已完成」一模一样。这里返回 False 让节点失败，整轮运行
        才会是 Failed —— 出问题的时候必须是红的。

        custom_action_param 的 reason 会被记进日志，用来区分中止原因。
        """

        def run(self, context: Context, argv: Any) -> bool:
            reason = str(_parse_params(argv).get("reason") or "流程中止")
            LOGGER.error("任务中止：%s", reason)
            return False

    @agent_server.custom_action("taobao_daily_loop")
    class TaobaoDailyLoop(CustomAction):
        """跑完一整轮淘金币日常任务。

        custom_action_param 支持 max_rounds 覆盖，以及界面上手动加的规则
        （extra_skip / extra_jump / extra_search / extra_quiz / extra_browse）。
        """

        def run(self, context: Context, argv: Any) -> bool:
            params = _parse_params(argv)
            try:
                max_rounds = int(params.get("max_rounds", DEFAULT_MAX_ROUNDS))
            except (TypeError, ValueError):
                LOGGER.warning("max_rounds 不是整数，改用默认值 %d", DEFAULT_MAX_ROUNDS)
                max_rounds = DEFAULT_MAX_ROUNDS

            try:
                config = load_config()
            except Exception as exc:  # noqa: BLE001 - 配置读不出来必须显式失败
                LOGGER.error("读取任务配置失败：%s", exc)
                return False

            _apply_extra_rules(config, params)
            LOGGER.info("===== 开始淘金币日常任务（最多 %d 轮）=====", max_rounds)
            return run_daily_loop(context, config, max_rounds=max_rounds)


    @agent_server.custom_action("taobao_open_energy_page")
    class TaobaoOpenEnergyPage(CustomAction):
        """从当前页面进入双11「做任务赚能量」面板。"""

        def run(self, context: Context, argv: Any) -> bool:
            try:
                config = load_config()
            except Exception as exc:  # noqa: BLE001 - 配置读不出来必须显式失败
                LOGGER.error("读取任务配置失败：%s", exc)
                return False
            return open_energy_page(context, config)

    @agent_server.custom_action("taobao_energy_loop")
    class TaobaoEnergyLoop(CustomAction):
        """跑完一整轮双11「做任务赚能量」。

        custom_action_param 支持 max_rounds 覆盖，以及界面上手动加的规则
        （extra_skip / extra_jump / extra_search / extra_quiz / extra_browse）。
        """

        def run(self, context: Context, argv: Any) -> bool:
            params = _parse_params(argv)
            try:
                max_rounds = int(params.get("max_rounds", DEFAULT_MAX_ROUNDS))
            except (TypeError, ValueError):
                LOGGER.warning("max_rounds 不是整数，改用默认值 %d", DEFAULT_MAX_ROUNDS)
                max_rounds = DEFAULT_MAX_ROUNDS

            try:
                config = load_config()
            except Exception as exc:  # noqa: BLE001 - 配置读不出来必须显式失败
                LOGGER.error("读取任务配置失败：%s", exc)
                return False

            _apply_extra_rules(config, params)
            LOGGER.info("===== 开始双11赚能量任务（最多 %d 轮）=====", max_rounds)
            return run_energy_loop(context, config, max_rounds=max_rounds)


def register_all(agent_server: Any) -> None:
    """保留一个统一入口，后续新增动作/识别器都从这里挂。"""
    register(agent_server)