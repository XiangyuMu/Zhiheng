# 完整 pytest 交付证据（#27）

在独立、干净的检出中安装锁定依赖并运行门禁；不要复制开发目录的数据库、
`.env` 或对象存储。需要 Python 3.12、uv 和可执行的 restic。

```sh
uv sync --all-extras --frozen
uv run ruff check .
uv run mypy src tests
uv run python scripts/pytest_delivery_gate.py --output /tmp/zhiheng-pytest-evidence --timeout 7200
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
