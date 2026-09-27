# Issue #17 首版交付矩阵

本文件把 Issue #1 的 16 个验收场景、后续确认的首版门禁和当前代码中的测试入口
映射到同一份交付审查记录。它是交付准备文件，不是通过报告。

## 基线与证据规则

| 项目 | 当前值 |
| --- | --- |
| 交付分支 | `main` |
| 验收对象 | 以持久证据目录的 `report.json.sha` 为准；测试前先提交，在同一干净检出运行 |
| 工作树 | 由报告中的 `clean_before`、`clean_after` 和 `same_sha` 证明，不能沿用文档编写时状态 |
| 本文件范围 | 映射需求、实现和验收入口；通过结论取自对应 SHA 的原始报告 |
| 当前证据入口 | `/Users/muxy/Projects/Zhiheng-delivery-evidence/<SHA>/`；完整 pytest、浏览器和提炼评估分开保留 |

证据状态使用以下含义：

- **实现入口**：当前代码中存在相关服务、API、Worker 或状态约束。
- **测试入口**：仓库中存在针对该行为的测试或验收脚本。
- **待验收**：必须在最终干净检出、同一提交 SHA 上重新运行并保存日志、截图和环境版本。
- **缺口**：现有实现或测试没有证明完整用户场景，或者仍有已知依赖阻塞。

历史文档中的“通过”只说明当时某个范围曾经运行过；根据 ADR 0004，若没有绑定最终
提交 SHA 的新鲜结果，不能作为首版交付证据。

## 16 个原始验收场景

