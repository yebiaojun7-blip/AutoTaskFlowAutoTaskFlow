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

    def __init__(self, value=None) -> None:
        self._value = value

    def wait(self) -> "FakeJob":
        return self

    def get(self):
        return self._value


class FakeController:
    def __init__(
        self,
        screens: list[list[dict]],
        size=(1280, 720),
        packages: list[str] | None = None,
    ) -> None:
        self.screens = screens
        self.index = 0
        self.clicks: list[tuple[int, int]] = []
        self.keys: list[int] = []
        self.swipes: list[tuple[int, int, int, int]] = []
        self.inputs: list[str] = []
        self.shells: list[str] = []
        self.foreground = "com.taobao.taobao"
        self.resolution = list(size)
        self.packages = packages

    def post_screencap(self) -> FakeJob:
        index = min(self.index, len(self.screens) - 1)
        self.index += 1
        return FakeJob(index)

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
        return FakeJob()

    def post_shell(self, cmd, timeout=20000) -> FakeJob:  # noqa: ANN001
        self.shells.append(str(cmd))
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

    def __init__(self, screens: list[list[dict]], packages: list[str] | None = None) -> None:
        self.screens = screens
        self.controller = FakeController(screens, packages=packages)
        self.tasker = SimpleNamespace(controller=self.controller)
        self.reco_entries: list[str] = []

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
        search_box = [text_item("搜索", 300, 120, 400, 60)]
        context = FakeContext([search_panel, search_box, DONE_ONLY, DONE_ONLY])

        actions.run_daily_loop(context, fast_config(), max_rounds=3)

        # 搜索任务必须真的把关键词敲进去。
        self.assertEqual(len(context.controller.inputs), 1)
        self.assertIn(context.controller.inputs[0], load_config()["search_keywords"])

    def test_round_limit_is_respected(self) -> None:
        context = FakeContext([PANEL_ROW])
        result = actions.run_daily_loop(context, fast_config(), max_rounds=2)
        self.assertTrue(result)
        self.assertLessEqual(len(context.controller.clicks), 2)


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


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()