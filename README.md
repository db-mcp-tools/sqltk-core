# dbt-core

**SQL 工具链运行时内核** · Library（不是 MCP）

> 所有 MCP 共享的地基：统一信封、方言归一、SQL 指纹、只读连库、AST 结构化、执行计划归一。
> 它**不知道**任何具体 MCP 的业务 —— 业务判断一律留在各自 MCP 仓库。

## 内容

| 模块 | 职责 |
|---|---|
| `dbcore.protocol` | `dbt.result/v1` 信封：`ok / verdict / confidence / tier / data / evidence / next_actions / tier_upgrade` |
| `dbcore.dialect` | 方言归一、语句切分、`unwrap_parens` 等 AST 小工具 |
| `dbcore.ids` | `instance_id`（密码不参与哈希）、`sql_fingerprint`、DSN 脱敏 |
| `dbcore.guard` | 只读断言、单语句断言（连库前的硬闸） |
| `dbcore.connectors` | 只读会话执行、`run_in_transaction(commit=False)` 干跑 |
| `dbcore.extract` | SQL → 结构化对象（表/列/谓词/聚合/排序/分页/JOIN/子查询） |
| `dbcore.plan` | MySQL/PG/Oracle 执行计划 → 统一 `PlanNode`，`diff_plans` 量化收益 |
| `dbcore.report` | 信封 → markdown（先结论后证据） |

## 依赖

只依赖 `sqlglot`（纯 Python）。数据库驱动是可选 extra：

```bash
pip install ./dbt-core
pip install "./dbt-core[all]"     # pymysql + psycopg + oracledb
```

## 为什么它是共享库而不是 MCP

协议信封、方言解析、指纹算法这些是**所有 MCP 的共同语言**，放进任何一个 MCP 都会造成其余 5 个反向依赖它。
共享库版本的升级是严格的向后兼容：加字段可以，改语义要升主版本。

## 开发

```bash
python tests/test_dbcore.py
```
