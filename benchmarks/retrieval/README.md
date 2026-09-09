# 检索技术基准

该基准只验证 Step 0 技术组合在指定硬件上的兼容性、延迟和资源占用：

- `BAAI/bge-m3` 文档与查询编码
- SQLite FTS5 + jieba 中文预分词
- sqlite-vec 0.1.9 向量写入与 KNN 查询
- FTS5 与向量结果的 RRF 混合检索

它使用确定性生成的合成知识片段，不读取知衡的正式数据库、私人配置或个人资料，也不等同于产品 RAG 质量评测。

## 环境

生产基线是 Python 3.12。建议使用独立环境：

```bash
UV_CACHE_DIR=/tmp/zhiheng-uv-cache uv venv --python 3.12 /tmp/zhiheng-retrieval-bench
UV_CACHE_DIR=/tmp/zhiheng-uv-cache uv pip install \
  --python /tmp/zhiheng-retrieval-bench/bin/python \
  -r benchmarks/retrieval/requirements.txt
```

## Smoke 基准

```bash
/tmp/zhiheng-retrieval-bench/bin/python benchmarks/retrieval/benchmark.py \
  --documents 300 \
  --queries 20 \
  --iterations 3 \
  --warmup 3 \
  --batch-size 4 \
  --device auto \
  --revision 5617a9f61b028005a4858fdac845db406aefb181 \
  --output benchmarks/retrieval/results/smoke.json \
  --report benchmarks/retrieval/results/smoke.md
```

## 目标服务器基线

```bash
/tmp/zhiheng-retrieval-bench/bin/python benchmarks/retrieval/benchmark.py \
  --documents 2000 \
  --queries 24 \
  --iterations 3 \
  --warmup 5 \
  --batch-size 8 \
  --device auto \
  --revision 5617a9f61b028005a4858fdac845db406aefb181 \
  --output benchmarks/retrieval/results/target-server.json \
  --report benchmarks/retrieval/results/target-server.md
```

有 CUDA 的服务器应分别运行 `--device cpu` 和 `--device cuda`；Apple Silicon 可以分别运行 `--device cpu` 和 `--device mps`。首次运行需要下载 bge-m3，因此模型下载时间不计入 `model_load_ms` 的可比性结论；正式对比应在模型已缓存后再运行一次。

如果目标环境无法直连 Hugging Face，可以由部署者显式设置受信任镜像，例如 `HF_ENDPOINT=https://hf-mirror.com`；报告仍必须记录并固定上面的模型 commit，不能只记录 `main`。

## 输出与解释

JSON 是机器可读的完整结果，Markdown 是摘要。关键字段包括：

- 模型加载时间、RSS、文档编码吞吐。
- 单条查询编码 p50/p95。
- FTS、向量与混合索引查询 p50/p95。
- 含查询编码的混合端到端 p50/p95。
- FTS、向量与混合 Recall@10。
- checkpoint 前后的 SQLite、WAL 与 SHM 大小。

合成集的 Recall@10 只用于检查检索链路和混合策略没有明显退化。真实论文、跨文档综合、冲突、时效和反方观点必须使用独立评测集。

## Step 0 判定

- sqlite-vec 能加载、写入 1024 维向量并完成 KNN 查询。
- 中文 FTS Recall@10 不低于 0.90。
- 混合 Recall@10 不低于向量 Recall@10。
- 目标服务器资源与延迟有完整记录，并由架构负责人根据部署预算接受。
- 如果这里只运行了开发机，结果只能标记为开发基线，不能关闭目标服务器任务。
