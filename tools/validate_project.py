"""校验 AutoTaskFlow 的本地配置，不连接设备、不需 MaaFramework 运行时。

校验范围（都是能在离线状态下确定的）：
1. interface.json 符合 ProjectInterface v2 的必填字段与类型；
2. resource[].path 指向的资源包目录真实存在，pipeline 目录能被加载；
3. 节点间 next/on_error 引用闭合，任务入口存在且可达；
4. recognition / action 只使用 MaaFramework 协议里真实存在的枚举值；
5. action 参数名与协议一致（如 InputText 用 input_text、ClickKey 用 key 数组）；
6. Custom 动作在 agent/ 下有对应的 custom_action 注册；
7. config/tasks.json 名单结构与内容合法；
8. agent/ 下所有 Python 文件可编译。
"""

from __future__ import annotations

import json
import py_compile
import re
import sys
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
INTERFACE_FILE = ROOT / "interface.json"
TASK_CONFIG_FILE = ROOT / "config" / "tasks.json"
AGENT_DIR = ROOT / "agent"

# MaaFramework 协议中真实存在的识别算法（JRecognitionType）。
VALID_RECOGNITION = {
    "DirectHit",
    "TemplateMatch",
    "FeatureMatch",
    "ColorMatch",
    "OCR",
    "NeuralNetworkClassify",
    "NeuralNetworkDetect",
    "Custom",
}

# MaaFramework 协议中真实存在的动作（JActionType）。注意没有 Wait / Back。
VALID_ACTION = {
    "DoNothing",
    "Click",
    "LongPress",
    "Swipe",
    "MultiSwipe",
    "TouchDown",
    "TouchMove",
    "TouchUp",
    "Scroll",
    "ClickKey",
    "LongPressKey",
    "KeyDown",
    "KeyUp",
    "InputText",
    "StartApp",
    "StopApp",
    "StopTask",
    "Command",
    "Shell",
    "Screencap",
    "Custom",
}

# 每类动作必需的参数字段（键 -> 期望的类型描述）。
ACTION_REQUIRED_PARAMS: dict[str, dict[str, str]] = {
    "InputText": {"input_text": "str"},
    "ClickKey": {"key": "list[int]"},
    "LongPressKey": {"key": "list[int]"},
    "KeyDown": {"key": "list[int]"},
    "KeyUp": {"key": "list[int]"},
    "StartApp": {"package": "str"},
    "StopApp": {"package": "str"},
    "Custom": {"custom_action": "str"},
    "Swipe": {"begin": "list[int]", "end": "list[int]"},
}

# 不得出现在节点里的字段：它们是早期误写的产物，框架会静默忽略。
FORBIDDEN_FIELDS = {"duration", "text", "stop_on_error"}

TASK_CONFIG_LISTS = (
    "blacklist",
    "jump_tasks",
    "search_tasks",
    "browse_tasks",
    "safety_keywords",
    "search_keywords",
)


class ValidationError(Exception):
    """配置不合法。"""


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValidationError(f"文件不存在: {path.relative_to(ROOT)}")
    with path.open("r", encoding="utf-8-sig") as file:
        try:
            value = json.load(file)
        except json.JSONDecodeError as error:
            raise ValidationError(f"{path.relative_to(ROOT)} JSON 解析失败: {error}") from error
    if not isinstance(value, dict):
        raise ValidationError(f"{path.relative_to(ROOT)} 的根节点必须是对象")
    return value


def strip_jump_back(reference: str) -> str:
    """把 "[JumpBack]NodeName" 还原为 "NodeName"。"""
    return reference[len("[JumpBack]") :] if reference.startswith("[JumpBack]") else reference


# --------------------------------------------------------------------------- #
# interface.json（ProjectInterface v2）
# --------------------------------------------------------------------------- #


