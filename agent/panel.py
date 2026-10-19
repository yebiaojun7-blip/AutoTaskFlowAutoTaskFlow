"""淘金币任务面板的纯逻辑：OCR 结果解析、安全词检测与任务挑选。

本模块不导入 maa，因此可以在没有 MaaFramework 运行时的机器上直接跑单元测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Collection, Iterable, Sequence

from agent.custom import TaskDecision, classify_task, load_config


# 可执行任务的分类结果，用于判断一次挑选是否命中。
EXECUTABLE_KINDS = ("search", "jump", "browse")

# 任务面板的特征词，用于判断"当前是否还停留在淘金币任务面板"。
# 真机（一加 Ace 3 / 淘宝 双11版）实测：面板标题为「赚金币抵钱」，
# 首屏一定能看到「今日累计奖励」与「完成进度 x/20」。
#
# 注意不要把「立即领取」「领取奖励」「去完成」这类通用按钮词放进来：
# 淘宝首页和促销浮层上也有「立即领取」，只命中一个词就会把首页误判成面板，
# 于是主循环把首页的「搜索」框当成搜索任务去点（实测点了 (629,190) 十几次）。
PANEL_MARKERS = (
    "赚金币抵钱",
    "今日累计奖励",
    "完成进度",
)

# 面板至少要命中这么多个特征词才算"在面板"。单个弱特征不足以判定。
PANEL_MIN_HITS = 2

# 面板里的操作按钮文案，仅用于定位点击目标，不参与页面判定。
PANEL_BUTTON_MARKERS = (
    "去完成",
    "领取奖励",
    "立即领取",
)

# 可点击的按钮文案。任务名在左侧、按钮在右侧同一行，所以点击应打在按钮上。
# 实测本机型上同一面板会混用「去完成」「去逛逛」「逛一逛」「领取奖励」。
DEFAULT_ACTION_BUTTONS = (
    "去完成",
    "立即完成",
    "去搜索",
    "去浏览",
    "去逛逛",
    "逛一逛",
    "领取奖励",
    "立即领取",
)

# 同一行文字的纵坐标容差（像素），用于把 OCR 结果还原成阅读顺序。
ROW_TOLERANCE = 20


@dataclass(frozen=True)
class OcrItem:
    """一条 OCR 识别结果。坐标是 MaaFramework 缩放后的截图坐标系。"""

    text: str
    x: int
    y: int
    width: int
    height: int
    score: float = 0.0

    @property
    def center(self) -> tuple[int, int]:
        """文字块中心点，用作点击坐标。"""
        return self.x + self.width // 2, self.y + self.height // 2

    @property
    def box(self) -> list[int]:
        """MaaFramework 使用的 [x, y, w, h] 形式。"""
        return [self.x, self.y, self.width, self.height]


@dataclass(frozen=True)
class PanelPick:
    """一次面板挑选的结果。item 为 None 表示当前屏没有可执行任务。"""

    decision: TaskDecision
    item: OcrItem | None
    skipped: tuple[str, ...] = ()


def normalize_text(text: Any) -> str:
    """去掉文本中的空白，OCR 常把中文字之间插入空格。"""
    return "".join(str(text).split())


_PROGRESS_RE = re.compile(r"[(（]\s*(\d+)\s*/\s*(\d+)\s*[)）]")


def task_progress(text: str) -> tuple[int, int] | None:
    """从任务文字里取出进度，例如「拍立淘逛感兴趣的宝贝(3/5)」→ (3, 5)。

    淘宝的任务进度直接写在任务名里。拿到它才能判断一次点击到底有没有生效：
    停留时间不够时进度不会增加，任务会一直停在 (0/5)，主循环只看轮次是发现不了的。
    """
    match = _PROGRESS_RE.search(normalize_text(text))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _field(raw: Any, name: str, default: Any = None) -> Any:
    """兼容 MaaFramework 返回的 dict 与 dataclass 两种形态。"""
    if isinstance(raw, dict):
        return raw.get(name, default)
    return getattr(raw, name, default)


def parse_ocr_items(results: Iterable[Any]) -> list[OcrItem]:
    """把 MaaFramework 的 OCR 结果统一成 OcrItem，并按阅读顺序排序。"""
    items: list[OcrItem] = []
    for raw in results or []:
        text = normalize_text(_field(raw, "text", ""))
        box = _field(raw, "box", None)
        if not text or not box or len(box) != 4:
            continue
        x, y, width, height = (int(value) for value in box)
        if width <= 0 or height <= 0:
            continue
        score = float(_field(raw, "score", 0.0) or 0.0)
        items.append(OcrItem(text, x, y, width, height, score))
    return reading_order(items)


def reading_order(items: Sequence[OcrItem]) -> list[OcrItem]:
    """按从上到下、同一行内从左到右排序。"""
    ordered: list[OcrItem] = []
    row: list[OcrItem] = []
    row_top: int | None = None
    for item in sorted(items, key=lambda value: (value.y, value.x)):
        if row_top is None or abs(item.y - row_top) <= ROW_TOLERANCE:
            row.append(item)
            row_top = item.y if row_top is None else row_top
            continue
        ordered.extend(sorted(row, key=lambda value: value.x))
        row = [item]
        row_top = item.y
    ordered.extend(sorted(row, key=lambda value: value.x))
    return ordered


def find_safety(items: Iterable[OcrItem], keywords: Iterable[str]) -> OcrItem | None:
    """命中安全验证类关键词时返回对应文字块，调用方应立即停止任务。"""
    for item in items:
        for keyword in keywords:
            normalized = normalize_text(keyword)
            if normalized and normalized in item.text:
                return item
    return None


def find_text_by_keywords(items: Iterable[OcrItem], keywords: Iterable[str]) -> OcrItem | None:
    """按给定文案顺序找第一个命中的文字块，未命中返回 None。"""
    wanted = [normalize_text(word) for word in keywords]
    for item in items:
        for word in wanted:
            if word and word in item.text:
                return item
    return None


def panel_visible(
    items: Iterable[OcrItem],
    markers: Iterable[str] = PANEL_MARKERS,
    *,
    min_hits: int = 1,
) -> bool:
    """当前屏是否仍显示淘金币任务面板。

    min_hits 用于需要更严格判定的场合：首页和促销浮层上也会出现「立即领取」
    之类的通用按钮词，只命中一个词不足以说明这是任务面板。
    """
    wanted = [normalize_text(marker) for marker in markers]
    wanted = [word for word in wanted if word]
    if not wanted:
        return False

    hits: set[str] = set()
    for item in items:
        for word in wanted:
            if word in item.text:
                hits.add(word)
        if len(hits) >= min_hits:
            return True
    return False


# 双11 组队活动页的特征词。这个页面是活动广告，没有日常任务，误入后直接退出。
# 注意：不要用「组队赢」——那个按钮在淘金币首页上也存在，会误判。
TEAM_PAGE_MARKERS = (
    "组队大奖赛",
    "车队已凑齐",
    "拉人助力",
    "组队完成",
)

# 淘金币赛车首页的特征词。与任务面板的区别：首页有「赚能量」和「升级赛车」，
# 面板则是「赚金币抵钱」「完成进度」。在首页按返回键会直接退出淘金币，
# 所以必须改点「赚金币」重新打开面板。
COIN_HOME_MARKERS = (
    "升级赛车",
    "赚能量",
)

# 淘金币首页上打开任务面板的入口文字。
COIN_HOME_ENTRY = ("赚金币",)

# 淘宝首页上通往淘金币的入口文字。真机实测首页那一排图标会被 OCR 合并成一条，
# 例如「领淘金币充值中心试用领取常买超市」，所以只能按字符占比推算点击位置。
COIN_ENTRY_KEYWORDS = ("淘金币", "领金币", "我的淘金币")

# 淘宝首页的特征词，用于区分"首页"和"启动后的促销浮层"。
# 首页按返回键会直接退出淘宝，促销浮层则必须按返回才能关掉，两者处理相反。
TB_HOME_MARKERS = ("搜索", "推荐", "关注", "购物车")

# 今日任务全部完成时弹窗的特征词。
# 真机实测：弹窗标题「今日速赚570淘金币」，正文「今日快速赚奖励已拿完」「记得明天再来」。
# OCR 有时把正文拆成几段，所以用「奖励已拿完」这种不含数字的短片段做匹配。
# 这是正常结束，不能当成识别失败去走 AbortTask。
DAILY_DONE_MARKERS = (
    "奖励已拿完",
    "记得明天再来",
    "继续浏览精选清单",
    "更多金币等你赚",
)


def daily_done_visible(items: Iterable[OcrItem]) -> bool:
    """当前屏是否显示"今日任务已全部完成"提示。"""
    return panel_visible(items, DAILY_DONE_MARKERS)


def task_panel_visible(items: Iterable[OcrItem]) -> bool:
    """严格判定当前屏是否真的在淘金币任务面板。

    和 panel_visible 的区别：至少要命中 PANEL_MIN_HITS 个面板专属特征词。
    首页、促销浮层、搜索结果页都可能零星出现「立即领取」，用单标记判定会把它们
    当成面板，进而把页面上的「搜索」框误认成搜索任务。
    """
    return panel_visible(items, PANEL_MARKERS, min_hits=PANEL_MIN_HITS)


def team_page_visible(items: Iterable[OcrItem]) -> bool:
    """当前屏是否是双11组队活动页（需要退出而不是继续操作）。"""
    return panel_visible(items, TEAM_PAGE_MARKERS)


def coin_home_visible(items: Iterable[OcrItem]) -> bool:
    """当前屏是否是淘金币赛车首页（可以点「赚金币」进入任务面板）。"""
    return panel_visible(items, COIN_HOME_MARKERS)


def taobao_home_visible(items: Iterable[OcrItem]) -> bool:
    """当前屏是否是淘宝 App 首页（不是促销浮层，也不是淘金币页面）。"""
    return panel_visible(items, TB_HOME_MARKERS)


def coin_entry_point(items: Sequence[OcrItem]) -> OcrItem | None:
    """在淘宝首页上定位「淘金币」入口文字块。"""
    return find_text_by_keywords(items, COIN_ENTRY_KEYWORDS)


def keyword_center(item: OcrItem, keyword: str) -> tuple[int, int] | None:
    """按关键词在文字块内的字符占比，推算该关键词的中心坐标。

    为什么不能直接点 item.center：OCR 会把首页同一排图标合并成一个文字块，
    例如「领淘金币充值中心试用领取常买超市」整块中心落在「充值中心」上，
    点下去就会进错页面。这里假设块内字符等宽，按关键词出现的字符下标折算。
    """
    text = item.text
    word = normalize_text(keyword)
    if not word or not text:
        return None
    index = text.find(word)
    if index < 0:
        return None
    left = item.x + item.width * index / len(text)
    right = item.x + item.width * (index + len(word)) / len(text)
    x = int((left + right) / 2)
    y = item.y + item.height // 2
    return x, y


def pick_task(
    items: Sequence[OcrItem],
    config: dict[str, Any] | None = None,
    *,
    kinds: Sequence[str] | None = None,
    exclude: Collection[str] = (),
) -> PanelPick:
    """按阅读顺序挑出第一个可执行任务。

    黑名单、已完成等不可执行项会被记录到 skipped 中，便于日志排查。
    kinds 用于限定只挑选某一类任务（Pipeline 中区分搜索/浏览/跳转分支）。
    exclude 里的任务名会被跳过，用于放弃那些反复执行也不推进的任务。
    """
    config = config or load_config()
    allowed = tuple(kinds) if kinds else EXECUTABLE_KINDS
    skipped: list[str] = []
    for item in items:
        text = normalize_text(item.text)
        if text in exclude:
            skipped.append(f"{text}(已放弃)")
            continue
        decision = classify_task(item.text, config)
        if decision.kind in allowed:
            return PanelPick(decision, item, tuple(skipped))
        skipped.append(f"{item.text}({decision.reason or decision.kind})")
    return PanelPick(TaskDecision("none", reason="当前屏没有可执行任务"), None, tuple(skipped))


def find_action_button(
    items: Sequence[OcrItem],
    target: OcrItem,
    buttons: Sequence[str] = DEFAULT_ACTION_BUTTONS,
) -> OcrItem | None:
    """在同一行内找到任务名右侧的可点击按钮，点击坐标应打在按钮上。

    OCR 通常把"浏览商品"和"去完成"识别成两个独立文字块。若整行文字本身
    就含有"去完成"（某些界面会合并成一条），直接返回该文字块。
    """
    for button in buttons:
        normalized = normalize_text(button)
        if normalized and normalized in target.text:
            return target

    best: OcrItem | None = None
    for item in items:
        if item is target or abs(item.y - target.y) > ROW_TOLERANCE:
            continue
        if item.x <= target.x:
            continue
        for button in buttons:
            normalized = normalize_text(button)
            if normalized and normalized in item.text:
                if best is None or item.x < best.x:
                    best = item
                break
    return best


def search_keyword(config: dict[str, Any], attempt: int = 0) -> str:
    """按轮次轮换搜索关键词，避免反复搜索同一词被判定为异常行为。"""
    keywords = config.get("search_keywords") or []
    if not keywords:
        return ""
    return str(keywords[attempt % len(keywords)])


def action_duration(kind: str, config: dict[str, Any]) -> float:
    """不同任务类型在目标页面的停留秒数。"""
    timing = config.get("timing", {})
    key = {
        "browse": "browse_duration_seconds",
        "jump": "jump_duration_seconds",
    }.get(kind)
    if not key:
        return 0.0
    return float(timing.get(key, 3))
