# MDL 问答与业务知识

MDL 定义可查询的模型、字段、关系和权限。知识条目补充业务口径、术语和经过审核的 SQL 示例，帮助问数智能体理解问题。当前版本支持四类条目：

| 类型 | 内容 | 示例 |
| --- | --- | --- |
| 问答 SQL | 问题、解释和基于 MDL 模型的只读 SQL | “无人机数量”应按什么字段去重、如何过滤设备类型 |
| 业务规则 | 指标定义、状态含义、统计范围等文字规则 | `domain=0` 表示无人机 |
| 字典术语 | 术语含义及业务别名 | “在役”表示当前可调度设备 |
| 分析规则 | 指标、文字条件、可执行结果断言和报告要求 | 设备数不能为负，并注明统计范围 |

## 使用步骤

1. 管理员在 MDL 工作台选择数据源，先保存需要使用的模型和字段。
2. 打开 MDL 工作台内的“知识库”页签，创建条目，填写标题、内容及可选的检索别名。问答 SQL 还需填写问题和 SQL。
3. 绑定条目涉及的全部模型和字段。业务文字涉及的敏感字段也必须绑定，才能按问数账号权限过滤。
4. 保存草稿，检查业务口径，再点击审核。问答 SQL 默认经过 Wren 规划和数据库 `EXPLAIN`；需要结果级回归时可填写 `expected_result`（最多 100 行），审核会在当前数据源执行受限查询并严格比较列名和行值。分析规则可在 `rule.checks` 中配置字段级断言，问数执行 SQL 后会用真实结果校验这些规则。
5. 用检索预览检查不同问法的匹配情况。预览会标出草稿和过期状态；问数实际使用时只引用已审核且仍适用的条目。

编辑已审核内容后，条目自动回到草稿。MDL 的模型、字段、关系或描述发生变化后，原审核条目会标记为过期，需要重新审核。权限变化会在每次检索时重新检查。

“导出知识 JSON”可下载当前本地编辑内容。版本冲突时可以选择“备份并刷新”：确认后先下载本地备份，再读取服务器最新版本，不会自动覆盖服务器内容。当前版本支持导出，尚未提供知识 JSON 导入功能。

## 问数时的行为

数据库问数入口自动加入当前账号可见的 MDL 显示名称、业务描述和语义类型，并按问题检索最多四条参考知识。问答 SQL 在引用前再次经过当前账号的 MDL 权限校验。草稿、过期条目、其他数据源条目和绑定了不可见字段的条目不会进入上下文。

参考知识帮助生成 SQL 和解释口径。当前数量、金额、时间范围等动态结果仍由本次查询获得，不能把历史答案中的数字当作当前结果。只有 `qa_sql` 明确打开 `reuse_on_exact_match`，且问题标准化后不存在不同 SQL 的冲突、权限和 MDL 指纹均有效时，运行时才会复用审核 SQL。冲突命中拒绝执行；多个条目使用同一条 SQL 时可以复用，并记录实际使用的条目 ID、标题、审核人及审核时间。

每次 SQL 调用先清空此前的查询证据，执行成功后记录本次 SQL、结果列、前 20 行、取回行数和断言报告。最终回答没有成功查询证据时不保留模型生成的数据结论；精确复用的结果使用确定性文字和实际结果表。断言失败、缺列、没有可执行断言都不能被描述为“校验通过”。知识存储损坏会阻止执行模型 SQL，避免静默丢弃审核口径。其他生成 SQL 仍需业务样例验收，不能由这些机制证明其与审核 SQL 在任意问法下等价。

分析规则的 `checks` 对全部取回记录执行，规则检索不受提示词 top 4 的截断影响；`conditions` 和 `guidance` 作为说明，`report_requirements` 进入提示词并在查询证据中列为核对事项。数值比较支持数据库 Decimal，拒绝 NaN/Infinity。达到连接器 1001 行取数上限时只报告至少取回多少行，将原本通过的断言标为未能完成全部校验；聚合 SQL 只返回一行计数时可验证该计数。

第一版使用中文片段、关键词、别名、词典术语和简化 IDF 权重匹配，无需 embedding 模型或新依赖。规范化后的问题或别名精确命中优先于长文本重叠；词典别名会扩展普通检索评分，但不会触发精确 SQL 复用。它不保证识别所有同义问法，可通过别名和术语补充常见表达。数据库 `EXPLAIN` 能验证 SQL 可规划，不能证明统计口径正确；计数去重、时间范围和状态过滤应优先使用 `expected_result` 做 SQL 回归，并用分析规则 `checks` 校验本次查询结果，仍需要业务审核。

## 通用知识空间绑定

DB-GPT 原有 `knowledge_space` 可以用于 MDL 问数，但普通 MDL 用户只能连接已经在 MDL 管理端审核绑定到当前数据源的空间。绑定记录独立保存在 `mdl.knowledge_spaces` 中，并记录空间指纹、MDL 指纹、字段绑定和可选用户/角色限制：

```json
{
  "title": "订单业务文档",
  "space_name": "order_rules",
  "models": {"orders": ["amount", "status"]},
  "roles": ["analyst"]
}
```

