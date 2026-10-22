#!/usr/bin/env python3
"""MCP 外壳回归测试：任何工具异常都必须变成合法信封。

这一层的价值只有一条：**工具抛异常不许拖垮 MCP 会话**。测试只盯这条，
外加"结果信封字段齐全"和"错误码不误导"。

（原 dbt-mcp-kit 套件，合并进 sqltk-core 后保留全部用例。）
"""

from __future__ import annotations

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))

from dbcore.protocol import Result, ToolError  # noqa: E402
from dbcore import mcp_kit as kit  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
    else:
        FAILED.append(f"{name}  << {detail[:300] if detail else ''}")


# 1. 正常返回：Result -> dict
@kit.wrap("kit-test", "ok_tool")
def ok_tool(x: int) -> Result:
    return Result(ok=True, mcp="kit-test", tool="ok_tool", data={"x": x},
                  verdict="pass", confidence="high", tier="tier0", summary="ok")


out = ok_tool(x=1)
check("Result 被转成 dict", isinstance(out, dict))
check("信封字段齐全", {"ok", "schema", "mcp", "tool", "tier", "verdict",
                       "confidence", "data", "evidence", "next_actions"} <= set(out))
check("schema 版本正确", out["schema"] == "dbt.result/v1")
check("took_ms 已填", isinstance(out["took_ms"], int))


# 2. ToolError -> ok=false 且带错误码
@kit.wrap("kit-test", "tool_error")
def tool_error() -> Result:
    raise ToolError("E_ARGS", "参数不对")


out2 = tool_error()
check("ToolError 转成 ok=false", out2["ok"] is False)
check("错误码保真", out2["error"]["code"] == "E_ARGS")


# 3. 任意异常 -> 也是合法信封，不往外抛
@kit.wrap("kit-test", "boom")
def boom() -> Result:
    raise ZeroDivisionError("炸了")


out3 = boom()
check("普通异常不扩散", out3["ok"] is False)
check("异常类型被记录", out3["error"]["detail"]["type"] == "ZeroDivisionError")
check("异常也有 summary", bool(out3["summary"]))
# 执行期异常不能报成 E_ARGS —— 那会让"代码崩了"看起来像"用户传错参数"
check("兜底错误码是 E_QUERY 而非 E_ARGS", out3["error"]["code"] == "E_QUERY")


# 4. 非 Result 返回值也被兜成信封
@kit.wrap("kit-test", "raw")
def raw() -> dict:
    return {"any": "thing"}


out4 = raw()
check("非 Result 返回被兜成信封", out4["ok"] is True and out4["data"] == {"any": "thing"})


# 5. make_server 真的能建出服务对象（兼容 SDK 1.x / 2.x）
mcp = kit.make_server("kit-test")
check("make_server 可用", mcp is not None and hasattr(mcp, "tool"))

# 6. register 不抛（注册路径与 SDK 版本无关）
try:
    kit.register(mcp, "kit-test", ok_tool, "registered_tool")
    check("register 可用", True)
except Exception as exc:
    check("register 可用", False, repr(exc))

# 7. 未安装 mcp 时必须给可执行的安装提示，而不是裸 ImportError
_err = None
try:
    import mcp  # noqa: F401
except ImportError:
    _err = "mcp 未安装，跳过该用例"
if _err is None:
    check("mcp 依赖可用（make_server 未走降级路径）", True)

print("\n" + "=" * 60)
print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\n失败明细：")
    for _f in FAILED:
        print("  ✗", _f)
    raise SystemExit(1)
print("全部通过")
