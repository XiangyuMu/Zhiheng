# ADR-0001：MVP 技术栈与检索基线

- 状态：Accepted（Step 0 基线）
- 日期：2026-09-02
- 决策范围：MVP / 单用户 / 单机双进程模块化单体
- 关联需求：[PRD](../../product/PRD.md)、[持续进化架构规范](../continuous-evolution.md)、[Roadmap](../../ROADMAP.md)

## 背景

知衡需要在本地或云服务器上运行，同时支持本地模型和经隐私门批准的外部模型 API。当前规模是单用户，架构要求：

- API 与后台 Worker 是同一模块化单体的两个进程。
- SQLite 是结构化状态、生命周期、可见性和发布状态的权威来源。
- 原始证据与版本化 Markdown 独立保存；FTS、向量、摘要和缓存均为可重建派生数据。
- 推断型画像确认前不能影响正式行为。
- 所有模型外发必须先完成数据最小化、敏感度判定、脱敏与复检；不确定时 fail closed。
- MVP 不引入微服务、独立消息代理、独立向量数据库、图数据库或模型训练。

技术栈应优先降低单用户部署和恢复成本，同时保留数据库、向量索引、模型供应商和任务执行器的替换边界。

## 决策驱动

1. 个人数据安全、确认门和隐私擦除必须由确定性代码执行。
2. 数据应可追溯、可重建、可回滚，并能在单服务器上可靠备份恢复。
3. 优先完成首个可验证检索与回答策略进化闭环，不为未出现的并发需求建设分布式系统。
4. 依赖必须允许在公开仓库中安全使用，并通过锁文件固定版本。

## 决策

### 1. 语言、应用与依赖管理

| 能力 | 选择 | 约束 |
|---|---|---|
| 运行时 | Python 3.12 | 生产和 CI 固定 3.12；不以开发机系统 Python 为准 |
| API | FastAPI、Pydantic v2、Uvicorn | API 进程默认单 Uvicorn worker；长任务不得在请求事务内执行 |
| 依赖管理 | uv、`pyproject.toml`、`uv.lock` | 所有生产和基准依赖必须锁定；禁止部署时无约束升级 |
| 数据访问 | SQLAlchemy 2.x 同步接口 | SQLite 操作使用短同步事务；异步模型/网络调用必须在事务外 |
| 数据迁移 | Alembic | 从空库和上一版本升级均需测试；不使用应用启动时隐式改表 |

选择同步 SQLAlchemy 是因为 SQLite 仍然同一时刻只执行一个写事务；异步驱动不会增加真实写并发，却会增加事务和故障恢复复杂度。

### 2. 权威存储与检索

| 能力 | 选择 | 约束 |
|---|---|---|
| 结构化权威 | SQLite，WAL 模式 | API 与 Worker 均可短写；设置 `busy_timeout`；事务内禁止网络、模型、解析、嵌入和大文件 I/O |
| 全文检索 | SQLite FTS5 | 保存原文；另建经应用层中文分词后的检索字段；FTS 结果进入上下文前必须回查正式视图授权 |
| 中文分词 | jieba 基线 | 只影响派生检索文本；可重新分词和重建，不作为事实权威 |
| 向量索引 | sqlite-vec 0.1.x，经 `VectorIndex` 端口隔离 | pre-v1 依赖必须精确锁定；索引按 generation 构建和切换；不得自行决定可见性 |
| Embedding | sentence-transformers + `BAAI/bge-m3`，基线 revision `5617a9f61b028005a4858fdac845db406aefb181` | 本地优先；保存模型 ID、revision、维度、归一化方式和 embedding generation |
| 重排序 | `BAAI/bge-reranker-v2-m3`，按需启用 | 不作为 Step 0 基准硬依赖；只有评测证明收益后进入默认查询路径 |

所有 FTS/向量记录必须携带 `source_id`、`source_version`、`visibility_scope` 和 `confirmation_generation`。任何候选结果进入模型上下文前，必须批量回到 SQLite 当前正式视图完成最终授权。

`sqlite-vec` 当前为 pre-v1 技术，因此只能出现在基础设施适配器中。若出现兼容性、规模或过滤性能问题，可在不改变领域接口的情况下迁移到 FAISS、Qdrant 或 PostgreSQL/pgvector。

### 3. 内容处理

| 内容 | 选择 | 说明 |
|---|---|---|
| PDF | pypdf；pdfplumber 作为版面/表格补充 | 默认不引入 AGPL 依赖；扫描 PDF 的 OCR 作为后续插件 |
| 网页 | httpx + trafilatura | 保存原始快照和来源元数据；动态网页需要时再引入 Playwright 抓取 |
| Markdown | markdown-it-py | 使用 AST 提取标题、引用、链接和代码块 |
| 图片 | Pillow | MVP 保存原图、缩略图、EXIF 和候选描述；描述不能替代视觉证据 |

### 4. 后台任务与持续进化

