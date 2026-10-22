"""用假设备验证淘金币主循环：挑任务、点击按钮、跳过黑名单、安全停止。

这里不依赖 MaaFramework 与真机，用 FakeContext 模拟 post_screencap /
run_recognition / post_click，从而验证 agent/actions.py 的流程编排是否正确。
"""

from __future__ import annotations

import logging
import os
import unittest
from types import SimpleNamespace

from agent import actions, panel
from agent.custom import APP_USER_ENV, app_user_id, load_config, start_app_command


class FakeJob:
    """模拟 MaaFramework 的 Job：wait() 返回自身，get() 取结果。"""

    def __init__(self, value=None, ok: bool = True) -> None:
        self._value = value
        self._ok = ok

    def wait(self) -> "FakeJob":
        return self

    def succeeded(self) -> bool:
        return self._ok

    def get(self):
        if not self._ok:
            raise RuntimeError("job failed")
        return self._value


class FakeImage(int):
    """假截图：既能当截图序号用，又带. shape 供手势坐标系读取。

    run_recognition 用 `image if isinstance(image, int) else 0` 取屏幕序号，
    所以这里继承 int 才能继续按序号映射 OCR 结果。
    """

    def __new__(cls, index: int, size: tuple[int, int]):
        obj = super().__new__(cls, index)
        obj.shape = (int(size[1]), int(size[0]), 3)
        return obj


class FakeController:
    def __init__(
        self,
        screens: list[list[dict]],
        size=(1280, 720),
        packages: list[str] | None = None,
        frame_size=None,
    ) -> None:
        self.screens = screens
        self.index = 0
        self.clicks: list[tuple[int, int]] = []
        self.keys: list[int] = []
        self.swipes: list[tuple[int, int, int, int]] = []
        self.inputs: list[str] = []
        self.shells: list[str] = []
        self.started: list[str] = []
        # 默认输入成功；置 False 用来复现 adb input text 拒绝非 ASCII 的情况。
        self.input_ok = True
        # 手机上用 MaaFwApp 时控制器是 Android 原生控制器：没有 shell，
        # 只能靠框架的 StartApp 拉起应用。
        self.shell_supported = True
        self.foreground = "com.taobao.taobao"
        self.resolution = list(size)
        # 截图尺寸与设备分辨率不是一回事：真机 1080x2376 缩到 720x1584。
        self.frame_size = frame_size
        self.packages = packages

    def post_screencap(self) -> FakeJob:
        index = min(self.index, len(self.screens) - 1)
        self.index += 1
        if self.frame_size is None:
            return FakeJob(index)
        return FakeJob(FakeImage(index, self.frame_size))

    def post_click(self, x, y, contact=0, pressure=1) -> FakeJob:  # noqa: ANN001
        self.clicks.append((int(x), int(y)))
        return FakeJob()

    def post_click_key(self, key) -> FakeJob:  # noqa: ANN001
        self.keys.append(int(key))
        return FakeJob()

    def post_swipe(self, x1, y1, x2, y2, duration=300, *args, **kwargs) -> FakeJob:  # noqa: ANN002
        self.swipes.append((int(x1), int(y1), int(x2), int(y2)))
        return FakeJob()

    def post_input_text(self, text) -> FakeJob:  # noqa: ANN001
        self.inputs.append(str(text))
        return FakeJob(ok=self.input_ok)

    def post_start_app(self, package) -> FakeJob:  # noqa: ANN001
        self.started.append(str(package))
        return FakeJob()

    def post_shell(self, cmd, timeout=20000) -> FakeJob:  # noqa: ANN001
        self.shells.append(str(cmd))
        if not self.shell_supported:
            # 框架对不支持 shell 的控制器会直接让作业失败
            return FakeJob(None, ok=False)
        if "topResumedActivity" in str(cmd):
            return FakeJob(
                f"  topResumedActivity=ActivityRecord{{263017643 u999 "
                f"{self.foreground}/com.taobao.tao.welcome.Welcome t11518}}"
            )
        if "pm list packages" in str(cmd) and self.packages is not None:
            return FakeJob("\n".join(f"package:{name}" for name in self.packages))
        return FakeJob("")


class FakeContext:
    """把截图序号映射到预先安排好的 OCR 结果。"""

    def __init__(self, screens: list[list[dict]], packages: list[str] | None = None, frame_size=None) -> None:
        self.screens = screens
        self.controller = FakeController(screens, packages=packages, frame_size=frame_size)
        self.tasker = SimpleNamespace(controller=self.controller)
        self.reco_entries: list[str] = []
        # 帧尺寸是模块级缓存（真机上整轮任务共用一个坐标系），测试之间要清掉。
        actions._FRAME_SIZE = None

    def run_recognition(self, entry, image, pipeline_override=None):  # noqa: ANN001
        self.reco_entries.append(entry)
        index = image if isinstance(image, int) else 0
        results = self.screens[min(index, len(self.screens) - 1)]
        return SimpleNamespace(all_results=results)


def fast_config() -> dict:
    """把等待时间全部压成 0，让单元测试瞬间跑完。"""
    config = load_config()
    config["timing"] = {
        "min_task_delay_seconds": 0,
        "max_task_delay_seconds": 0,
        "browse_duration_seconds": 0,
        "jump_duration_seconds": 0,
        "max_empty_rounds": 1,
        "max_attempts_per_task": 2,
    }
    return config


def text_item(text: str, x: int, y: int, width: int = 200, height: int = 40) -> dict:
    return {"text": text, "box": [x, y, width, height], "score": 0.95}


def _registered_action(name: str):
    """取回 register() 里注册的动作类，避免测试重复声明一遍。"""

    class _Collector:
        def __init__(self) -> None:
            self.actions: dict = {}

        def custom_action(self, action_name: str):
            def wrapper(cls):
                self.actions[action_name] = cls
                return cls

            return wrapper

    collector = _Collector()
    actions.register(collector)
    return collector.actions[name]


PANEL_ROW = [
    text_item("赚金币抵钱", 100, 150, 300, 48),
    text_item("今日累计奖励", 100, 220, 240, 40),
    text_item("浏览商品", 100, 300),
    text_item("去完成", 900, 300, 120, 40),
]

# 面板上一行任务全部领完的样子：任务按钮变成「已领取」，面板特征仍在。
DONE_ONLY = [
    text_item("赚金币抵钱", 100, 150, 300, 48),
    text_item("今日累计奖励", 100, 220, 240, 40),
    text_item("完成进度 20/20", 100, 260, 260, 36),
    text_item("已领取", 100, 300),
]

# 真机（一加 Ace 3 PJE110 / 淘宝 2026-10）实测的整屏 OCR 结果，坐标原样抄自日志。
# 这版面板文案与旧版完全不同，是回归测试的基准：任务名在左、右侧金币按钮
# 「+90」才是可点区域，任务名下方的「浏览15秒」等只是说明文字。
DEVICE_PANEL = [
    text_item("今日速赚570淘金币", 28, 188, 377, 47),
    text_item("为你找到最简单的任务，2分钟即可完成", 32, 240, 400, 24),
    text_item("完成下方任务得额外金币", 45, 313, 252, 23),
    text_item("+180", 594, 325, 60, 31),
    text_item("额外得", 596, 363, 58, 24),
    text_item("任务到访得金币", 130, 447, 202, 33),
    text_item("每日来任务面板", 130, 488, 165, 28),
    text_item("+10", 605, 460, 45, 33),
    text_item("逛清单，每15秒30金币(0/2)", 128, 580, 371, 37),
    text_item("+60", 599, 607, 50, 33),
    text_item("运动装备焕新", 146, 745, 144, 28),
    text_item("精明囤货清单", 332, 745, 143, 28),
    text_item("搜一搜你心仪的宝贝(0/3)", 130, 849, 329, 33),
    text_item("浏览15秒", 128, 891, 98, 29),
    text_item("+90", 602, 862, 53, 32),
    text_item("淘金币趣味课堂", 130, 990, 204, 33),
    text_item("参与答题", 128, 1027, 100, 35),
    text_item("+30", 604, 1004, 51, 32),
    text_item("去蚂蚁庄园逛逛哟", 131, 1133, 230, 28),
    text_item("点击去逛", 128, 1171, 98, 30),
    text_item("+50", 602, 1145, 53, 31),
    text_item("去芭芭农场逛一逛", 131, 1273, 232, 28),
    text_item("浏览5秒", 126, 1310, 92, 34),
    text_item("+30", 601, 1282, 56, 38),
    text_item("拍立淘逛感兴趣的宝贝(0/5)", 130, 1412, 358, 33),
    text_item("浏览5秒", 128, 1452, 88, 29),
    text_item("+20", 604, 1425, 51, 33),
]


class PanelParseTest(unittest.TestCase):
    def test_reading_order_sorts_by_row_then_column(self) -> None:
        items = panel.parse_ocr_items(
            [
                text_item("下", 100, 500),
                text_item("右", 800, 300),
                text_item("左", 100, 305),
            ]
        )
        self.assertEqual([item.text for item in items], ["左", "右", "下"])

    def test_ocr_item_center_and_box(self) -> None:
        item = panel.parse_ocr_items([text_item("淘金币", 100, 200, 200, 40)])[0]
        self.assertEqual(item.center, (200, 220))
        self.assertEqual(item.box, [100, 200, 200, 40])

    def test_whitespace_is_stripped(self) -> None:
        items = panel.parse_ocr_items([text_item("浏 览 商 品", 100, 300)])
        self.assertEqual(items[0].text, "浏览商品")

    def test_invalid_box_is_dropped(self) -> None:
        self.assertEqual(panel.parse_ocr_items([{"text": "坏数据", "box": [1, 2]}]), [])
        self.assertEqual(panel.parse_ocr_items([{"text": "", "box": [1, 2, 3, 4]}]), [])

    def test_action_button_is_on_same_row_right_side(self) -> None:
        items = panel.parse_ocr_items(PANEL_ROW)
        task = panel.find_text_by_keywords(items, ("浏览商品",))
        assert task is not None
        button = panel.find_action_button(items, task)
        self.assertIsNotNone(button)
        assert button is not None
        self.assertEqual(button.text, "去完成")
        self.assertEqual(button.center, (960, 320))

    def test_find_text_by_keywords(self) -> None:
        items = panel.parse_ocr_items(PANEL_ROW)
        self.assertIsNotNone(panel.find_text_by_keywords(items, ("搜索", "去完成")))
        self.assertIsNone(panel.find_text_by_keywords(items, ("不存在的词",)))


