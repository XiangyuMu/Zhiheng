from typing import Any

__all__ = [
    "G005RetrievalEvaluationResult",
    "G005RetrievalMetrics",
    "evaluate_g005_retrieval_runtime",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from zhiheng.evaluation.g005_retrieval import (
            G005RetrievalEvaluationResult,
            G005RetrievalMetrics,
            evaluate_g005_retrieval_runtime,
        )

        exports = {
            "G005RetrievalEvaluationResult": G005RetrievalEvaluationResult,
            "G005RetrievalMetrics": G005RetrievalMetrics,
            "evaluate_g005_retrieval_runtime": evaluate_g005_retrieval_runtime,
        }
        return exports[name]
    raise AttributeError(name)
