"""Benchmark Zhiheng's MVP Chinese retrieval stack with synthetic public-safe data."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import platform
import random
import sqlite3
import statistics
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jieba
import numpy as np
import psutil
import sqlite_vec
from sentence_transformers import SentenceTransformer

MODEL_ID = "BAAI/bge-m3"
TOP_K = 10
RETRIEVAL_DEPTH = 50
RRF_K = 60


@dataclass(frozen=True)
class GoldCase:
    key: str
    title: str
    text: str
    query: str


GOLD_CASES: tuple[GoldCase, ...] = (
    GoldCase(
        "candidate-memory",
        "候选记忆确认门",
        "Agent 推断出的用户偏好只能进入候选记忆。用户确认以前，候选内容不能进入正式画像、推荐特征、回答上下文或决策约束。",
        "如何防止未确认的用户偏好影响正式推荐？",
    ),
    GoldCase(
        "evidence-source",
        "原始证据与摘要",
        "原始论文、网页快照和对话轨迹是根证据。摘要、知识卡片和向量索引都是派生表示，不能替代原始证据。",
        "知识摘要能否替代论文原文作为证据？",
    ),
    GoldCase(
        "sqlite-wal",
        "SQLite WAL 与短事务",
        "SQLite 使用 WAL 和 busy timeout。API 与 Worker 都可以执行短写事务，但模型调用、网络访问、文档解析和嵌入不得位于数据库事务中。",
        "为什么模型调用不能放在 SQLite 写事务里面？",
    ),
    GoldCase(
        "fts5",
        "中文全文检索",
        "中文全文检索使用 jieba 预分词后的派生字段写入 SQLite FTS5，同时保留未经分词的原始正文用于展示和引用。",
        "中文资料如何使用 FTS5 做关键词检索？",
    ),
    GoldCase(
        "vector-index",
        "可重建向量索引",
        "向量索引只负责语义召回，是可以从正式内容重建的派生数据。索引记录必须携带来源版本和确认代次。",
        "向量数据库是不是知识事实的权威来源？",
    ),
    GoldCase(
        "hybrid-rag",
        "混合 RAG",
        "普通知识查询同时进行 BM25 和向量召回，再使用 RRF 融合、过滤和可选重排序，最后回到原始证据生成引用。",
        "BM25 和语义向量怎样组合成混合检索？",
    ),
    GoldCase(
        "agentic-rag",
        "Agentic RAG 路由",
        "只有跨文档综合、冲突分析和复杂决策才使用多轮 Agentic RAG。标题、日期和明确状态应使用简单查询。",
        "哪些问题才应该启动多轮 Agentic RAG？",
    ),
    GoldCase(
        "privacy-gateway",
        "外部模型隐私网关",
        "外部模型调用前必须依次执行数据最小化、敏感度分类、个人信息检测、占位符脱敏和二次复检。任何步骤不确定都要拒绝外发。",
        "发送给外部模型之前需要经过哪些隐私检查？",
    ),
    GoldCase(
        "local-model",
        "本地模型回退边界",
        "敏感内容优先使用本地模型。如果脱敏失败且本地模型不可用，系统应解释并拒绝请求，不能把原文降级发送给外部 API。",
        "本地模型不可用时能否把敏感原文发给云模型？",
    ),
    GoldCase(
        "soft-delete",
        "可恢复删除",
        "soft delete 会写入 tombstone 并立即从正式查询隐藏，但保留原始字节和审计记录，因此可以恢复为新的版本和确认代次。",
        "哪一种删除方式可以恢复原始资料？",
    ),
    GoldCase(
        "privacy-erase",
        "隐私擦除状态机",
        "privacy erase 是不可逆操作。系统必须先持久化擦除账本 intent，再取消任务、清除权威内容和派生索引，逐项验证零残留后才能完成。",
        "隐私擦除为什么必须先写账本再删除文件？",
    ),
    GoldCase(
        "outbox",
        "Outbox 与后台任务",
        "导入事务在 SQLite 中同时提交内容版本、当前指针和 outbox。后台 Worker 使用幂等键、租约、心跳、退避和 dead letter 处理任务。",
        "后台 Worker 如何避免任务静默丢失？",
    ),
    GoldCase(
        "dead-letter",
        "死信重试",
        "dead letter 是可观察的失败终态。人工重试必须创建新的 pending attempt，并保留之前每次失败原因，不能覆盖历史。",
        "死信任务重新执行时应该怎样保留失败历史？",
    ),
    GoldCase(
        "reviewer",
        "独立审核权限",
        "Proposer 只能提交提案，Validator 只能追加验证结果，Reviewer 只能追加审核决定，只有确定性 Publisher 可以推进发布状态。",
        "为什么生成策略提案的 Agent 不能自己批准发布？",
    ),
    GoldCase(
        "canary",
        "单用户 Canary",
        "单用户策略发布先经过离线 replay 和 shadow，再进入限定任务族 canary。样本量或观察期不足时不得自动晋升，稳定版本始终可以立即回退。",
        "单用户流量很少时怎样进行策略灰度发布？",
    ),
    GoldCase(
        "eval-sets",
        "持续进化评测集",
        "提案发布前需要通过边界集、迁移集、保留集和安全集。安全集出现任何新增失败都必须阻止发布或立即回滚。",
        "持续进化提案发布前需要通过哪些评测集？",
    ),
    GoldCase(
        "knowledge-gap",
        "知识缺口推荐",
        "知识缺口推荐应关联正式目标、覆盖率、资料质量、时效和观点多样性。知识库缺少资料不能直接表述为用户能力不足。",
        "如何避免把知识库缺资料说成用户没有能力？",
    ),
    GoldCase(
        "decision-support",
        "决策支持边界",
        "决策回答应展示目标、约束、方案、收益、成本、机会成本、风险和关键假设，但不能自动交易、发送消息或执行其他外部动作。",
        "个人决策 Agent 可以自动替用户下单吗？",
    ),
    GoldCase(
        "paper-import",
        "论文导入流程",
        "论文导入先保存不可变 PDF，再提取元数据、章节和证据定位，生成知识单元候选，确认后建立 FTS 和向量索引。",
        "一篇 PDF 论文从上传到可以问答需要哪些步骤？",
    ),
    GoldCase(
        "image-evidence",
        "图片证据",
        "摄影、穿搭和论文图表必须保留原图。文字描述只用于辅助检索，不能取代视觉证据。",
        "图片的自动文字描述能否替代原图保存？",
    ),
    GoldCase(
        "backup-restore",
        "备份恢复与擦除重放",
        "恢复 SQLite 和证据快照以前必须先加载独立擦除账本，并在 readiness 前重放所有 durable erase intent，避免旧备份复活已擦除对象。",
        "怎样防止恢复旧备份后重新出现已擦除资料？",
    ),
    GoldCase(
        "model-audit",
        "模型调用审计",
        "模型调用审计记录 provider、model、数据分类、脱敏规则版本、HMAC payload fingerprint、token、成本、延迟和结果状态，不默认保存原始 prompt。",
        "模型调用审计应该记录什么且避免记录什么？",
    ),
    GoldCase(
        "l0-profile",
        "L0 核心画像",
        "L0 只包含已经正式确认的身份、活跃目标、关键约束和风险，并有严格体积预算；原始细节通过 L2 证据检索核验。",
        "核心用户画像应该加载哪些内容？",
    ),
    GoldCase(
        "mece",
        "MECE 主分类",
        "每条知识只有一个主生活领域分类，可以拥有多个标签、实体和跨领域关系。掌握程度不参与一级分类。",
        "知识分类是否应该按照用户有没有掌握来划分？",
    ),
)


DISTRACTOR_TEMPLATES: tuple[str, ...] = (
    "摄影曝光由光圈、快门速度和感光度共同决定。练习时应记录光线、焦段和拍摄意图。",
    "资产配置需要考虑期限、流动性、风险承受能力和再平衡规则，历史收益不能保证未来表现。",
    "研究计划应把问题、假设、实验、数据和复现实验分开记录，避免只保存最终结论。",
    "沟通中的积极倾听包括复述事实、确认感受、澄清需要和讨论可执行的下一步。",
    "新闻资料需要区分事实报道、评论、预测和未经证实的说法，并保留发布时间和来源。",
    "穿搭选择可以从场景、天气、版型、颜色和个人舒适度五个维度进行比较。",
    "软件测试需要覆盖正常路径、边界条件、异常恢复和可观测性，不能只验证函数返回值。",
    "论文阅读可以先识别研究问题、方法、数据、结论和限制，再与相关工作建立联系。",
    "长期学习计划需要定义目标、先修知识、练习反馈和阶段复盘，避免只收集学习资料。",
    "健康信息具有时效性和个体差异，重要判断应核验可靠来源并咨询专业人员。",
)


class PeakRssSampler:
    def __init__(self, process: psutil.Process, interval: float = 0.05) -> None:
        self.process = process
        self.interval = interval
        self.peak = process.memory_info().rss
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> int:
        self._stop.set()
        self._thread.join(timeout=2)
        self.peak = max(self.peak, self.process.memory_info().rss)
        return self.peak

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.peak = max(self.peak, self.process.memory_info().rss)
            except psutil.Error:
                return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=int, default=300)
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument(
        "--device", choices=("auto", "cpu", "mps", "cuda"), default="auto"
    )
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument("--revision", default="main")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260902)
    args = parser.parse_args()
    if args.documents < len(GOLD_CASES):
        parser.error(f"--documents must be at least {len(GOLD_CASES)}")
    if not 1 <= args.queries <= len(GOLD_CASES):
        parser.error(f"--queries must be between 1 and {len(GOLD_CASES)}")
    if args.iterations < 1 or args.warmup < 0 or args.batch_size < 1:
        parser.error(
            "iterations and batch-size must be positive; warmup cannot be negative"
        )
    return args


def choose_device(requested: str) -> str:
    if requested != "auto":
        return requested
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def resolve_model_revision(model_id: str, requested_revision: str) -> str:
    is_commit = len(requested_revision) == 40 and all(
        character in "0123456789abcdef" for character in requested_revision.lower()
    )
    if is_commit:
        return requested_revision
    try:
        from huggingface_hub import model_info

        return model_info(model_id, revision=requested_revision).sha
    except (httpx.HTTPError, OSError, RuntimeError, ValueError):
        return requested_revision


def pretokenize(text: str) -> str:
    return " ".join(
        token.strip() for token in jieba.cut(text, cut_all=False) if token.strip()
    )


def fts_match_expression(text: str) -> str:
    tokens = []
    for token in jieba.cut_for_search(text):
        token = token.strip()
        if not token or all(not char.isalnum() and char != "_" for char in token):
            continue
        tokens.append('"' + token.replace('"', '""') + '"')
    return " OR ".join(dict.fromkeys(tokens)) or '"__empty_query__"'


def make_corpus(
    document_count: int, seed: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rng = random.Random(seed)
    documents: list[dict[str, Any]] = []
    gold_rowids: dict[str, int] = {}
    for case in GOLD_CASES:
        rowid = len(documents) + 1
        documents.append(
            {
                "id": rowid,
                "doc_id": f"gold-{case.key}",
                "text": f"{case.title}\n{case.text}",
            }
        )
        gold_rowids[case.key] = rowid
    while len(documents) < document_count:
        index = len(documents) + 1
        template = rng.choice(DISTRACTOR_TEMPLATES)
        qualifier = rng.choice(
            ("基础记录", "阶段笔记", "复盘摘要", "学习卡片", "资料摘录")
        )
        documents.append(
            {
                "id": index,
                "doc_id": f"synthetic-{index:05d}",
                "text": f"{qualifier} {index}\n{template} 本条为公开合成基准资料，编号 {index}。",
            }
        )
    return documents, gold_rowids


def percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def latency_summary(samples_ms: Sequence[float]) -> dict[str, float]:
    return {
        "count": len(samples_ms),
        "mean_ms": round(statistics.fmean(samples_ms), 3) if samples_ms else 0.0,
        "p50_ms": round(percentile(samples_ms, 0.50), 3),
        "p95_ms": round(percentile(samples_ms, 0.95), 3),
        "max_ms": round(max(samples_ms), 3) if samples_ms else 0.0,
    }


def bytes_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def rss_mb(process: psutil.Process) -> float:
    return round(process.memory_info().rss / (1024 * 1024), 2)


def serialize_vector(vector: np.ndarray) -> bytes:
    array = np.asarray(vector, dtype=np.float32)
    return sqlite_vec.serialize_float32(array.tolist())


def fts_search(
    connection: sqlite3.Connection, query: str, limit: int
) -> list[tuple[int, float]]:
    rows = connection.execute(
        """
        SELECT rowid, bm25(chunks_fts) AS score
        FROM chunks_fts
        WHERE chunks_fts MATCH ?
        ORDER BY score
        LIMIT ?
        """,
        (fts_match_expression(query), limit),
    ).fetchall()
    return [(int(rowid), float(score)) for rowid, score in rows]


def vector_search(
    connection: sqlite3.Connection, query_vector: np.ndarray, limit: int
) -> list[tuple[int, float]]:
    rows = connection.execute(
        """
        SELECT rowid, distance
        FROM chunk_vec
        WHERE embedding MATCH ? AND k = ?
        ORDER BY distance
        """,
        (serialize_vector(query_vector), limit),
    ).fetchall()
    return [(int(rowid), float(distance)) for rowid, distance in rows]


def rrf_merge(
    fts_rows: Sequence[tuple[int, float]],
    vector_rows: Sequence[tuple[int, float]],
    limit: int,
) -> list[int]:
    scores: dict[int, float] = {}
    for rows in (fts_rows, vector_rows):
        for rank, (rowid, _) in enumerate(rows, start=1):
            scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (RRF_K + rank)
    return [
        rowid
        for rowid, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[
            :limit
        ]
    ]


def recall_at_k(results: dict[str, Sequence[int]], gold: dict[str, int]) -> float:
    hits = sum(1 for key, rowid in gold.items() if rowid in results.get(key, ()))
    return hits / len(gold) if gold else 0.0


def gpu_memory(device: str) -> dict[str, float | None]:
    import torch

    if device == "cuda" and torch.cuda.is_available():
        return {
            "allocated_mb": round(torch.cuda.memory_allocated() / (1024 * 1024), 2),
            "peak_allocated_mb": round(
                torch.cuda.max_memory_allocated() / (1024 * 1024), 2
            ),
        }
    if device == "mps" and hasattr(torch, "mps"):
        current = getattr(torch.mps, "current_allocated_memory", lambda: 0)()
        driver = getattr(torch.mps, "driver_allocated_memory", lambda: 0)()
        return {
            "allocated_mb": round(current / (1024 * 1024), 2),
            "driver_allocated_mb": round(driver / (1024 * 1024), 2),
        }
    return {"allocated_mb": None, "peak_allocated_mb": None}


def markdown_report(result: dict[str, Any]) -> str:
    retrieval = result["retrieval"]
    resources = result["resources"]
    model = result["embedding"]
    lines = [
        "# Zhiheng 检索基准结果",
        "",
        f"- 运行时间：{result['run']['created_at']}",
        f"- 机器：{result['system']['platform']} / {result['system']['machine']}",
        f"- Python：{result['system']['python']}",
        f"- 设备：{result['system']['device']}",
        f"- 文档 / 查询：{result['dataset']['documents']} / {result['dataset']['queries']}",
        f"- 模型：{model['model_id']} @ `{model['resolved_revision']}`",
        "",
        "## 模型与资源",
        "",
        f"- 模型加载：{model['model_load_ms']:.1f} ms",
        f"- 文档编码：{model['document_encode_ms']:.1f} ms，{model['documents_per_second']:.2f} docs/s",
        f"- 查询编码 p50 / p95：{model['query_latency']['p50_ms']:.1f} / {model['query_latency']['p95_ms']:.1f} ms",
        f"- 模型快照磁盘：{model['model_snapshot_bytes'] / (1024 * 1024 * 1024):.2f} GB",
        f"- RSS 基线 / 模型后 / 峰值：{resources['rss_before_model_mb']:.1f} / {resources['rss_after_model_mb']:.1f} / {resources['rss_peak_mb']:.1f} MB",
        f"- 基准 wall / CPU：{resources['benchmark_wall_seconds']:.2f} / {resources['process_cpu_seconds']:.2f} s",
        f"- SQLite 大小（checkpoint 后）：{resources['db_after_checkpoint_bytes'] / (1024 * 1024):.2f} MB",
        "",
        "## 检索",
        "",
        "| 路径 | p50 | p95 | Recall@10 |",
        "|---|---:|---:|---:|",
        f"| FTS5 | {retrieval['fts_latency']['p50_ms']:.2f} ms | {retrieval['fts_latency']['p95_ms']:.2f} ms | {retrieval['fts_recall_at_10']:.3f} |",
        f"| sqlite-vec | {retrieval['vector_latency']['p50_ms']:.2f} ms | {retrieval['vector_latency']['p95_ms']:.2f} ms | {retrieval['vector_recall_at_10']:.3f} |",
        f"| RRF 混合（不含编码） | {retrieval['hybrid_index_latency']['p50_ms']:.2f} ms | {retrieval['hybrid_index_latency']['p95_ms']:.2f} ms | {retrieval['hybrid_recall_at_10']:.3f} |",
        f"| RRF 混合端到端 | {retrieval['hybrid_e2e_latency']['p50_ms']:.2f} ms | {retrieval['hybrid_e2e_latency']['p95_ms']:.2f} ms | {retrieval['hybrid_recall_at_10']:.3f} |",
        "",
        "## 自动检查",
        "",
    ]
    for name, check in result["checks"].items():
        lines.append(
            f"- {'PASS' if check['passed'] else 'FAIL'}：{name} — {check['detail']}"
        )
    lines.extend(
        [
            "",
            "> 该结果只验证合成数据上的技术兼容性与资源基线。若运行机器不是最终目标服务器，必须在目标服务器复跑后才能完成 Step 0 性能验收。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    benchmark_started = time.perf_counter()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    device = choose_device(args.device)
    process = psutil.Process()
    cpu_before = process.cpu_times()
    sampler = PeakRssSampler(process)
    sampler.start()
    rss_before_model = rss_mb(process)
    created_at = datetime.now(UTC).isoformat()
    resolved_revision = resolve_model_revision(args.model, args.revision)
    documents, all_gold = make_corpus(args.documents, args.seed)
    cases = GOLD_CASES[: args.queries]
    gold = {case.key: all_gold[case.key] for case in cases}

    with tempfile.TemporaryDirectory(prefix="zhiheng-retrieval-benchmark-") as temp_dir:
        db_path = Path(temp_dir) / "retrieval.sqlite3"
        connection = sqlite3.connect(db_path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
        sqlite_version = connection.execute("SELECT sqlite_version()").fetchone()[0]
        sqlite_vec_version = connection.execute("SELECT vec_version()").fetchone()[0]

        connection.executescript(
            """
            CREATE TABLE chunks (
                id INTEGER PRIMARY KEY,
                doc_id TEXT NOT NULL UNIQUE,
                raw_text TEXT NOT NULL,
                fts_text TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE chunks_fts USING fts5(
                fts_text,
                content='chunks',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 0'
            );
            """
        )
        fts_insert_started = time.perf_counter()
        connection.executemany(
            "INSERT INTO chunks(id, doc_id, raw_text, fts_text) VALUES (?, ?, ?, ?)",
            [
                (doc["id"], doc["doc_id"], doc["text"], pretokenize(doc["text"]))
                for doc in documents
            ],
        )
        connection.execute(
            "INSERT INTO chunks_fts(rowid, fts_text) SELECT id, fts_text FROM chunks"
        )
        connection.commit()
        fts_insert_ms = (time.perf_counter() - fts_insert_started) * 1000

        model_load_started = time.perf_counter()
        model = SentenceTransformer(
            args.model, revision=resolved_revision, device=device
        )
        model_load_ms = (time.perf_counter() - model_load_started) * 1000
        rss_after_model = rss_mb(process)
        from huggingface_hub import snapshot_download

        snapshot_path = Path(
            snapshot_download(
                args.model, revision=resolved_revision, local_files_only=True
            )
        )
        model_snapshot_bytes = directory_size(snapshot_path)

        if device == "cuda":
            import torch

            torch.cuda.reset_peak_memory_stats()

        warmup_text = ["知衡检索技术基准预热文本"]
        for _ in range(args.warmup):
            model.encode(
                warmup_text,
                batch_size=1,
                normalize_embeddings=True,
                show_progress_bar=False,
            )

        document_encode_started = time.perf_counter()
        document_vectors = model.encode(
            [doc["text"] for doc in documents],
            batch_size=args.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        ).astype(np.float32, copy=False)
        document_encode_ms = (time.perf_counter() - document_encode_started) * 1000
        dimensions = int(document_vectors.shape[1])
        if dimensions != 1024:
            raise RuntimeError(
                f"Expected bge-m3 to produce 1024 dimensions, got {dimensions}"
            )

        connection.execute(
            f"CREATE VIRTUAL TABLE chunk_vec USING vec0(embedding float[{dimensions}])"
        )
        vector_insert_started = time.perf_counter()
        connection.executemany(
            "INSERT INTO chunk_vec(rowid, embedding) VALUES (?, ?)",
            [
                (doc["id"], serialize_vector(vector))
                for doc, vector in zip(documents, document_vectors, strict=True)
            ],
        )
        connection.commit()
        vector_insert_ms = (time.perf_counter() - vector_insert_started) * 1000

        query_vectors: dict[str, np.ndarray] = {}
        query_encode_latencies: list[float] = []
        for case in cases:
            started = time.perf_counter()
            vector = model.encode(
                [case.query],
                batch_size=1,
                normalize_embeddings=True,
                show_progress_bar=False,
                convert_to_numpy=True,
            )[0].astype(np.float32, copy=False)
            query_encode_latencies.append((time.perf_counter() - started) * 1000)
            query_vectors[case.key] = vector

        for case in cases[: min(args.warmup, len(cases))]:
            fts_search(connection, case.query, RETRIEVAL_DEPTH)
            vector_search(connection, query_vectors[case.key], RETRIEVAL_DEPTH)

        fts_results: dict[str, list[int]] = {}
        vector_results: dict[str, list[int]] = {}
        hybrid_results: dict[str, list[int]] = {}
        fts_latencies: list[float] = []
        vector_latencies: list[float] = []
        hybrid_index_latencies: list[float] = []
        rng = random.Random(args.seed + 1)
        ordered_cases = list(cases) * args.iterations
        rng.shuffle(ordered_cases)
        for case in ordered_cases:
            started = time.perf_counter()
            fts_rows = fts_search(connection, case.query, RETRIEVAL_DEPTH)
            fts_latencies.append((time.perf_counter() - started) * 1000)

            started = time.perf_counter()
            vector_rows = vector_search(
                connection, query_vectors[case.key], RETRIEVAL_DEPTH
            )
            vector_latencies.append((time.perf_counter() - started) * 1000)

            started = time.perf_counter()
            hybrid_rows = rrf_merge(
                fts_search(connection, case.query, RETRIEVAL_DEPTH),
                vector_search(connection, query_vectors[case.key], RETRIEVAL_DEPTH),
                TOP_K,
            )
            hybrid_index_latencies.append((time.perf_counter() - started) * 1000)
            fts_results[case.key] = [rowid for rowid, _ in fts_rows[:TOP_K]]
            vector_results[case.key] = [rowid for rowid, _ in vector_rows[:TOP_K]]
            hybrid_results[case.key] = hybrid_rows

        hybrid_e2e_latencies: list[float] = []
        for case in cases:
            started = time.perf_counter()
            vector = model.encode(
                [case.query],
                batch_size=1,
                normalize_embeddings=True,
                show_progress_bar=False,
                convert_to_numpy=True,
            )[0].astype(np.float32, copy=False)
            rrf_merge(
                fts_search(connection, case.query, RETRIEVAL_DEPTH),
                vector_search(connection, vector, RETRIEVAL_DEPTH),
                TOP_K,
            )
            hybrid_e2e_latencies.append((time.perf_counter() - started) * 1000)

        db_before_checkpoint = bytes_size(db_path)
        wal_before_checkpoint = bytes_size(Path(str(db_path) + "-wal"))
        shm_before_checkpoint = bytes_size(Path(str(db_path) + "-shm"))
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
        db_after_checkpoint = bytes_size(db_path)
        wal_after_checkpoint = bytes_size(Path(str(db_path) + "-wal"))
        shm_after_checkpoint = bytes_size(Path(str(db_path) + "-shm"))

        peak_rss = sampler.stop() / (1024 * 1024)
        cpu_after = process.cpu_times()
        process_cpu_seconds = (cpu_after.user - cpu_before.user) + (
            cpu_after.system - cpu_before.system
        )
        benchmark_wall_seconds = time.perf_counter() - benchmark_started
        fts_recall = recall_at_k(fts_results, gold)
        vector_recall = recall_at_k(vector_results, gold)
        hybrid_recall = recall_at_k(hybrid_results, gold)
        result = {
            "run": {
                "created_at": created_at,
                "seed": args.seed,
                "command_arguments": vars(args)
                | {"output": str(args.output), "report": str(args.report)},
                "result_fingerprint": "",
            },
            "system": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "python": platform.python_version(),
                "sqlite": sqlite_version,
                "sqlite_vec": sqlite_vec_version,
                "device": device,
                "logical_cpu_count": psutil.cpu_count(logical=True),
                "physical_cpu_count": psutil.cpu_count(logical=False),
                "system_memory_mb": round(
                    psutil.virtual_memory().total / (1024 * 1024), 2
                ),
                "packages": {
                    "sqlite-vec": package_version("sqlite-vec"),
                    "sentence-transformers": package_version("sentence-transformers"),
                    "torch": package_version("torch"),
                    "numpy": package_version("numpy"),
                    "jieba": package_version("jieba"),
                    "psutil": package_version("psutil"),
                },
            },
            "dataset": {
                "documents": len(documents),
                "queries": len(cases),
                "gold_cases": list(gold),
                "synthetic_only": True,
                "contains_personal_data": False,
            },
            "embedding": {
                "model_id": args.model,
                "requested_revision": args.revision,
                "resolved_revision": resolved_revision,
                "dimensions": dimensions,
                "normalized": True,
                "batch_size": args.batch_size,
                "model_load_ms": round(model_load_ms, 3),
                "document_encode_ms": round(document_encode_ms, 3),
                "documents_per_second": round(
                    len(documents) / (document_encode_ms / 1000), 3
                ),
                "query_latency": latency_summary(query_encode_latencies),
                "model_snapshot_bytes": model_snapshot_bytes,
            },
            "indexing": {
                "fts_insert_ms": round(fts_insert_ms, 3),
                "vector_insert_ms": round(vector_insert_ms, 3),
                "vector_bytes_estimated": len(documents) * dimensions * 4,
            },
            "retrieval": {
                "top_k": TOP_K,
                "retrieval_depth": RETRIEVAL_DEPTH,
                "rrf_k": RRF_K,
                "iterations": args.iterations,
                "warmup": args.warmup,
                "fts_latency": latency_summary(fts_latencies),
                "vector_latency": latency_summary(vector_latencies),
                "hybrid_index_latency": latency_summary(hybrid_index_latencies),
                "hybrid_e2e_latency": latency_summary(hybrid_e2e_latencies),
                "fts_recall_at_10": round(fts_recall, 4),
                "vector_recall_at_10": round(vector_recall, 4),
                "hybrid_recall_at_10": round(hybrid_recall, 4),
            },
            "resources": {
                "rss_before_model_mb": rss_before_model,
                "rss_after_model_mb": rss_after_model,
                "rss_peak_mb": round(peak_rss, 2),
                "benchmark_wall_seconds": round(benchmark_wall_seconds, 3),
                "process_cpu_seconds": round(process_cpu_seconds, 3),
                "cpu_to_wall_ratio": round(
                    process_cpu_seconds / benchmark_wall_seconds, 3
                ),
                "gpu": gpu_memory(device),
                "db_before_checkpoint_bytes": db_before_checkpoint,
                "wal_before_checkpoint_bytes": wal_before_checkpoint,
                "shm_before_checkpoint_bytes": shm_before_checkpoint,
                "db_after_checkpoint_bytes": db_after_checkpoint,
                "wal_after_checkpoint_bytes": wal_after_checkpoint,
                "shm_after_checkpoint_bytes": shm_after_checkpoint,
            },
            "checks": {
                "sqlite_vec_loaded": {
                    "passed": sqlite_vec_version.removeprefix("v")
                    == package_version("sqlite-vec"),
                    "detail": f"extension={sqlite_vec_version}, package={package_version('sqlite-vec')}",
                },
                "bge_m3_dimensions": {
                    "passed": dimensions == 1024,
                    "detail": f"dimensions={dimensions}",
                },
                "fts_recall_at_10": {
                    "passed": fts_recall >= 0.90,
                    "detail": f"recall={fts_recall:.3f}, threshold=0.900",
                },
                "hybrid_not_below_vector": {
                    "passed": hybrid_recall >= vector_recall,
                    "detail": f"hybrid={hybrid_recall:.3f}, vector={vector_recall:.3f}",
                },
            },
        }
        fingerprint_input = json.dumps(
            result, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")
        result["run"]["result_fingerprint"] = hashlib.sha256(
            fingerprint_input
        ).hexdigest()
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        args.report.write_text(markdown_report(result), encoding="utf-8")

    print(
        json.dumps(
            {
                "output": str(args.output),
                "report": str(args.report),
                "checks": result["checks"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if all(check["passed"] for check in result["checks"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