| # | 需求 | 实现入口 | 测试入口 | 当前证据与缺口 |
| ---: | --- | --- | --- | --- |
| 1 | 一段对话中的多个独立判断形成多个草稿；保留原文、前提、来源和候选领域 | `jobs/memory_extraction.py`、`conclusions/extraction.py`、`api/conclusions.py` | `tests/integration/test_conclusion_extraction.py`、`tests/evaluation/test_issue29_conclusion_extraction.py` | **实现入口+测试入口**。仍需在真实应用入口和独立 Worker 上验证；#29 的 50 段质量结果必须绑定最终 SHA |
| 2 | 草稿跨会话暂存；批准前不得进入另一会话回答，包括原始对话和派生检索 | `conclusions/repository.py`、`query/conversations.py`、资格过滤 | `tests/integration/test_conclusion_lifecycle.py`、`test_cross_conversation_qualification.py`、`tests/e2e/check_qualification.cjs` | **实现入口+测试入口**。浏览器脚本已覆盖两个 context，但仍需在干净检出真实运行并保存截图/日志 |
| 3 | 只批准选中的条目；重复提交幂等；版本变化不能继承旧批准 | 结论版本、ETag、幂等写入和审核 API | `tests/integration/test_conclusion_lifecycle.py`、`test_conclusion_relations.py`、`test_memory_api.py` | **部分实现证据**。#2 的草稿 CAS 与 #12 的关系双方版本约束仍是独立交付依赖，需确认最终代码和 HTTP 回归 |
| 4 | 未确认前提的正式结论按条件使用，不能把假设当成用户事实 | `conclusions/applicability.py`、回答上下文资格 | `tests/integration/test_conclusion_applicability.py`、`test_answer_memory_context.py` | **实现入口+测试入口**。缺少最终浏览器中“如果”条件显示的同 SHA 证据 |
| 5 | 明确事实变化或有效期届满自动暂停；缓存和索引未重建也不得继续使用 | `conclusions/applicability.py`、当前资格复核和缓存撤销 | `tests/integration/test_conclusion_applicability.py` | **实现入口+测试入口**。需要把暂停后的回答资格和缓存复用场景纳入最终浏览器/HTTP 报告 |
| 6 | 重复、补充、修订、条件并存和冲突分别展示；用户批准后保留历史 | `conclusions/relations.py`、关系审核和正式化服务 | `tests/unit/test_conclusion_relations.py`、`tests/integration/test_conclusion_relations.py`、`tests/e2e/check_review_relations.cjs` | **实现入口+测试入口**。浏览器脚本覆盖关系审核主路径；需重新运行并证明旧关系、双方版本和来源在最终 SHA 上可追溯 |
| 7 | 明确且无冲突的个人陈述可自动记忆；引用、玩笑、假设和推断不能自动成为事实 | `jobs/memory_extraction.py`、`memory/repository.py` | `tests/integration/test_memory_repository.py`、`tests/contracts/test_memory_candidate_isolation.py`、#29 固定负例 | **部分实现证据**。固定集覆盖负例，但自动记忆与对话提炼是两条边界；需在最终报告分开给出误提炼率和正式记忆资格 |
| 8 | 明确时间变化保留历史；无法由时间解释的不一致进入冲突 | `memory/personal_updates.py`、冲突查询/上下文提示 | `tests/integration/test_conclusion_applicability.py`、`tests/e2e/check_workspace_full.cjs` | **实现入口+局部测试入口**。真实浏览器脚本有冲突提示，但尚未证明“历史保留+时序更新”完整链路 |
| 9 | 冲突可确认、补充、稍后处理或跳过；相关回答按条件回答或暂缓，无关回答继续 | `query/conflicts.py`、`api/memory.py`、上下文提示 UI | `tests/integration/test_memory_context.py`、`tests/e2e/check_workspace_full.cjs` | **实现入口+测试入口**。需在最终浏览器运行中保存弹窗选择、持久待办和回答资格证据；关闭/超时不批准仍需明确断言 |
| 10 | 缺失信息只在实质影响任务时提示，说明原因并允许跳过 | 上下文提示判定和前端提示组件 | `tests/e2e/check_workspace_full.cjs`、相关 memory/context 集成测试 | **部分实现证据**。当前浏览器脚本证明部分提示路径，尚未单独证明“无关缺失不弹窗”和“补充后重试”在真实 UI 中成立 |
| 11 | 普通对话只显示待审核数量；主动打开审核中心后可恢复草稿并执行审核 | `api/static/review-center.*`、`api/conclusions.py` | `tests/integration/test_conclusion_lifecycle.py`、`tests/e2e/check_workspace_full.cjs` | **实现入口+测试入口**。需真实浏览器确认普通对话不被逐条打断、刷新/重新登录后待办仍在 |
| 12 | 易变信息相关复用时复核；稳定信息不因时间流逝被改写 | 适用性和个人信息有效期字段 | `tests/integration/test_conclusion_applicability.py`、`tests/integration/test_memory_context.py` | **局部测试入口**。缺少一条明确的浏览器用户场景和“稳定信息不重复询问”证据，属于首版验收待补 |
| 13 | 唯一主领域、跨域关联；个人档案与经历是独立记录类型；同一条目不重复存储 | `classification/taxonomy.py`、结论分类 API/审核 | `tests/integration/test_topic_taxonomy.py`、`test_conclusion_classification.py` | **实现入口+测试入口**。需在最终浏览器或 HTTP 证据中证明分类与记录类型同时展示且不产生重复正式条目 |
| 14 | 新条目分类随审核；已有分类及领域结构的变更需批准，历史可追溯 | `classification/suggestions.py`、分类审核和 ETag | `tests/integration/test_conclusion_classification.py`、`test_topic_taxonomy.py`、`test_classification_node_errors.py` | **实现入口+测试入口**。#15 的动态目录和逐条迁移必须在同一验收中覆盖；当前浏览器脚本没有完整分类迁移操作证据 |
| 15 | 旧分类升级逐条迁移；保留原文、条目、前提、来源和历史引用；未批准不改变正式归属 | `classification/taxonomy.py`、迁移预览/批准 API | `tests/integration/test_topic_taxonomy.py` | **局部测试入口**。缺少真实浏览器逐条迁移和恢复后引用检查；这是 #7/#15 的交付缺口 |
| 16 | 认证、并发版本、删除和恢复边界不退化；删除后派生结果不能复活 | 认证/CSRF/ETag、隐私擦除账本、对象和索引清理、恢复重放 | `tests/integration/test_privacy_physical_erase.py`、`test_answer_replay_authority.py`、`test_restic_restore_install.py`、`test_startup_recovery_barrier.py` | **实现入口+大量测试入口**，历史 `421fbae` 全量已通过；最终 SHA 仍须重跑隐私擦除、restic 恢复和旧快照不复活场景 |

## 首版新增门禁

