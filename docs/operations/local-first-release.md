# 单用户首版本地操作手册

以下命令在 macOS/Linux 的 Bash 或 Zsh 中执行。工作目录始终为仓库根目录。
路径均由当前用户目录推导；示例不含真实密钥。此手册验证本机回环地址，不覆盖公网生产部署。

## 1. 空环境与依赖

需要 Git、Python 3.12、uv、Node.js/npm、restic 和浏览器。可用系统包管理器安装：

```sh
# macOS（已安装 Homebrew）
brew install uv node restic
# Ubuntu 的 restic：sudo apt-get update && sudo apt-get install restic
# uv 的安装方法：https://docs.astral.sh/uv/getting-started/installation/
uv python install 3.12
restic version
node --version
npm --version

git clone https://github.com/XiangyuMu/Zhiheng.git
cd Zhiheng
git switch main
# 本地尚未推送的交付候选应使用其现有检出或可信 Git bundle；远端 main 不代表未推送提交。
git rev-parse HEAD
uv sync --frozen --extra dev
```

统一验收使用锁定的 Python 和 npm 依赖；Playwright 会由验收脚本安装。
向量模型下载、本地大模型和外部 Provider 不是本地文本链路的安装前提。

## 2. 首次配置与迁移

只在首次创建实例时运行下段。配置独立于仓库，不会随检出清理；脚本拒绝覆盖已有配置和账本。
`development` 仅用于回环地址上的本地首版，切勿将此配置直接暴露公网。

```sh
export ZHIHENG_LOCAL_ROOT="$HOME/.local/share/zhiheng"
umask 077
uv run python - <<'PY'
import os, secrets, shlex
from pathlib import Path
root = Path(os.environ['ZHIHENG_LOCAL_ROOT']).expanduser().resolve()
root.mkdir(parents=True, exist_ok=True, mode=0o700)
config = root / 'local.env'
if config.exists() or (root / 'erase-journal.jsonl').exists():
    raise SystemExit('实例已存在；请加载原配置，不要重新生成密钥或清空账本')
(root / 'objects').mkdir(mode=0o700, exist_ok=True)
values = {
    'ZHIHENG_ENVIRONMENT': 'development',
    'ZHIHENG_DATABASE_URL': f'sqlite:///{root / "zhiheng.sqlite"}',
    'ZHIHENG_DATABASE_PATH': str(root / 'zhiheng.sqlite'),
    'ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH': str(root / 'objects'),
    'ZHIHENG_ERASE_JOURNAL_PATH': str(root / 'erase-journal.jsonl'),
    'ZHIHENG_SECRET_KEY': secrets.token_urlsafe(48),
    'ZHIHENG_EXTERNAL_MODELS_ENABLED': 'false',
    'ZHIHENG_API_HOST': '127.0.0.1',
    'ZHIHENG_API_PORT': '8000',
}
with config.open('x') as f:
    f.write(''.join(f'export {k}={shlex.quote(v)}\n' for k, v in values.items()))
(root / 'erase-journal.jsonl').touch(exist_ok=False, mode=0o600)
PY
. "$ZHIHENG_LOCAL_ROOT/local.env"
uv run --frozen python scripts/upgrade_database.py "$ZHIHENG_DATABASE_PATH"
```

空账本只用于全新实例；曾有擦除记录的实例必须保留原账本、`.head` 和可能存在的 `.pending`，
不能通过创建空文件绕过恢复检查。密钥用于会话和账本验证，升级时必须保持不变。
不要复制开发仓库中的 `.env`；额外的本机 Provider 等配置会改变行为。

## 3. 启动与浏览器使用

在两个终端分别进入同一仓库，加载同一配置：

```sh
export ZHIHENG_LOCAL_ROOT="$HOME/.local/share/zhiheng"
. "$ZHIHENG_LOCAL_ROOT/local.env"
# 终端 A
uv run --frozen uvicorn zhiheng.api.main:app --host 127.0.0.1 --port 8000
```

```sh
export ZHIHENG_LOCAL_ROOT="$HOME/.local/share/zhiheng"
. "$ZHIHENG_LOCAL_ROOT/local.env"
# 终端 B
uv run --frozen zhiheng-worker --role worker --idle-seconds 1
```

第三个终端可执行 `curl --fail http://127.0.0.1:8000/healthz`。打开
`http://127.0.0.1:8000/login`，首次创建账户（密码至少 12 字符），后续使用原账户登录。
浏览器流程：研究页 → 导入 → 粘贴文本并提交 → 等待后台任务完成 → 知识库搜索 → 点击资料/引用回到原文。
只有 `succeeded` 且原文对象、索引指针存在时才可检索；资料列表出现不等于处理完成。

审核中心处理结论草稿、前提、分类和关系；分类中心逐条确认迁移；相关冲突或缺失信息弹窗可延期、跳过或补充。
PDF 无可选解析器时应显示 `unsupported` 和原因。404 停止轮询，5xx/网络错误有限重试后提示暂时无法确认，
不能将它们视为导入成功。需要诊断时保留 API 和 Worker 输出，不手动修改任务状态。
停止服务时分别在终端 A/B 按 Ctrl-C，等待退出；数据库未迁移或恢复检查失败时两者都会拒绝启动。

