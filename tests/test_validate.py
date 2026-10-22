"""校验器自身的测试：确保它真的能拦住协议错误，而不是只打印 OK。"""

from __future__ import annotations

import json
import logging
import unittest

from tools import validate_project as vp


class ValidateNodeTest(unittest.TestCase):
    """直接测节点校验函数，能精确定位是哪个规则在生效。"""

    def validate(self, node: dict) -> None:
        vp.validate_node("TestNode", {**node, "__file__": "test.json"})

    def test_valid_node_passes(self) -> None:
        self.validate({"recognition": "OCR", "expected": ["淘金币"], "action": "Click"})

    def test_wait_action_is_rejected(self) -> None:
        """协议里没有 Wait 动作，这是本项目最初的真实 bug。"""
        with self.assertRaises(vp.ValidationError):
            self.validate({"recognition": "DirectHit", "action": "Wait", "duration": 3000})

    def test_back_action_is_rejected(self) -> None:
        """协议里没有 Back 动作，返回键必须用 ClickKey key:[4]。"""
        with self.assertRaises(vp.ValidationError):
            self.validate({"recognition": "DirectHit", "action": "Back"})

    def test_click_key_requires_int_list(self) -> None:
        with self.assertRaises(vp.ValidationError):
            self.validate({"recognition": "DirectHit", "action": "ClickKey", "key": 4})
        self.validate({"recognition": "DirectHit", "action": "ClickKey", "key": [4]})

    def test_input_text_requires_input_text_field(self) -> None:
        with self.assertRaises(vp.ValidationError):
            self.validate(
                {"recognition": "DirectHit", "action": "InputText", "text": "手机壳"}
            )
        self.validate(
            {"recognition": "DirectHit", "action": "InputText", "input_text": "手机壳"}
        )

    def test_start_app_requires_package(self) -> None:
        with self.assertRaises(vp.ValidationError):
            self.validate({"recognition": "DirectHit", "action": "StartApp"})
        self.validate(
            {
                "recognition": "DirectHit",
                "action": "StartApp",
                "package": "com.taobao.taobao",
            }
        )

    def test_custom_action_requires_name(self) -> None:
        with self.assertRaises(vp.ValidationError):
            self.validate({"recognition": "DirectHit", "action": "Custom"})

    def test_unknown_recognition_is_rejected(self) -> None:
        with self.assertRaises(vp.ValidationError):
            self.validate({"recognition": "Magic", "action": "DoNothing"})

    def test_delays_must_be_int(self) -> None:
        with self.assertRaises(vp.ValidationError):
            self.validate({"recognition": "DirectHit", "action": "DoNothing", "pre_delay": "200"})

    def test_forbidden_fields_are_rejected(self) -> None:
        with self.assertRaises(vp.ValidationError):
            self.validate({"recognition": "DirectHit", "action": "DoNothing", "duration": 1})


class JumpBackTest(unittest.TestCase):
    def test_strip_jump_back(self) -> None:
        self.assertEqual(vp.strip_jump_back("[JumpBack]SafetyVerify"), "SafetyVerify")
        self.assertEqual(vp.strip_jump_back("SafetyVerify"), "SafetyVerify")


class ReachabilityTest(unittest.TestCase):
    def test_unreachable_node_is_detected(self) -> None:
        nodes = {
            "A": {"__file__": "t.json", "next": ["B"]},
            "B": {"__file__": "t.json"},
            "孤儿": {"__file__": "t.json"},
        }
        references = vp.collect_references(nodes)
        self.assertEqual(vp.find_unreachable(nodes, references, ["A"]), ["孤儿"])


class DeadEndNextTest(unittest.TestCase):
    """next 的最后一条必须必然命中，否则框架原地无限重试、任务不会自行结束。

    真机事故：DailyTaskLoop 的 next 只有两个 OCR 节点，任务全部做完后的完成
    提示页两个都不命中。MaaFramework 不会回落到 on_error，而是每秒重跑一次
    识别列表，日志停在最后一句「今日任务已完成，正常结束」，进程一直不退出。
    """

    def test_next_ending_in_ocr_is_reported(self) -> None:
        nodes = {
            "Loop": {"__file__": "t.json", "next": ["[JumpBack]Safety", "Finish"]},
            "Safety": {"__file__": "t.json", "recognition": "OCR", "expected": ["安全验证"]},
            "Finish": {"__file__": "t.json", "recognition": "OCR", "expected": ["领取奖励"]},
        }
        self.assertEqual(vp.find_dead_end_next(nodes), ["t.json:Loop -> Finish"])

    def test_next_ending_in_direct_hit_passes(self) -> None:
        nodes = {
            "Loop": {"__file__": "t.json", "next": ["Safety", "Stop"]},
            "Safety": {"__file__": "t.json", "recognition": "OCR", "expected": ["安全验证"]},
            "Stop": {"__file__": "t.json", "recognition": "DirectHit", "action": "StopTask"},
        }
        self.assertEqual(vp.find_dead_end_next(nodes), [])

    def test_node_without_next_is_not_flagged(self) -> None:
        """收尾节点本来就没有后继，不算死胡同。"""
        nodes = {"Stop": {"__file__": "t.json", "recognition": "DirectHit", "action": "StopTask"}}
        self.assertEqual(vp.find_dead_end_next(nodes), [])

    def test_no_real_node_can_wedge_the_run(self) -> None:
        """真实的 Pipeline 不允许出现会卡死的 next 列表。"""
        nodes, _ = vp.load_pipeline(vp.ROOT / "assets" / "resource")
        self.assertEqual(vp.find_dead_end_next(nodes), [])


