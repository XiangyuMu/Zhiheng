from __future__ import annotations

from zhiheng.jobs.classification import (
    ClaimedClassificationJob,
    ClassificationSuggestionJobExecutor,
    process_classification_jobs_once,
)
from zhiheng.jobs.knowledge_contract import (
    ImportPublicStatus,
    JobStatus,
    KnowledgeLifecycleStatus,
)
from zhiheng.jobs.knowledge_indexing import (
    KnowledgeIndexJobExecutor,
    KnowledgeJobRepository,
    process_knowledge_jobs_once,
)
from zhiheng.jobs.memory_extraction import (
    MEMORY_EXTRACTION_EVENT,
    MEMORY_EXTRACTION_JOB_TYPE,
    ClaimedMemoryExtractionJob,
    ExtractedMemory,
    HeuristicConversationMemoryExtractor,
    MemoryExtractionJobExecutor,
    process_memory_extraction_jobs_once,
)
from zhiheng.jobs.outbox import OutboxRepository


class PdfCapabilityUnavailable(RuntimeError):
    """Stable failure for a worker without the optional PDF parser."""

    code = "unsupported_pdf_parser"


try:
    from zhiheng.jobs.pdf_parsing import (  # type: ignore[import-not-found]
        KNOWLEDGE_PARSE_PDF_JOB_TYPE,
        PdfParseJobExecutor,
        PdfParseJobResult,
        configured_pdf_parse_executor,
    )
except ModuleNotFoundError as exc:  # Optional PDF parser dependencies are outside the MVP runtime.
    if exc.name != "zhiheng.jobs.pdf_parsing":
        raise

    KNOWLEDGE_PARSE_PDF_JOB_TYPE = "knowledge.parse_pdf"
    PdfParseJobExecutor = object
    PdfParseJobResult = object

    def configured_pdf_parse_executor(*args: object, **kwargs: object) -> None:
        raise PdfCapabilityUnavailable(
            "PDF parsing is unsupported: install the optional PDF parser dependencies"
        )


__all__ = [
    "KnowledgeIndexJobExecutor",
    "KnowledgeJobRepository",
    "ImportPublicStatus",
    "JobStatus",
    "KnowledgeLifecycleStatus",
    "OutboxRepository",
    "ClaimedClassificationJob",
    "ClassificationSuggestionJobExecutor",
    "process_classification_jobs_once",
    "KNOWLEDGE_PARSE_PDF_JOB_TYPE",
    "PdfParseJobExecutor",
    "PdfParseJobResult",
    "configured_pdf_parse_executor",
    "PdfCapabilityUnavailable",
    "process_knowledge_jobs_once",
    "ClaimedMemoryExtractionJob",
    "ExtractedMemory",
    "HeuristicConversationMemoryExtractor",
    "MemoryExtractionJobExecutor",
    "MEMORY_EXTRACTION_EVENT",
    "MEMORY_EXTRACTION_JOB_TYPE",
    "process_memory_extraction_jobs_once",
]
