"""MCP 运行时外壳 —— 把 dbcore 的 Result 协议接到 MCP SDK 上。

职责四条：

  1. 兼容 MCP SDK 1.x（`FastMCP`）与 2.x（`MCPServer`）—— 两个版本入口不同
  2. 把工具函数的返回值包成 `dbt.result/v1` 信封（Result → dict）
  3. **任何异常都变成 ok=false 的信封**，绝不让工具异常拖垮 MCP 会话
  4. `cli()`：同一个入口既当 stdio server，也当命令行调试口（`tools` / `call`）

用法：

    from dbcore.mcp_kit import cli, make_server, register

    mcp = make_server("sql-review", __version__)
    register(mcp, "sql-review", my_tool)

    def main() -> int:
        return cli("sql-review", TOOLS, mcp)

为什么放在 dbcore 而不是独立包：
    它只依赖本包的 protocol，且是每个 MCP 都要用的横切适配层。
    独立成包时，一个 MCP 要装 4 个包，其中 1 个只有 142 行 ——
    脚手架的维护成本超过了它承载的逻辑。

为什么 mcp 是延迟导入：
    `dbcore` 同时服务于**非 MCP** 场景（如 change_risk 只做风险评估不暴露工具）。
    若在模块顶层 `import mcp`，这些场景会被迫安装 MCP SDK。
    真正的依赖声明在 `sqltk-core[mcp]` 这个 extra 里。
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
                "未安装 MCP 依赖。执行：pip install \"sqltk-core[mcp]\""
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


# ------------------------------------------------------------------
# 命令行入口
# ------------------------------------------------------------------

USAGE = """\
用法：
  {cmd}                              起 stdio server（MCP 客户端就是这么调的）
  {cmd} tools [--json]               列出全部工具
  {cmd} call <工具名> --args '<json>' 本地调一次，打印 dbt.result/v1 信封

例：
  {cmd} tools
  {cmd} call list_rules --args '{{}}'
  {cmd} call review_sql --args '{{"sql": "SELECT * FROM t WHERE 1=1"}}'

退出码：0 = ok，1 = 工具返回 ok=false，2 = 用法错误。
"""


def cli(mcp_name: str, tools: dict[str, Callable], mcp: Any = None,
        argv: list[str] | None = None) -> int:
    """MCP 进程的命令行入口。

    为什么需要：各仓 README 都写着「命令行调试（不挂客户端也能用）：
    `mcp-sql-review tools` / `call <tool> --args ...`」，但入口只有一句
    `mcp.run()` —— argv 被完全忽略，敲 `tools` 实际是起了一个等待 stdin 的
    server，终端看起来就是卡死。文档承诺的东西必须真的存在。

    实现放在 mcp_kit 而不是每个仓各写一份：6 个 MCP 的差别只有名字和工具表。
    """
    import inspect
    import json
    import sys

    args = list(sys.argv[1:] if argv is None else argv)

    def usage(code_ok: bool = False) -> int:
        prog = "python -m " + mcp_name.replace("-", "_")
        print(USAGE.format(cmd=prog), end="")
        return 0 if code_ok else 2

    # 无参数 = 正常起 server。以 - 开头的是 SDK 自己的开关（如 --transport stdio），
    # 原样交给 mcp.run()，不要抢它的参数。
    if not args or (args[0].startswith("-") and args[0] not in ("-h", "--help")):
        if mcp is None:
            print("没有可运行的 server 实例", file=sys.stderr)
            return 2
        mcp.run()
        return 0

    sub = args[0]
    if sub in ("help", "-h", "--help"):
        return usage(code_ok=True)

    if sub == "tools":
        items = sorted(tools.items())
        if "--json" in args:
            print(json.dumps(
                [{"tool": n, "doc": (inspect.getdoc(f) or "").splitlines()[0]
                  if inspect.getdoc(f) else ""} for n, f in items],
                ensure_ascii=False, indent=2))
            return 0
        print(f"[{mcp_name}] {len(items)} 个工具\n")
        for name, fn in items:
            doc = (inspect.getdoc(fn) or "").splitlines()
            print(f"  {name:26s} {doc[0] if doc else ''}")
        return 0

    if sub == "call":
        rest = args[1:]
        if not rest:
            print("缺少工具名。用法：call <工具名> --args '<json>'\n", file=sys.stderr)
            return usage()
        name = rest[0]
        if name not in tools:
            print(f"未知工具 {name!r}。可用的有：{', '.join(sorted(tools))}", file=sys.stderr)
            return 2
        payload: dict = {}
        if "--args" in rest:
            i = rest.index("--args")
            raw = rest[i + 1] if i + 1 < len(rest) else ""
            try:
                payload = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError as exc:
                print(f"--args 不是合法 JSON：{exc}", file=sys.stderr)
                return 2
            if not isinstance(payload, dict):
                print("--args 必须是 JSON 对象", file=sys.stderr)
                return 2
        out = wrap(mcp_name, name)(tools[name])(**payload)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1

    print(f"未知子命令 {sub!r}\n", file=sys.stderr)
    return usage()
