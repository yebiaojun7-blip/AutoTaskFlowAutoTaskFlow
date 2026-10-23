"""淘金币任务面板的纯逻辑：OCR 结果解析、安全词检测与任务挑选。

本模块不导入 maa，因此可以在没有 MaaFramework 运行时的机器上直接跑单元测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Collection, Iterable, Sequence

from agent.custom import TaskDecision, classify_task, load_config


# 可执行任务的分类结果，用于判断一次挑选是否命中。
# quiz 单列一类：答题任务点进去是一道二选一的单选题，按浏览停留再久也不推进。
EXECUTABLE_KINDS = ("search", "jump", "browse", "quiz")

# 任务面板的特征词，用于判断"当前是否还停留在淘金币任务面板"。
#
# 面板文案会随淘宝版本漂移，所以新旧两套都留：任一套命中 PANEL_MIN_HITS 个即可。
#
# 旧版（此前唯一支持的版本）：赚金币抵钱 / 今日累计奖励 / 完成进度 x/20
#
# 新版（一加 Ace 3 PJE110 / 淘宝 2026-10）实测：
#   今日速赚570淘金币 / 为你找到最简单的任务，2分钟即可完成
#   完成下方任务得额外金币 / 任务到访得金币
#   逛清单，每15秒30金币(0/2) / 搜一搜你心仪的宝贝(0/3) / 淘金币趣味课堂
#   去蚂蚁庄园逛逛哟 / 去芭芭农场逛一逛 / 拍立淘逛感兴趣的宝贝(0/5)
# 这版面板上旧词**一个都不出现**，只认旧词会让 task_panel_visible() 永远为 False，
# 主循环于是拼命按返回键、点关闭按钮，在同一页面死转十几分钟。
#
# 注意不要把「立即领取」「领取奖励」「去完成」这类通用按钮词放进来：
# 淘宝首页和促销浮层上也有「立即领取」，只命中一个词就会把首页误判成面板，
# 于是主循环把首页的「搜索」框当成搜索任务去点（实测点了 (629,190) 十几次）。
#
# 「今日速赚」刻意不放进来：任务全做完后的弹窗标题也是「今日速赚570淘金币」，
# 它必须只靠 DAILY_DONE_MARKERS 判定，混进面板特征词会让弹窗被当成面板。
PANEL_MARKERS = (
    # 旧版面板
    "赚金币抵钱",
    "今日累计奖励",
    "完成进度",
    # 新版面板
    "完成下方任务得额外金币",
    "任务到访得金币",
    "淘金币趣味课堂",
    "逛清单",
    "搜一搜你心仪的宝贝",
    "拍立淘逛感兴趣的宝贝",
    # 任务列表底部的「换一换」：这个是**列表末尾**的按钮，任务快做完时
    # 上面那几条汇总文案全被滚出屏幕，它是屏上唯一还留着的面板专属文案。
    "换一换",
)

# 面板至少要命中这么多个特征词才算"在面板"。单个弱特征不足以判定。
PANEL_MIN_HITS = 2

# 靠行结构判定面板时，屏上至少要有这么多条任务行特征（说明文字 + 右侧金币）。
# 只数一条太松：商品详情页也可能出现一个「+50」。
PANEL_ROW_MIN_HITS = 2

# 可点击的按钮文案。任务名在上、按钮在下一行，两者左对齐。
# 实测本机型的面板会混用「浏览5秒」「浏览15秒」「参与答题」「点击去逛」
# 「每日来任务面板」，都带变动的数字或动词，只能按前缀匹配。
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

# 同一行任务名与右侧按钮允许的纵坐标差（像素）。实测 0~27：
# 「任务到访得金币」的 +10 几乎平齐，「逛清单」的 +60 差 27，所以不能只给 20。
ACTION_BUTTON_DY = 45

# 任务行右侧金币按钮的文字形式（「+90」「+60」），面板上就是它本身可点。
_REWARD_RE = re.compile(r"\+\s*\d+")


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

# 面板顶部的「完成进度 20/20」不带括号，单独用一个宽松正则。
_OVERALL_RE = re.compile(r"(\d+)\s*/\s*(\d+)")

# OCR 常把数字认成形近字母：「去蚂蚁庄园逛逛哟(0/1)」会被识别成 (O/1)，
# 于是同一个任务在日志和去重里被当成两个不同任务，白白多跑两轮。
_SHAPE_MAP = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1"})


def task_key(text: str) -> str:
    """任务名比较用的键：把 OCR 形近字母并入数字。

    只用于比较与进度解析，不用于显示——界面上该显示什么还是显示什么。
    """
    return normalize_text(text).translate(_SHAPE_MAP)


def task_progress(text: str) -> tuple[int, int] | None:
    """从任务文字里取出进度，例如「拍立淘逛感兴趣的宝贝(3/5)」→ (3, 5)。

    淘宝的任务进度直接写在任务名里。拿到它才能判断一次点击到底有没有生效：
    停留时间不够时进度不会增加，任务会一直停在 (0/5)，主循环只看轮次是发现不了的。
    """
    match = _PROGRESS_RE.search(task_key(text))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def dedupe_key(text: str) -> str:
    """去重用的任务键：在 task_key 基础上去掉末尾的进度标记。

    进度会随执行而变（(0/2) → (1/2)），把它留在键里就意味着同一个任务每推进
    一次就换一个身份：「试了两次没进展就放弃」的计数被重置，任务永远放弃不了；
    反过来一旦改成粘性放弃，又会在有进展时把人误伤。去掉进度才能一处记账。
    """
    return _PROGRESS_RE.sub("", task_key(text)).strip()


# 任务名下方那行灰色说明文字。真机（一加 Ace 3）实测每行任务的结构是：
#     任务名      「搜一搜你心仪的宝贝(0/3)」
#     说明文字    「浏览15秒」
# 说明文字本身不是任务，但它可能命中名单词（「点击去逛」就在 jump_tasks 里），
# 一旦被当成任务挑走，就会去点它的中心坐标——那里是纯文本，点下去毫无反应。
# 双11「做任务赚能量」面板上，同一条说明文字后面还会紧跟奖励数字：
#     「点击去逛+400」「浏览15秒+400」「浏览5秒+400」
# 不带这段尾缀的写法只在淘金币面板上成立，不放开就会把它们当成任务名，
# 「点击去逛+400」还命中 jump_tasks 名单，于是被挑走、点在纯文本上白耗一轮。
_SUBTITLE_RE = re.compile(
    r"^(?:浏览\d+秒|参与答题|点击去逛|每日来.{0,8}|去完成|立即完成|已领取|已完成)(?:\+\d+)?$"
)


def is_subtitle(text: str) -> bool:
    """这条 OCR 文本是否是任务名下方的说明文字（不是任务本身）。"""
    return _SUBTITLE_RE.match(task_key(text)) is not None


# 任务名到下方说明文字的实际纵向距离：真机实测 34~43 像素，
# 上下相邻两行任务间距约 140 像素，给到 60 不会串到下一行。
_SUBTITLE_DY = 60
# 说明文字与任务名左边缘的允许偏差。两者本来就左对齐，容差只为抵消 OCR 抖动，
# 给太大反而会让它抓到同一行的其它文字块。
_SUBTITLE_DX = 40


def subtitle_for(items: Sequence[OcrItem], target: OcrItem) -> OcrItem | None:
    """取任务名正下方那行说明文字（「点击去逛」「参与答题」「浏览15秒」）。


    这两个字本身不能点，但它是**唯一能判断任务类型的地方**：面板上有的任务名
    完全自造词（「去神奇海洋逛逛吧」「领闪购红包下单直接省」），不在任何名单里，
    类型只写在副标题的「点击去逛」上。不读它，这两行就永远是 unknown、
    永远不会被挑中，最后只剩别的任务被反复点。
    """
    best: OcrItem | None = None
    best_dy = _SUBTITLE_DY + 1
    for item in items:
        if item is target or not is_subtitle(item.text):
            continue
        dy = item.y - target.y
        if dy <= 0 or dy > _SUBTITLE_DY:
            continue
        if abs(item.x - target.x) > _SUBTITLE_DX:
            continue
        if dy < best_dy:
            best = item
            best_dy = dy
    return best


def repeats_task_above(items: Sequence[OcrItem], target: OcrItem) -> bool:
    """这行是否是正上方那个任务名的复述。

    双11「做任务赚能量」面板把说明文字写成「任务名 + 奖励」，于是同一行任务
    会出现两条文字：

        逛一逛蚂蚁庄园(0/1)
        逛一逛蚂蚁庄园+800

    第二行既没有进度标记，又能通过「右侧有按钮」的检查（它和任务名共用同一个
    「去完成」），还命中 jump_tasks 名单，于是会被当成一个独立任务挑走 ——
    点下去和上面那个任务同一处，白耗一轮。判据是「文字包含上方任务名去掉进度
    后的全部字样」。

    不能只看「结尾带 +N」：能量面板上「去答题最高得+1000」本身就是一个任务，
    它同样没有进度、同样以奖励数字结尾，只能靠上面的任务名是否被复述来区分。
    """
    if task_progress(target.text) is not None:
        return False
    text = task_key(target.text)
    for item in items:
        if item is target or task_progress(item.text) is None:
            continue
        name = dedupe_key(item.text)
        if not name or name not in text:
            continue
        dy = target.y - item.y
        if dy <= 0 or dy > _SUBTITLE_DY:
            continue
        if abs(item.x - target.x) > _SUBTITLE_DX:
            continue
        return True
    return False


def task_finished(text: str) -> bool:
    """任务名里的进度已经做满（「逛清单，每15秒30金币(2/2)」）。

    「已完成」「已领取」是淘宝把按钮换掉之后才出现的文字，做满但还没领奖的
    任务仍叫原名。只靠 classify_task 认那两个字，就会把 (2/2) 的行当成可执行
    任务反复点进去——点了也没奖励，白耗一轮。
    """
    progress = task_progress(text)
    return progress is not None and progress[1] > 0 and progress[0] >= progress[1]


# 右侧金币按钮的起点要比任务名右边缘再往右这么多，才算「本行的按钮」。
# 真机上面板宽 720，任务名右边缘最宽到 501，金币从 600 起，留 20 的余量即可；
# 而顶部「+180 额外得」那个汇总金币在 y=325，靠上面的纵向距离把它排除。
_REWARD_MIN_GAP = 20
# 金币与任务名的纵向偏移上限，与 find_action_button 保持一致。
_REWARD_DY = ACTION_BUTTON_DY


def has_actionable_control(
    items: Sequence[OcrItem],
    target: OcrItem,
    buttons: Sequence[str] = DEFAULT_ACTION_BUTTONS,
) -> bool:
    """这一行的右侧是否还挂着可点的按钮（金币「+30」或「去完成」）。

    这是判断「这一行到底做没做完」唯一可靠的信号：做完并领过奖的行，右侧金币
    换成灰色对勾，而**对勾是图标、OCR 认不出文字**，任务名也不带「已完成」。
    于是只剩「右侧还有没有按钮」这一条可用：有按钮说明还能拿，没有就是完了。

    不做这个判断的后果是「任务到访得金币」这种行（没有 (x/y) 进度、做完后
    文字不变）会被一直当成可执行任务，每轮点进去一次，直到轮次耗尽。

    按钮文案与金币两种形态都要认：新版面板用金币当按钮，旧版是「去完成」。
    """
    name_right = target.x + target.width + _REWARD_MIN_GAP
    for item in items:
        if item is target or item.x < name_right:
            continue
        if abs(item.y - target.y) > _REWARD_DY:
            continue
        if _is_reward_badge(item.text) or _is_action_text(item.text, buttons):
            return True
    return False


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


def find_text_by_keywords(
    items: Iterable[OcrItem],
    keywords: Iterable[str],
    *,
    skip_nav_titles: bool = False,
) -> OcrItem | None:
    """按给定文案顺序找第一个命中的文字块，未命中返回 None。

    skip_nav_titles 用来跳过带返回箭头的页面标题：「←淘金币答题57464」含
    「淘金币」，但它是内层页面的标题，点它只会原地不动。
    """
    wanted = [normalize_text(word) for word in keywords]
    for item in items:
        if skip_nav_titles and any(glyph in item.text for glyph in NAV_GLYPHS):
            continue
        for word in wanted:
            if word and word in item.text:
                return item
    return None


def nav_title(items: Iterable[OcrItem]) -> OcrItem | None:
    """当前屏的页面标题（带返回箭头的内层页），首页返回 None。

    有它说明用户已经翻进了某一层内页（领券中心、商品详情、活动会场……）。
    这些页面的正文里很常见「淘金币」三个字，例如领券中心的
    「淘金币频道内，可叠加所有优惠」「淘金币频道到手价：」，「需淘金币1500」。
    在这种页面上继续按「首页那一排图标」去找淘金币入口，就会点在纯文本上，
    页面纹丝不动 —— 真机连点四次同一处，最后任务报「进不去面板」。
    所以判断顺序必须是「先认出这是内页、按返回退出」，而不是继续找入口。
    """
    for item in items:
        if any(glyph in item.text for glyph in NAV_GLYPHS):
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

# 任务名里出现这些字样时，说明要点「打开」跳外部应用。
# 判断依据是**这台手机上装没装**，不是当前登录账号装没装 ——
# 应用分身只是把淘宝克隆了一份，本机应用是共用的；分身里点头条/支付宝
# 依然会拉起主账号的那个应用。所以这里只挡「整台手机都没有」的任务
# （例如本机没装今日头条，点下去必失败），装了的一律照常去做。
# 键是任务名里的关键词，值是对应的应用包名。
OFFSITE_APPS = {
    "蚂蚁庄园": "com.eg.android.AlipayGphone",
    "蚂蚁森林": "com.eg.android.AlipayGphone",
    "支付宝": "com.eg.android.AlipayGphone",
    "闲鱼": "com.taobao.idlefish",
    "今日头条": "com.ss.android.article.news",
    "头条": "com.ss.android.article.news",
    "百度": "com.baidu.searchbox",
    "快手": "com.smile.gifmaker",
}

# 搜索框的锚文本。淘宝的搜索栏写的是「搜索宝贝」「搜索商品」这种
# 「搜索 + 你要搜的东西」；而页面顶部的推广横幅写的是「搜索有福利」
# 「搜索后浏览立得奖励」，是「搜索 + 收益」。
#
# 真机实测（一加 Ace 3 / 淘宝 2026-10）只按「搜索」两个字做包含匹配会命中横幅：
# 横幅的纵坐标比搜索栏高 100 像素，点下去输入框拿不到焦点，接着敲进去的
# 关键词直接丢掉，最后按「搜索」搜的是个空框 —— 进度永远停在 (1/3)。
# 所以锚点必须是完整短语，不能是裸的「搜索」。
SEARCH_BOX_HINTS = ("搜索宝贝", "搜索商品", "搜你想知道的", "请输入", "搜一搜")

# 面板上的任务名「搜一搜你心仪的宝贝(1/3)」也含锚文本，靠长度上限挡掉。
SEARCH_BOX_MAX_LEN = 8

# 提交搜索用的按钮文案。输入框本身也可能写着「搜索」，所以调用方要后找它。
SEARCH_SUBMIT_WORDS = ("搜索一下", "搜索", "搜")


def search_box(
    items: Iterable[OcrItem], submit: OcrItem | None = None
) -> OcrItem | None:
    """定位搜索输入框：命中锚文本，且整块文案足够短。

    submit 是同一页上找到的提交按钮。给了按钮就再要求输入框与它同一行：
    搜索栏是输入框和「搜索」按钮并排的一行，页面别处的横幅不在这一行上。
    两道条件都过才返回，避免关键词敲进一个没有焦点的文本框。
    """
    candidates: list[OcrItem] = []
    for item in items:
        text = normalize_text(item.text)
        if not text or len(text) > SEARCH_BOX_MAX_LEN:
            continue
        if any(word in text for word in SEARCH_BOX_HINTS):
            candidates.append(item)
    if submit is not None:
        button_y = submit.center[1]
        candidates = [
            item for item in candidates
            if abs(item.center[1] - button_y) <= ROW_TOLERANCE
        ]
    return candidates[0] if candidates else None


def search_submit_button(items: Iterable[OcrItem]) -> OcrItem | None:
    """定位提交搜索的按钮；淘宝有时根本没有按钮，只能靠回车提交。"""
    wanted = [normalize_text(word) for word in SEARCH_SUBMIT_WORDS]
    for word in wanted:
        for item in items:
            if normalize_text(item.text) == word:
                return item
    return None


def required_package(task_text: str) -> str | None:
    """这个任务要点「打开」跳哪个应用；不需要外部应用时返回 None。

    用于在整台手机都没装该应用时提前跳过，避免点下去弹框、等待、再白跑一轮。
    """
    text = normalize_text(task_text)
    for marker, package in OFFSITE_APPS.items():
        if marker in text:
            return package
    return None


def task_unreachable(task_text: str, installed: Collection[str]) -> bool:
    """任务要跳的应用整台手机都没有 —— 这个任务注定完不成。

    installed 必须是**本机全量**包名（不带 --user 查询），不能传某个账号的包名，
    否则装了支付宝的分身也会被误判成完不成。传空集合表示「查询失败，不知道」，
    这时一律返回 False —— 宁可直接做，也不能把所有任务都当掉。
    """
    package = required_package(task_text)
    if package is None or not installed:
        return False
    return package not in installed


def overall_progress(items: Iterable[OcrItem]) -> tuple[int, int] | None:
    """读面板顶部的「完成进度 n/m」，没有这一行时返回 None。

    主账号的面板**没有**进度行（只能看到下面任务全做完后领一个总金币），
    分身账号的面板才有。所以它只能作为「已完成」的充分条件，不能当必要条件。

    这行的写法不带括号（「完成进度 20/20」），跟任务名里的 (3/5) 不同，
    所以不能直接复用 task_progress 的括号正则。
    """
    for item in items:
        if "完成进度" not in task_key(item.text):
            continue
        progress = task_progress(item.text)
        if progress is not None:
            return progress
        match = _OVERALL_RE.search(task_key(item.text))
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def overall_done(items: Iterable[OcrItem]) -> bool:
    """从面板本身看，今日是否已经全部做完。

    只认「完成进度 n/m 且 n >= m」这一条证据，也就是分身账号面板顶部那行。

    刻意**不**把「当前屏没有可执行任务」当成完成的证据：任务面板是可以下滑的，
    停在中间时下面的任务根本不在截图里，用这条判断会提前结束。
    没有进度行时（主账号的面板）交给主循环向上滑回顶部复查，靠连续空轮判定结束，
    那条路径本来就是安全的。
    """
    progress = overall_progress(items)
    return progress is not None and progress[1] > 0 and progress[0] >= progress[1]


# 淘金币首页的特征词。与任务面板的区别：首页有「赚能量」和「升级赛车」，
# 面板则是「赚金币抵钱」「完成进度」。在首页按返回键会直接退出淘金币，
# 所以必须改点「赚金币」重新打开面板。
COIN_HOME_MARKERS = (
    "升级赛车",
    "赚能量",
)

# 淘金币首页上打开任务面板的入口文字。
COIN_HOME_ENTRY = ("赚金币",)

# 双11「做任务赚能量」面板的特征词。
#
# 这个面板和淘金币面板的**行结构完全一样**：左边任务名带 (x/y)、下面一行
# 说明、右侧「去完成」/「去兑换」，所以任务挑选逻辑可以原样复用；差别只在顶部：
#  淘金币面板 —— 「赚金币抵钱」「完成进度 n/m」
#  能量面板   —— 「做任务赚能量」「任务奖励 …得车饰」「当前进度 n/50」
#
# 「当前进度 0/50」是活动累计计数，**不是今日完成度**：它从 0 起、做任务才涨，
# 绝不能当「今日已做完」的判据（overall_done 只认「完成进度」，这里刻意用
# 「当前进度」就是为了不被它误命中）。
ENERGY_PANEL_MARKERS = (
    "做任务赚能量",
    "当前进度",
    "任务奖励",
    # 面板可以整屏下滑，头部那三行会滚出屏幕，只剩固定的标题和任务行。
    # 下面这些是只在能量面板出现的任务名，用来覆盖下滑后的状态。
    "每拉1人最高得",
    "天猫积分兑换能量",
    "去玩趣味小游戏",
    "去快手打卡领手机",
    "逛一逛支付宝金豆夺宝",
)

# 单个特征词不足以说明是能量面板（「任务奖励」在别的活动页也可能出现）。
ENERGY_PANEL_MIN_HITS = 2

# 淘金币首页上打开能量面板的入口，与「去赚金币」并排。
ENERGY_HOME_ENTRY = ("去赚能量", "赚能量")

# 淘宝首页上通往淘金币的入口文字。真机实测首页那一排图标会被 OCR 合并成一条，
# 例如「领淘金币充值中心试用领取常买超市」，所以只能按字符占比推算点击位置。
COIN_ENTRY_KEYWORDS = ("淘金币", "领金币", "我的淘金币")

# 内层页面的标题前会带返回箭头，例如「←淘金币答题57464」。首页那一排图标
# 即使被 OCR 合并成一整块（「领淘金币充值中心试用领取常买超市」）也不含这些符号，
# 所以按导航符号排除比按长度排除可靠 —— 合并块比标题还长。
NAV_GLYPHS = ("←", "→")

# 淘金币答题页（每日答题活动，会从首页「去赚金币」附近被误点进来）。
# 这一层按返回能直接退出，但页面上没有任务入口，必须识别出来主动退出，
# 否则会一直点标题里的「淘金币」，页面纹丝不动。
QUIZ_PAGE_MARKERS = ("答答星球", "我选好了", "答题")

# 答题页上的选项块。真机实测 OCR 把字母和选项内容合成一块：
#     A表演者姓黄 / B两种乐器的名字
# 字母后面必须紧跟中文，这样面板上的拉丁字母串（「TAO」「OA」）和结果页的
# 「正确答案：A」都不会被误认成选项；题面「1.猜一猜：…单选题」以数字开头，
# 也进不来。
QUIZ_OPTION_RE = re.compile(r"^[A-D][^\x00-\x7f]{2,}$")

# 答题页底部的提交按钮。它是**灰的**，选完选项才变黄，所以点击顺序不能反。
QUIZ_SUBMIT_WORDS = ("我选好了",)

# 答完后的结果页特征：真机实测会出现「正确答案：A」「你的答案：A」，
# 页面底部变成「已成功领取奖励 回到主页」。看到它才算这次答题真的完成了。
QUIZ_DONE_MARKERS = ("正确答案", "你的答案", "已成功领取奖励")

# 淘宝首页的特征词，用于区分"首页"和"启动后的促销浮层"。
# 首页按返回键会直接退出淘宝，促销浮层则必须按返回才能关掉，两者处理相反。
TB_HOME_MARKERS = ("搜索", "推荐", "关注", "购物车")

# 首页底部标签栏的文字。商品详情页也有「搜索」「购物车」，只能靠这一栏区分：
# 有标签栏说明还在首页（按返回会退出淘宝），没有就说明已经进到内层页面（该按返回）。
HOME_TAB_MARKERS = ("我的淘宝",)

# 淘宝首页上的促销弹层会把首页入口盖住，但 OCR 仍可能识别到弹层后面的
# 「搜索」「推荐」和「淘金币」。命中这些弹层文案时必须先关掉当前层，再重扫首页。
#
# 这里只放**弹层专属**的文案：真机实测首页顶部的「双11开门红|券后低至7.2折」
# 横幅上就有「立即抢>」，把它当特征词会让正常首页被当成弹层，处理方式却相反
# （弹层要按返回，首页要按返回就退出淘宝），主循环于是反复退出-重开淘宝。
PROMO_LAYER_MARKERS = (
    "提前领好券",
    "惊喜券",
    "至高领",
    "双11狂欢",
)

# 弹层判定同样要求多个特征词：首页在不同活动期间会零星出现单个活动词
# （如顶栏的「双11狂欢」），单个命中不足以说明入口被盖住。
PROMO_LAYER_MIN_HITS = 2

# 今日任务全部完成时弹窗的特征词。
# 真机实测：弹窗标题「今日速赚570淘金币」，正文「今日快速赚奖励已拿完」「记得明天再来」。
# OCR 有时把正文拆成几段，所以用「奖励已拿完」这种不含数字的短片段做匹配。
# 这是正常结束，不能当成识别失败去走 AbortTask。
#
# 只放「完成」才有可能出现的说法。曾经收过「更多金币等你赚」，但它是面板底部的
# 常驻推广语 —— 真机 12:05:08 那帧任务还剩四行没做，它照样显示，于是整轮任务
# 被当成「今日已做完」提前收尾。这类招揽文案不能当完成证据。
DAILY_DONE_MARKERS = (
    "奖励已拿完",
    "记得明天再来",
    "继续浏览精选清单",
)


def daily_done_visible(items: Iterable[OcrItem]) -> bool:
    """当前屏是否显示"今日任务全部完成"提示。"""
    return panel_visible(items, DAILY_DONE_MARKERS)


# 已经跳到淘宝以外（支付宝、浏览器、小程序）时才能看到的文字。
#
# 手机端跑任务时控制器是 Android 原生控制器，**没有 shell**：问不出前台包名，
# 也就无法知道「现在最前面的是不是淘宝」。这时唯一的信号就是屏幕上有什么字。
# 一旦落到这些页面上，按多少次返回都回不到淘宝面板（返回键只会退出当前应用），
# 只能重新把淘宝拉起来。所以这些词是「必须重启淘宝」的判据，而不是普通干扰页。
#
# 只放**支付宝外壳**的固定文案，并且要求命中两条：任务跳过去的小程序页里文案随活动
# 而变，而「蚂蚁庄园」「蚂蚁森林」「扫一扫」在淘宝自己的任务行和搜索框上就有，
# 拿它们判定会让正常的面板被当成「跳到别的应用」从而反复重启淘宝。
FOREIGN_APP_MARKERS = (
    "收付款",
    "看股票行情",
    "记账本",
    "卡包",
    "余额宝",
    "出行",
)
FOREIGN_APP_MIN_HITS = 2


def foreign_app_visible(items: Iterable[OcrItem]) -> bool:
    """当前屏是否已经不在淘宝里（跳到了支付宝 / 浏览器 / 小程序）。"""
    return panel_visible(items, FOREIGN_APP_MARKERS, min_hits=FOREIGN_APP_MIN_HITS)


def _panel_row_evidence(items: Iterable[OcrItem]) -> int:
    """数一下屏上有几条「任务行」痕迹：说明文字或右侧金币。"""
    count = 0
    for item in items:
        if is_subtitle(item.text) or _is_reward_badge(item.text):
            count += 1
    return count


def task_panel_visible(items: Iterable[OcrItem]) -> bool:
    """严格判定当前屏是否真的在淘金币任务面板。

    和 panel_visible 的区别：至少要命中 PANEL_MIN_HITS 个面板专属特征词。
    首页、促销浮层、搜索结果页都可能零星出现「立即领取」，用单标记判定会把它们
    当成面板，进而把页面上的「搜索」框误认成搜索任务。

    能量面板也要先排除：它和淘金币面板共用「搜一搜你心仪的宝贝」「拍立淘逛
    感兴趣的宝贝」两个任务名，真机上能量面板能命中 PANEL_MARKERS 两条，
    于是被当成了淘金币面板。

    特征词凑不够时按行结构兜底：任务快做完的时候淘宝会换掉一批任务，顶部的
    「完成下方任务得额外金币」这类汇总文案被滚出屏幕，整屏只剩任务行和底部的
    「换一换」。此时特征词可能只命中一条，但界面确实就是任务面板 —— 判成
    「不在面板」会让恢复流程去点面板标题里的「淘金币」，然后一直失败到停任务。
    """
    items = list(items)
    if energy_panel_visible(items):
        return False
    if panel_visible(items, PANEL_MARKERS, min_hits=PANEL_MIN_HITS):
        return True
    # 首页底部标签栏是「还在首页」的铁证，有它就不能按行结构认面板。
    if home_tab_bar_visible(items):
        return False
    return _panel_row_evidence(items) >= PANEL_ROW_MIN_HITS


def team_page_visible(items: Iterable[OcrItem]) -> bool:
    """当前屏是否是双11组队活动页（需要退出而不是继续操作）。"""
    return panel_visible(items, TEAM_PAGE_MARKERS)


def energy_panel_visible(items: Iterable[OcrItem]) -> bool:
    """当前屏是否是双11「做任务赚能量」面板。"""
    return panel_visible(items, ENERGY_PANEL_MARKERS, min_hits=ENERGY_PANEL_MIN_HITS)


def coin_home_visible(items: Iterable[OcrItem]) -> bool:
    """当前屏是否是淘金币赛车首页（可以点「赚金币」进入任务面板）。

    能量面板的标题「做任务赚能量」里也含「赚能量」，会被 COIN_HOME_MARKERS
    错认成首页，于是「点赚金币开面板」的动作打在面板标题上，页面纹丝不动。
    所以先排除能量面板。
    """
    items = list(items)
    if energy_panel_visible(items):
        return False
    return panel_visible(items, COIN_HOME_MARKERS)


def quiz_page_visible(items: Iterable[OcrItem]) -> bool:
    """当前屏是否是淘金币答题页（没有任务入口，按返回退出）。"""
    return panel_visible(items, QUIZ_PAGE_MARKERS, min_hits=2)


def quiz_answered(items: Iterable[OcrItem]) -> bool:
    """答题页是否已经答完、正显示结果（正确答案 / 已领取奖励）。"""
    return panel_visible(items, QUIZ_DONE_MARKERS)


def quiz_option(items: Iterable[OcrItem]) -> OcrItem | None:
    """答题页上的一个选项，取最上面那个，没有则返回 None。

    单选题的正确答案猜不出来，也不需要猜：任务文案写的是「参与答题」，
    奖励挂在参不参与上而不是答对与否，随便选一个再提交即可。
    """
    options = [item for item in items if QUIZ_OPTION_RE.match(item.text)]
    if not options:
        return None
    return min(options, key=lambda item: (item.y, item.x))


def quiz_submit(items: Iterable[OcrItem]) -> OcrItem | None:
    """答题页底部的提交按钮，未渲染出来时返回 None。"""
    return find_text_by_keywords(items, QUIZ_SUBMIT_WORDS)


def taobao_home_visible(items: Iterable[OcrItem]) -> bool:
    """当前屏是否是淘宝 App 首页（不是促销浮层，也不是淘金币页面）。"""
    return panel_visible(items, TB_HOME_MARKERS)


def home_tab_bar_visible(items: Iterable[OcrItem]) -> bool:
    """当前屏是否显示首页底部标签栏。

    商品详情页、搜索结果页同样有「搜索」「购物车」，只有标签栏里的「我的淘宝」
    是首页独有的。区分这两类很重要：首页按返回会退出淘宝，详情页必须按返回。
    """
    return panel_visible(items, HOME_TAB_MARKERS)


def promo_layer_visible(items: Iterable[OcrItem]) -> bool:
    """当前屏是否有盖住淘宝首页入口的促销弹层。"""
    return panel_visible(items, PROMO_LAYER_MARKERS, min_hits=PROMO_LAYER_MIN_HITS)


# 跳出淘宝时的确认框。真机实测有三种：
#
#   1. 系统「打开其他应用」横向排版（跳支付宝小程序）：
#        “淘宝(分身)”想要打开“支付宝”小程序 / □始终允许打开 / 取消 | 打开
#   2. 系统「打开其他应用」纵向排版（一加 Ace 3，跳闲鱼）：
#        “淘宝(分身)”想要打开“闲鱼” / 30天内允许 / 仅本次允许 / 取消
#      这种**没有「打开」按钮**，只按「打开」找会找不到按钮然后直接放弃，
#      于是永远停在这个框上。
#   3. 淘宝自己的站外跳转提示（跳头条等外部页）：
#        提示 / 下个页面不受我们控制哦，使用时请注意安全 https://z.toutiao.com
#        取消 | 浏览器打开
#      这种框按返回键关不掉，只能点按钮。
#
# 这些框都盖在任务面板上方，面板文字仍然能被 OCR 认出来，所以不检测它们的话
# 主循环会以为还在面板上，反复点任务按钮、反复被同一个框拦下。
OPEN_APP_DIALOG_MARKERS = (
    "想要打开",
    "始终允许打开",
    "天内允许",
    "仅本次允许",
    "不受我们控制",
    "浏览器打开",
)

# 按优先级排列：优先「只有本次」，避免给分身长期授权；
# 「浏览器打开」是站外跳转提示上的确认按钮。
OPEN_APP_DIALOG_CONFIRM = (
    "打开",
    "允许",
    "仅本次允许",
    "30天内允许",
    "始终允许打开",
    "浏览器打开",
)


def open_app_dialog_visible(items: Iterable[OcrItem]) -> bool:
    """当前屏是否挡着「想要打开其他应用」的确认框。"""
    return panel_visible(items, OPEN_APP_DIALOG_MARKERS)


def open_app_dialog_button(items: Iterable[OcrItem], words: Sequence[str]) -> OcrItem | None:
    """在确认框上找确认按钮。

    只认完全相等的文字块：标题里也含「打开」（想要打开…），
    用包含匹配会点到标题上，什么都不会发生。

    words 按优先级排列 —— 纵向排版没有「打开」，只能退到「仅本次允许」。
    """
    by_text = {normalize_text(item.text): item for item in items}
    for word in words:
        item = by_text.get(normalize_text(word))
        if item is not None:
            return item
    return None


def coin_entry_point(items: Sequence[OcrItem]) -> OcrItem | None:
    """在淘宝首页上定位「淘金币」入口文字块。

    只认首页那一排图标的短标签。内层页面的标题里也有「淘金币」，例如答题页顶部
    的「←淘金币答题57464」、结果页的「淘金币抵扣」说明，直接按包含匹配会点进去
    出不来（点击后页面不变，于是每轮都重复点同一处）。

    内层页面的**正文**同样会带「淘金币」：领券中心上就有「淘金币频道内，可叠加
    所有优惠」「淘金币频道到手价：」「需淘金币1500」。这些都是句子或价格说明，
    不是图标标签。首页的图标标签最长也就「领淘金币充值中心试用领取常买超市」
    这种不带标点的短串，因此按「句子特征」排除：带冒号、带「频道/到手价/需」、
    或明显偏长的块一律不算入口。
    """
    for item in items:
        text = item.text
        if any(glyph in text for glyph in NAV_GLYPHS):
            continue
        if _looks_like_sentence(text):
            continue
        for keyword in COIN_ENTRY_KEYWORDS:
            if normalize_text(keyword) in text:
                return item
    return None


# 内页正文的句子特征：冒号、逗号、句号，或价目说明里的这些词。
# 首页图标那一排是扫码式的短标签，一个都不会带。
_SENTENCE_MARKS = ("：", ":", "，", ",", "。", "、")
_SENTENCE_WORDS = (
    "频道",
    "到手价",
    "需淘金币",
    "可叠加",
    "优惠",
    "折扣",
    "抵扣",
)
# 首页图标标签拼在一起也就二十来个字，超过这个长度只能是段落文字。
_ENTRY_MAX_LEN = 24


def _looks_like_sentence(text: str) -> bool:
    """这个文字块像不像一页正文，而不是一排图标里的短标签。"""
    if len(text) > _ENTRY_MAX_LEN:
        return True
    if any(mark in text for mark in _SENTENCE_MARKS):
        return True
    return any(word in text for word in _SENTENCE_WORDS)


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
    tried: Collection[str] = (),
) -> PanelPick:
    """挑出一个可执行任务：先挑做到一半的，再按阅读顺序挑。

    黑名单、已完成等不可执行项会被记录到 skipped 中，便于日志排查。
    kinds 用于限定只挑选某一类任务（Pipeline 中区分搜索/浏览/跳转分支）。
    exclude 里的任务名会被跳过，用于放弃那些反复执行也不推进的任务。
    tried 里的任务名表示「这次运行已经做过」，排序时排到没做过的后面。

    排序刻意不是纯阅读顺序：面板上排在前面的任务可能永远推不动（例如
    「任务到访得金币」这种没有 (x/y)、点了也不涨的），纯按顺序挑就会一直
    挑中它，把轮次全耗在它身上，而下面那些真正还能推进的任务一次都轮不到。
    做到一半的 (1/3) 已经证明能推进，先做完它更划算。

    tried 还要再往前一层：像「拍立淘(0/5)」这种要做五遍的任务如果一直优先，
    它上方的行会把轮次吃光，下方一屏之外的「去蚂蚁森林收能量」一轮都轮不到。
    所以先铺一遍没做过的，再回头做需要多遍的。
    """
    config = config or load_config()
    allowed = tuple(kinds) if kinds else EXECUTABLE_KINDS
    skipped: list[str] = []
    candidates: list[tuple[int, int, OcrItem, TaskDecision]] = []
    for item in items:
        # 说明文字（「浏览15秒」「参与答题」「点击去逛」）不是任务，
        # 它就在任务名正下方，且「点击去逛」本身还命中 jump_tasks 名单，
        # 不排除就会被当成任务挑走，点它只是点在纯文本上。
        if is_subtitle(item.text):
            continue
        # 说明文字也可能写成「任务名 + 奖励」（「逛一逛蚂蚁庄园+800」），
        # 它带不了进度、却和上面的任务共用同一个「去完成」，会被当成另一个任务。
        if repeats_task_above(items, item):
            continue
        text = dedupe_key(item.text)
        if text in exclude:
            skipped.append(f"{item.text}(已放弃)")
            continue
        # 任务名认不出来时用副标题兜底：面板上有的任务名是自造词，类型只写在
        # 「点击去逛」这种说明里，不看它这些行永远是 unknown、永远挑不到。
        subtitle = subtitle_for(items, item)
        decision = classify_task(
            item.text, config, hint=subtitle.text if subtitle is not None else ""
        )
        if decision.kind not in allowed:
            skipped.append(f"{item.text}({decision.reason or decision.kind})")
            continue
        if task_finished(item.text):
            skipped.append(f"{item.text}(已做满)")
            continue
        progress = task_progress(item.text)
        # 奖励已领走的行右侧金币换成灰色对勾，OCR 认不出对勾、任务名也不变，
        # 只剩「右边还有没有按钮」这一个信号。不判它，「任务到访得金币」
        # 这类无进度行会被当成可执行任务，每轮点进去一次直到轮次耗尽。
        #
        # 但这个信号只能用来**排除**，不能用来**认定**：金币按钮是独立的一小块，
        # OCR 在一帧里漏掉它很常见（真机 12:04:46 那一整屏只认出顶部一个 +300，
        # 四行的按钮全漏了），漏掉就被读成「已完成」，任务再也挑不到。
        # 所以带 (1/2)(1/4) 这种没做满的进度、又缺按钮时，以进度为准放行。
        if not has_actionable_control(items, item):
            if progress is None or progress[1] <= 0:
                skipped.append(f"{item.text}(右侧无按钮，已完成)")
                continue
        # 0 = 还没做过的，1 = 本次运行已经做过（可能还要多做几遍）。
        fresh = 0 if text not in tried else 1
        # 0 = 做到一半的优先，保持 discover 顺序稳定。
        rank = 0 if progress is not None and progress[0] > 0 else 1
        candidates.append((fresh, rank, item, decision))

    if not candidates:
        return PanelPick(TaskDecision("none", reason="当前屏没有可执行任务"), None, tuple(skipped))
    candidates.sort(key=lambda entry: (entry[0], entry[1]))
    _, _, item, decision = candidates[0]
    return PanelPick(decision, item, tuple(skipped))


def find_action_button(
    items: Sequence[OcrItem],
    target: OcrItem,
    buttons: Sequence[str] = DEFAULT_ACTION_BUTTONS,
) -> OcrItem | None:
    """找到任务名对应行的可点击按钮，点击坐标应打在按钮上。

    真机（一加 Ace 3）实测一行任务的结构是：左边任务名，名字下方一行灰色小字
    （「浏览15秒」「参与答题」「点击去逛」—— 这些是**说明文字，不是按钮**），
    右边是本行真正的按钮：一个黄色金币，文字只有「+90」「+60」这种。

    所以命中条件有两条，缺一不可：
        1. 起点在任务名右边缘之外 —— 顶部「+180 额外得」那个汇总金币在 x=325，
           而「逛清单」名字右边缘在 x=501，靠它把汇总金币排除掉；
        2. 纵向偏移在 ACTION_BUTTON_DY 以内 —— 实测金币相对任务名的 dy 为 9~27，
           原来的 ROW_TOLERANCE=20 太小，「逛清单」的 +60（dy=27）就漏掉了。

    找不到时返回 None，调用方会退化成点击任务名本身（整行卡片可点）。
    """
    name_right = target.x + target.width
    best: OcrItem | None = None
    best_dy = 0
    for item in items:
        if item is target or item.x < name_right:
            continue
        dy = abs(item.y - target.y)
        if dy > ACTION_BUTTON_DY:
            continue
        if not (_is_action_text(item.text, buttons) or _is_reward_badge(item.text)):
            continue
        if best is None or dy < best_dy:
            best = item
            best_dy = dy
    if best is not None:
        return best

    # 右侧没有找到按钮时，才考虑「按钮文案被并进了任务名」这种情况。
    # 顺序不能反：任务名「去芭芭农场逛一逛」里就含名单词「逛一逛」，
    # 先做包含匹配会把它自己当成按钮返回，右侧真正的 +30 反而永远找不到。
    for button in buttons:
        normalized = normalize_text(button)
        if normalized and normalized in target.text:
            return target
    return None


def _is_action_text(text: str, buttons: Sequence[str]) -> bool:
    """文字是否命中任一按钮文案。"""
    normalized = normalize_text(text)
    return any(word and normalize_text(word) in normalized for word in buttons)


def _is_reward_badge(text: str) -> bool:
    """是否是任务行右侧的金币金额块（「+90」「+60」）。

    面板上每行的按钮就是这枚金币，文字里没有动词可匹配，只能按「+数字」认。
    """
    return _REWARD_RE.fullmatch(normalize_text(text)) is not None


def search_keyword(config: dict[str, Any], attempt: int = 0) -> str:
    """按轮次轮换搜索关键词，避免反复搜索同一词被判定为异常行为。"""
    keywords = config.get("search_keywords") or []
    if not keywords:
        return ""
    return str(keywords[attempt % len(keywords)])


# 任务要求的浏览秒数：写在任务名里（「逛清单，每15秒30金币」）
# 或名字下方那行说明文字里（「浏览15秒」「浏览5秒」）。
_REQUIRED_SECONDS_RE = re.compile(r"(?:浏览|每)\s*(\d+)\s*秒")


def required_seconds(items: Sequence[OcrItem], target: OcrItem) -> float:
    """这个任务在目标页面要求的浏览秒数。

    淘宝按「页面有没有在动」累计浏览时长，停着不动不计时，所以这个秒数就是
    下限：没滑够就去下一个任务，进度不会增加，等于白做一次。秒数写在任务名
    或它下方那行说明文字里，只认任务名自己与紧挨着它下方的那条说明 —— 整屏
    每行都有一条说明文字，取错行会让停留时间凭空翻几倍。
    """
    own = _REQUIRED_SECONDS_RE.search(normalize_text(target.text))
    if own:
        return float(own.group(1))

    best = 0.0
    best_dy: int | None = None
    for item in items:
        if not is_subtitle(item.text):
            continue
        dy = item.y - target.y
        if dy <= 0 or dy > ACTION_BUTTON_DY * 2:
            continue
        match = _REQUIRED_SECONDS_RE.search(normalize_text(item.text))
        if not match:
            continue
        if best_dy is None or dy < best_dy:
            best_dy = dy
            best = float(match.group(1))
    return best


# 满足停留要求后多留的秒数：淘宝计时是整秒粒度，卡在边界结束容易被判成没满。
DWELL_MARGIN = 4.0


def dwell_seconds(kind: str, config: dict[str, Any], required: float = 0.0) -> float:
    """这个任务该停留多久。

    required 是任务自己写明的秒数（「浏览15秒」「每15秒30金币」），可能为 0。
    淘宝的浏览任务按「页面在动」累计时长，所以这个秒数就是真正的下限：
    停不够就去下一个任务，进度不会增加，等于白做一次。

    配置里的时长只是**没识别到要求时的兜底值**，不能当成下限。把两者取 max 的
    后果是配置值变成永远打不破的地板：配置 22 秒时，一个写着「浏览5秒」的任务
    也要停满 22 秒 —— 多出来的 17 秒既拿不到额外奖励，也不推进任何进度，
    纯粹是白等。
    """
    timing = config.get("timing", {})
    key = {
        "browse": "browse_duration_seconds",
        "jump": "jump_duration_seconds",
    }.get(kind)
    if not key:
        return 0.0
    if required > 0:
        return required + DWELL_MARGIN
    return float(timing.get(key, 3))