- 使用 SQLite `jobs`、`outbox_events` 和独立 Worker 轮询实现任务执行。
- APScheduler 只负责周期性写入待执行任务，不作为队列或状态权威。
- Job 必须包含幂等键、租约、心跳、指数退避、最大尝试、dead letter 和 crash recovery。
- Proposer、Validator、Reviewer、User approver 和 Publisher 的状态机由领域代码实现，不使用通用 Agent 框架代替。
- RAG 编排使用自建薄层：结构化直查、FTS/向量并行、RRF 融合、可选重排、SQLite 最终授权、证据加载和引用生成。

### 5. 模型与隐私网关

| 能力 | 选择 | 约束 |
|---|---|---|
| 本地模型 | Ollama HTTP API | 默认本地路径；后续在有并发/GPU证据时评估 vLLM |
| 外部模型 | 官方供应商 SDK；OpenAI 使用官方 Python SDK | 适配器只能接收隐私网关签发的 `ApprovedOutboundPayload` |
| 通用兼容接口 | httpx 实现受控的 OpenAI-compatible adapter | 不允许业务代码直接调用供应商 URL |
| PII 检测/脱敏 | Presidio analyzer + 中文自定义 recognizer + 确定性替换器 | 脱敏器和分类器不得通过外部模型处理原始文本；复检不确定即拒绝外发 |

MVP 不引入 LiteLLM。模型适配规模有限，自建薄适配层更容易证明隐私门无旁路，并减少供应链与日志泄露面。

`presidio-anonymizer` 暂不纳入运行时依赖。G003 复审发现其 `cryptography`
传递约束会扩大供应链风险，MVP 采用本仓库内确定性替换器完成脱敏；Presidio 只保留
analyzer 边界，负责调用本地规则 recognizer。

### 6. 前端、认证和部署

| 能力 | 选择 |
|---|---|
| 前端 | React + TypeScript + Vite |
| 数据请求 | TanStack Query |
| 路由 | React Router |
| UI | Tailwind CSS + shadcn/ui |
| 认证 | 服务端 Session、随机高熵 token、服务端存储其哈希、Argon2 密码哈希、HttpOnly/Secure/SameSite cookie、CSRF 防护 |
| 部署 | Docker Compose：Caddy、API、Worker；Ollama 为可选 profile |
| HTTPS | Caddy 自动证书或部署者提供的证书 |
| 备份 | restic 加密快照 + SQLite 一致性备份 + evidence/Markdown manifest + 恢复前 erase ledger 重放 |

单用户系统不使用 JWT 作为浏览器会话，不引入 OAuth/SSO，除非部署边界后续发生变化。

### 7. 可观测性、测试和评测

| 能力 | 选择 |
|---|---|
| 结构化日志 | Python logging + structlog JSON renderer |
| 指标 | prometheus-client |
| Trace | OpenTelemetry，本地或自托管导出；默认不向第三方发送原始属性 |
| 单元/集成测试 | pytest、pytest-cov、Hypothesis |
| E2E | Playwright |
| 代码质量 | Ruff + mypy |
| RAG/evolution eval | 仓库内自建固定 JSONL/YAML fixtures 与确定性 runner |

通用 LLM 评测框架可以用于辅助分析，但不能取代候选隔离、状态机、隐私、安全集和发布门的确定性断言。

## 小型检索基准门

仓库中的 `benchmarks/retrieval/benchmark.py` 用合成、无个人信息的中文资料验证部署兼容性和资源预算。基准必须在最终目标服务器上使用锁定依赖和相同参数复跑。

需要记录：

- bge-m3 模型加载时间、文档/查询编码吞吐、p50/p95 延迟、进程 RSS 和可用 GPU 显存。
- FTS5、sqlite-vec 和 RRF 混合检索的查询 p50/p95、Recall@10。
- SQLite 文件大小、向量维度、文档数、查询数、设备、Python/SQLite/sqlite-vec/模型版本。
- 首次冷启动与预热后的稳态结果必须分开解释。

Step 0 的通过条件：

1. sqlite-vec 能加载、建表、写入 bge-m3 向量、查询并从空库重建。
2. 中文 FTS5 使用相同预处理可稳定返回标注目标；小型合成集 Recall@10 不低于 0.90。
3. 混合检索 Recall@10 不低于仅向量检索。
4. 目标服务器的模型加载、稳态延迟、峰值内存和磁盘占用被记录，并由架构负责人基于部署预算明确接受或拒绝；本 ADR 不在缺少目标硬件信息时伪造统一性能阈值。
5. 基准结果不是产品 RAG 质量验收，真实论文、冲突、时效、反方观点和个人记忆仍需独立评测集。

当前开发机结果仅作为兼容性基线；如果开发机不是目标服务器，不得据此关闭目标服务器基准任务。

## 开发机基线结果

2026-09-02 使用锁定模型 revision 和合成数据完成了两档 CPU 基准：

