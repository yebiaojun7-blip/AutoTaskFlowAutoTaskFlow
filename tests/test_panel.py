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
    def __init__(self, screens: list[list[dict]], size=(1280, 720)) -> None:
        self.screens = screens
        self.index = 0
        self.clicks: list[tuple[int, int]] = []
        self.keys: list[int] = []
        self.swipes: list[tuple[int, int, int, int]] = []
        self.inputs: list[str] = []
        self.shells: list[str] = []
        self.resolution = list(size)

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
        return FakeJob("")


class FakeContext:
    """把截图序号映射到预先安排好的 OCR 结果。"""

    def __init__(self, screens: list[list[dict]]) -> None:
        self.screens = screens
        self.controller = FakeController(screens)
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
        context = FakeContext([PANEL_ROW, DONE_ONLY, DONE_ONLY])
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


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()