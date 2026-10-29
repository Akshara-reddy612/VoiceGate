"""Bounded retry on transient Gemini overload, exercised through the real google-genai client.

No network: the SDK is given an httpx client whose transport returns scripted status codes.
"""

import asyncio
import json

import httpx
import pytest
from google.genai import errors as genai_errors

import agent.llm_loop as llm_loop
from agent.llm_loop import GeminiChatModel, Message

OK_BODY = {"candidates": [{"content": {"role": "model", "parts": [{"text": "OK"}]}, "finishReason": "STOP"}]}


def _error_body(code, status):
    return {"error": {"code": code, "status": status, "message": f"scripted {status}"}}


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    # Same retry policy (attempt count, status codes); only the sleeps are shrunk so tests stay fast.
    monkeypatch.setattr(llm_loop, "RETRY_INITIAL_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(llm_loop, "RETRY_MAX_DELAY_SECONDS", 0.02)
    monkeypatch.setattr(llm_loop, "RETRY_JITTER_SECONDS", 0.0)
    monkeypatch.setattr(llm_loop, "RATE_LIMIT_DEFAULT_WAIT_SECONDS", 0.01)


def scripted_model(statuses):
    """GeminiChatModel whose HTTP layer answers with `statuses` in order (200 = success).
    An item may be (status, error_body) to script Google's error details."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert ":generateContent" in request.url.path
        item = statuses[min(len(seen), len(statuses) - 1)]
        status, body = item if isinstance(item, tuple) else (item, None)
        seen.append(status)
        if status == 200:
            return httpx.Response(200, json=OK_BODY)
        names = {429: "RESOURCE_EXHAUSTED", 503: "UNAVAILABLE", 404: "NOT_FOUND", 500: "INTERNAL"}
        return httpx.Response(status, json=body or _error_body(status, names[status]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = GeminiChatModel(model="gemini-test", api_key="AIza-unit-test-not-real", httpx_async_client=client)
    return model, seen


def generate(model):
    return asyncio.run(model.generate("system", [Message(role="user", text="hi")], []))


def test_503_then_success_is_retried():
    model, seen = scripted_model([503, 503, 200])
    assert generate(model).text == "OK"
    assert seen == [503, 503, 200]


def test_429_then_success_is_retried():
    model, seen = scripted_model([429, 200])
    assert generate(model).text == "OK"
    assert seen == [429, 200]


def test_persistent_503_stops_after_bounded_attempts():
    model, seen = scripted_model([503])  # always 503
    with pytest.raises(genai_errors.ServerError) as e:
        generate(model)
    assert e.value.code == 503
    assert len(seen) == llm_loop.RETRY_ATTEMPTS == 3  # bounded: never retries indefinitely


@pytest.mark.parametrize("status, exc", [(404, genai_errors.ClientError), (500, genai_errors.ServerError)])
def test_non_transient_errors_are_not_retried(status, exc):
    model, seen = scripted_model([status, 200])
    with pytest.raises(exc):
        generate(model)
    assert seen == [status]  # one attempt only: a bad model or a server bug is not "try again"


def test_retry_policy_is_configured_on_the_sdk():
    opts = llm_loop.gemini_http_options()
    r = opts.retry_options
    assert r.attempts == 3 and r.http_status_codes == [503] and r.exp_base == 2.0  # 429 is handled separately
    assert opts.timeout == llm_loop.REQUEST_TIMEOUT_SECONDS * 1000


# ---------- 429: quota / rate limit (per project). At most one retry, guided by Google's own details ----------


def quota_429(quota_id=None, retry_delay=None):
    details = []
    if quota_id:
        details.append({"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_requests",
                                        "quotaId": quota_id, "quotaValue": "20"}]})
    if retry_delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay})
    return (429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded.", "details": details}})


def test_short_rate_limit_is_retried_once_after_googles_delay():
    model, seen = scripted_model([quota_429("GenerateRequestsPerMinutePerProjectPerModel", "0.01s"), 200])
    assert generate(model).text == "OK" and seen == [429, 200]


def test_persistent_rate_limit_gets_only_one_retry():
    model, seen = scripted_model([quota_429(retry_delay="0.01s")])  # always 429
    with pytest.raises(genai_errors.ClientError):
        generate(model)
    assert seen == [429, 429]  # bounded: 1 call + 1 retry, no SDK retries stacked on top


def test_daily_quota_is_not_retried():
    model, seen = scripted_model([quota_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "0.01s"), 200])
    with pytest.raises(genai_errors.ClientError):
        generate(model)
    assert seen == [429]  # retrying cannot help before the daily reset; spend nothing more


def test_long_retry_delay_fails_fast():
    model, seen = scripted_model([quota_429("GenerateRequestsPerMinutePerProjectPerModel", "45s"), 200])
    with pytest.raises(genai_errors.ClientError):
        generate(model)
    assert seen == [429]


def test_quota_details_parsing():
    e = genai_errors.ClientError(429, quota_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "33.5s")[1])
    q = llm_loop.quota_details(e)
    assert q == {"quota_id": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                 "quota_metric": "generativelanguage.googleapis.com/generate_content_requests",
                 "quota_limit": "20", "retry_after_seconds": 33.5, "daily": True}
    bare = llm_loop.quota_details(genai_errors.ClientError(429, {"error": {"code": 429, "message": "x"}}))
    assert bare["quota_id"] is None and bare["retry_after_seconds"] is None and bare["daily"] is False