- Smoke：300 chunks / 20 queries。
- Baseline：2,000 chunks / 24 queries，3 次查询迭代；结果见 [`development-macos-cpu.md`](../../../benchmarks/retrieval/results/development-macos-cpu.md)，原始 JSON 见同目录。

Baseline 环境：macOS arm64、10 个逻辑 CPU、24 GB 内存、Python 3.12.9、SQLite 3.45.3、sqlite-vec 0.1.9、sentence-transformers 6.0.1、PyTorch 2.13.0、CPU 推理。当前 PyTorch 环境的 MPS/CUDA 均不可用。

| 指标 | 结果 |
|---|---:|
| bge-m3 缓存后加载 | 1.02 s |
| bge-m3 模型快照 | 2.14 GB |
| 文档编码吞吐 | 29.77 chunks/s |
| 单查询编码 p50 / p95 | 81.81 / 83.74 ms |
| FTS5 查询 p50 / p95 | 0.47 / 1.02 ms |
| sqlite-vec 查询 p50 / p95 | 1.20 / 1.46 ms |
| RRF 混合索引查询 p50 / p95 | 1.70 / 2.27 ms |
| 含查询编码的混合检索 p50 / p95 | 83.84 / 86.19 ms |
| FTS / 向量 / 混合 Recall@10 | 1.000 / 1.000 / 1.000 |
| 进程 RSS：模型后 / 峰值 | 985.56 / 2,208.05 MB |
| SQLite：2,000 chunks，checkpoint 后 | 9.21 MB |

判断：

- SQLite FTS5 + jieba 和 sqlite-vec 0.1.9 在该规模下通过 MVP 兼容性与延迟基线，可以进入实现。
- bge-m3 在 CPU 上的稳态单查询端到端 p95 小于 100 ms，质量链路通过；开发基线接受其作为默认高质量 embedding。
- bge-m3 仍是当前主要资源成本：单个 embedding 进程峰值约 2.2 GB，模型快照约 2.14 GB。目标服务器应至少为 embedding Worker 预留约 3 GB 内存；是否同时运行本地聊天模型必须独立估算，不能从本结果推断。
- 合成集过小且结构清晰，Recall@10 不能代表真实资料质量；需要真实但脱敏/合成的论文和生活知识评测集继续验证。
- 目标服务器尚未指定，因此 Step 0 的“目标服务器性能接受”仍为待办；本次只能关闭技术兼容性和开发机基线。

## 未选择的方案

- PostgreSQL/pgvector：单用户 MVP 不需要多节点写入和服务化运维；满足迁移条件后再决策。
- Qdrant、Chroma、Milvus：增加独立服务、备份和隐私擦除面。
- Redis/RabbitMQ + Celery/RQ/Dramatiq：当前任务规模可由 SQLite outbox 可靠承载。
- LangChain、LlamaIndex：其抽象不能替代正式视图授权、确认门、审计和受保护发布状态机。
- Next.js：MVP 不需要 SSR，额外 Node 服务会增加部署面。
- Kubernetes：当前没有多节点、高可用或自动扩缩容需求。
- PyMuPDF 作为默认解析器：技术能力强，但公开仓库和未来分发需要单独处理 AGPL/商业许可决策。

## 后果

正面：

- 一台服务器即可部署，备份和恢复边界可控。
- AI、解析和评测生态完整，首个进化闭环可以在一个代码库中验证。
- 数据库、索引、模型和任务执行器都有明确替换端口。

负面：

- SQLite 需要严格短事务、WAL checkpoint 和写冲突监控。
- sqlite-vec pre-v1 需要精确锁定与适配器隔离。
- bge-m3 在无 GPU 或小内存服务器上可能成为主要资源瓶颈，必须以目标服务器基准决定是否改用较小模型。
- 自建状态机和 RAG 薄层比快速拼接通用框架需要更多确定性测试，但这是确认门和可验证进化所要求的成本。

## 迁移触发条件

- SQLite 锁等待持续违反已批准 SLO，或需要多节点/多设备并发写：评估 PostgreSQL。
- sqlite-vec 构建、过滤或查询延迟持续超预算：评估 FAISS、Qdrant 或 pgvector。
- Ollama 吞吐无法满足已批准并发且已有 GPU 服务器：评估 vLLM。
- SQLite Worker 无法满足吞吐、租约和故障恢复要求：评估外部 broker，但保留 outbox 作为跨存储提交边界。
- bge-m3 内存或编码延迟超预算：评估较小的中文/多语言 embedding 模型，并在同一评测集上比较召回退化。

## 后续动作

1. ~~运行并保存当前开发机的小型基准结果。~~ 已完成。
2. 确定正式目标服务器后，以相同锁定依赖、模型 revision 和参数复跑。
3. 将目标服务器结果和接受结论附在本 ADR 或独立 benchmark report 中。
4. Step 0 完成前生成项目 `uv.lock`，并在 CI 中验证 Python 3.12、SQLite FTS5 和 sqlite-vec 加载。
