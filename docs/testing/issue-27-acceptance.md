# Issue #27 完整 pytest 门禁验收记录

## 结论与提交边界

验收对象为 `421fbaec46cf9ee64adb4d8b51c8532d73dc2922`，在独立干净检出中运行。
完整 pytest 正常结束：777 项通过、0 失败、0 跳过，门禁耗时 1127.928 秒。
本次提交只归档已产生的证据，不修改产品或测试代码，也不声称在本记录提交 SHA 上重新运行了测试。

可复核摘要见 [issue-27-evidence.json](issue-27-evidence.json)，其中保留测试节点、
实际命令、环境版本、开始时间、耗时、提交 SHA、失败集合、原始文件 SHA-256。
本地完整原件位于 `/tmp/zhiheng-issue17-final-evidence/`；临时目录不是长期存储，
跨机器审计时必须另行保存原件或按 [运行说明](pytest-delivery-gate.md) 重新采集。
摘要不能替代完整日志，也不保证临时原件永久可用。

## 验收标准与证据

| 要求 | 实际结果 |
| --- | --- |
| 干净检出全量正常结束 | 开始和结束均干净；termination=completed，exit_code=0 |
| 全量结果完整 | collection=777，JUnit=777，report_complete=true |
| 首个失败与失败集合 | JUnit 和增量事件均无失败；failed_nodes=[]，failure_groups={} |
| 隐私擦除 | privacy 必需组 7 项通过 |
| G006 fixed suite | g006_fixed_suite 必需组 4 项通过 |
| release validation | release_validation 必需组 1 项通过 |
| promotion / rollback | promotion_rollback 必需组 1 项，lifecycle 4 项通过 |
| Worker recovery | worker_recovery 必需组 14 项通过 |
| restic restore | restic_restore 必需组 20 项通过，无超时终止 |
| 质量检查 | 同一 SHA 的依赖、编译、Ruff、Mypy、pytest、浏览器和提炼评估步骤均 passed |

失败分类机制保留原始详情；文件分组仅表示功能归属，根因未确认时明确标记
`unconfirmed`，不把隐私擦除或 restic 耗时自动归为 G006 故障。
当前运行无失败，因此不存在需要补写推测根因的失败节点。

## 已修复问题、剩余风险与范围限制

- 隐私擦除：`2b356dc` 和 `966d363` 修复无引用 receipt 及权威来源链路；当前全量运行覆盖相关回归。
- restic 有界执行与诊断：`adcf80c`、`708f003`、`b90d995`、`3cac9a0` 修复超时、进程清理和回归稳定性；当前 20 项恢复测试通过。历史耗时放大不等于当前功能失败。
- G006：`f5ebdd3` 修正可检索数据契约的回归保护；本次固定集、发布验证、晋升、回滚和恢复均通过。
- 门禁完整性：`55949c4` 校验收集数量、JUnit 和必需组，避免部分执行被认定为通过。

本次全量执行未发现仍失败的产品测试，但不能据此证明产品没有问题。
[Issue #17 矩阵](issue-17-delivery-matrix.md) 中的分类迁移、条件式回答、暂停隔离、
完整缺失信息 UI 等场景仍需按各自要求补充实际验收证据；矩阵中的工作树和 SHA 描述是旧基线快照。
这些是产品交付证据缺口，不属于可接受的 MVP 限制。restic 测试还存在本机临时路径回退，
本次通过仅证明记录环境；其他机器必须安装可用 restic 或显式配置 `ZHIHENG_RESTIC_BINARY`。

可接受的 MVP 边界沿用 ADR 0006：单用户、确定性分类加人工审核、可解释提炼和受控响应。
LLM 分类、复杂语义关系、生产多用户隔离和公网部署是未来增强。
#27 的 pytest 门禁具备通过证据；整个首版交付仍受 ADR 0004–0006 和剩余场景证据约束。
