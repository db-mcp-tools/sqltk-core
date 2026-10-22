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
- **`WITH` 子句的 arg key 是 `with_`（带下划线），不是 `with`** —— 取 CTE 别名必须
  `ast.args.get("with_") or ast.args.get("with")` 双 key 兜底，否则 `cte_names` 恒为空，
  于是 `WITH t AS (...) SELECT * FROM t` 会把 CTE 名当成真实表（JOIN002 曾因此误报 blocker）
- 判断语句类型别用 `kind` 字符串跨方言比对：`MERGE` / `OPTIMIZE` / `GRANT` 在 mysql 方言下
  都 fallback 成 `other`。用 `isinstance(stmt.ast, exp.Xxx)` 更稳（见 `guard.unsafe_in_transaction`）
- `exp.Update` **没有** `.limit()` 方法（那是 `exp.Select` 的）；给 UPDATE/DELETE 设 LIMIT 要
  `ast.set("limit", exp.Limit(expression=exp.Literal.number(n)))`，且 `set()` 返回 None（原地改）

## 连库的红线

- `connectors` 里任何执行路径必须经 `guard.assert_readonly` + 单语句断言
- 写操作只能走 `run_in_transaction(commit=False)`（干跑）
- **`commit=False` 的语句必须过 `guard.unsafe_in_transaction`**：只有 INSERT/UPDATE/DELETE/MERGE
  能在事务里可靠回滚，DDL 在 MySQL/Oracle 隐式提交。这条校验写在 `run_in_transaction` 内部，
  不要依赖调用方先判对类型 —— 上层一个 `startswith` 被注释绕过，就会变成"改完库还告诉用户没改"
- 驱动是可选依赖，缺失时必须给清晰的安装提示，不许抛原始 ImportError
