# Zhiheng 检索基准结果

- 运行时间：2026-09-02T04:50:21.744045+00:00
- 机器：macOS-26.6.2-arm64-arm-64bit / arm64
- Python：3.12.9
- 设备：cpu
- 文档 / 查询：300 / 20
- 模型：BAAI/bge-m3 @ `5617a9f61b028005a4858fdac845db406aefb181`

## 模型与资源

- 模型加载：1078.1 ms
- 文档编码：12185.5 ms，24.62 docs/s
- 查询编码 p50 / p95：79.7 / 85.9 ms
- 模型快照磁盘：2.14 GB
- RSS 基线 / 模型后 / 峰值：388.2 / 987.2 / 2042.3 MB
- 基准 wall / CPU：17.92 / 24.43 s
- SQLite 大小（checkpoint 后）：4.24 MB

## 检索

| 路径 | p50 | p95 | Recall@10 |
|---|---:|---:|---:|
| FTS5 | 0.22 ms | 0.35 ms | 1.000 |
| sqlite-vec | 0.67 ms | 0.79 ms | 1.000 |
| RRF 混合（不含编码） | 0.89 ms | 1.15 ms | 1.000 |
| RRF 混合端到端 | 82.18 ms | 85.48 ms | 1.000 |

## 自动检查

- PASS：sqlite_vec_loaded — extension=v0.1.9, package=0.1.9
- PASS：bge_m3_dimensions — dimensions=1024
- PASS：fts_recall_at_10 — recall=1.000, threshold=0.900
- PASS：hybrid_not_below_vector — hybrid=1.000, vector=1.000

> 该结果只验证合成数据上的技术兼容性与资源基线。若运行机器不是最终目标服务器，必须在目标服务器复跑后才能完成 Step 0 性能验收。