class DevicePanelTest(unittest.TestCase):
    """真机面板布局的回归测试：任务名在左，右侧金币按钮才是可点区域。"""

    def setUp(self) -> None:
        self.items = panel.parse_ocr_items(DEVICE_PANEL)
        self.config = load_config()

    def test_panel_is_recognised(self) -> None:
        self.assertTrue(panel.task_panel_visible(self.items))

    def test_panel_text_does_not_contain_retired_markers(self) -> None:
        """旧版三个特征词一个都不在屏上，靠它们判定会永远认不出面板。"""
        texts = "".join(item.text for item in self.items)
        self.assertNotIn("赚金币抵钱", texts)
        self.assertNotIn("今日累计奖励", texts)
        self.assertNotIn("完成进度", texts)

    def test_daily_done_panel_is_not_taken_as_task_panel(self) -> None:
        """「今日速赚」是完成弹窗和面板共用的标题，不能作为面板特征词。"""
        self.assertNotIn("今日速赚", panel.PANEL_MARKERS)

    def test_subtitle_is_not_picked_as_task(self) -> None:
        """「点击去逛」命中 jump_tasks 名单，但它只是任务名下方的说明文字。"""
        pick = panel.pick_task(self.items, self.config)
        self.assertIsNotNone(pick.item)
        assert pick.item is not None
        self.assertNotEqual(pick.item.text, "点击去逛")
        self.assertNotIn("点击去逛", pick.item.text)

    def test_first_task_offscreen_order(self) -> None:
        """按阅读顺序应挑到最上面那个可执行任务。"""
        pick = panel.pick_task(self.items, self.config)
        self.assertIsNotNone(pick.item)
        assert pick.item is not None
        self.assertEqual(pick.item.text, "任务到访得金币")

    def test_button_is_the_coin_badge_on_the_right(self) -> None:
        pick = panel.pick_task(self.items, self.config)
        assert pick.item is not None
        button = panel.find_action_button(self.items, pick.item)
        self.assertIsNotNone(button)
        assert button is not None
        self.assertEqual(button.text, "+10")

    def test_each_task_row_finds_its_own_badge(self) -> None:
        """每一行的金币必须跟该行对齐，不能串到别的行。"""
        expected = {
            "逛清单，每15秒30金币(0/2)": "+60",
            "搜一搜你心仪的宝贝(0/3)": "+90",
            "淘金币趣味课堂": "+30",
            "去蚂蚁庄园逛逛哟": "+50",
            "去芭芭农场逛一逛": "+30",
            "拍立淘逛感兴趣的宝贝(0/5)": "+20",
        }
        for name, badge in expected.items():
            task = panel.find_text_by_keywords(self.items, (name,))
            self.assertIsNotNone(task, name)
            assert task is not None
            button = panel.find_action_button(self.items, task)
            self.assertIsNotNone(button, name)
            assert button is not None
            self.assertEqual(button.text, badge, name)

    def test_summary_badge_is_not_used_as_button(self) -> None:
        """顶部「+180 额外得」是汇总金币，不能当成任何一行的按钮。"""
        task = panel.find_text_by_keywords(self.items, ("任务到访得金币",))
        assert task is not None
        button = panel.find_action_button(self.items, task)
        assert button is not None
        self.assertNotEqual(button.text, "+180")


class PickTaskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()

    def test_blacklist_item_is_skipped_and_browse_is_picked(self) -> None:
        items = panel.parse_ocr_items(
            [
                text_item("绑定银行卡", 100, 300),
                text_item("去完成", 900, 300, 120, 40),
                text_item("浏览商品", 100, 400),
                text_item("去完成", 900, 400, 120, 40),
            ]
        )
        pick = panel.pick_task(items, self.config)
        self.assertIsNotNone(pick.item)
        assert pick.item is not None
        self.assertEqual(pick.item.text, "浏览商品")
        self.assertEqual(pick.decision.kind, "browse")
        self.assertTrue(any("绑定银行卡" in entry for entry in pick.skipped))

    def test_kind_filter_limits_selection(self) -> None:
        items = panel.parse_ocr_items(PANEL_ROW)
        self.assertIsNone(panel.pick_task(items, self.config, kinds=("search",)).item)
        self.assertIsNotNone(panel.pick_task(items, self.config, kinds=("browse",)).item)

    def test_search_decision_carries_keyword(self) -> None:
        items = panel.parse_ocr_items([text_item("搜索商品", 100, 300)])
        pick = panel.pick_task(items, self.config)
        assert pick.item is not None
        self.assertEqual(pick.decision.kind, "search")
        # keyword 是点击目标（面板上的任务名），search_keyword 才是要输入的关键词。
        self.assertEqual(pick.decision.keyword, "搜索商品")
        self.assertIn(pick.decision.search_keyword, self.config["search_keywords"])

    def test_no_executable_task_returns_none(self) -> None:
        items = panel.parse_ocr_items(DONE_ONLY)
        pick = panel.pick_task(items, self.config)
        self.assertIsNone(pick.item)

    def test_safety_keyword_detected(self) -> None:
        items = panel.parse_ocr_items([text_item("请完成安全验证", 100, 300)])
        found = panel.find_safety(items, self.config["safety_keywords"])
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.text, "请完成安全验证")

    def test_panel_visible_requires_marker(self) -> None:
        self.assertTrue(panel.panel_visible(panel.parse_ocr_items(PANEL_ROW)))
        self.assertFalse(panel.panel_visible(panel.parse_ocr_items([text_item("商品详情", 100, 100)])))


class DailyLoopTest(unittest.TestCase):
    def test_browse_task_is_clicked_then_loop_ends(self) -> None:
        # 主账号的面板没有「完成进度」行，任务全部领完后靠滑回顶部复查收尾。
        claimed = [
            text_item("赚金币抵钱", 100, 150, 300, 48),
            text_item("今日累计奖励", 100, 220, 240, 40),
            text_item("已领取", 100, 300),
        ]
        context = FakeContext([PANEL_ROW, claimed, claimed])
        config = fast_config()

        result = actions.run_daily_loop(context, config, max_rounds=5)

        self.assertTrue(result)
        # 点击应落在右侧"去完成"按钮，而不是左侧任务名。
        self.assertEqual(context.controller.clicks[0], (960, 320))
        # 任务做完要按返回键回到面板。
        self.assertIn(4, context.controller.keys)
        # 没有可执行任务时向上滑动复查。
        self.assertTrue(context.controller.swipes)
        # Agent 只用约定的 OCR 节点名。
        self.assertEqual(set(context.reco_entries), {"PanelFullOcr"})

    def test_blacklist_only_panel_does_not_click(self) -> None:
        blacklist_only = [
            text_item("赚金币抵钱", 100, 150, 300, 48),
            text_item("今日累计奖励", 100, 220, 240, 40),
            text_item("绑定银行卡", 100, 300),
            text_item("去完成", 900, 300, 120, 40),
        ]
        context = FakeContext([blacklist_only, blacklist_only, blacklist_only])
        result = actions.run_daily_loop(context, fast_config(), max_rounds=5)

        self.assertTrue(result)
        self.assertEqual(context.controller.clicks, [])

    def test_safety_verification_stops_immediately(self) -> None:
        safety_screen = [text_item("请完成验证", 100, 300)]
        context = FakeContext([safety_screen])

        result = actions.run_daily_loop(context, fast_config(), max_rounds=5)

        self.assertFalse(result)  # False 表示主动安全停止
        self.assertEqual(context.controller.clicks, [])
        self.assertEqual(context.controller.keys, [])

    def test_search_task_types_keyword(self) -> None:
        search_panel = [
            text_item("赚金币抵钱", 100, 150, 300, 48),
            text_item("今日累计奖励", 100, 220, 240, 40),
            text_item("搜索商品", 100, 300),
            text_item("去完成", 900, 300, 120, 40),
        ]
        search_page = [
            text_item("搜索宝贝", 107, 211, 145, 29),
            text_item("搜索", 610, 207, 65, 38),
        ]
        context = FakeContext([search_panel, search_page, search_page, DONE_ONLY])

        actions.run_daily_loop(context, fast_config(), max_rounds=3)

        # 搜索任务必须真的把关键词敲进去。
        self.assertEqual(len(context.controller.inputs), 1)
        self.assertIn(context.controller.inputs[0], load_config()["search_keywords"])

    def test_round_limit_is_respected(self) -> None:
        context = FakeContext([PANEL_ROW])
        result = actions.run_daily_loop(context, fast_config(), max_rounds=2)
        self.assertTrue(result)
        self.assertLessEqual(len(context.controller.clicks), 2)


