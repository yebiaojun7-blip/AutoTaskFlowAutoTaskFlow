"""MaaFramework AgentServer 启动入口。

由 interface.json 的 agent.child_args 拉起：
    python ./agent/main.py <socket_id>
"""

from __future__ import annotations

import logging
import sys


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main() -> int:
    _setup_logging()
    logger = logging.getLogger("autotaskflow")

    if len(sys.argv) < 2:
        print("Usage: python main.py <socket_id>", file=sys.stderr)
        print("socket_id 由 Interface/AgentIdentifier 提供", file=sys.stderr)
        return 1

    try:
        from maa.agent.agent_server import AgentServer
        from maa.toolkit import Toolkit
    except ImportError as exc:
        logger.error("未安装 maa 运行时（pip install maa），无法启动 AgentServer：%s", exc)
        return 2

    from agent import actions

    Toolkit.init_option("./")

    socket_id = sys.argv[-1]
    actions.register_all(AgentServer)

    logger.info("AgentServer 启动，socket_id=%s", socket_id)
    AgentServer.start_up(socket_id)
    try:
        AgentServer.join()
    finally:
        AgentServer.shut_down()
        logger.info("AgentServer 已关闭")
    return 0


if __name__ == "__main__":
    sys.exit(main())
