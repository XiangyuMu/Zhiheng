from typing import Any

__all__ = [
    "G005RetrievalEvaluationResult",
    "G005RetrievalMetrics",
    "evaluate_g005_retrieval_runtime",
    "Issue29EvaluationResult",
    "Issue29Metrics",
    "evaluate_issue29_conclusion_extraction",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        if name in {
            "Issue29EvaluationResult",
            "Issue29Metrics",
            "evaluate_issue29_conclusion_extraction",
        }:
            from zhiheng.evaluation.issue29_conclusion_extraction import (
                Issue29EvaluationResult,
                Issue29Metrics,
                evaluate_issue29_conclusion_extraction,
            )

            exports = {
                "Issue29EvaluationResult": Issue29EvaluationResult,
                "Issue29Metrics": Issue29Metrics,
                "evaluate_issue29_conclusion_extraction": evaluate_issue29_conclusion_extraction,
            }
            return exports[name]
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
