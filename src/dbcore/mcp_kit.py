"""MCP 运行时外壳 —— 把 dbcore 的 Result 协议接到 MCP SDK 上。

职责只有三条：

  1. 兼容 MCP SDK 1.x（`FastMCP`）与 2.x（`MCPServer`）—— 两个版本入口不同
  2. 把工具函数的返回值包成 `dbt.result/v1` 信封（Result → dict）
  3. **任何异常都变成 ok=false 的信封**，绝不让工具异常拖垮 MCP 会话

用法：

    from dbcore.mcp_kit import make_server, register

    mcp = make_server("sql-review", __version__)
    register(mcp, "sql-review", my_tool)

为什么放在 dbcore 而不是独立包：
    它只依赖本包的 protocol，且是每个 MCP 都要用的横切适配层。
    独立成包时，一个 MCP 要装 4 个包，其中 1 个只有 142 行 ——
    脚手架的维护成本超过了它承载的逻辑。

为什么 mcp 是延迟导入：
    `dbcore` 同时服务于**非 MCP** 场景（如 change_risk 只做风险评估不暴露工具）。
    若在模块顶层 `import mcp`，这些场景会被迫安装 MCP SDK。
    真正的依赖声明在 `dbt-core[mcp]` 这个 extra 里。
"""

from __future__ import annotations

import functools
import time
from typing import Any, Callable

from dbcore.protocol import PROTOCOL_VERSION, Result, fail as make_fail

_SERVER_CLS: Any = None


def _server_cls() -> Any:
    """按需解析 MCP SDK 的服务端类，并缓存。

    1.x 叫 FastMCP，2.x 改名 MCPServer，两代入口不同 —— 这是唯一需要兼容的差异。
    """
    global _SERVER_CLS
    if _SERVER_CLS is not None:
        return _SERVER_CLS
    try:                                   # MCP SDK 2.x
        from mcp.server.mcpserver import MCPServer as cls  # type: ignore
    except ImportError:
        try:                               # MCP SDK 1.x
            from mcp.server.fastmcp import FastMCP as cls  # type: ignore
        except ImportError as exc:         # pragma: no cover
            raise ImportError(
                "未安装 MCP 依赖。执行：pip install \"dbt-core[mcp]\""
            ) from exc
    _SERVER_CLS = cls
    return cls


def make_server(name: str, version: str = PROTOCOL_VERSION) -> Any:
    return _server_cls()(name, version=version)


def wrap(mcp_name: str, tool_name: str | None = None) -> Callable:
    """把返回 Result 的函数包装成 MCP 工具。

    自动完成三件事：Result → dict、异常 → ok=false 信封、计时。
    """

    def deco(fn: Callable) -> Callable:
        name = tool_name or fn.__name__

        @functools.wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> dict:
            t0 = time.time()
            try:
                r = fn(*args, **kwargs)
                if not isinstance(r, Result):
                    # 允许工具直接返回裸 dict：补齐成合法信封，tier 只能是 tier0
                    r = Result(ok=True, mcp=mcp_name, tool=name, data=r,
                               verdict="pass", confidence="low", tier="tier0")
                r.took_ms = int((time.time() - t0) * 1000)
                return r.to_dict()
            except Exception as exc:                       # 绝不让工具异常拖垮 MCP 会话
                # ToolError 会被 fail() 原样保留错误码；其余归 E_QUERY（执行期异常），
                # 不能归 E_ARGS —— 那会让"代码崩了"看起来像"用户传错了参数"。
                r = make_fail(mcp_name, name, exc, code="E_QUERY",
                              summary=f"工具执行异常：{type(exc).__name__}: {exc}")
            r.took_ms = int((time.time() - t0) * 1000)
            return r.to_dict()

        inner.__name__ = name
        inner.__wrapped_result__ = True                    # 供 CLI 区分已包装的函数
        return inner

    return deco


def register(mcp: Any, mcp_name: str, func: Callable, tool_name: str | None = None) -> Callable:
    """把函数注册为 MCP 工具（自动套 Result 包装）。"""
    wrapped = wrap(mcp_name, tool_name)(func)
    try:
        mcp.tool(wrapped)
    except TypeError:                                       # 部分版本要求显式 name
        mcp.tool(name=tool_name or func.__name__)(wrapped)
    return wrapped
