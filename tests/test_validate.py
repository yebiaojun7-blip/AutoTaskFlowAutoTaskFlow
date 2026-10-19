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


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