def validate_interface(interface: dict[str, Any]) -> tuple[list[Path], list[str]]:
    """校验 interface.json，返回（资源包根目录列表，任务入口名列表）。"""
    if interface.get("interface_version") != 2:
        raise ValidationError("interface.json 缺少 interface_version: 2（PI v2 必需）")

    for key in ("name", "version"):
        if not isinstance(interface.get(key), str) or not interface[key]:
            raise ValidationError(f"interface.json 的 {key} 必须是非空字符串")

    controllers = interface.get("controller")
    if not isinstance(controllers, list) or not controllers:
        raise ValidationError("interface.json 的 controller 必须是数组（PI v2）")
    for controller in controllers:
        if not isinstance(controller, dict):
            raise ValidationError("controller 数组元素必须是对象")
        if not isinstance(controller.get("name"), str) or not controller["name"]:
            raise ValidationError("controller 缺少 name")
        if not isinstance(controller.get("type"), str) or not controller["type"]:
            raise ValidationError(f"controller {controller.get('name')} 缺少 type")

    resources = interface.get("resource")
    if not isinstance(resources, list) or not resources:
        raise ValidationError("interface.json 的 resource 必须是数组（PI v2）")

    bundle_roots: list[Path] = []
    for resource in resources:
        if not isinstance(resource, dict):
            raise ValidationError("resource 数组元素必须是对象")
        paths = resource.get("path")
        if not isinstance(paths, list) or not paths or not all(isinstance(item, str) for item in paths):
            raise ValidationError(f"resource {resource.get('name')} 的 path 必须是非空字符串数组")
        for relative in paths:
            if relative.replace("\\", "/").rstrip("/").endswith("/pipeline"):
                raise ValidationError(
                    f"resource path 应指向资源包根目录而不是 pipeline 目录: {relative}"
                )
            root = (ROOT / relative).resolve()
            if not root.is_dir():
                raise ValidationError(f"资源包目录不存在: {relative}")
            if not (root / "pipeline").is_dir():
                raise ValidationError(f"资源包缺少 pipeline 目录: {relative}")
            bundle_roots.append(root)

    tasks = interface.get("task")
    if not isinstance(tasks, list) or not tasks:
        raise ValidationError("interface.json 的 task 必须是数组（PI v2）")
    entries: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            raise ValidationError("task 数组元素必须是对象")
        for key in ("name", "entry"):
            if not isinstance(task.get(key), str) or not task[key]:
                raise ValidationError(f"task 元素缺少 {key}")
        entries.append(task["entry"])

    agent = interface.get("agent")
    if agent is not None:
        if not isinstance(agent, dict):
            raise ValidationError("agent 必须是对象")
        if not isinstance(agent.get("child_exec"), str) or not agent["child_exec"]:
            raise ValidationError("agent 缺少 child_exec")
        args = agent.get("child_args")
        if not isinstance(args, list) or not args or not all(isinstance(item, str) for item in args):
            raise ValidationError("agent.child_args 必须是非空字符串数组")
        script = next((item for item in args if item.endswith(".py")), None)
        if script is None:
            raise ValidationError("agent.child_args 中未找到 Python 入口脚本")
        script_path = (ROOT / script.lstrip("./")).resolve()
        if not script_path.is_file():
            raise ValidationError(f"Agent 入口脚本不存在: {script}")

    return bundle_roots, entries


OPTION_TYPES = ("select", "checkbox", "input", "hotkey", "switch")
# 配置项允许改写的 pipeline 字段。本项目只用 custom_action_param 传运行参数，
# 写错字段名框架会默默忽略，所以在这里白名单化。
OPTION_OVERRIDE_FIELDS = ("custom_action_param", "enabled", "timeout", "expected", "next")