class RealProjectTest(unittest.TestCase):
    """真实的 interface.json / pipeline 必须能通过完整校验。"""

    def test_project_validates(self) -> None:
        messages = vp.validate()
        self.assertTrue(any("PI v2" in message for message in messages))

    def test_all_custom_actions_are_registered(self) -> None:
        nodes, _ = vp.load_pipeline(vp.ROOT / "assets" / "resource")
        used = vp.collect_custom_actions(nodes)
        self.assertTrue(used, "Pipeline 里应当有 Custom 动作")
        self.assertLessEqual(used, vp.collect_registered_actions())

    def test_interface_is_v2(self) -> None:
        interface = vp.read_json(vp.INTERFACE_FILE)
        self.assertEqual(interface["interface_version"], 2)
        self.assertIsInstance(interface["controller"], list)
        self.assertIsInstance(interface["resource"], list)
        self.assertIsInstance(interface["task"], list)


class BrokenProjectTest(unittest.TestCase):
    """改造真实文件的临时副本，确认校验器会报错而不是放行。"""

    def test_pipeline_with_wait_action_fails(self) -> None:
        """把 Wait 塞进一个真实节点，校验必须失败。"""
        path = vp.ROOT / "assets" / "resource" / "pipeline" / "action.json"
        original = path.read_text(encoding="utf-8")
        try:
            data = json.loads(original)
            data["SafetyVerify"]["action"] = "Wait"
            data["SafetyVerify"]["duration"] = 3000
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            with self.assertRaises(vp.ValidationError):
                vp.validate()
        finally:
            path.write_text(original, encoding="utf-8")

    def test_missing_reference_fails(self) -> None:
        path = vp.ROOT / "assets" / "resource" / "pipeline" / "entry.json"
        original = path.read_text(encoding="utf-8")
        try:
            data = json.loads(original)
            data["OpenTaobao"]["next"] = ["不存在的节点"]
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            with self.assertRaises(vp.ValidationError):
                vp.validate()
        finally:
            path.write_text(original, encoding="utf-8")

    def test_interface_v1_shape_fails(self) -> None:
        original = vp.INTERFACE_FILE.read_text(encoding="utf-8")
        try:
            data = json.loads(original)
            data.pop("interface_version", None)
            vp.INTERFACE_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            with self.assertRaises(vp.ValidationError):
                vp.validate()
        finally:
            vp.INTERFACE_FILE.write_text(original, encoding="utf-8")

    def test_pipeline_path_pointing_at_pipeline_dir_fails(self) -> None:
        interface = {
            "interface_version": 2,
            "name": "T",
            "version": "0",
            "controller": [{"name": "Android", "type": "Adb"}],
            "resource": [{"name": "R", "path": ["assets/resource/pipeline"]}],
            "task": [{"name": "T", "entry": "OpenTaobao"}],
        }
        with self.assertRaises(vp.ValidationError):
            vp.validate_interface(interface)


class OptionTest(unittest.TestCase):
    """配置项写错不会报错，只会在界面上少一个开关，所以必须校验。"""

    def nodes(self) -> dict:
        return {"OpenTaobao": {}, "OpenCoinPage": {}}

    def interface(self, **overrides) -> dict:
        data = {
            "task": [{"name": "T", "entry": "OpenTaobao", "option": ["运行账号"]}],
            "option": {
                "运行账号": {
                    "type": "select",
                    "default_case": "主账号",
                    "cases": [
                        {
                            "name": "主账号",
                            "pipeline_override": {
                                "OpenTaobao": {"custom_action_param": {"user_id": 0}}
                            },
                        }
                    ],
                }
            },
        }
        data.update(overrides)
        return data

    def test_real_interface_options_are_valid(self) -> None:
        interface = vp.read_json(vp.INTERFACE_FILE)
        nodes, _ = vp.load_pipeline(vp.ROOT / "assets" / "resource")
        self.assertGreaterEqual(vp.validate_options(interface, nodes), 1)

    def test_unknown_type_fails(self) -> None:
        self.assertGreaterEqual(vp.validate_options(self.interface(), self.nodes()), 1)
        with self.assertRaises(vp.ValidationError):
            vp.validate_options(self.interface(option={"x": {"type": "magic", "cases": [{"name": "a"}]}}), self.nodes())

    def test_default_case_must_exist(self) -> None:
        data = self.interface()
        data["option"]["运行账号"]["default_case"] = "不存在的选项"
        with self.assertRaises(vp.ValidationError):
            vp.validate_options(data, self.nodes())

    def test_task_referencing_missing_option_fails(self) -> None:
        data = self.interface(option={})
        with self.assertRaises(vp.ValidationError):
            vp.validate_options(data, self.nodes())

    def test_override_must_target_existing_node(self) -> None:
        """节点名写错时框架会静默忽略，配置项就等于没生效。"""
        data = self.interface()
        data["option"]["运行账号"]["cases"][0]["pipeline_override"] = {
            "OpenTaoBao": {"custom_action_param": {"user_id": 0}}
        }
        with self.assertRaises(vp.ValidationError):
            vp.validate_options(data, self.nodes())

    def test_override_unknown_field_fails(self) -> None:
        data = self.interface()
        data["option"]["运行账号"]["cases"][0]["pipeline_override"] = {
            "OpenTaobao": {"custom_action_params": {"user_id": 0}}
        }
        with self.assertRaises(vp.ValidationError):
            vp.validate_options(data, self.nodes())

    def test_user_id_must_be_non_negative_int(self) -> None:
        for bad in ("999", -1, True, None, 999.0):
            with self.subTest(bad=bad):
                data = self.interface()
                data["option"]["运行账号"]["cases"][0]["pipeline_override"] = {
                    "OpenTaobao": {"custom_action_param": {"user_id": bad}}
                }
                with self.assertRaises(vp.ValidationError):
                    vp.validate_options(data, self.nodes())


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
