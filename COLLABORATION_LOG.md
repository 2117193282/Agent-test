# Collaboration Log
## Task Understanding
- Goal: 补全企业 Agent 后端闭环，完善权限感知RAG检索，建立工具边界和权限矩阵，完成脱敏，完善管理后台指标
- Non-goals: 不引入外部数据库；不重写项目架构；不写死公开用户/公开SKU/公开fixture路径或固定工具输出。
- Protected contracts: 保留API路径、公开字段、标准工具名、审计动作、权限语义、数据库、错误结构
## Collaboration Disclosure
- Primary AI software/model or human name: Codex/梁宏烨
- Other tools or collaborators: 本地python环境
- Division of work: Codex提供实现方案并完成代码实现，人工检验代码、手动测试。
## Ambiguities And Assumptions
| Item | Impact | Decision |
| "只分析"和"创建审批"的意图边界 | 决定是否规划 OA 步骤 | 按关键词匹配：含"只分析/仅分析/不创建"的归 analysis_only，其余含"创建审批/生成草稿"的归 replenishment |
| oa:approval:write 但 prompt 要求创建审批 | 影响 run 是否失败或跳过 | Planner 缺权时不规划 OA 步骤；Executor 二次校验缺权时跳过并记录 tool.skipped，不视为失败 |
## AGENTS.md Historical Notes Review
| Historical note | Adopted or rejected | Evidence |
| 公开测试只检查 API 外形，可以先不实现完整运行事件和审计日志。 | Rejected |已实现 tool.call、tool.skipped、approval.draft.create、permission.denied 等审计|
| 当前 fixture 主要是两个SKU，可以按它们写固定分支。 | Rejected |Planner 用正则提取 SKU，不写死任何 SKU|
| Dashboard 字段可以按实现方便重命名。 | Rejected |保留 README 所有字段名不变|
| 用户能创建任务就默认允许创建 OA 草稿。 | Rejected |需要oa:approval:write权限才执行 OA 步骤|
| 知识库 citation 和过滤文档列表可以后置。 | Rejected |search 返回完整 citations + filtered_doc_ids|
| 工具异常可以吞掉并返回空结果。 | Rejected |异常使 run 进入 failed 状态，不吞异常|
## Root Cause Notes
| Symptom | Evidence | Root cause | Fix |
run 创建后进占位失败状态 | alice/bob 测试失败 | worker/planner/executor 是占位实现 | 实现完整 Planner → Executor → 持久化流程
知识库没有 citation 且返回 debug 信息 | RAG 测试失败 | search.py 返回空答案 + debug/candidate_note | 实现权限检索、排序、答案生成、引用溯源
events 对不存在 run 返回空列表 | 可见性测试失败 | events 接口没校验 run 是否存在 | 加 404 + _check_run_visibility
权限拒绝没有审计记录 | mallory 创建任务被拒后审计为空 | require_permissions 只抛 403 | 拒绝前写入 deny 审计
提示词注入没被拦截 | 创建含注入文本的任务不报错 | detect_prompt_injection 未被调用 | create_task 入口调用，命中返回 422
敏感字段出现在 API 响应 | 工具返回含 vendor_secret/unit_cost_usd | 工具结果直接透传 | call() 和 _sanitize() 两层脱敏
缺权工具可能被执行 | OA 写缺权时可能出现 OA-DRAFT | executor 没有工具级权限校验 | 加 TOOL_PERMISSIONS，逐步骤校验
## Compatibility Notes
| Surface | Existing behavior | Change | Compatibility plan |
API | run/events 任何认证用户可读 | 仅请求人/创建人/admin 可读 | 授权用户无影响，无权返回 403
API | events 对不存在 run 返回空列表 | 不存在返回 404 | 改进错误处理
API | 创建任务接受任何 prompt | 注入检测命中返回 422 | 仅影响恶意内容
Database | 无 schema 变更 | 无变更 | 向后兼容
Permissions | 无工具级权限校验 | 加 TOOL_PERMISSIONS 映射 + Planner/Worker/Executor 三层检查 | OA 缺权时 planner 不规划步骤，读工具缺权时 worker 预检失败
Audit logs | 只有成功操作审计 | 加 tool.call、tool.skipped、permission.denied、task.rejected | payload 落库前脱敏
## Verification
| Command | Result | Notes |
| `py scripts/self_check.py` | 4 passed, 1 warning | 公开自检通过。warning 来自 Starlette/httpx 弃用提示，不影响功能。 |
| `py -m pytest -q` | 4 passed, 6 xpassed, 1 warning | 6 个 acceptance guidance 测试在 xfail 标记下变为 XPASS，说明全部验收条件已满足。 |
| `py -m pytest -q --runxfail` | 6 passed, 1 warning | 强制运行验收指导测试，确认核心闭环和安全边界全部通过。 |
## Remaining Risks
- Planner 采用正则匹配，不是生产级自然语言意图分类器
- token 成本是 FakeLLM 的估算，不代表真实模型计费。
- OA 写权限不足时只记录了 tool.skipped 事件，没有写入 decision=deny 的审计日志
- Worker 的权限预检在发出 run.started 事件之后执行，预检失败时事件已落库
- 提示词注入检测基于正则，覆盖测试用例中的常见模式，但无法拦截所有变体