def validate_options(
    interface: dict[str, Any], nodes: dict[str, dict[str, Any]]
) -> int:
    """校验 interface.json 的 option，返回配置项个数。

    option 写错不会报错，只会在客户端界面上少一个开关，或者 pipeline_override
    改到不存在的节点——届时所有实际效果都消失，排查起来极难。
    """
    options = interface.get("option")
    if options is None:
        return 0
    if not isinstance(options, dict):
        raise ValidationError("interface.json 的 option 必须是对象")

    for name, option in options.items():
        origin = f"option {name}"
        if not isinstance(option, dict):
            raise ValidationError(f"{origin} 必须是对象")
        kind = option.get("type", "select")
        if kind not in OPTION_TYPES:
            raise ValidationError(f"{origin} 的 type 非法: {kind!r}")
        cases = option.get("cases")
        if not isinstance(cases, list) or not cases:
            raise ValidationError(f"{origin} 的 cases 必须是非空数组")

        case_names: set[str] = set()
        for case in cases:
            if not isinstance(case, dict):
                raise ValidationError(f"{origin} 的 cases 元素必须是对象")
            case_name = case.get("name")
            if not isinstance(case_name, str) or not case_name:
                raise ValidationError(f"{origin} 的 cases 元素缺少 name")
            if case_name in case_names:
                raise ValidationError(f"{origin} 的 case 重名: {case_name}")
            case_names.add(case_name)
            _validate_override(f"{origin} 的 case {case_name}", case, nodes)

        default_case = option.get("default_case")
        if default_case is None:
            continue
        wanted = default_case if isinstance(default_case, list) else [default_case]
        if not all(isinstance(item, str) for item in wanted):
            raise ValidationError(f"{origin} 的 default_case 必须是字符串或字符串数组")
        unknown = [item for item in wanted if item not in case_names]
        if unknown:
            raise ValidationError(f"{origin} 的 default_case 引用了不存在的 case: {', '.join(unknown)}")

    # 任务引用到的配置项必须真的存在，否则界面上什么都不显示。
    for task in interface.get("task", []):
        for referenced in task.get("option", []) or []:
            if referenced not in options:
                raise ValidationError(
                    f"任务 {task.get('name')} 引用了未定义的配置项: {referenced}"
                )

    return len(options)


def _validate_override(
    origin: str, holder: dict[str, Any], nodes: dict[str, dict[str, Any]]
) -> None:
    """检查一段 pipeline_override：节点必须存在，字段与取值都在允许范围内。"""
    override = holder.get("pipeline_override")
    if override is None:
        return
    if not isinstance(override, dict):
        raise ValidationError(f"{origin} 的 pipeline_override 必须是对象")
    for node_name, patch in override.items():
        if node_name not in nodes:
            raise ValidationError(f"{origin} 的 pipeline_override 指向不存在的节点: {node_name}")
        if not isinstance(patch, dict):
            raise ValidationError(f"{origin} 的 pipeline_override[{node_name}] 必须是对象")
        unknown = sorted(set(patch) - set(OPTION_OVERRIDE_FIELDS))
        if unknown:
            raise ValidationError(
                f"{origin} 的 pipeline_override[{node_name}] 含未支持字段: {', '.join(unknown)}"
            )
        if "custom_action_param" in patch:
            _validate_action_param(f"{origin} 的 {node_name}", patch["custom_action_param"])


def _validate_action_param(origin: str, param: Any) -> None:
    """custom_action_param 必须是对象，且 user_id（如有）必须是非负整数。"""
    if not isinstance(param, dict):
        raise ValidationError(f"{origin} 的 custom_action_param 必须是对象")
    if "user_id" not in param:
        return
    user_id = param["user_id"]
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 0:
        raise ValidationError(f"{origin} 的 custom_action_param.user_id 必须是非负整数")


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


def load_pipeline(bundle_root: Path) -> tuple[dict[str, dict[str, Any]], int]:
    """加载资源包下全部 Pipeline JSON，返回（节点表，文件数）。"""
    pipeline_dir = bundle_root / "pipeline"
    files = sorted(pipeline_dir.glob("*.json"))
    if not files:
        raise ValidationError(f"{pipeline_dir.relative_to(ROOT)} 下没有 Pipeline 文件")

    nodes: dict[str, dict[str, Any]] = {}
    for path in files:
        document = read_json(path)
        for name, node in document.items():
            if not isinstance(node, dict):
                raise ValidationError(f"{path.name} 的节点 {name} 必须是对象")
            if name in nodes:
                raise ValidationError(f"节点名重复定义: {name}（{path.name}）")
            nodes[name] = {**node, "__file__": path.name}
    return nodes, len(files)


