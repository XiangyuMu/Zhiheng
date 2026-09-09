# 知衡 Zhiheng

知衡是一个只服务个人的自进化知识库 Agent。它围绕个人完整生活管理知识，根据目标调用证据辅助查询与决策，并通过可确认、可追溯、可回滚的记忆机制持续理解用户。

## 当前阶段

项目处于产品定义与架构设计阶段。完整需求见 [产品 PRD](docs/product/PRD.md)，开发节奏见 [Roadmap](docs/ROADMAP.md)。

## 核心边界

- 单用户、服务器部署
- 支持本地模型与外部模型 API
- 文件、SQLite、全文检索和向量检索混合存储
- 推断型用户画像必须确认后才能生效
- 首版不做多用户、个人模型训练和自动对外行动

## GitHub 工作流

1. 用 Issue 描述需求、缺陷、研究任务或技术债。
2. 每个 Issue 只对应一个可验证结果，并关联 Roadmap 里程碑。
3. 从 `main` 创建短生命周期分支，例如 `feat/memory-candidate-review`。
4. 通过 Pull Request 合并，PR 必须引用 Issue 并附验证证据。
5. `main` 始终代表可复现、通过验证的稳定状态。

## 文档索引

- [完整产品 PRD](docs/product/PRD.md)
- [需求访谈摘要](docs/product/discovery/deep-interview-summary.md)
- [产品与开发路线图](docs/ROADMAP.md)
- [MVP 技术栈与检索基线 ADR](docs/architecture/adr/0001-mvp-technology-stack.md)

## 状态约定

- `idea`：尚未进入近期计划
- `ready`：范围和验收标准明确，可以开始
- `in progress`：正在实施
- `blocked`：存在明确阻塞因素
- `in review`：等待验证或合并
- `done`：验收完成并已合入稳定分支