class SearchTaskTest(unittest.TestCase):
    """搜索类任务：页面渲染慢、没有提交按钮、找不到输入框三种情况都要正确处理。"""

    SEARCH_PANEL = [
        text_item("赚金币抵钱", 100, 150, 300, 48),
        text_item("今日累计奖励", 100, 220, 240, 40),
        text_item("搜一搜你心仪的宝贝(0/3)", 45, 313, 252, 23),
        text_item("浏览15秒", 45, 350, 120, 23),
        text_item("+90", 594, 325, 60, 31),
    ]

    # 真机（一加 Ace 3 / 淘宝 2026-10）搜索页的整屏 OCR，坐标原样抄自日志。
    # 顶部「搜索有福利」「搜索后浏览立得奖励」是推广横幅，真正的输入框是
    # 和「搜索」按钮同一行的「Q搜索宝贝」（Q 是放大镜图标被认成了字母）。
    SEARCH_PAGE = [
        text_item("搜索有福利", 65, 112, 153, 29),
        text_item("搜索后浏览立得奖励", 451, 108, 228, 28),
        text_item("Q搜索宝贝", 107, 211, 145, 29),
        text_item("搜索", 610, 207, 65, 38),
        text_item("历史搜索", 20, 288, 126, 35),
        text_item("搜索发现", 19, 536, 127, 34),
    ]

    # 点完任务按钮后淘宝先出一张空白加载页，搜索框要晚一拍才渲染出来。
    LOADING = [text_item("加载中", 300, 600, 120, 30)]

    def setUp(self) -> None:
        # 等待重试的间隔在真机上是秒级，测试里必须跳过，否则每个用例白等好几秒。
        self._original_sleep = actions._sleep
        actions._sleep = lambda seconds: None

    def tearDown(self) -> None:
        actions._sleep = self._original_sleep

    def test_panel_task_name_is_not_a_search_box(self) -> None:
        """面板上的任务名「搜一搜你心仪的宝贝」含锚文本，但不是输入框。"""
        items = panel.parse_ocr_items(self.SEARCH_PANEL)
        self.assertIsNone(panel.search_box(items))
        self.assertIsNotNone(panel.search_box(panel.parse_ocr_items(self.SEARCH_PAGE)))

    def test_promo_banner_is_not_mistaken_for_the_search_box(self) -> None:
        """「搜索有福利」这条横幅比搜索栏高 100 像素，点它关键词就丢了。

        真机实测只按「搜索」两个字匹配会选中横幅，输入框没拿到焦点，
        敲进去的关键词直接丢掉，进度一直停在 (1/3)。
        """
        items = panel.parse_ocr_items(self.SEARCH_PAGE)
        submit = panel.search_submit_button(items)
        self.assertIsNotNone(submit)

        box = panel.search_box(items, submit)

        self.assertIsNotNone(box)
        self.assertIn("搜索宝贝", box.text)
        # 点击坐标必须落在搜索栏（y 约 225），而不是横幅（y 约 126）。
        self.assertGreater(box.center[1], 180)

    def test_search_box_is_found_after_page_finishes_rendering(self) -> None:
        screens = [
            self.SEARCH_PANEL,
            self.LOADING,
            self.LOADING,
            self.SEARCH_PAGE,
            self.SEARCH_PAGE,
            DONE_ONLY,
        ]
        context = FakeContext(screens)

        actions.run_daily_loop(context, fast_config(), max_rounds=1)

        # 加载页出现时不能放弃，必须等到搜索框渲染出来再输入。
        self.assertEqual(len(context.controller.inputs), 1)
        self.assertIn(context.controller.inputs[0], load_config()["search_keywords"])

    def test_search_is_committed_by_clicking_the_submit_button(self) -> None:
        screens = [self.SEARCH_PANEL, self.SEARCH_PAGE, self.SEARCH_PAGE, DONE_ONLY]
        context = FakeContext(screens)

        actions.run_daily_loop(context, fast_config(), max_rounds=1)

        self.assertEqual(len(context.controller.inputs), 1)
        # 输入框中心 (179,225) 之后应当点提交按钮 (642,226)。
        self.assertIn((642, 226), context.controller.clicks)

    def test_search_without_submit_button_is_committed_by_enter(self) -> None:
        """淘宝的搜索页有时没有按钮，只能靠回车提交。"""
        no_button = [item for item in self.SEARCH_PAGE if item["text"] != "搜索"]
        screens = [self.SEARCH_PANEL] + [no_button] * 8 + [DONE_ONLY]
        context = FakeContext(screens)

        actions.run_daily_loop(context, fast_config(), max_rounds=1)

        self.assertEqual(len(context.controller.inputs), 1)
        # KEYCODE_ENTER = 66；不发回车就停在输入法上，等于没搜。
        self.assertIn(66, context.controller.keys)

    def test_search_box_never_appears_is_not_counted_as_done(self) -> None:
        """找不到输入框时不能算完成任务，否则主循环会反复重跑同一个任务。"""
        screens = [self.SEARCH_PANEL] + [self.LOADING] * 8
        context = FakeContext(screens)

        actions.run_daily_loop(context, fast_config(), max_rounds=1)

        self.assertEqual(context.controller.inputs, [])
        # 没搜过就必须退出来，不能停在加载页上。
        self.assertIn(4, context.controller.keys)

    def test_input_failure_is_not_counted_as_done(self) -> None:
        """关键词没输进去时不能算完成，更不能按回车提交空搜索。

        adb 的 input text 只认 ASCII，中文关键词会让它直接失败。之前不检查
        作业成败，输入失败也照旧按回车，等于搜了个空：任务进度一分不涨，
        而日志写着「输入搜索关键词」，看起来一切正常。

        这里刻意用没有提交按钮的搜索页：那种页面靠回车提交，才能验证
        「输入失败 → 不发回车」。
        """
        no_button = [item for item in self.SEARCH_PAGE if item["text"] != "搜索"]
        screens = [self.SEARCH_PANEL] + [no_button] * 8 + [DONE_ONLY]
        context = FakeContext(screens)
        context.controller.input_ok = False

        actions.run_daily_loop(context, fast_config(), max_rounds=1)

        self.assertEqual(len(context.controller.inputs), 1)
        # 输入失败就不该再提交：回车提交的是空搜索，比不搜更浪费时间。
        self.assertNotIn(66, context.controller.keys)
        # 也要退出去，不能停在搜索页上继续重试同一个任务。
        self.assertIn(4, context.controller.keys)


class DwellScrollTest(unittest.TestCase):
    """清单 / 会场 / 课堂这类页面静止不动就不再计时长，停留期间必须滑动。"""

    def _slow_config(self, kind_seconds: float = 12) -> dict:
        config = fast_config()
        config["timing"]["browse_duration_seconds"] = kind_seconds
        config["timing"]["jump_duration_seconds"] = kind_seconds
        return config

    def test_browse_dwell_scrolls_while_waiting(self) -> None:
        context = FakeContext([PANEL_ROW, DONE_ONLY, DONE_ONLY])
        original = actions._sleep
        actions._sleep = lambda seconds: None
        try:
            actions.run_daily_loop(context, self._slow_config(), max_rounds=1)
        finally:
            actions._sleep = original

        # 停留 12 秒 = 两次 5 秒的间隔，至少要滑一次；只往上滑，不滑回看过的地方。
        swipes = context.controller.swipes
        self.assertTrue(swipes)
        for _, y1, _, y2 in swipes:
            self.assertGreater(y1, y2, f"应当是往上滑，实际 ({y1} -> {y2})")

    def test_short_dwell_does_not_scroll(self) -> None:
        """停留时间不足一个间隔时不滑动，避免为极短任务浪费一次手势。"""
        context = FakeContext([PANEL_ROW, DONE_ONLY, DONE_ONLY])
        original = actions._sleep
        actions._sleep = lambda seconds: None
        try:
            actions.run_daily_loop(context, self._slow_config(3), max_rounds=1)
        finally:
            actions._sleep = original

        self.assertEqual(context.controller.swipes, [])

    def test_search_dwell_scrolls_results_page(self) -> None:
        search_panel = [
            text_item("赚金币抵钱", 100, 150, 300, 48),
            text_item("今日累计奖励", 100, 220, 240, 40),
            text_item("搜索商品", 100, 300),
            text_item("去完成", 900, 300, 120, 40),
        ]
        search_page = [
            text_item("搜索宝贝", 107, 211, 145, 29),
            text_item("搜索", 610, 207, 65, 38),
        ]
        context = FakeContext([search_panel, search_page, search_page, DONE_ONLY])
        original = actions._sleep
        actions._sleep = lambda seconds: None
        try:
            actions.run_daily_loop(context, self._slow_config(), max_rounds=1)
        finally:
            actions._sleep = original

        # 关键词搜索后停在结果页，同样要滑。
        self.assertTrue(context.controller.swipes)

    def test_dwell_without_scroll_still_waits(self) -> None:
        """不需要滑动的场景（如只看弹窗）应保持原样，不额外发手势。"""
        context = FakeContext([PANEL_ROW])
        original = actions._sleep
        actions._sleep = lambda seconds: None
        try:
            actions._dwell(context, 6, scroll=False)
        finally:
            actions._sleep = original

        self.assertEqual(context.controller.swipes, [])

    def test_swipe_uses_screencap_size_not_device_resolution(self) -> None:
        """手势坐标要落在截图范围内，不能按设备分辨率算。

        真机 1080x2376 的截图缩到了 720x1584。如果按设备分辨率算，
        上滑起点 y 会超出截图底边，手势被当成从顶部按下，页面滑不动。
        """
        context = FakeContext([PANEL_ROW, DONE_ONLY, DONE_ONLY], frame_size=(720, 1584))
        context.controller.resolution = [1080, 2376]
        original = actions._sleep
        actions._sleep = lambda seconds: None
        try:
            actions.run_daily_loop(context, self._slow_config(), max_rounds=1)
        finally:
            actions._sleep = original

        swipes = context.controller.swipes
        self.assertTrue(swipes)
        for x1, y1, x2, y2 in swipes:
            self.assertLess(x1, 720)
            self.assertLess(x2, 720)
            self.assertLess(y1, 1584)
            self.assertLess(y2, 1584)
            self.assertGreaterEqual(min(y1, y2), 0)

    def test_swipe_starts_above_bottom_gesture_bar(self) -> None:
        """滑动起点不能落到底部手势栏里。

        屏幕底部约 8% 是系统手势导航条和淘宝底部标签栏，手指按在那里
        会被当成返回/切页手势，整次滑动直接失效。
        """
        context = FakeContext([PANEL_ROW, DONE_ONLY, DONE_ONLY], frame_size=(720, 1584))
        original = actions._sleep
        actions._sleep = lambda seconds: None
        try:
            actions.run_daily_loop(context, self._slow_config(), max_rounds=1)
            actions._swipe_up(context, delay=0)
        finally:
            actions._sleep = original

        swipes = context.controller.swipes
        self.assertTrue(swipes)
        for _, y1, _, y2 in swipes:
            self.assertLessEqual(max(y1, y2), int(1584 * 0.85))


