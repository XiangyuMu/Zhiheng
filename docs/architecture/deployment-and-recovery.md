# 部署与恢复

`deploy/docker-compose.yml` 以 Caddy + API + Worker 部署单用户实例，SQLite 数据库位于命名卷。API 仅在 Compose 内部网络暴露，公网入口统一由 Caddy 提供；生产环境应通过 `ZHIHENG_SITE_ADDRESS` 配置域名并启用自动 HTTPS。

本地模型是可选能力。运行 `docker compose --profile local-model up -d` 时，Ollama 与 API 共享网络命名空间，因此 API 仍只通过安全策略允许的 `http://127.0.0.1:11434` 访问模型；未启用 profile 时不会启动或下载本地模型。

`scripts/backup_restic.py` 对 SQLite 在线快照及被引用的 evidence/Markdown 文件生成私有暂存包，校验每个文件的摘要和大小后调用 restic 加密备份，输出完整 snapshot ID。候选和软删除资料仍需可恢复；隐私擦除资料不进入新清单。缺失、损坏、越界路径和未列入清单的文件会阻止备份或恢复。返回码非零（包括 restic 的部分备份返回码 3）不视为成功。

运行前明确配置 `ZHIHENG_DATABASE_PATH`、`ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH`、`RESTIC_REPOSITORY`，并通过私有环境或密码文件配置 `RESTIC_PASSWORD` / `RESTIC_PASSWORD_FILE`。仓库初始化由部署操作单独完成，脚本不会自动初始化、修改保留策略或删除快照。

备份/恢复脚本默认从 `PATH` 调用 `restic`。CI 会显式安装 restic、打印 `restic version`，并通过 `ZHIHENG_RESTIC_BINARY` 固定测试使用的二进制，避免恢复保护用例被环境缺失静默跳过。本地如果需要使用非 PATH 中的二进制，可设置例如 `ZHIHENG_RESTIC_BINARY=/opt/restic/0.19.1/restic`；该值只属于运行环境，不应写入产品代码或提交包含个人路径的配置。

`src/zhiheng/restore.py` 的恢复准备阶段只操作隔离目录：验证完整清单、迁移数据库、将原绝对路径重定位到暂存对象目录、重放最新独立擦除账本并压实数据库。此阶段不安装服务数据库。已用真实 restic 0.19.1 和合成数据验证加密备份、恢复、`check`，并验证旧备份恢复准备不会复活后续擦除的字节，也不会误删现有源目录。

完整安装入口是 `scripts/restore_restic.py`。除备份配置外，必须设置完整 64 位十六进制 `ZHIHENG_RESTIC_SNAPSHOT_ID`、目标数据库/对象目录以及最新独立 `ZHIHENG_ERASE_JOURNAL_PATH` 和 `ZHIHENG_SECRET_KEY`。入口持有独占数据库维护锁和共享账本锁，恢复到私有暂存目录后先重放擦除，再以不可覆盖的方式安装存活文件，重定位数据库引用，并再次针对目标目录重放擦除，最后压实、校验、原子替换数据库。已有内容冲突、符号链接越界、错误目标根目录或 SQLite sidecar 都会阻止安装。

先停止 API/Worker 和所有外部数据库客户端再运行恢复；脚本拒绝活动客户端而非自动终止它们。中断发生在文件安装之后、数据库替换之前时，可能留下未被当前数据库引用的对象；不会提前发布新数据库。独立账本不得放在对象目录内，也绝不从旧快照覆盖。

旧 `scripts/backup.sh` / `restore.sh` 保留为数据库级兼容演练，使用 AES-256-CBC + PBKDF2 和 HMAC；它们不包含完整对象文件，不能替代 restic 完整备份。

`ZHIHENG_ERASE_JOURNAL_PATH` 指定主数据库之外的擦除账本，使用 `ZHIHENG_SECRET_KEY` 验证记录。擦除意图在数据库修改前写入账本，并同步到磁盘；恢复必须使用包含备份之后所有擦除意图的最新账本。伴随的 `.head` 文件签名记录最后序号及摘要，用于检测账本末尾截断。账本或其末尾记录缺失、签名错误或不一致时，已有非空账本的恢复拒绝执行。

账本与 `.head` 必须一起独立保管，不能从待恢复的旧备份覆盖。两者同时回退到同一历史副本不能仅靠本地签名发现，灾难恢复仍需验证最新保管副本。写入中断造成两者不一致时目前会拒绝继续，需要恢复经过核验的最新副本。

恢复入口 `scripts/restore.sh` 持有独占数据库维护锁，覆盖校验、迁移、擦除重放及替换全过程。应用数据库连接在打开前持有同一侧文件的共享锁，直到连接真正关闭（包括连接池中的空闲连接）；因此恢复会拒绝仍在运行的应用，恢复期间新的应用连接也会被拒绝。维护锁文件不得删除。原生 sqlite3 或其他不遵守该锁协议的客户端仍须停用，不应直接运行内部 `restore_database.sh`。

部署验收尚未完成：仍需完成账本中断恢复及灾难恢复保管验证、服务停启操作演练和容器演练。容器构建曾因 Docker Hub 匿名 token 请求超时而失败，且本地无缓存 Python 基础镜像；Compose 配置校验通过不等于容器运行通过。本地完整恢复测试不代替目标服务器签收。

密码只通过环境变量注入，不得写入仓库或命令行参数。

官方参考：[仓库初始化与密码](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html)、[备份与返回码](https://restic.readthedocs.io/en/stable/040_backup.html)、[按 snapshot ID 恢复](https://restic.readthedocs.io/en/stable/050_restore.html)。

### Restic restore execution deadline

The restore CLI bounds the restic subprocess (including its transport process
group) with a 30-second default deadline. Set
`ZHIHENG_RESTIC_RESTORE_TIMEOUT_SECONDS` to a positive, finite number of seconds
for larger repositories. Outer job/test deadlines must allow additional time for
journal replay, bundle validation and installation.

On a restic failure the CLI exits nonzero and writes a JSON diagnostic to stderr
with `error_code`, `reason` and `elapsed_seconds`. Codes are
`RESTIC_RESTORE_TIMEOUT`, `RESTIC_REPOSITORY_LOCKED` (restic exit 11),
`RESTIC_AUTH_FAILED` (exit 12), `RESTIC_RESTORE_FAILED` (other nonzero exits),
`RESTIC_RESTORE_UNAVAILABLE` and `RESTIC_RESTORE_CONFIG_INVALID`.
Older restic versions without distinct exit codes use the generic failure code.
Raw subprocess output is discarded so repository URLs and credentials cannot
appear in these diagnostics. A timeout kills and reaps the restic process group;
the staging directory is removed before returning, without replacing the live
database. This deadline covers restic execution, not the later SQLite validation
and installation steps. Existing preflight validation failures retain their
current diagnostics.