## 4. 完整备份与独立账本保管

首次初始化 restic 仓库（已有仓库跳过初始化和密码生成）。密码文件由程序随机生成，不放入仓库：

```sh
export RESTIC_REPOSITORY="$ZHIHENG_LOCAL_ROOT/restic-repository"
export RESTIC_PASSWORD_FILE="$ZHIHENG_LOCAL_ROOT/restic-password"
uv run python - <<'PY'
import os, secrets
from pathlib import Path
with Path(os.environ['RESTIC_PASSWORD_FILE']).open('x') as f:
    f.write(secrets.token_urlsafe(48) + '\n')
PY
chmod 600 "$RESTIC_PASSWORD_FILE"
restic init
```

后续备份仅需重新加载实例配置，并设置上述 `RESTIC_REPOSITORY`、`RESTIC_PASSWORD_FILE`：

```sh
uv run --frozen python scripts/backup_restic.py
restic snapshots --json
restic check
```

备份脚本输出完整 snapshot ID，保留它用于恢复。脚本校验数据库和原文清单；非零退出不算成功。
同一磁盘上的备份只能用于本地演练，不能防止整盘损坏。独立安全保存实例配置、restic 密码、
**最新**擦除账本及其 `.head`（以及存在时的 `.pending`）；不要把账本副本回退到所恢复快照的时间。
账本需随着后续擦除持续更新，复制一致版本前应停止写入服务。异机保管及目标服务器灾难演练属于后续运维。

## 5. 升级与恢复

升级：停止 API/Worker → 完成备份并记录旧 SHA/snapshot → 切换到经审查的目标提交 →
`uv sync --frozen --extra dev` → 加载原配置 → 执行下列迁移 → 重启两个服务并检查浏览器链路。

```sh
uv run --frozen python scripts/upgrade_database.py "$ZHIHENG_DATABASE_PATH"
```

不要用旧代码直接打开新 schema。需要回退时使用经验证的备份恢复流程，而非手改 Alembic 版本。

先在**新目标目录**演练恢复，停止 API/Worker 及所有数据库客户端。保持原密钥和最新外部账本，
从 `restic snapshots --json` 复制完整 64 位 snapshot ID 到提示中：

```sh
printf '输入完整 snapshot ID: '
read -r ZHIHENG_RESTIC_SNAPSHOT_ID
export ZHIHENG_RESTIC_SNAPSHOT_ID
export ZHIHENG_RESTIC_RESTORE_TIMEOUT_SECONDS=300
export ZHIHENG_RESTORE_ROOT="$ZHIHENG_LOCAL_ROOT/recovery-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$ZHIHENG_RESTORE_ROOT"
export ZHIHENG_DATABASE_PATH="$ZHIHENG_RESTORE_ROOT/zhiheng.sqlite"
export ZHIHENG_DATABASE_URL="sqlite:///$ZHIHENG_DATABASE_PATH"
export ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH="$ZHIHENG_RESTORE_ROOT/objects"
uv run --frozen python scripts/restore_restic.py
uv run --frozen python scripts/upgrade_database.py "$ZHIHENG_DATABASE_PATH"
```

两个服务启动时使用这组恢复后的路径（不要再加载会覆盖路径的原 `local.env`），保留原密钥和账本，
重复登录 → 搜索 → 查看原文；核对已擦除资料不会出现。隔离验证成功后再安排实际切换，原数据暂保留。
恢复遇到锁、账本缺失/签名异常、对象冲突或超时，应保留诊断并修复来源；不要删除维护锁或清空账本强行继续。
超时只覆盖 restic 子进程阶段。更完整限制见 [恢复设计](../architecture/deployment-and-recovery.md)。

## 6. 同提交交付验收

使用最终提交的干净检出，不带 `.env`、运行数据或未提交文件；系统 `PATH` 上必须有 restic。
从原仓库导出已提交版本的隔离本地 clone（不推送）：

```sh
release_sha=$(git rev-parse HEAD)
release_root="$HOME/.local/share/zhiheng-delivery"
mkdir -p "$release_root"
release_checkout="$release_root/checkout-$release_sha"
git clone --no-hardlinks --no-checkout . "$release_checkout"
git -C "$release_checkout" checkout --detach "$release_sha"
cd "$release_checkout"
uv run --frozen python scripts/delivery_acceptance.py \
  --output "$release_root/evidence-$release_sha" --timeout 7200
```

每次重跑换一个全新的输出目录，保留失败报告。统一门禁执行迁移、真实 API/Worker、浏览器、全量 pytest、
Ruff/Mypy 和固定集提炼。验收机器需保持唤醒，暂停或系统休眠可能导致浏览器计时超时，发生后仍记录为失败并重跑。
核对 `report.json` 的 SHA、干净状态、无跳过、所有阶段通过，以及 `browser/report.json` 中完整的工具版本。
日志和截图仅使用合成数据，完整证据存于持久目录；将报告与 [需求矩阵](../testing/issue-17-delivery-matrix.md)
一起交付，不能以本说明或历史绿色测试替代。