TEAM_PAGE = [
    text_item("双11组队大奖赛", 100, 120, 400, 50),
    text_item("车队已凑齐", 100, 300, 400, 80),
    text_item("拉人助力得能量", 100, 700, 300, 40),
]

COIN_HOME = [
    text_item("淘金币双11狂享20亿", 75, 97, 421, 51),
    text_item("升级赛车拿钱", 242, 605, 238, 51),
    text_item("赚金币", 25, 651, 85, 35),
    text_item("赚能量", 613, 650, 84, 36),
]

# 真机截图（一加 Ace 3 PJE110 / 淘宝 2026-10-21，淘宝首页）：坐标原样抄自 probe 日志。
# 关键点：顶部横幅带「立即抢>」，顶栏还有「双11穿」，都不是遮住入口的促销弹层；
# 「领淘金币」是第一排最左的图标，必须按关键词折算点击位置。
DEVICE_HOME = [
    text_item("关注", 25, 89, 74, 46),
    text_item("推荐", 114, 85, 85, 55),
    text_item("闪购外卖", 218, 94, 121, 36),
    text_item("盒马", 347, 94, 72, 36),
    text_item("国补", 427, 94, 72, 36),
    text_item("飞猪", 509, 94, 71, 36),
    text_item("双11穿", 581, 90, 119, 42),
    text_item("搜索", 593, 168, 73, 43),
    text_item("领淘金币", 24, 320, 102, 29),
    text_item("芭芭农场", 147, 320, 106, 29),
    text_item("红包签到", 271, 320, 109, 29),
    text_item("淘宝闪购", 395, 320, 108, 28),
    text_item("天猫超市", 522, 320, 108, 29),
    text_item("双11开门红|券后低至7.2折", 24, 678, 453, 44),
    text_item("立即抢>", 491, 683, 105, 36),
    text_item("购物金充值膨胀", 482, 763, 193, 33),
    text_item("88VIP无门槛券", 371, 1179, 326, 56),
    text_item("券额至高", 387, 1258, 87, 29),
    text_item("购物车", 467, 1516, 73, 31),
    text_item("我的淘宝", 594, 1516, 98, 35),
]


# 真机失败帧（2026-10-21 15:19:55，淘宝刚被拉起、首页还在渲染）：同一屏被 OCR
# 读了两遍，顶部那排图标（「领淘金币」）当时还没渲染出来，但底部标签栏已经在。
# 旧逻辑在这里落到兜底分支按返回，而首页按返回是退出淘宝，于是连续退出六次。
DEVICE_HOME_RENDERING = [
    text_item("关注", 25, 89, 74, 46),
    text_item("推荐", 115, 87, 84, 49),
    text_item("搜索", 593, 168, 73, 43),
    text_item("双11开门红券后低至7.2折", 24, 405, 453, 45),
    text_item("立即抢>", 491, 683, 105, 36),
    text_item("双11官方立减15%", 250, 900, 300, 40),
    text_item("再按一次返回键退出手机淘宝", 300, 1100, 400, 40),
    text_item("购物车", 467, 1516, 73, 31),
    text_item("我的淘宝", 596, 1518, 95, 29),
]


class PageKindTest(unittest.TestCase):
    def test_page_kinds_are_distinguished(self) -> None:
        team = panel.parse_ocr_items(TEAM_PAGE)
        home = panel.parse_ocr_items(COIN_HOME)
        panel_items = panel.parse_ocr_items(PANEL_ROW)

        self.assertTrue(panel.team_page_visible(team))
        self.assertFalse(panel.coin_home_visible(team))
        self.assertFalse(panel.panel_visible(team))

        self.assertTrue(panel.coin_home_visible(home))
        self.assertFalse(panel.team_page_visible(home))
        self.assertFalse(panel.panel_visible(home))

        self.assertTrue(panel.panel_visible(panel_items))
        self.assertFalse(panel.team_page_visible(panel_items))
        self.assertFalse(panel.coin_home_visible(panel_items))

    def test_team_page_is_not_clicked(self) -> None:
        """组队活动页没有日常任务，即使识别到文字也不应点击。"""
        context = FakeContext([TEAM_PAGE, PANEL_ROW, DONE_ONLY])
        result = actions.run_daily_loop(context, fast_config(), max_rounds=2)

        self.assertTrue(result)
        self.assertEqual(context.controller.clicks, [])


class RecoverTest(unittest.TestCase):
    def test_coin_home_opens_panel_by_clicking_earn_coin(self) -> None:
        """在赛车首页要按「赚金币」进入面板，而不是按返回退出淘金币。"""
        context = FakeContext([COIN_HOME, COIN_HOME, PANEL_ROW, DONE_ONLY])
        result = actions.run_daily_loop(context, fast_config(), max_rounds=2)

        self.assertTrue(result)
        # 必须点首页左下角的「赚金币」(25,651,85,35) 中心，而不是按返回键。
        self.assertTrue(context.controller.clicks)
        self.assertEqual(context.controller.clicks[0], (67, 668))
        self.assertNotIn(4, context.controller.keys)

    def test_team_page_is_left_by_pressing_back(self) -> None:
        """组队页是广告页，应该按返回退出，且不点击任何内容。"""
        context = FakeContext([TEAM_PAGE, TEAM_PAGE, PANEL_ROW, DONE_ONLY])
        result = actions.run_daily_loop(context, fast_config(), max_rounds=2)

        self.assertTrue(result)
        self.assertIn(4, context.controller.keys)
        self.assertEqual(context.controller.clicks, [])

    def test_alipay_dialog_is_closed_and_taobao_reopened(self) -> None:
        """真跑到支付宝去了就把淘宝重新打开。"""
        context = FakeContext([PANEL_ROW, PANEL_ROW, DONE_ONLY])
        context.controller.foreground = "com.eg.android.AlipayGphone"

        self.assertTrue(actions._recover_to_panel(context, fast_config()))

        self.assertTrue(
            any("am start" in shell and "taobao" in shell for shell in context.controller.shells)
        )

    def test_staying_in_taobao_does_not_restart_it(self) -> None:
        """还在淘宝里时不能反复启动应用，那样每轮都要从首页重找入口。"""
        context = FakeContext([PANEL_ROW, DONE_ONLY])

        self.assertTrue(actions._recover_to_panel(context, fast_config()))
        self.assertFalse(any("am start" in shell for shell in context.controller.shells))
        self.assertEqual(context.controller.keys, [])

    def test_home_without_coin_entry_never_presses_back(self) -> None:
        """首页那排图标还没渲染出来时，返回键会直接退出淘宝，只能滑动或重启。"""
        context = FakeContext(
            [DEVICE_HOME_RENDERING, DEVICE_HOME_RENDERING, DEVICE_HOME, PANEL_ROW]
        )
        result = actions.open_coin_page(context, fast_config())

        self.assertTrue(result)
        self.assertNotIn(4, context.controller.keys)
        # 找不到入口时先把信息流拉回顶部，图标行滚回来后再点「淘金币」。
        self.assertTrue(context.controller.swipes)
        self.assertTrue(context.controller.clicks)

    def test_home_without_coin_entry_restarts_taobao_instead_of_backing_out(self) -> None:
        """滑动也找不到入口时，重启淘宝而不是一路按返回把自己退出应用。"""
        context = FakeContext([DEVICE_HOME_RENDERING] * 6 + [PANEL_ROW])
        actions._recover_to_panel(context, fast_config(), tries=4)

        self.assertNotIn(4, context.controller.keys)
        self.assertTrue(any("taobao" in shell for shell in context.controller.shells))


# 真机截图（应用分身 → 支付宝小程序）：横向排版，带「打开」按钮
#   “淘宝(分身)”想要打开“支付宝”小程序 / □始终允许打开 / 取消 | 打开
OPEN_APP_DIALOG = [
    text_item("“淘宝(分身)”想要打开“支付宝”小程序", 62, 828, 596, 96),
    text_item("始终允许打开", 90, 900, 200, 40),
    text_item("取消", 168, 960, 100, 50),
    text_item("打开", 528, 960, 100, 50),
]

# 真机截图（一加 Ace 3，淘宝分身 → 闲鱼）：纵向排版，没有「打开」按钮
#   “淘宝(分身)”想要打开“闲鱼” / 30天内允许 / 仅本次允许 / 取消
OPEN_APP_DIALOG_VERTICAL = [
    text_item("“淘宝(分身)”想要打开“闲鱼”", 136, 1077, 447, 38),
    text_item("30天内允许", 268, 1196, 180, 43),
    text_item("仅本次允许", 276, 1303, 167, 40),
    text_item("取消", 321, 1405, 74, 43),
]

# 弹框盖在面板上方时，面板文字依然能被 OCR 认出来。
PANEL_WITH_DIALOG = PANEL_ROW + OPEN_APP_DIALOG
PANEL_WITH_VERTICAL_DIALOG = PANEL_ROW + OPEN_APP_DIALOG_VERTICAL