| 门禁 | 代码/测试入口 | 当前判断 | 完成交付所需证据 |
| --- | --- | --- | --- |
| 干净检出与单 SHA | `docs/adr/0004-main-as-release-branch-and-evidence-gated-delivery.md`、`scripts/delivery_acceptance.py` | 执行器要求干净检出；实际状态见对应 SHA 的报告 | 新检出记录 SHA、依赖版本、工作树前后状态，所有步骤均绑定同一 SHA |
| 编译、Ruff、全量 Mypy | `scripts/delivery_acceptance.py`、`docs/testing/pytest-delivery-gate.md` | 编译、规范和类型检查步骤由统一执行器采集 | `compileall`、`ruff check .`、`mypy src tests` 全部退出 0；不得用 `type: ignore` 隐藏错误 |
| 迁移和启动屏障 | `scripts/upgrade_database.py`、`tests/integration/test_startup_recovery_barrier.py`、`tests/integration/test_worker_runtime.py` | 有实现和回归入口 | 空库迁移、支持版本升级、API/Worker 启动；迁移/恢复失败时两者都拒绝启动并输出可诊断错误 |
| 真实导入到检索链路 | `scripts/browser_acceptance.sh`、`tests/e2e/check_workspace_full.cjs`、`tests/integration/test_worker_knowledge_indexing.py` | 脚本要求真实 API 与 Worker，尚无本轮运行证据 | 登录 → 粘贴文本 → 持久化任务 → 独立 Worker 消费 → `succeeded` 且原文对象和索引指针存在 → 知识库可见 → 搜索 → 回到原文 |
| 导入失败语义 | `src/zhiheng/jobs/knowledge_contract.py`、`tests/e2e/test_import_polling.cjs`、`docs/adr/0005-startup-import-qualification-and-browser-failure-semantics.md` | 状态和轮询规则已有测试入口 | `failed`/`unsupported`/`partial` 不进入正式上下文；404 停止轮询；5xx/网络错误有限退避；页面显示稳定错误码和原因 |
| 备份恢复和隐私擦除 | `tests/integration/test_backup_manifest.py`、`test_restic_restore_install.py`、`test_privacy_physical_erase.py`、`scripts/restore_restic.py` | 有实现和故障回归，但不是当前 SHA 的通过证明 | 隔离 DB、对象目录、restic 仓库和擦除账本真实演练；恢复后可迁移、可检索、可回原文，已擦除内容不被旧快照复活 |
| 自动提炼质量 | `tests/fixtures/conclusions/issue29_synthetic_dialogues.json`、`src/zhiheng/evaluation/issue29_conclusion_extraction.py` | 固定 50 段与阈值已落地；本轮未运行 | 结论召回率 ≥90%、前提保留率 ≥95%、原文偏移 100%、引用/玩笑/假设误提炼为 0；报告逐例结果、分母分子、未识别项和 SHA |
| 审核资格和关系闭环 | `tests/e2e/check_review_relations.cjs`、`tests/e2e/check_qualification.cjs` | 有浏览器脚本和集成测试 | 未批准/拒绝/延期/暂停内容不进入正式回答；批准后只使精确版本获得资格；关系审批过期、重试和失败回滚均可审计 |

## 首版边界

这些能力属于首版产品范围，必须完成实现并取得对应证据：

- 单用户部署下的原始材料保存、结论提炼、前提/来源/分类保存和集中审核。
- 正式知识资格隔离、关系审核、适用性暂停、个人记忆控制、隐私擦除。
- 确定性分类建议，人工审核后生效；个人档案与经历作为记录类型维度。
- 真实 API/Worker/浏览器导入检索链路，以及隔离环境备份恢复。

下列是已确认的 MVP 限制，不应伪装成缺陷，也不能用来掩盖首版门禁失败：

- 分类建议首版使用可解释的确定性规则，不承诺 LLM 分类。
- 关系判断采用当前可解释实现，不承诺开放域复杂语义关系的完整覆盖。
- 首版边界是单用户部署，不承诺生产级多租户隔离。
- 生产服务器、公网 HTTPS 和目标服务器性能签收属于后续运维工作。

下列属于未来增强：

- LLM 驱动的分类建议、更强的语义关系判断和开放域提炼质量。
- 多用户/租户隔离、生产部署、目标服务器性能和公网 HTTPS。
- 超出固定合成集的开放域质量保证，以及完整向量路径和外部 provider 可用性签收。

## 交付前行动

1. 将本轮修改审查并提交；以该 SHA 的干净检出执行全部门禁，不把开发工作树调试运行当作签收。
2. 在干净检出中依次运行迁移、编译、Ruff、Mypy、`pytest_delivery_gate.py`、浏览器验收和 #29 固定集；为每步保留日志、JUnit、截图、依赖版本和 SHA。
3. 复核 #25 隐私擦除、#27 全量 pytest 交付门禁和 #29 提炼质量结果；任一失败都保持 Issue #17 未完成。
4. 对矩阵中标记“部分实现证据”的 #10、#12、#14、#15 补真实 HTTP/浏览器场景，尤其是无关缺失不弹窗、分类逐条迁移和恢复后历史引用。
5. 若门禁修复产生新提交，使用新 SHA 完整重跑并替换旧证据；旧报告只能作为历史参考。

只有当所有交付阻塞项有同一最终 SHA 的动态证据、工作树干净且独立审查通过时，Issue #17
才具备关闭依据。Issue 的开放或关闭状态本身不构成验收证据。

## 本轮补齐场景的证据索引

`tests/e2e/check_delivery_contracts.cjs` 通过真实浏览器操作分类审核、回答和上下文提示。
其逐项结果在 `browser/delivery-contracts/checks.json`；截图、请求错误及失败信息与结果共同保存。
具体覆盖以该文件中的逐项断言为准，不把登录后的 fetch 结果冒充 UI 操作。
导入故障中的网络/HTTP 故障注入与真实 Worker 不支持任务必须分别标记。

统一浏览器报告在 `browser/report.json`，全量 pytest 在 `pytest/evidence.json`。
即使失败也应保存阶段、退出码、已完成检查及已有日志，失败或缺失子报告不能成为通过。
分类、暂停和缺失信息等旧表“待补”描述代表本轮之前的证据缺口；只有新脚本实际通过后才解除，
不能因为脚本文件存在就认定完成。完整证据持久保存方法见 [运行说明](pytest-delivery-gate.md)。
