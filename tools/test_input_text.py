"""临时脚本：在当前已聚焦的搜索框上验证 post_input_text（ASCII / 中文）。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import maa  # noqa: F401,E402
from maa.controller import AdbController  # noqa: E402
from maa.toolkit import Toolkit  # noqa: E402


def shot(ctrl, name: str) -> None:
    img = ctrl.post_screencap().wait().get()
    if img is not None:
        cv2.imwrite(str(ROOT / "debug" / name), img)
        print("saved", name)
    else:
        print("screencap failed", name)


def clear(ctrl) -> None:
    ctrl.post_shell("input keyevent 123").wait()
    for _ in range(25):
        ctrl.post_shell("input keyevent 67").wait()
    time.sleep(1.0)


def main() -> None:
    Toolkit.init_option(ROOT)
    device = Toolkit.find_adb_devices(None)[0]
    ctrl = AdbController(
        adb_path=str(device.adb_path),
        address=device.address,
        screencap_methods=device.screencap_methods,
        input_methods=device.input_methods,
        config=device.config,
    )
    print("connect ok:", ctrl.post_connection().wait().succeeded)

    # 搜索框已在焦点上（上一步手动点开），直接测 post_input_text
    res = ctrl.post_input_text("bbb").wait()
    print("post_input_text ascii ->", res.succeeded, res)
    time.sleep(1.5)
    shot(ctrl, "pi_ascii.png")

    clear(ctrl)
    res = ctrl.post_input_text("手机壳").wait()
    print("post_input_text cn ->", res.succeeded, res)
    time.sleep(1.5)
    shot(ctrl, "pi_cn.png")


if __name__ == "__main__":
    main()