def validate_node(name: str, node: dict[str, Any]) -> None:
    origin = f"{node['__file__']}:{name}"

    forbidden = FORBIDDEN_FIELDS & node.keys()
    if forbidden:
        raise ValidationError(f"{origin} 含协议不支持的字段: {', '.join(sorted(forbidden))}")

    recognition = node.get("recognition", "DirectHit")
    if recognition not in VALID_RECOGNITION:
        raise ValidationError(f"{origin} 的 recognition 非法: {recognition!r}")

    action = node.get("action", "DoNothing")
    if action not in VALID_ACTION:
        raise ValidationError(
            f"{origin} 的 action 非法: {action!r}（合法值见 MaaFramework JActionType）"
        )

    for key, expected in ACTION_REQUIRED_PARAMS.get(action, {}).items():
        if key not in node:
            raise ValidationError(f"{origin} 的动作 {action} 缺少参数 {key}")
        value = node[key]
        if expected == "str" and not (isinstance(value, str) and value):
            raise ValidationError(f"{origin} 的 {action}.{key} 必须是非空字符串")
        if expected == "list[int]":
            if not (
                isinstance(value, list)
                and value
                and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
            ):
                raise ValidationError(f"{origin} 的 {action}.{key} 必须是整数数组")

    if action == "InputText" and "text" in node:
        raise ValidationError(f"{origin} 使用了 text，协议要求字段名为 input_text")

    if recognition == "Custom" and not isinstance(node.get("custom_recognition"), str):
        raise ValidationError(f"{origin} 使用 Custom 识别但缺少 custom_recognition")

    for key in ("timeout", "pre_delay", "post_delay", "rate_limit", "repeat", "max_hit"):
        if key in node and not isinstance(node[key], int):
            raise ValidationError(f"{origin} 的 {key} 必须是整数")

    if "enabled" in node and not isinstance(node["enabled"], bool):
        raise ValidationError(f"{origin} 的 enabled 必须是布尔值")


def collect_references(nodes: dict[str, dict[str, Any]]) -> dict[str, set[str]]:
    """收集每个节点引用的后继节点（含 next / on_error / interrupt）。"""
    references: dict[str, set[str]] = {}
    for name, node in nodes.items():
        targets: set[str] = set()
        for field in ("next", "on_error", "interrupt"):
            values = node.get(field, [])
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                raise ValidationError(f"{node['__file__']}:{name} 的 {field} 必须是字符串或字符串数组")
            targets.update(strip_jump_back(item) for item in values)
        references[name] = targets
    return references


def find_unreachable(
    nodes: dict[str, dict[str, Any]],
    references: dict[str, set[str]],
    entries: Iterable[str],
) -> list[str]:
    """从任务入口出发做可达性分析，找出从没被走到的节点。"""
    seen: set[str] = set()
    stack = [entry for entry in entries if entry in nodes]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(references.get(current, set()))
    return sorted(set(nodes) - seen)


def collect_custom_actions(nodes: dict[str, dict[str, Any]]) -> set[str]:
    return {
        node["custom_action"]
        for node in nodes.values()
        if node.get("action") == "Custom" and isinstance(node.get("custom_action"), str)
    }


def collect_registered_actions() -> set[str]:
    """从 agent/ 源码里找出所有 @...custom_action("name") 注册名。"""
    pattern = re.compile(r"custom_action\(\s*[\"']([^\"']+)[\"']")
    registered: set[str] = set()
    for path in AGENT_DIR.rglob("*.py"):
        registered.update(pattern.findall(path.read_text(encoding="utf-8")))
    return registered


def collect_agent_referenced_nodes(candidates: Iterable[str]) -> set[str]:
    """找出被 Agent 代码引用到的节点名。

    Agent 会用 context.run_recognition("PanelFullOcr", image) 这类方式复用节点，
    这些节点不出现在任何 next 里，因此不能算作"不可达"。
    这里要求节点名以字符串字面量出现在 agent/ 源码中——包括函数默认参数和常量，
    比只匹配调用点更贴合实际写法。
    """
    sources = "\n".join(path.read_text(encoding="utf-8") for path in AGENT_DIR.rglob("*.py"))
    referenced: set[str] = set()
    for name in candidates:
        if f'"{name}"' in sources or f"'{name}'" in sources:
            referenced.add(name)
    return referenced


# --------------------------------------------------------------------------- #
# 任务配置
# --------------------------------------------------------------------------- #


