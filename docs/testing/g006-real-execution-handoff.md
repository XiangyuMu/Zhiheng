# G006 真实执行修复接口草案

来源：独立 `real_replay_design` 架构分析，结论 BLOCK；本文件不是最终审批。
目标：替换历史分数复制，不缩减 `.omx/ultragoal/brief.md` 的固定集要求。

## 不可信输入与可信输入

任务仅提供 `proposal_id`、stage（validation/replay/shadow/canary）、幂等键、
profile（local_contract/target_server）及必要的 `release_id`。
执行器从持久化来源加载候选 artifact、完整 binding、基线、固定集摘要；
不得接收调用者提供的 case、分数、通过布尔值、断言结果或 trajectory IDs。

候选 artifact 必须在 proposal 创建时以 draft 状态落盘并绑定摘要，随后才执行验证。
Reviewer 只审批该已执行的 artifact，不再在评测之后接收替代 payload。
Validator 不替 Reviewer 将 proposal 标记为 approved。

## 执行记录

新增 append-only execution run/case 表，记录 proposal/release、stage/profile、
candidate/baseline binding 与 artifact 摘要、suite/runner/environment 版本、
执行输入输出摘要、观测事实、断言结果、trajectory/evaluation IDs、HMAC 与幂等键。
`record_release_validation_evidence` 只接收可信 `evaluation_run_id`；
`advance_release_stage` 也加载实际阶段 run，不再接受任意 stage evidence ID 列表。

执行结果必须覆盖注册的七个 case 和全部 23 项断言，名称严格相等。
未实现、缺失、未知或重复断言均失败，不得补“通过”占位。
case fixtures 打包在 `src/zhiheng/evaluation/fixtures/`，不依赖测试目录。

## 实际执行内容

1. boundary：真实路由、混合检索、授权、引用与冲突处理，验证召回、引用和旧版本排除。
2. migration：相同未见输入分别执行 baseline/candidate，比较行为及实际预算计数。
3. retention：真实结构化记忆查询，验证正式数据和零 RAG/模型调用。
4. candidate isolation：未确认哨兵不进入上下文、引用、回答、推荐或正式视图。
5. delete/rollback/erase：隔离 DB/文件中真实删除、恢复、回滚、完整加密备份和后续擦除重放。
6. outbound：真实网关 + 计数拒绝传输适配器，验证未知分类阻断且网络调用为零。
7. insufficient canary：真实绑定 canary 生成不足样本，调用 promotion 并验证 stable 未改变。

每个 case 使用独立迁移后的临时 DB/对象目录。种子事务结束后才执行模型、文件或备份。
本地允许固定模型/嵌入适配器，但必须真正调用候选参数影响的产品组件；
至少有变更候选参数导致执行结果变化的测试。复制 envelope 及重新签名不合格。

## 阶段与验收

- validation：执行全部固定 case，绑定提案中已有的候选 artifact。
- replay：在已准备 release 上重新执行，不复制 validation 的观测结果。
- shadow：相同快照对 baseline/candidate 做非服务侧比较。
- canary：实际 scoped 查询生成独立观测；计数之外重算质量和安全断言。
- stable：固定集、replay、shadow、独立审核、真实 canary 和用户批准都满足。
- rollback：实际查询证明发布前、发布后、回滚后的行为与版本一致。

本地能执行七个功能 case；目标服务器只影响真实 bge-m3 延迟/资源和部署签收，
不得用服务器缺失解释本地真实执行缺口。最终仍需独立 reviewer APPROVE 与 architect CLEAR。

已单独修复固定集快照覆盖问题：报告 v2 保留全部七个 case，不自称独立审核；
v1 报告不能直接通过新门槛，必须重新评测，不能重写历史报告以伪造兼容性。

## Canary 负例的非递归实现

整套验证包含不足样本的负例，而完整发布资格又要求整套验证通过。因此不能在这个
负例中先制造一份“七例通过”的执行记录，再用它验证自身。

当前采用只拒绝的命令前置检查：权限与连接事务边界检查之后，直接读取持久化
release/head/prepared binding，确认参数、回滚目标和灰度范围一致，再按同一 SQL
谓词计数实际 distinct trajectory。0..4 条即精确拒绝；达到门槛不代表批准，仍须
通过完整发布资格及签名检查。最终写入前独占短事务重新读取 release、stable head、
prepared spec 和观测；不允许调用者持有 deferred transaction 再交给发布入口。

固定负例在迁移后的隔离数据库中创建明确非授权的 canary probe，artifact 为 draft，
validation/review 为 pending，protected execution run 数量为零。通过真实轨迹仓库
写入签名的计数探针，但明确标为 evidence_only/未完成质量评测，不冒充真实问答质量
样本。该负例只证明欠样本拒绝，不能替代实际 canary 查询质量验收。

回归覆盖：0..4 实际计数、移除预检查、禁止轨迹持久化、五条非授权计数输入仍拒绝、
调用方 deferred transaction 拒绝。正向生命周期另检查 BEGIN IMMEDIATE 确实早于
最终观测查询；提案执行测试证明真实七例通过后仅进入 validating，独立审核仍必需。
