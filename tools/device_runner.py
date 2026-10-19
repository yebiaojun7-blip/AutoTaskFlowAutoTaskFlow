"""真机运行 / 校准工具（无需 MaaPiCli，纯 Python 直连 ADB）。

两种用法：

    # 1. 只看当前屏幕：列出 OCR 识别到的所有文字 + 保存截图
    python -m tools.device_runner --probe

    # 2. 真跑一遍完整任务
    python -m tools.device_runner

为什么不用 MaaPiCli：
    本仓库的 interface.json 在仓库根目录，而 ProjectInterface v2 的 resource.path
    是相对启动目录解析的；直接用 MaaPiCli 需要额外做一份 install/ 目录并改路径。
    这个脚本绕过 interface.json，直接用 MaaLibrary 加载 assets/resource 并在同一
    进程内注册自定义动作，省掉 agent 子进程 + socket 那一层。

注意：本模块只在真正运行时才 import maa（延迟导入），
      因此 `python -m unittest` 在没有装 MaaFw 的机器上依然能跑。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from agent.custom import APP_USER_ENV

# 仓库根目录 = 本文件的上一级
ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "assets" / "resource"

LOGGER = logging.getLogger("autotaskflow.device")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def load_framework() -> None:
    """import maa 并确认框架动态库能加载。

    pip 安装的 MaaFw 包自带 bin/ 下的全部 DLL，不用另外下载 release 压缩包。
    """
    try:
        import maa  # noqa: F401
        from maa.library import Library
    except ImportError as exc:  # pragma: no cover - 环境问题，非逻辑分支
        raise SystemExit(
            "未安装 MaaFw 运行时。请先执行：\n    python -m pip install MaaFw"
        ) from exc

    LOGGER.info("MaaFramework 版本: %s", Library.version())


def build_controller(address: str | None, adb_path: str | None) -> Any:
    """找到一台 ADB 设备并建好控制器（已连接、已截图可用）。"""
    from maa.controller import AdbController
    from maa.toolkit import Toolkit

    # init_option 会在同目录生成 config/maa_option.json，日志落在 debug/ 下。
    Toolkit.init_option(ROOT)

    devices = Toolkit.find_adb_devices(adb_path)
    if not devices:
        raise SystemExit(
            "没有找到 ADB 设备。请检查：\n"
            "  1. 手机已开启【开发者选项 → USB 调试】；\n"
            "  2. USB 线已连接，手机上点了【允许调试】；\n"
            "  3. 命令行 `adb devices` 能看到设备；\n"
            "  4. 若用无线调试，需先 adb connect <ip:port>。"
        )

    if address:
        matched = [d for d in devices if address in d.address]
        if not matched:
            names = ", ".join(f"{d.name}({d.address})" for d in devices)
            raise SystemExit(f"未找到地址包含 {address!r} 的设备。当前设备: {names}")
        device = matched[0]
    else:
        device = devices[0]
        if len(devices) > 1:
            LOGGER.warning(
                "检测到 %d 台设备，默认使用第一台 %s。可用 --address 指定。",
                len(devices),
                device.address,
            )

    LOGGER.info("使用设备: %s (%s)", device.name, device.address)

    controller = AdbController(
        adb_path=str(device.adb_path),
        address=device.address,
        screencap_methods=device.screencap_methods,
        input_methods=device.input_methods,
        config=device.config,
    )
    job = controller.post_connection().wait()
    if not job.succeeded:
        raise SystemExit(
            "连接设备失败。常见原因：\n"
            "  - 手机上未授权本机调试；\n"
            "  - 设备被其他程序（模拟器/其他脚本）占用；\n"
            "  - minicap/maatouch 推送失败，可尝试重启 adb：adb kill-server"
        )

    LOGGER.info("设备原始分辨率: %s（首次截图后才有值）", controller.resolution)
    return controller


def build_resource(controller: Any) -> Any:
    """加载资源包并在**同一进程内**注册自定义动作。"""
    from maa.resource import Resource
    from maa.tasker import Tasker

    if not BUNDLE.is_dir():
        raise SystemExit(f"资源目录不存在: {BUNDLE}")

    ocr_dir = BUNDLE / "model" / "ocr"
    if not (ocr_dir / "det.onnx").is_file() or not (ocr_dir / "rec.onnx").is_file():
        raise SystemExit(
            f"OCR 模型缺失: {ocr_dir}\n"
            "下载 https://download.maafw.xyz/MaaCommonAssets/OCR/ppocr_v6/ppocr_v6-small.zip\n"
            f"解压后应得到 {ocr_dir}\\det.onnx、keys.txt、rec.onnx。"
        )

    resource = Resource()
    if not resource.post_bundle(str(BUNDLE)).wait().succeeded:
        raise SystemExit(
            "资源包加载失败（pipeline JSON 有错）。跑一遍校验：\n"
            "    python -m tools.validate_project"
        )
    LOGGER.info("资源包已加载，节点数: %d", len(resource.node_list or []))

    # 关键一步：进程内注册，不需要 agent 子进程 / socket。
    from agent import actions

    class _InlineRegistrar:
        """把 AgentServer 风格的装饰器适配到 Resource.register_custom_action。"""

        @staticmethod
        def custom_action(name: str):
            def wrapper(cls: type) -> type:
                if not resource.register_custom_action(name, cls()):
                    raise SystemExit(f"自定义动作注册失败: {name}")
                LOGGER.info("已注册自定义动作: %s", name)
                return cls

            return wrapper

    actions.register_all(_InlineRegistrar)

    tasker = Tasker()
    if not tasker.bind(resource, controller):
        raise SystemExit("Tasker 绑定资源/控制器失败。")
    return tasker


def dump_image(image: Any, out_path: Path) -> None:
    """把 numpy 截图存成 PNG，方便核对缩放基准与 OCR 结果。"""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        LOGGER.warning("未安装 Pillow，跳过截图保存")
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # MaaFramework 给的截图是 BGR
    Image.fromarray(image[:, :, ::-1]).save(out_path)
    LOGGER.info("截图已保存: %s", out_path)


def probe(tasker: Any) -> int:
    """只截一张图跑 OCR，打印识别到的全部文字。用于核对文案与坐标。"""
    from maa.pipeline import JOCR

    from agent import panel
    from agent.custom import app_user_id, load_config

    controller = tasker.controller
    image = controller.post_screencap().wait().get()
    if image is None or image.size == 0:
        LOGGER.error("截图失败")
        return 1

    height, width = image.shape[:2]
    LOGGER.info("截图尺寸: %dx%d（点击坐标与此一致）", width, height)
    LOGGER.info("设备原始分辨率: %s", controller.resolution)
    LOGGER.info("目标 Android 用户: %d", app_user_id(load_config()))
    dump_image(image, ROOT / "debug" / "probe.png")

    # post_recognition(识别类型, 识别参数, 图片)；返回的是 TaskJob，
    # .get() 拿到 TaskDetail，真正的文字块在 nodes[0].recognition.all_results。
    task_detail = tasker.post_recognition("OCR", JOCR(), image).wait().get()
    if task_detail is None or not task_detail.nodes:
        LOGGER.error("OCR 识别任务没有返回节点详情")
        return 1

    reco = task_detail.nodes[0].recognition
    items = panel.parse_ocr_items(getattr(reco, "all_results", None) or [])

    if not items:
        LOGGER.error(
            "OCR 没有识别到任何文字。请检查：\n"
            "  - assets/resource/model/ocr/ 下是否有 det.onnx / rec.onnx / keys.txt；\n"
            "  - 当前屏幕是否真的有中文文字（在桌面/黑屏时识别不到是正常的）。"
        )
        return 1

    LOGGER.info("识别到 %d 个文字块（按阅读顺序）：", len(items))
    for item in items:
        LOGGER.info(
            "  %-20s 中心(%4d,%4d) 框%s 置信度%.2f",
            item.text,
            item.center[0],
            item.center[1],
            item.box,
            item.score,
        )

    # 顺手用真实数据验证一遍任务挑选逻辑
    pick = panel.pick_task(items)
    if pick.item:
        LOGGER.info("挑中的任务: %s -> %s", pick.decision.kind, pick.item.text)
        if pick.decision.search_keyword:
            LOGGER.info("搜索词: %s", pick.decision.search_keyword)

        # 关键闭环：真的能找到右侧按钮，才算这条任务可以点。
        # 找不到按钮时任务会被判为"可执行"却点不动，是线上最常见的静默失败。
        button = panel.find_action_button(items, pick.item)
        if button is not None:
            # 打印 center 而不是 x/y：真正 post_click 用的是 item.center。
            LOGGER.info("右侧按钮: %s @ 中心(%d, %d)", button.text, *button.center)
        else:
            LOGGER.warning(
                "右侧按钮: 未找到！任务名 %s 在同一行没有匹配的按钮文案，"
                "点击会落在任务名上。请把实际按钮文字补进 DEFAULT_ACTION_BUTTONS。",
                pick.item.text,
            )
    else:
        LOGGER.info("当前屏幕没有可执行任务（跳过: %s）", "、".join(pick.skipped) or "无")
    LOGGER.info("是否在淘金币面板: %s", panel.panel_visible(items))
    LOGGER.info("当前页面类型: %s", _page_kind(items))
    return 0


def _page_kind(items: "list[Any]") -> str:
    """把当前屏归类，便于校准日志一眼看出停在哪。"""
    from agent import panel

    if panel.daily_done_visible(items):
        return "今日任务已完成（正常结束）"
    if panel.panel_visible(items):
        return "任务面板"
    if panel.team_page_visible(items):
        return "组队活动页（应退出）"
    if panel.coin_home_visible(items):
        return "淘金币首页（可点赚金币进面板）"
    return "未知页面"


def run_task(tasker: Any, entry: str, max_rounds: int | None = None) -> int:
    """从指定入口跑一遍完整任务链。max_rounds 用于临时压低主循环轮数。"""
    LOGGER.info("开始执行任务，入口节点: %s", entry)
    override = None
    if max_rounds is not None:
        # 第二个位置参数是 pipeline_override，用来临时覆盖节点参数。
        override = {
            "DailyTaskLoop": {"custom_action_param": json.dumps({"max_rounds": max_rounds})}
        }
        LOGGER.info("本轮限制最多 %d 轮", max_rounds)
    detail = tasker.post_task(entry, override).wait().get()
    if detail is None:
        LOGGER.error("任务执行失败，没有拿到详情")
        return 1

    for node in detail.nodes:
        LOGGER.info("  节点 %-20s 识别=%s", node.name, node.recognition.hit if node.recognition else "-")

    if detail.status.succeeded:
        LOGGER.info("任务完成")
        return 0

    LOGGER.error(
        "任务未成功结束（StopTask/AbortTask）。若停在 AbortTask，"
        "通常说明命中了安全验证（滑块/验证码），属于预期的主动保护。"
    )
    return 1


def calibrate(tasker: Any, tap: tuple[int, int] | None, swipe: tuple[int, int, int, int] | None) -> int:
    """校准辅助：在设备上执行一次点击或滑动，用来把手机摆到目标页面。"""
    controller = tasker.controller

    if tap is not None:
        x, y = tap
        LOGGER.info("点击 (%d, %d)", x, y)
        if not controller.post_click(x, y).wait().succeeded:
            LOGGER.error("点击失败")
            return 1

    if swipe is not None:
        x1, y1, x2, y2 = swipe
        LOGGER.info("滑动 (%d,%d) -> (%d,%d)", x1, y1, x2, y2)
        if not controller.post_swipe(x1, y1, x2, y2, 400).wait().succeeded:
            LOGGER.error("滑动失败")
            return 1

    return 0


def list_users(controller: Any) -> int:
    """列出设备上的 Android 用户，帮助确定应用分身所在的用户 ID。"""
    job = controller.post_shell("pm list users")
    job.wait()
    output = str(job.get() or "")
    LOGGER.info("设备上的 Android 用户：")
    for line in output.splitlines():
        if "UserInfo" in line:
            LOGGER.info("  %s", line.strip())
    LOGGER.info(
        "淘宝安装情况可用 `adb shell pm list packages --user <ID> | findstr taobao` 核对。"
        "应用分身通常是 MultiApp（本机为 999）。"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="淘宝淘金币任务真机运行工具")
    parser.add_argument("--probe", action="store_true", help="只截图 + OCR，不执行任务")
    parser.add_argument("--entry", default="OpenTaobao", help="任务入口节点，默认 OpenTaobao")
    parser.add_argument("--address", default=None, help="指定设备地址（多设备时用）")
    parser.add_argument("--adb", default=None, help="指定 adb 可执行文件路径")
    parser.add_argument("--tap", nargs=2, type=int, metavar=("X", "Y"), help="校准：点一下屏幕")
    parser.add_argument(
        "--swipe", nargs=4, type=int, metavar=("X1", "Y1", "X2", "Y2"), help="校准：滑动屏幕"
    )
    parser.add_argument(
        "--users", action="store_true", help="列出设备上的 Android 用户（找应用分身用户 ID）"
    )
    parser.add_argument(
        "--user",
        type=int,
        default=None,
        metavar="ID",
        help="目标 Android 用户：0 是主用户，应用分身通常为 999",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        metavar="N",
        help="限制主循环最多处理 N 轮（调试时用，默认取 Pipeline 配置）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    # 在加载框架前设置，actions.app_user_id() 只在运行时读取该环境变量。
    if args.user is not None:
        os.environ[APP_USER_ENV] = str(args.user)

    load_framework()
    controller = build_controller(args.address, args.adb)
    if args.users:
        return list_users(controller)
    tasker = build_resource(controller)

    try:
        if args.tap or args.swipe:
            code = calibrate(
                tasker,
                tuple(args.tap) if args.tap else None,
                tuple(args.swipe) if args.swipe else None,
            )
            if code != 0:
                return code
        if args.probe or args.tap or args.swipe:
            return probe(tasker)
        return run_task(tasker, args.entry, args.max_rounds)
    finally:
        try:
            tasker.post_stop().wait()
        except Exception:  # noqa: BLE001 - 清理失败不应掩盖真实结果
            pass



if __name__ == "__main__":
    sys.exit(main())
