# Zhiheng 检索基准结果

- 运行时间：2026-09-02T04:51:40.624696+00:00
- 机器：macOS-26.6.2-arm64-arm-64bit / arm64
- Python：3.12.9
- 设备：cpu
- 文档 / 查询：2000 / 24
- 模型：BAAI/bge-m3 @ `5617a9f61b028005a4858fdac845db406aefb181`

## 模型与资源

- 模型加载：1024.0 ms
- 文档编码：67173.3 ms，29.77 docs/s
- 查询编码 p50 / p95：81.8 / 83.7 ms
- 模型快照磁盘：2.14 GB
- RSS 基线 / 模型后 / 峰值：387.9 / 985.6 / 2208.1 MB
- 基准 wall / CPU：73.49 / 123.42 s
- SQLite 大小（checkpoint 后）：9.21 MB

## 检索

| 路径 | p50 | p95 | Recall@10 |
|---|---:|---:|---:|
| FTS5 | 0.47 ms | 1.02 ms | 1.000 |
| sqlite-vec | 1.20 ms | 1.46 ms | 1.000 |
| RRF 混合（不含编码） | 1.70 ms | 2.27 ms | 1.000 |
| RRF 混合端到端 | 83.84 ms | 86.19 ms | 1.000 |

## 自动检查

- PASS：sqlite_vec_loaded — extension=v0.1.9, package=0.1.9
- PASS：bge_m3_dimensions — dimensions=1024
- PASS：fts_recall_at_10 — recall=1.000, threshold=0.900
- PASS：hybrid_not_below_vector — hybrid=1.000, vector=1.000

> 该结果只验证合成数据上的技术兼容性与资源基线。若运行机器不是最终目标服务器，必须在目标服务器复跑后才能完成 Step 0 性能验收。
