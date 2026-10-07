# Provider 模型目录与调用协议

- **Status:** accepted
- **Date:** 2026-10-07

Provider 配置采用统一的基础边界：Provider 类型、显示名称、Base URL、服务端保存的 API Key、模型目录、协议和能力确认；Provider 特有字段放在扩展配置中。模型目录支持显式刷新和手动添加，刷新结果由服务端取得并缓存，浏览器只看到脱敏模型元数据。发现失败保留上次缓存并显示失败原因，不能自动清空已启用模型。

模型能力分为文本生成、多模态输入和 Embedding。目录或适配器只能提供能力建议，用户确认并启用后才可路由；每种能力设置一个明确默认模型，首版不做静默自动回退。目录刷新后消失的模型标记为过期并保留，等待用户处理。旧的文本和多模态字符串白名单迁移为已确认的手动模型记录，Embedding 不从模型名称推断。

协议作为显式模型属性保存：OpenAI 使用 Responses，DeepSeek 按官方支持选择 Responses 或 Chat Completions，兼容 Provider 使用 Chat Completions，Embedding 只走 Embeddings 协议。OpenAI 的官方默认地址为 `https://api.openai.com/v1`；DeepSeek 新配置默认使用官方 `https://api.deepseek.com`，历史明确填写的 `/v1` 地址继续兼容且不得重复拼接路径；自定义兼容 Provider 必须使用 HTTPS，Ollama 保留本机回环限制。两者都使用服务端 Bearer API Key。

OpenAI 的 `/v1/models` 目录主要提供模型身份元数据，不能单独证明模型能力；能力必须来自官方模型资料或适配器规则并经用户确认。DeepSeek 当前官方目录和模型资料没有 Embedding endpoint，因此 DeepSeek 不声明 Embedding 能力；没有已确认 Embedding 模型时，需要向量索引的新任务进入明确的 `unsupported` 或待处理状态，已有全文检索和已具备索引的知识不受影响。

参考：

- OpenAI Models、Responses、Chat Completions、Embeddings API reference：
  https://platform.openai.com/docs/models
  https://platform.openai.com/docs/api-reference/models/list
  https://platform.openai.com/docs/api-reference/responses/create
  https://platform.openai.com/docs/api-reference/chat/create
  https://platform.openai.com/docs/api-reference/embeddings/create
- DeepSeek authentication、model list、chat completion、responses：
  https://api-docs.deepseek.com/quick_start/authentication
  https://api-docs.deepseek.com/api/list-models/
  https://api-docs.deepseek.com/api/create-chat-completion/
  https://api-docs.deepseek.com/api/create-response/

模型 Key 仍遵循 ADR 0006 的加密持久化和不回显边界；模型发现请求、连接测试和调用均不得把 Key 或完整响应正文返回浏览器。
