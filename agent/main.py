"""MaaFramework AgentServer 启动入口。

由 interface.json 的 agent.child_args 拉起：
    python ./agent/main.py <socket_id>
"""

from __future__ import annotations

import logging
import os
import sys

# 以脚本路径启动时 sys.path[0] 是 agent/ 自身，`from agent import ...` 找不到包；
# 把 PI 根目录（agent/ 的上一级）补进去，脚本方式和 -m 方式都能跑
_PI_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PI_ROOT not in sys.path:
    sys.path.insert(0, _PI_ROOT)


def _setup_logging() -> None:
    # 必须写 stdout。basicConfig 默认走 stderr，而客户端把 agent 的 stderr 一律当成
    # AgentError（红色报错档）——「第 2 轮：执行…」这种正常进度会全变成错误，
    # 真正的 traceback 也就淹没在里面了。stderr 只留给解释器与加载器自己的异常。
    #
    # 级别前缀要留：客户端认的就是行首的 `[LEVEL]`，把它抬成日志条目的级别列，
    # 再从正文里剥掉。写别的格式，级别就只剩正文里一串没人解析的方括号。
    #
    # 不带时间戳：客户端日志面板每条本来就有一列时间，正文再带一遍就是
    # 「11:33:10 2026-10-22 11:33:10,966 [INFO] …」两段时间。
    #
    # 不带 logger 名：本项目只有一个 logger，每行重复一遍既占宽又没信息量。
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="[%(levelname)s] %(message)s",
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