def validate_task_config(config: dict[str, Any]) -> int:
    for key in TASK_CONFIG_LISTS:
        value = config.get(key)
        if not isinstance(value, list) or not value:
            raise ValidationError(f"config/tasks.json 的 {key} 必须是非空数组")
        if not all(isinstance(item, str) and item.strip() for item in value):
            raise ValidationError(f"config/tasks.json 的 {key} 必须只包含非空字符串")

    timing = config.get("timing")
    if not isinstance(timing, dict):
        raise ValidationError("config/tasks.json 缺少 timing 对象")

    required_timing = {
        "min_task_delay_seconds": (int, float),
        "max_task_delay_seconds": (int, float),
        "browse_duration_seconds": (int, float),
        "jump_duration_seconds": (int, float),
        "max_empty_rounds": (int,),
    }
    for key, types in required_timing.items():
        value = timing.get(key)
        if isinstance(value, bool) or not isinstance(value, types):
            raise ValidationError(f"config/tasks.json 的 timing.{key} 类型不合法")

    if timing["min_task_delay_seconds"] > timing["max_task_delay_seconds"]:
        raise ValidationError("timing.min_task_delay_seconds 不能大于 max_task_delay_seconds")

    app = config.get("app")
    if not isinstance(app, dict):
        raise ValidationError("config/tasks.json 缺少 app 对象")
    for key in ("package", "activity"):
        value = app.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"config/tasks.json 的 app.{key} 必须是非空字符串")
    user_id = app.get("user_id")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 0:
        raise ValidationError("config/tasks.json 的 app.user_id 必须是非负整数")

    return sum(len(config[key]) for key in TASK_CONFIG_LISTS)


def compile_agent() -> int:
    count = 0
    for path in AGENT_DIR.rglob("*.py"):
        py_compile.compile(str(path), doraise=True)
        count += 1
    if count == 0:
        raise ValidationError("agent/ 下没有 Python 文件")
    return count


# --------------------------------------------------------------------------- #


def validate() -> list[str]:
    messages: list[str] = []

    interface = read_json(INTERFACE_FILE)
    bundle_roots, entries = validate_interface(interface)

    all_nodes: dict[str, dict[str, Any]] = {}
    file_count = 0
    for root in bundle_roots:
        nodes, count = load_pipeline(root)
        file_count += count
        for name in nodes:
            if name in all_nodes:
                raise ValidationError(f"跨资源包节点名重复: {name}")
        all_nodes.update(nodes)

    for name, node in all_nodes.items():
        validate_node(name, node)

    option_count = validate_options(interface, all_nodes)

    references = collect_references(all_nodes)
    dangling = sorted({target for targets in references.values() for target in targets} - set(all_nodes))
    if dangling:
        raise ValidationError(f"Pipeline 引用了未定义节点: {', '.join(dangling)}")

    for entry in entries:
        if entry not in all_nodes:
            raise ValidationError(f"任务入口不存在: {entry!r}")

    unreachable = find_unreachable(all_nodes, references, entries)
    # Agent 按名字直接复用的节点（OCR 节点）不算不可达。
    unreachable = [name for name in unreachable if name not in collect_agent_referenced_nodes(unreachable)]
    if unreachable:
        raise ValidationError(f"存在从任务入口不可达的节点: {', '.join(unreachable)}")

    used_actions = collect_custom_actions(all_nodes)
    registered = collect_registered_actions()
    missing_actions = sorted(used_actions - registered)
    if missing_actions:
        raise ValidationError(
            f"Pipeline 引用了未注册的自定义动作: {', '.join(missing_actions)}"
            f"（已注册: {', '.join(sorted(registered)) or '无'}）"
        )

    rule_count = validate_task_config(read_json(TASK_CONFIG_FILE))
    agent_files = compile_agent()

    messages.append(f"Interface: PI v2 校验通过，{len(entries)} 个任务入口 / {option_count} 个配置项")
    messages.append(
        f"Pipeline: {file_count} 个文件 / {len(all_nodes)} 个节点 / "
        f"{sum(len(value) for value in references.values())} 个引用"
    )
    messages.append(f"自定义动作: {', '.join(sorted(used_actions)) or '无'}")
    messages.append(f"名单: {rule_count} 条规则")
    messages.append(f"Agent: {agent_files} 个 Python 文件编译通过")
    return messages


def main() -> int:
    try:
        for message in validate():
            print(f"OK {message}")
    except (OSError, ValidationError, py_compile.PyCompileError) as error:
        print(f"ERROR {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