# 真机截图（淘宝站外跳转提示，跳头条）：
#   提示 / 下个页面不受我们控制哦，使用时请注意安全 https://z.toutiao.com
#   取消 | 浏览器打开
EXTERNAL_JUMP_TIP = [
    text_item("提示", 90, 689, 93, 51),
    text_item("下个页面不受我们控制哦，使用时请", 93, 759, 517, 39),
    text_item("注意安全https://z.toutiao.com", 94, 800, 428, 36),
    text_item("取消", 360, 897, 65, 38),
    text_item("浏览器打开", 475, 895, 148, 38),
]

PANEL_WITH_EXTERNAL_TIP = PANEL_ROW + EXTERNAL_JUMP_TIP


class OpenAppDialogTest(unittest.TestCase):
    def test_dialog_is_recognized(self) -> None:
        items = panel.parse_ocr_items(OPEN_APP_DIALOG)
        self.assertTrue(panel.open_app_dialog_visible(items))
        # 面板文字也还在，所以不能只靠「在面板上」判断是否卡住。
        self.assertTrue(panel.task_panel_visible(panel.parse_ocr_items(PANEL_WITH_DIALOG)))

    def test_confirm_button_is_not_the_title(self) -> None:
        """标题里也含「打开」，按钮必须取精确匹配的「打开」。"""
        items = panel.parse_ocr_items(OPEN_APP_DIALOG)
        button = panel.open_app_dialog_button(items, panel.OPEN_APP_DIALOG_CONFIRM)
        self.assertIsNotNone(button)
        self.assertEqual(button.text, "打开")

    def test_dialog_is_confirmed_by_open(self) -> None:
        """跳转任务要真的进去做，弹框上要选「打开」而不是「取消」。"""
        context = FakeContext([PANEL_WITH_DIALOG, PANEL_ROW, DONE_ONLY])
        self.assertTrue(actions._recover_to_panel(context, fast_config()))

        self.assertIn((578, 985), context.controller.clicks)
        self.assertNotIn((218, 985), context.controller.clicks)

    def test_loop_keeps_going_after_dialog(self) -> None:
        """弹框确认后主循环要继续做别的任务，不能停在原地。"""
        context = FakeContext(
            [PANEL_ROW, PANEL_WITH_DIALOG, PANEL_ROW, PANEL_ROW, DONE_ONLY]
        )
        result = actions.run_daily_loop(context, fast_config(), max_rounds=3)

        self.assertTrue(result)
        self.assertIn((578, 985), context.controller.clicks)

    def test_jump_task_enters_the_app_before_returning(self) -> None:
        """跳转类任务：确认弹框 → 停留 → 返回，而不是直接放弃。"""
        jump_row = [
            text_item("赚金币抵钱", 100, 150, 300, 48),
            text_item("今日累计奖励", 100, 220, 240, 40),
            text_item("去蚂蚁庄园逛逛哟(0/1)", 100, 300),
            text_item("去逛逛", 900, 300, 120, 40),
        ]
        context = FakeContext([jump_row, PANEL_WITH_DIALOG, jump_row, DONE_ONLY])
        actions.run_daily_loop(context, fast_config(), max_rounds=2)

        # 打开了外部小程序，做完还要退回淘宝。
        self.assertIn((578, 985), context.controller.clicks)
        self.assertIn(4, context.controller.keys)
    def test_vertical_dialog_without_open_button_is_handled(self) -> None:
        """纵向弹框没有「打开」，只能点「仅本次允许」——否则永远卡在这个框上。"""
        items = panel.parse_ocr_items(OPEN_APP_DIALOG_VERTICAL)
        self.assertTrue(panel.open_app_dialog_visible(items))

        button = panel.open_app_dialog_button(items, panel.OPEN_APP_DIALOG_CONFIRM)
        self.assertIsNotNone(button)
        self.assertEqual(button.text, "仅本次允许")

    def test_vertical_dialog_is_clicked_through_in_loop(self) -> None:
        """纵向弹框也要能点过去，而不是反复点任务按钮。"""
        jump_row = [
            text_item("赚金币抵钱", 100, 150, 300, 48),
            text_item("今日累计奖励", 100, 220, 240, 40),
            text_item("点击去逛+20", 100, 300),
            text_item("去逛逛", 900, 300, 120, 40),
        ]
        context = FakeContext(
            [jump_row, PANEL_WITH_VERTICAL_DIALOG, jump_row, DONE_ONLY]
        )
        actions.run_daily_loop(context, fast_config(), max_rounds=2)

        # 「仅本次允许」块 [276,1303,167,40] 的中心
        self.assertIn((359, 1323), context.controller.clicks)
        # 不能去点「取消」[321,1405,74,43]
        self.assertNotIn((358, 1426), context.controller.clicks)

    def test_shortest_permission_is_preferred(self) -> None:
        """同时存在多个选项时选限时最短的，别给分身长期授权。"""
        items = panel.parse_ocr_items(
            OPEN_APP_DIALOG_VERTICAL
            + [text_item("始终允许打开", 268, 1250, 180, 43)]
        )
        button = panel.open_app_dialog_button(items, panel.OPEN_APP_DIALOG_CONFIRM)
        self.assertIsNotNone(button)
        self.assertEqual(button.text, "仅本次允许")

    def test_external_jump_tip_is_recognized(self) -> None:
        """站外跳转提示也要当成拦路弹框：按返回键关不掉它。"""
        items = panel.parse_ocr_items(EXTERNAL_JUMP_TIP)
        self.assertTrue(panel.open_app_dialog_visible(items))
        self.assertFalse(panel.task_panel_visible(items))

    def test_external_jump_tip_is_clicked_through(self) -> None:
        """站外提示要点「浏览器打开」继续，而不是卡住反复按返回。"""
        jump_row = [
            text_item("赚金币抵钱", 100, 150, 300, 48),
            text_item("今日累计奖励", 100, 220, 240, 40),
            text_item("点击去逛+40", 100, 300),
            text_item("去逛逛", 900, 300, 120, 40),
        ]
        context = FakeContext(
            [jump_row, PANEL_WITH_EXTERNAL_TIP, jump_row, DONE_ONLY]
        )
        actions.run_daily_loop(context, fast_config(), max_rounds=2)

        # 「浏览器打开」块 [475,895,148,38] 的中心
        self.assertIn((549, 914), context.controller.clicks)
        self.assertNotIn((392, 916), context.controller.clicks)

DAILY_DONE_PAGE = [
    text_item("今日速赚570淘金币", 32, 193, 371, 39),
    text_item("今日快速赚奖励已拿完", 150, 600, 420, 45),
    text_item("记得明天再来", 280, 660, 160, 30),
]


class DailyDoneTest(unittest.TestCase):
    def test_daily_done_marker_is_detected(self) -> None:
        items = panel.parse_ocr_items(DAILY_DONE_PAGE)
        self.assertTrue(panel.daily_done_visible(items))
        # 完成提示页不是任务面板，也不应被当作组队页。
        self.assertFalse(panel.panel_visible(items))
        self.assertFalse(panel.team_page_visible(items))

    def test_daily_done_ends_normally_without_clicking(self) -> None:
        """任务已全部完成是正常结束，不能判定为失败（False 表示安全停止）。"""
        context = FakeContext([DAILY_DONE_PAGE])
        result = actions.run_daily_loop(context, fast_config(), max_rounds=3)

        self.assertTrue(result)
        self.assertEqual(context.controller.clicks, [])
        self.assertEqual(context.controller.keys, [])


class HomeMisjudgeTest(unittest.TestCase):
    """淘宝首页上有「立即领取」和「搜索」，不能因此被当成任务面板。"""

    HOME = [
        text_item("关注", 25, 90, 74, 45),
        text_item("推荐", 114, 85, 85, 55),
        text_item("搜索", 593, 168, 73, 44),
        text_item("立即领取", 110, 448, 53, 32),
        text_item("领淘金币充值中心试用领取常买超市", 245, 443, 447, 28),
    ]

    REAL_PANEL = [
        text_item("赚金币抵钱", 100, 120, 300, 48),
        text_item("今日累计奖励", 100, 200, 240, 40),
        text_item("完成进度 3/20", 100, 250, 260, 36),
        text_item("浏览商品", 100, 300),
        text_item("去完成", 900, 300, 120, 40),
    ]

    def test_home_is_not_a_task_panel(self) -> None:
        items = panel.parse_ocr_items(self.HOME)
        # 首页上的「立即领取」不再算面板特征，严格判定必须为 False。
        self.assertFalse(panel.panel_visible(items))
        self.assertFalse(panel.task_panel_visible(items))

    def test_real_panel_is_recognized(self) -> None:
        items = panel.parse_ocr_items(self.REAL_PANEL)
        self.assertTrue(panel.task_panel_visible(items))

    def test_single_marker_is_not_enough(self) -> None:
        only_one = panel.parse_ocr_items([text_item("赚金币抵钱", 100, 120, 300, 48)])
        self.assertFalse(panel.task_panel_visible(only_one))

    def test_home_banner_with_grab_button_is_not_a_promo_layer(self) -> None:
        """真机首页顶部的「双11开门红」横幅带「立即抢」，不是遮住入口的弹层。"""
        banner = panel.parse_ocr_items(
            [
                text_item("双11开门红|券后低至7.2折", 24, 678, 453, 44),
                text_item("立即抢>", 491, 683, 105, 36),
            ]
        )
        self.assertFalse(panel.promo_layer_visible(banner))
        # 真正的弹层同时命中多个专属文案，仍然要判出来。
        layer = panel.parse_ocr_items(
            [
                text_item("双11提前领好券", 85, 100, 225, 40),
                text_item("苹果惊喜券至高领450元", 33, 259, 317, 39),
            ]
        )
        self.assertTrue(panel.promo_layer_visible(layer))

    def test_home_search_box_is_not_a_task(self) -> None:
        """首页上的「搜索」框不能被当成搜索任务去点击。"""
        config = load_config()
        items = panel.parse_ocr_items(self.HOME)
        pick = panel.pick_task(items, config)
        self.assertIsNone(pick.item)


