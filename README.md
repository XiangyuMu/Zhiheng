# 知衡 Zhiheng

知衡是单用户个人知识库：保存原始材料，提炼带前提和来源的结论，经用户审核后用于后续回答，
并管理个人记忆、冲突、分类和隐私擦除。

## 首版使用

项目已具有本地可验证的首版实现。是否交付以最终 `main` 提交的干净检出验收报告为准。

1. 按 [本地安装与使用手册](docs/operations/local-first-release.md) 安装依赖、配置隔离数据目录并迁移数据库。
2. 分别启动 API 和 Worker，在浏览器创建单用户账户，粘贴文本并等待可检索，然后搜索和查看原文。
3. 在审核中心批准结论；未审核草稿不能进入后续回答。

## 范围与交付

- [0.1.0 发布说明与能力边界](docs/releases/0.1.0.md)
- [需求—实现—测试映射](docs/testing/issue-17-delivery-matrix.md)
- [同 SHA 交付门禁和持久证据](docs/testing/pytest-delivery-gate.md)
- [备份与恢复设计](docs/architecture/deployment-and-recovery.md)
- [产品规格](docs/product/evolving-knowledge-memory-spec.md)、[领域词汇](CONTEXT.md)、[路线图](docs/ROADMAP.md)

首版使用规则提炼和确定性分类，结论与分类建议经人工审核后生效；明确且无冲突的个人陈述
可按既定规则自动记忆。LLM 分类、多用户、开放域质量保证和生产服务器/公网 HTTPS 属于后续增强。

任务使用 [GitHub Issues](https://github.com/XiangyuMu/Zhiheng/issues)，每张票据保持可独立验收。
`main` 是唯一最终交付分支；改动审查并合入后，必须重新验证对应 SHA。参见
[ADR 0004](docs/adr/0004-main-as-release-branch-and-evidence-gated-delivery.md)。
