from __future__ import annotations

import httpx
import pytest

from zhiheng.models.gateway import _diagnostic_code

_REQUEST = httpx.Request("POST", "https://provider.example.test/v1/chat/completions")


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadError("[Errno 32] Broken pipe", request=_REQUEST),
        httpx.ReadError("", request=_REQUEST),
        httpx.WriteError("[Errno 32] Broken pipe", request=_REQUEST),
        httpx.ReadError("[Errno 104] Connection reset by peer", request=_REQUEST),
        httpx.RemoteProtocolError(
            "Server disconnected without sending a response.", request=_REQUEST
        ),
        OSError(32, "Broken pipe"),
    ],
)
def test_transport_level_failures_report_network_error(exc: Exception) -> None:
    assert _diagnostic_code(exc) == "network_error"


def test_server_error_status_stays_provider_error() -> None:
    response = httpx.Response(500, request=_REQUEST)
    with pytest.raises(httpx.HTTPStatusError) as raised:
        response.raise_for_status()

    assert _diagnostic_code(raised.value) == "provider_error"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ReadTimeout("timed out", request=_REQUEST), "timeout"),
        (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"), "tls_error"),
        (httpx.ReadError("401 auth rejected", request=_REQUEST), "authentication_failed"),
    ],
)
def test_specific_codes_keep_precedence_over_network_error(exc: Exception, expected: str) -> None:
    assert _diagnostic_code(exc) == expected
