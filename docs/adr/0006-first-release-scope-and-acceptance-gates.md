# 首版产品范围与验收门禁

**Status:** accepted

知衡首版以单用户可持续知识库为交付边界：隐私擦除的来源绑定、真实浏览器导入到检索链路、对话结论审核和个人记忆控制必须可用。自动提炼使用可解释规则和受控模型响应即可，但固定合成对话集必须达到显式判断召回率至少 90%、前提保留率至少 95%、原文定位 100% 有效，并且引用、玩笑和假设不得误入自动正式记忆；未识别表达必须显式展示且可回到原文。分类建议首版使用确定性规则并经人工审核后生效，LLM 分类、复杂语义关系、多用户隔离、公网 HTTPS 和生产服务器演练列为后续增强。

首版发布必须在干净检出中完成数据库迁移、API/Worker 启动、完整真实浏览器链路、隔离环境备份恢复和全量测试；所有证据绑定同一提交 SHA。隐私擦除误删或漏删派生结果属于发布阻塞，其他缺少新鲜证据的能力不能宣称已交付。

质量评估使用至少 50 段固定合成对话，报告匹配规则、分子分母、逐例结果和误提炼率。受控模型响应只能验证处理契约，质量分数必须来自实际运行的提炼器；任何结论未经用户批准均不得成为正式知识。未识别结果提供原文和手动补充入口，不将未知遗漏宣称为精确统计。

真实浏览器必须证明“登录 → 粘贴文本 → 创建持久化导入任务 → 独立 Worker 消费 → succeeded 且原文对象和索引指针存在 → 知识库可见 → 搜索 → 回到原文”。失败与不支持场景沿用 [ADR 0005](0005-startup-import-qualification-and-browser-failure-semantics.md) 的资格与轮询规则。备份恢复使用隔离合成数据和真实 restic，验证数据库与原文完整、恢复后可检索，以及旧快照不复活已擦除内容。

本决策补充 [ADR 0004](0004-main-as-release-branch-and-evidence-gated-delivery.md)，不表示上述门禁已经通过。需求依据为 [Issue #1](https://github.com/XiangyuMu/Zhiheng/issues/1)；隐私阻塞由 [#25](https://github.com/XiangyuMu/Zhiheng/issues/25) 跟踪，提炼质量由 [#29](https://github.com/XiangyuMu/Zhiheng/issues/29) 跟踪，全量测试与同提交交付证据分别由 [#27](https://github.com/XiangyuMu/Zhiheng/issues/27)、[#17](https://github.com/XiangyuMu/Zhiheng/issues/17) 跟踪，资产与使用说明由 [#30](https://github.com/XiangyuMu/Zhiheng/issues/30) 跟踪。