class CoinEntryTest(unittest.TestCase):
    """首页那一排图标会被 OCR 合并成一个文字块，点击点必须按关键词折算。"""

    MERGED = [text_item("领淘金币充值中心试用领取常买超市", 245, 443, 447, 28)]
    PROMO = [
        text_item("双11提前领好券", 85, 100, 225, 40),
        text_item("苹果惊喜券至高领450元", 33, 259, 317, 39),
    ]
    # 真机 2026-10-21 15:38 的答题页：标题块含「淘金币」，被当成首页入口反复点击。
    QUIZ = [
        text_item("1. 猜一猜：曲艺表演“双簧”最早得名于单选题", 24, 60, 600, 50),
        text_item("←淘金币答题57464", 19, 97, 387, 41),
        text_item("A表演者姓黄", 30, 300, 200, 40),
        text_item("B两种乐器的名字", 260, 300, 260, 40),
        text_item("每日答题", 30, 400, 150, 40),
        text_item("赢金币", 500, 500, 120, 40),
        text_item("题目来源：支付宝·答答星球", 100, 700, 400, 40),
        text_item("我选好了", 400, 1300, 200, 60),
    ]

    def test_keyword_center_lands_on_the_keyword_not_the_block_center(self) -> None:
        item = panel.parse_ocr_items(self.MERGED)[0]
        point = panel.keyword_center(item, "淘金币")
        self.assertIsNotNone(point)
        # 整块中心是 (468, 457)，落在「充值中心」上，点它会进错页面。
        self.assertEqual(item.center, (468, 457))
        x, y = point
        self.assertLess(x, 400)
        self.assertEqual(y, 457)

    def test_keyword_center_returns_none_when_absent(self) -> None:
        item = panel.parse_ocr_items(self.MERGED)[0]
        self.assertIsNone(panel.keyword_center(item, "赚金币"))

    def test_home_entry_is_found_on_taobao_home(self) -> None:
        items = panel.parse_ocr_items(self.MERGED + [text_item("搜索", 593, 168, 73, 44)])
        self.assertTrue(panel.taobao_home_visible(items))
        self.assertIsNotNone(panel.coin_entry_point(items))
        # 促销浮层不是首页，不能被当成首页去点入口。
        self.assertFalse(panel.taobao_home_visible(panel.parse_ocr_items(self.PROMO)))
        self.assertTrue(panel.promo_layer_visible(panel.parse_ocr_items(self.PROMO)))

    def test_recover_clicks_keyword_position_on_home(self) -> None:
        """停在淘宝首页时，应按关键词位置点「淘金币」进入面板。"""
        home = self.MERGED + [text_item("搜索", 593, 168, 73, 44)]
        context = FakeContext([home, PANEL_ROW, DONE_ONLY])
        result = actions.open_coin_page(context, fast_config())

        self.assertTrue(result)
        self.assertTrue(context.controller.clicks)
        x, y = context.controller.clicks[0]
        self.assertLess(x, 400)
        self.assertEqual(y, 457)

    def test_recover_presses_back_on_promo_layer(self) -> None:
        """促销浮层挡住首页时必须按返回关掉，而不是点击浮层内容。"""
        context = FakeContext([self.PROMO, PANEL_ROW, DONE_ONLY])
        result = actions.open_coin_page(context, fast_config())

        self.assertTrue(result)
        self.assertIn(4, context.controller.keys)
        self.assertEqual(context.controller.clicks, [])

    def test_recover_closes_promo_before_clicking_home_entry(self) -> None:
        """弹层仍识别出首页文字时，也必须先关弹层再点入口。"""
        promo_on_home = self.PROMO + self.MERGED + [text_item("搜索", 593, 168, 73, 44)]
        context = FakeContext([promo_on_home, self.MERGED + [text_item("搜索", 593, 168, 73, 44)], PANEL_ROW, DONE_ONLY])
        result = actions.open_coin_page(context, fast_config())

        self.assertTrue(result)
        self.assertIn(4, context.controller.keys)
        self.assertEqual(context.controller.clicks[0], (314, 457))

    def test_recover_does_not_press_back_on_real_home(self) -> None:
        """真机淘宝首页带有「立即抢」横幅，仍必须直接点「淘金币」而不是按返回退出。"""
        home = DEVICE_HOME
        context = FakeContext([home, PANEL_ROW, DONE_ONLY])
        result = actions.open_coin_page(context, fast_config())

        self.assertTrue(result)
        self.assertNotIn(4, context.controller.keys)

    def test_quiz_page_title_is_not_taken_as_home_entry(self) -> None:
        """答题页标题「←淘金币答题57464」含关键词，但不是首页入口。"""
        self.assertIsNone(panel.coin_entry_point(panel.parse_ocr_items(self.QUIZ)))
        self.assertTrue(panel.quiz_page_visible(panel.parse_ocr_items(self.QUIZ)))

    def test_recover_leaves_quiz_page_by_back(self) -> None:
        """误入答题页时按返回退出，不能一直点标题原地打转。"""
        context = FakeContext([self.QUIZ, self.QUIZ, PANEL_ROW, DONE_ONLY])
        result = actions.open_coin_page(context, fast_config())

        self.assertTrue(result)
        self.assertIn(4, context.controller.keys)
        for x, y in context.controller.clicks:
            self.assertFalse(x < 400 and y < 150, f"点了答题页标题 {(x, y)}")


class AppUserTest(unittest.TestCase):
    """应用分身跑在独立 Android 用户下（本机 999），必须按配置指定用户。"""

    def tearDown(self) -> None:
        os.environ.pop(APP_USER_ENV, None)

    def test_start_command_targets_configured_user(self) -> None:
        command = start_app_command("com.taobao.taobao", "com.taobao.tao.welcome.Welcome", 999)
        self.assertEqual(
            command,
            "am start --user 999 -n com.taobao.taobao/com.taobao.tao.welcome.Welcome",
        )

    def test_user_id_reads_config(self) -> None:
        config = load_config()
        config["app"] = {**config["app"], "user_id": 999}
        self.assertEqual(app_user_id(config), 999)

    def test_environment_overrides_config(self) -> None:
        """临时切用户不该改配置文件，环境变量优先级更高。"""
        config = load_config()
        config["app"] = {**config["app"], "user_id": 0}
        os.environ[APP_USER_ENV] = "999"
        self.assertEqual(app_user_id(config), 999)

    def test_invalid_user_id_is_rejected(self) -> None:
        config = load_config()
        config["app"] = {**config["app"], "user_id": "999"}
        with self.assertRaises(ValueError):
            app_user_id(config)

    def test_start_app_runs_shell_with_user_flag(self) -> None:
        context = FakeContext([PANEL_ROW])
        config = load_config()
        config["app"] = {**config["app"], "user_id": 999}

        self.assertTrue(actions.start_app(context, config))
        self.assertEqual(len(context.controller.shells), 1)
        self.assertIn("--user 999", context.controller.shells[0])

    def test_interface_option_user_id_wins_over_config(self) -> None:
        """界面上的「运行账号」比 config/tasks.json 优先，否则选分身不生效。"""
        context = FakeContext([PANEL_ROW])
        config = load_config()
        config["app"] = {**config["app"], "user_id": 0}

        self.assertTrue(actions.start_app(context, config, user_id=999))
        self.assertIn("--user 999", context.controller.shells[0])

    def test_no_shell_falls_back_to_framework_start_app(self) -> None:
        """手机端控制器没有 shell：主账号要能用框架 StartApp 拉起来。"""
        context = FakeContext([PANEL_ROW])
        context.controller.shell_supported = False
        config = load_config()
        config["app"] = {**config["app"], "user_id": 0}

        self.assertTrue(actions.start_app(context, config))
        self.assertEqual(context.controller.started, ["com.taobao.taobao"])

    def test_no_shell_cannot_start_clone_user(self) -> None:
        """没有 shell 时分身账号（user 999）必须明确失败，不能默默跑去主账号。"""
        context = FakeContext([PANEL_ROW])
        context.controller.shell_supported = False
        config = load_config()
        config["app"] = {**config["app"], "user_id": 999}

        self.assertFalse(actions.start_app(context, config))
        self.assertEqual(context.controller.started, [])

    def test_no_shell_skips_foreground_and_package_probes(self) -> None:
        """没有 shell 时前台包名与安装列表都取不到，主循环不能因此卡死。"""
        context = FakeContext([PANEL_ROW, DONE_ONLY])
        context.controller.shell_supported = False

        self.assertEqual(actions._foreground_package(context), "")
        self.assertEqual(actions._installed_packages(context), set())
        self.assertTrue(actions.run_daily_loop(context, fast_config(), max_rounds=2))

    def test_custom_action_reads_user_id_from_param(self) -> None:
        """配置项经 pipeline_override 写进 custom_action_param，动作要收得到。"""
        context = FakeContext([PANEL_ROW])
        argv = SimpleNamespace(custom_action_param={"user_id": 999})

        start_cls = _registered_action("taobao_start_app")
        self.assertTrue(start_cls().run(context, argv))
        self.assertIn("--user 999", context.controller.shells[0])


