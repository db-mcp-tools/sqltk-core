# dbt-core 维护约定

## 这个包不许知道业务

`dbcore` 里不许出现 `sql_review` / `dbwatch` 等任何 MCP 包名。
一旦出现，就说明抽象层放错了，把那段逻辑挪回对应 MCP。

## 协议改动流程

1. 改 `src/dbcore/protocol.py`（加字段/加枚举/加错误码，只许向后兼容）
2. 同步改 `docs/PROTOCOL.md`（在工具链根目录）
3. 在 `tests/test_dbcore.py` 加断言
4. 升次版本号，然后按顺序升 6 个 MCP 的依赖下限

## 已知的 sqlglot 30 坑（加规则/加提取时一定会再遇到）

- `FROM` 的 arg key 是 `from_`，不是 `from`
- `LIMIT n OFFSET m` 的结构是 `Select.args["offset"] = Offset(expression=Literal)`
- `DATE()` 被规范化成 `TsOrDsToDate`
- `SUBSTRING` 的参数在 `this/start/length`，不在 `expressions`
- **`exp.And` 是 `exp.Func` 的子类** —— 判断「字段上套函数」必须先排除
  Connector / Predicate / Binary
- `And` / `Or` 是 Binary（`this` / `expression`），没有 `expressions` 列表
- `COUNT(DISTINCT x)` 的 inner 是 `Distinct(Column)`，要先展开

## 连库的红线

- `connectors` 里任何执行路径必须经 `guard.assert_readonly` + 单语句断言
- 写操作只能走 `run_in_transaction(commit=False)`（干跑）
- 驱动是可选依赖，缺失时必须给清晰的安装提示，不许抛原始 ImportError
