# 完整 pytest 交付证据（#27）

在独立、干净的检出中安装锁定依赖并运行门禁；不要复制开发目录的数据库、
`.env` 或对象存储。需要 Python 3.12、uv、Node.js/npm 和可执行的 restic。
restic 使用系统安装（macOS：`brew install restic`；Ubuntu：`sudo apt-get install restic`），
先运行 `restic version` 确认可用；统一采集只使用 `PATH`，不会读取本机临时目录回退。

```sh
uv sync --extra dev --frozen
uv run ruff check .
uv run mypy src tests
uv run python scripts/pytest_delivery_gate.py --output "$HOME/.local/share/zhiheng-pytest-evidence" --timeout 7200
```

每次使用一个尚不存在、位于检出目录之外的输出目录。测试运行所用 SHA、命令、
依赖版本、耗时、退出码及工作树状态写入 `evidence.json`；`pytest.log` 和
`pytest.xml` 保留完整输出及 JUnit 结果。超时或中断属于不完整执行，不能认定
未执行的测试通过，也不能把测试耗时直接当作功能故障。

失败所在测试文件仅表示功能分组，不自动证明根因。依据失败详情、单独复现和
调用链确认根因后，在交付报告记录：首个失败、全部失败节点、复现命令、根因、
修复提交及重跑结果。隐私擦除、restic 外部工具和 G006 发布链必须分别归因。

必须检查隐私擦除、G006 固定集、发布 validation、生命周期、晋升/回滚、
Worker recovery 和 restic restore 的必需证据组，不能以跳过代替通过。
CI 上传与提交绑定的 `pytest-delivery` artifact，即使测试失败也保留证据。

本门禁通过只代表完整 pytest 及指定测试组通过。规范、类型、迁移、API/Worker
启动及真实浏览器验收仍须按 ADR 0004 单独通过，才能声明产品交付完成。
已知产品缺陷不能改称 MVP 限制；MVP 限制需引用已确认范围并说明测试所证明的边界。

## 持久保存交付证据

本机交付证据使用检出目录之外的持久目录，避免系统清理 `/tmp` 丢失原始日志：

```sh
release_sha=$(git rev-parse HEAD)
uv run python scripts/delivery_acceptance.py \
  --output "$HOME/.local/share/zhiheng-delivery/evidence-${release_sha}" \
  --timeout 7200
```

在其他机器将输出根目录替换为自己的持久目录；每次运行使用全新的目录。
该命令保存完整 pytest 日志、JUnit、增量事件、浏览器截图和 API/Worker 日志，
以及带相对路径 SHA-256 清单的统一报告。清单包含浏览器子报告；仅顶层报告自身不参与自身哈希。
证据目录不随临时检出清理，也不提交含运行时信息的完整日志到产品仓库。

历史 `421fbae` 原件曾从临时目录复制至当时的持久证据根目录并逐文件核对哈希。
迁移归档时保留子目录 `421fbaec46cf9ee64adb4d8b51c8532d73dc2922/`；
实际根目录由证据保管者提供，不属于安装或执行依赖。它仅证明该历史 SHA；
后续代码的签收必须使用新的同 SHA 报告。
本地持久目录并不等同于异机备份；需要转移时完整复制目录并按报告中的哈希核对。