class CustomActionParamTest(unittest.TestCase):
    """自定义动作参数会被框架再序列化一次，解析必须容忍多层 JSON。"""

    def test_dict_is_returned_as_is(self) -> None:
        argv = SimpleNamespace(custom_action_param={"max_rounds": 3})
        self.assertEqual(actions._parse_params(argv), {"max_rounds": 3})

    def test_single_and_double_encoded_json_both_work(self) -> None:
        single = SimpleNamespace(custom_action_param='{"max_rounds": 3}')
        double = SimpleNamespace(custom_action_param='"{\\"max_rounds\\": 3}"')
        self.assertEqual(actions._parse_params(single), {"max_rounds": 3})
        self.assertEqual(actions._parse_params(double), {"max_rounds": 3})

    def test_empty_and_garbage_fall_back_to_empty_dict(self) -> None:
        for raw in ("", "   ", None, "不是 JSON", "[1, 2]"):
            with self.subTest(raw=raw):
                argv = SimpleNamespace(custom_action_param=raw)
                self.assertEqual(actions._parse_params(argv), {})

    def test_missing_attribute_is_tolerated(self) -> None:
        self.assertEqual(actions._parse_params(SimpleNamespace()), {})

    def test_double_encoded_param_actually_limits_rounds(self) -> None:
        """真机复现过的崩溃点：参数被双层编码后 max_rounds 取不到。"""
        context = FakeContext([PANEL_ROW])
        argv = SimpleNamespace(custom_action_param='"{\\"max_rounds\\": 1}"')

        loop_cls = _registered_action("taobao_daily_loop")
        self.assertTrue(loop_cls().run(context, argv))
        self.assertEqual(len(context.controller.clicks), 1)


class ProgressTest(unittest.TestCase):
    """任务进度写在任务名里，读它才能知道一次点击有没有真的生效。"""

    def test_progress_is_parsed_from_task_text(self) -> None:
        self.assertEqual(panel.task_progress("拍立淘逛感兴趣的宝贝(3/5)"), (3, 5))
        self.assertEqual(panel.task_progress("好物沉浸看（0/5）"), (0, 5))
        self.assertEqual(panel.task_progress("浏览商品 ( 12 / 20 )"), (12, 20))

    def test_text_without_progress_returns_none(self) -> None:
        for text in ("浏览商品", "去完成", "(3/5", "3/5)"):
            with self.subTest(text=text):
                self.assertIsNone(panel.task_progress(text))

    def test_ocr_confused_letter_is_read_as_digit(self) -> None:
        """OCR 把 (0/1) 认成 (O/1) 时，同一个任务不能被当成两个。"""
        self.assertEqual(panel.task_progress("去蚂蚁庄园逛逛哟(O/1)"), (0, 1))
        self.assertEqual(panel.task_progress("去蚂蚁庄园逛逛哟(l/1)"), (1, 1))
        self.assertEqual(
            panel.task_key("去蚂蚁庄园逛逛哟(O/1)"),
            panel.task_key("去蚂蚁庄园逛逛哟(0/1)"),
        )

    def test_confused_letter_does_not_double_run_the_same_task(self) -> None:
        """同一任务名在两种 OCR 结果下交替出现，只应计一次。"""

        def stuck(spelling: str) -> list[dict]:
            return [
                text_item("赚金币抵钱", 100, 150, 300, 48),
                text_item("今日累计奖励", 100, 220, 240, 40),
                text_item(f"浏览商品({spelling}/5)", 100, 300),
                text_item("去完成", 900, 300, 120, 40),
            ]

        context = FakeContext([stuck("0"), stuck("O"), stuck("0"), stuck("O")])
        result = actions.run_daily_loop(context, fast_config(), max_rounds=10)

        self.assertTrue(result)
        self.assertEqual(len(context.controller.clicks), 2)

    def test_stalled_task_is_not_retried_forever(self) -> None:
        """进度一直 0/5 时必须放弃该任务，否则会无限重试同一个。"""
        stuck = [
            text_item("赚金币抵钱", 100, 150, 300, 48),
            text_item("今日累计奖励", 100, 220, 240, 40),
            text_item("浏览商品(0/5)", 100, 300),
            text_item("去完成", 900, 300, 120, 40),
        ]
        context = FakeContext([stuck])
        result = actions.run_daily_loop(context, fast_config(), max_rounds=10)

        self.assertTrue(result)
        # max_attempts_per_task=2：尝试两次仍无进展就放弃，不会耗掉全部轮次。
        self.assertEqual(len(context.controller.clicks), 2)

    def test_task_without_progress_field_is_also_abandoned(self) -> None:
        """「点击去逛+30」没有 (x/y) 进度，靠执行次数兜底。"""
        stuck = [
            text_item("赚金币抵钱", 100, 150, 300, 48),
            text_item("今日累计奖励", 100, 220, 240, 40),
            text_item("点击去逛+30", 100, 300),
        ]
        context = FakeContext([stuck])
        result = actions.run_daily_loop(context, fast_config(), max_rounds=10)

        self.assertTrue(result)
        self.assertEqual(len(context.controller.clicks), 2)

    def test_task_that_progresses_is_kept(self) -> None:
        def panel_with(progress: int) -> list[dict]:
            return [
                text_item("赚金币抵钱", 100, 150, 300, 48),
                text_item("今日累计奖励", 100, 220, 240, 40),
                text_item(f"浏览商品({progress}/3)", 100, 300),
                text_item("去完成", 900, 300, 120, 40),
            ]

        # 进度在涨就一直做下去，不应被 max_attempts_per_task 提前放弃。
        screens = [
            panel_with(0),
            panel_with(1),
            panel_with(1),
            panel_with(1),
            panel_with(2),
            panel_with(2),
            panel_with(2),
            panel_with(3),
            panel_with(3),
            panel_with(3),
        ]
        context = FakeContext(screens)
        result = actions.run_daily_loop(context, fast_config(), max_rounds=10)

        self.assertTrue(result)
        self.assertGreaterEqual(len(context.controller.clicks), 3)


class DwellRequirementTest(unittest.TestCase):
    """停留时长必须够任务自己要求的秒数，否则进度不增长、任务白做一次。"""

    def setUp(self) -> None:
        self.items = panel.parse_ocr_items(DEVICE_PANEL)
        self.config = load_config()

    def test_seconds_written_in_task_name_are_read(self) -> None:
        """「逛清单，每15秒30金币」的 15 秒就要求在这个页面上待够。"""
        task = panel.find_text_by_keywords(self.items, ("逛清单",))
        assert task is not None
        self.assertEqual(panel.required_seconds(self.items, task), 15)

    def test_seconds_written_in_the_subtitle_below_are_read(self) -> None:
        """「浏览15秒」是任务名下方那行说明文字，秒数写在它里面。"""
        task = panel.find_text_by_keywords(self.items, ("搜一搜你心仪的宝贝",))
        assert task is not None
        self.assertEqual(panel.required_seconds(self.items, task), 15)

    def test_subtitle_of_another_row_is_not_used(self) -> None:
        """每行上下都有「浏览5秒」，取错行会让停留时间凭空变成别的数字。"""
        task = panel.find_text_by_keywords(self.items, ("去蚂蚁庄园逛逛哟",))
        assert task is not None
        # 它自己那行写的是「点击去逛」，没有秒数，不能借用邻居的 15 秒或 5 秒。
        self.assertEqual(panel.required_seconds(self.items, task), 0)

    def test_dwell_never_goes_below_the_required_seconds(self) -> None:
        total = panel.dwell_seconds("browse", self.config, required=15)
        self.assertGreater(total, 15)
        # 没要求秒数时退回配置里的兜底值。
        self.assertEqual(
            panel.dwell_seconds("browse", self.config), 
            float(self.config["timing"]["browse_duration_seconds"]),
        )

    def test_dwell_is_zero_for_kinds_without_page(self) -> None:
        """搜索没有「目标停留页面」的固定时长语义，由 _run_search 单独处理。"""
        self.assertEqual(panel.dwell_seconds("search", self.config, required=15), 0.0)

    def test_configured_dwell_is_long_enough_for_a_fifteen_second_task(self) -> None:
        """配置里的兜底时长本身就要能覆盖最常见的 15 秒任务。"""
        self.assertGreaterEqual(
            self.config["timing"]["browse_duration_seconds"], 15
        )
        self.assertGreaterEqual(self.config["timing"]["jump_duration_seconds"], 15)


class DedupeKeyTest(unittest.TestCase):
    """去重键不能带进度，否则同一任务每推进一格就换一次身份。"""

    def test_progress_suffix_is_removed(self) -> None:
        self.assertEqual(
            panel.dedupe_key("逛清单，每15秒30金币(0/2)"),
            panel.dedupe_key("逛清单，每15秒30金币(1/2)"),
        )

    def test_progress_does_not_leak_into_the_key(self) -> None:
        self.assertNotIn("0/2", panel.dedupe_key("逛清单，每15秒30金币(0/2)"))

    def test_task_without_progress_is_unchanged(self) -> None:
        self.assertEqual(panel.dedupe_key("淘金币趣味课堂"), "淘金币趣味课堂")