绑定记录必须属于当前受保护数据源；`models` 里的字段必须对当前 MDL 用户可见；可选 `users` 和 `roles` 用于进一步收窄可用账号。未绑定、未审核、已过期、绑定到其他数据源、或引用了当前账号不可见字段的知识空间会被拒绝。管理员仍可使用普通知识空间管理能力。

在 MDL 工作台的知识库页，使用“通用知识库绑定”卡片保存、编辑和审核空间。空间内容通过现有 DB-GPT 文档导入功能维护。审核指纹覆盖空间配置、文档内容/问题及所有分页读取的 chunk 内容和元数据；任何相关变化都需要重新审核。检索前后均重新检查授权和指纹。配置 embedding 时使用现有 RAG；没有 embedding 时使用授权空间 chunk 的词法检索。仅连接/超时/文件后端异常允许转用经元数据验证的词法检索；权限和元数据异常保持拒绝。

中文多字片段使用完整词段和相邻双字片段匹配，避免“人员”因单字“人”匹配到“无人机”。只共享“数量”“多少”等通用问句片段时不会命中。状态码没有配置业务含义时，提示规则要求报告原始值并注明含义待确认；这仍需在真实模型评估中验证。

## 存储与接口

知识按数据源独立保存，默认位于 `configs/mdl.knowledge/` 的哈希文件中。目录已被 Git 忽略；真实业务知识不随代码发布。写入使用版本检查和原子替换，版本冲突时保留当前编辑内容供核对。

管理员接口：

- `GET /api/v1/mdl/knowledge?database=<name>`：读取条目及状态。
- `PUT /api/v1/mdl/knowledge`：保存 `{database, revision, entries}`。
- `POST /api/v1/mdl/knowledge/approve`：审核 `{database, revision, id}`。
- `POST /api/v1/mdl/knowledge/search`：预览 `{database, question, top_k}`。
- `GET /api/v1/mdl/knowledge-spaces?database=<name>`：读取通用知识空间绑定及状态。
- `PUT /api/v1/mdl/knowledge-spaces`：保存 `{database, revision, spaces}`。
- `POST /api/v1/mdl/knowledge-spaces/approve`：审核 `{database, revision, id}`。
- `POST /api/v1/mdl/knowledge-spaces/search`：返回当前账号可用于问数的已审核空间。

条目使用 `kind=qa_sql|business_rule|glossary|analysis_rule`，并包含 `title`、`content`、`aliases` 和 `models`（模型名到字段名列表的映射）。`glossary` 使用 `terms=[{term,meaning,aliases}]`，`analysis_rule` 使用 `rule={metric,conditions,guidance,checks,report_requirements}`，其中 `checks=[{column,operator,value,severity,message}]` 可选，支持 `eq/ne/gt/gte/lt/lte/between/in/not_null/is_null`；两种结构不能交叉使用。`qa_sql` 可选 `expected_result={columns,rows}` 和 `reuse_on_exact_match`。审核状态、审核元数据和 MDL 指纹由服务端维护，客户端伪造字段会被丢弃；编辑 SQL、口径、绑定字段、分析规则或期望结果后会自动退审。

## 验证

运行 [本地运行指南](MDL_LOCAL_RUNTIME.md) 中的 MDL 测试命令。回归覆盖权限过滤、数据源隔离、版本冲突、编辑退审、MDL 变化失效、只读 SQL 审核和原生连接器的上下文注入。评估业务准确性时，另行准备经过业务确认的问题、SQL 和期望口径，比较命中率及实际查询结果。

`tests/mdl/test_knowledge_retrieval_eval.py` 提供 16 个合成业务问题，覆盖分类、别名、去重计数、登记行数、未定义状态和无关问题，并使用实际 Wren 规划和 DuckDB 数据核对 SQL 示例。合成数据含重复及空设备 SN，去重结果为 2，登记行数为 4，避免把不同统计口径当成同一个答案。这组回归通过代表这些已知问法工作正常，不代表任意自然语言问题都能准确检索。`scripts/mdl_knowledge_eval.py` 可对已保存的 MDL 知识库输出机器可读 JSON 验收报告，用于业务方维护自己的固定问法集。

本地真实问数评估的 SSE 和结果保存在 `.runtime/mdl/knowledge-*.sse`、`.runtime/mdl/knowledge-*.json`，不会随代码提交。记录可用于核对检索引用、工具 SQL、独立查询结果和最终文字解释。

无需模型凭据的可复现验收：

```bash
.venv/bin/python scripts/mdl_knowledge_eval.py --demo \
  --output .runtime/mdl/knowledge-acceptance.json
```

该命令在临时目录创建合成 MDL、审核知识和 DuckDB，通过真实 `sql_query`、MDL 连接器及 Wren 执行，输出检索 8 个问题、SQL 结果/精确复用 3 项、权限 2 项的 JSON 报告。精确复用用例故意传入错误的 `SELECT 999`，验证实际执行审核口径并返回 2，最终答案不出现模型编造的 999。退出码非零表示验收失败。此固定集通过不表示真实 LLM 对任意问题的泛化准确率；真实业务应维护自己的问题、统计口径和结果回归集。
