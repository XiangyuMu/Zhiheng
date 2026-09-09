class QuerySessionStub:
    """Unit-only session for injected lookup/retrieval/verifier ports.

    These tests exercise orchestration without SQLite. Transaction correctness
    is covered separately with real sessions in integration tests.
    """

    def __init__(self) -> None:
        self.commit_count = 0

    def commit(self) -> None:
        self.commit_count += 1