class OffsiteAppTest(unittest.TestCase):
    """跳外部应用的任务只在整台手机都没装该应用时才放弃。

    关键在于判断范围是**手机**而不是当前账号：应用分身只是把淘宝克隆了一份，
    本机应用共用，分身里点「去蚂蚁庄园」照样能拉起支付宝。
    """

    PHONE = [
        "com.taobao.taobao",
        "com.eg.android.AlipayGphone",
        "com.taobao.idlefish",
        "com.baidu.searchbox",
        "com.heytap.browser",
    ]

    def test_required_package_is_read_from_task_name(self) -> None:
        self.assertEqual(panel.required_package("去蚂蚁庄园逛逛哟(0/1)"), "com.eg.android.AlipayGphone")
        self.assertEqual(panel.required_package("去逛逛今日头条"), "com.ss.android.article.news")
        self.assertIsNone(panel.required_package("浏览商品"))

    def test_task_is_unreachable_only_when_phone_lacks_the_app(self) -> None:
        self.assertFalse(panel.task_unreachable("去蚂蚁庄园逛逛哟(0/1)", self.PHONE))
        self.assertFalse(panel.task_unreachable("去闲鱼逛逛", self.PHONE))
        # 这台手机没装今日头条，这类任务点下去必然失败。
        self.assertTrue(panel.task_unreachable("去逛逛今日头条", self.PHONE))

    def test_alipay_task_runs_even_though_data_space_lacks_the_package(self) -> None:
        """复现用户反馈：分身里支付宝任务必须照做，不能被包名判断当掉。"""
        screen = [
            text_item("赚金币抵钱", 100, 150, 300, 48),
            text_item("今日累计奖励", 100, 220, 240, 40),
            text_item("去蚂蚁庄园逛逛哟(0/1)", 100, 300),
            text_item("去完成", 900, 300, 120, 40),
        ]
        # packages 传的是整机清单（含支付宝），而不是分身账号的 15 个包。
        context = FakeContext([screen], packages=self.PHONE)
        actions.run_daily_loop(context, fast_config(), max_rounds=2)

        # 任务真的被点下去做了（点「去完成」，做完按返回）。
        self.assertTrue(context.controller.clicks)
        self.assertEqual(set(context.controller.clicks), {(960, 320)})
        self.assertIn(4, context.controller.keys)

    def test_task_needing_missing_app_is_skipped_without_clicking(self) -> None:
        # 「去逛逛」类任务要真的跳出淘宝；本机没装今日头条，点下去必失败。
        screen = [
            text_item("赚金币抵钱", 100, 150, 300, 48),
            text_item("今日累计奖励", 100, 220, 240, 40),
            text_item("去逛逛今日头条", 100, 300),
            text_item("去完成", 900, 300, 120, 40),
        ]
        context = FakeContext([screen], packages=self.PHONE)
        with self.assertLogs("autotaskflow", level="WARNING") as logs:
            actions.run_daily_loop(context, fast_config(), max_rounds=3)

        self.assertEqual(context.controller.clicks, [])
        self.assertTrue(any("没装这个应用" in line for line in logs.output))

    def test_unknown_package_list_does_not_block_anything(self) -> None:
        """取不到包名列表时应退回「全部可尝试」，不能把任务全当掉。"""
        self.assertFalse(panel.task_unreachable("去逛逛今日头条", set()))


class CompletionTest(unittest.TestCase):
    """今日是否做完，要按两种面板形态分别判断。"""

    def test_progress_row_reaching_total_means_done(self) -> None:
        items = panel.parse_ocr_items(DONE_ONLY)
        self.assertEqual(panel.overall_progress(items), (20, 20))
        self.assertTrue(panel.overall_done(items))

    def test_progress_row_not_full_is_not_done(self) -> None:
        items = panel.parse_ocr_items(
            [
                text_item("赚金币抵钱", 100, 150, 300, 48),
                text_item("完成进度 3/20", 100, 260, 260, 36),
                text_item("浏览商品(0/5)", 100, 300),
                text_item("去完成", 900, 300, 120, 40),
            ]
        )
        self.assertEqual(panel.overall_progress(items), (3, 20))
        self.assertFalse(panel.overall_done(items))

    def test_main_account_has_no_progress_row_but_can_still_be_done(self) -> None:
        """主账号面板没有「完成进度」，这时不能根据「没找到进度行」就判完成。

        面板可以下滑，停在中间时下面的任务根本不在截图里，所以无进度行时
        必须交给主循环滑回顶部复查，靠连续空轮判定结束。
        """
        items = panel.parse_ocr_items(
            [
                text_item("赚金币抵钱", 100, 150, 300, 48),
                text_item("今日累计奖励", 100, 220, 240, 40),
                text_item("已领取", 100, 300),
                text_item("领取总金币", 100, 360),
            ]
        )
        self.assertIsNone(panel.overall_progress(items))
        self.assertFalse(panel.overall_done(items))


class QuizTaskTest(unittest.TestCase):
    """「淘金币趣味课堂」点进去是二选一的单选题，不是浏览页。

    真机实测：面板上这一行的说明文字是「参与答题」，点进去是一道题 + A/B 两个
    选项 + 底部「我选好了」。按浏览停留 22 秒再返回，进度一动不动，任务永远
    停在面板上；必须选一个选项、点提交，金币才会加。
    """

    # 真机（一加 Ace 3 / 淘宝 2026-10-21）答题页整屏 OCR，坐标原样抄自日志。
    QUIZ_PAGE = [
        text_item("←淘金币答题57494", 22, 179, 383, 33),
        text_item("每日答题", 27, 252, 183, 86),
        text_item("赢金币", 234, 260, 241, 71),
        text_item("1.猜一猜：曲艺表演“双簧”最早得名于单选题", 17, 483, 595, 35),
        text_item("A表演者姓黄", 45, 594, 184, 33),
        text_item("B两种乐器的名字", 48, 721, 233, 28),
        text_item("题目来源：支付宝·答答星球", 239, 1181, 241, 23),
        text_item("我选好了", 168, 1465, 384, 34),
    ]

    # 提交后的结果页：真机实测多了「正确答案：A」「你的答案：A」和底部
    # 「已成功领取奖励 回到主页」，金币 57494 → 57524。
    QUIZ_RESULT = QUIZ_PAGE + [
        text_item("正确答案：A", 38, 853, 180, 33),
        text_item("你的答案：A", 38, 899, 146, 29),
        text_item("已成功领取奖励回到主页", 168, 1465, 384, 34),
    ]

    # 任务面板上这一行：名字「淘金币趣味课堂」+ 说明「参与答题」+ 右侧 +30。
    PANEL_WITH_QUIZ = [
        text_item("完成下方任务得额外金币", 45, 313, 252, 23),
        text_item("淘金币趣味课堂", 130, 587, 204, 33),
        text_item("参与答题", 128, 625, 100, 35),
        text_item("+30", 604, 610, 51, 33),
        text_item("去蚂蚁庄园逛逛哟", 130, 727, 233, 33),
        text_item("点击去逛", 126, 768, 100, 30),
        text_item("+50", 602, 750, 52, 33),
    ]

    def setUp(self) -> None:
        self._original_sleep = actions._sleep
        actions._sleep = lambda seconds: None

    def tearDown(self) -> None:
        actions._sleep = self._original_sleep

    def test_quiz_task_is_not_classified_as_browse(self) -> None:
        """按浏览处理是这个 bug 的根源：停留再久进度也不涨。"""
        decision = panel.classify_task("淘金币趣味课堂", load_config())
        self.assertEqual(decision.kind, "quiz")

    def test_quiz_task_is_picked_by_the_panel(self) -> None:
        items = panel.parse_ocr_items(self.PANEL_WITH_QUIZ)
        pick = panel.pick_task(items, load_config())
        self.assertIsNotNone(pick.item)
        assert pick.item is not None
        self.assertEqual(pick.item.text, "淘金币趣味课堂")
        self.assertEqual(pick.decision.kind, "quiz")

    def test_option_and_submit_are_found_on_the_quiz_page(self) -> None:
        items = panel.parse_ocr_items(self.QUIZ_PAGE)
        option = panel.quiz_option(items)
        self.assertIsNotNone(option)
        assert option is not None
        # 题面（以数字开头）不能被当成选项。
        self.assertEqual(option.text, "A表演者姓黄")
        self.assertIsNotNone(panel.quiz_submit(items))

    def test_result_page_is_recognised_as_answered(self) -> None:
        self.assertFalse(panel.quiz_answered(panel.parse_ocr_items(self.QUIZ_PAGE)))
        self.assertTrue(panel.quiz_answered(panel.parse_ocr_items(self.QUIZ_RESULT)))

    def test_quiz_task_picks_an_option_and_submits(self) -> None:
        screens = [
            self.PANEL_WITH_QUIZ,
            self.QUIZ_PAGE,
            self.QUIZ_PAGE,
            self.QUIZ_RESULT,
            DONE_ONLY,
        ]
        context = FakeContext(screens)

        actions.run_daily_loop(context, fast_config(), max_rounds=1)

        # 选项块中心 (137,610)、提交按钮中心 (360,1482)。
        self.assertIn((137, 610), context.controller.clicks)
        self.assertIn((360, 1482), context.controller.clicks)

    def test_quiz_task_does_not_just_dwell_and_go_back(self) -> None:
        """按浏览处理时只会滑动 + 返回；答题必须有选选项这一步。"""
        screens = [
            self.PANEL_WITH_QUIZ,
            self.QUIZ_PAGE,
            self.QUIZ_PAGE,
            self.QUIZ_RESULT,
            DONE_ONLY,
        ]
        context = FakeContext(screens)

        actions.run_daily_loop(context, fast_config(), max_rounds=1)

        option = panel.quiz_option(panel.parse_ocr_items(self.QUIZ_PAGE))
        assert option is not None
        self.assertIn(option.center, context.controller.clicks)

    def test_quiz_without_options_is_not_counted_as_done(self) -> None:
        """答题页没渲染出选项时不能算完成，否则会一直空跑同一个任务。"""
        blank = [text_item("加载中", 300, 600, 120, 30)]
        screens = [self.PANEL_WITH_QUIZ] + [blank] * 12 + [DONE_ONLY]
        context = FakeContext(screens)

        with self.assertLogs("autotaskflow", level="WARNING") as logs:
            actions.run_daily_loop(context, fast_config(), max_rounds=1)

        self.assertTrue(any("没找到选项" in line for line in logs.output))
        self.assertTrue(any("未能真正执行" in line for line in logs.output))
        # 没找到选项就不能瞎点：提交按钮一次都不该被按。
        self.assertNotIn((360, 1482), context.controller.clicks)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()