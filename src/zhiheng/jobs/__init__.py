from __future__ import annotations

from zhiheng.core.config import Settings
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
from zhiheng.jobs.pdf_parsing import (
    KNOWLEDGE_PARSE_PDF_JOB_TYPE,
    PdfParseJobExecutor,
    PdfParseJobResult,
)
from zhiheng.jobs.pdf_parsing import (
    configured_pdf_parse_executor as _configured_pdf_parse_executor,
)


class PdfCapabilityUnavailable(RuntimeError):
    """Stable failure for a worker without the optional PDF parser."""

    code = "unsupported_pdf_parser"


def configured_pdf_parse_executor(settings: Settings | None = None) -> PdfParseJobExecutor | None:
    if settings is None:
        raise PdfCapabilityUnavailable(
            "PDF parsing is unsupported: install the optional PDF parser dependencies"
        )
    return _configured_pdf_parse_executor(settings)

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
