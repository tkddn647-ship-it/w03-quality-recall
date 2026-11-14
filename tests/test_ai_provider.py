"""AI 공급자 전환(AI_PROVIDER=gemini) · 한도 초과(429) → '요약 실패 - 수동 작성'.

Gemini 호출은 httpx.post 가짜로, Claude 호출은 가짜 클라이언트로 대체한다(실제 API 비용·키 불필요).
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("W03_DB", str(Path(tempfile.mkdtemp()) / "test_w03.db"))
for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GEMINI_API_KEY", "AI_PROVIDER"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from w03 import ai, presets  # noqa: E402
from w03.config import GEMINI_MODEL  # noqa: E402

from test_normal_scenario import _ai_output, _manual  # noqa: E402

B1 = presets.B1


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        assert c.post("/admin/reset", data={"confirm_text": "초기화"}, follow_redirects=False).status_code == 303
        c.post("/assess")
        yield c


def _state(c):
    return c.get("/api/state").json()


def _gemini(monkeypatch, reply=None, status=200, body=None, key="test-gemini-key"):
    """AI_PROVIDER=gemini + 가짜 generateContent. reply(요청 본문 → 출력 dict)가 있으면 정상 응답."""
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if key:
        monkeypatch.setenv("GEMINI_API_KEY", key)
    else:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json})
        if reply is None:
            return httpx.Response(status, json=body, request=httpx.Request("POST", url))
        text = __import__("json").dumps(reply(json), ensure_ascii=False)
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
            "modelVersion": GEMINI_MODEL, "responseId": "resp_test"})

    monkeypatch.setattr(ai.httpx, "post", post)
    return calls


def _facts_from(req):
    return json.loads(req["contents"][0]["parts"][0]["text"].split("\n", 1)[1])


# ---------- AI ① 상신 요약 ----------

def test_gemini_summary_pass_uses_same_schema(client, monkeypatch):
    calls = _gemini(monkeypatch, reply=lambda req: _ai_output(_facts_from(req)))
    assert _state(client)["ai_configured"] is True
    client.post("/submissions")
    sub = _state(client)["submissions"][0]
    assert (sub["ai_source"], sub["ai_status"]) == ("gemini", ai.STATUS_OK)
    req = calls[0]
    assert req["url"].endswith(f"/models/{GEMINI_MODEL}:generateContent")
    assert req["json"]["generationConfig"]["responseJsonSchema"] == ai.SCHEMA
    assert req["json"]["generationConfig"]["responseMimeType"] == "application/json"
    assert req["headers"]["x-goog-api-key"] == "test-gemini-key" and "test-gemini-key" not in req["url"]
    assert "Gemini" in client.get("/submissions/1").text
    assert client.post("/submissions/1/batches/B-02-2602-01/decide", data={
        "decision": "approve", "decider": "위원장", "reason": "기준 충족"}, follow_redirects=False).status_code == 303


def test_gemini_summary_mismatch_warns(client, monkeypatch):
    _gemini(monkeypatch, reply=lambda req: _ai_output(_facts_from(req), rate_delta=1.5))
    client.post("/submissions")
    sub = _state(client)["submissions"][0]
    assert sub["ai_status"] == ai.STATUS_WARN
    problems = json.loads(sub["ai_check"])["problems"]
    assert not any("rate_prod_pct" in p for p in problems)        # 목록·수치는 계산값을 넣으므로 인용 불일치 없음
    assert any("본문 숫자" in p for p in problems)                  # AI 문장 속 틀린 숫자는 그대로 잡힘


def test_gemini_429_goes_to_manual_with_reason(client, monkeypatch):
    _gemini(monkeypatch, status=429, body={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                     "message": "Quota exceeded for free tier requests"}})
    client.post("/submissions")
    sub = _state(client)["submissions"][0]
    assert sub["ai_status"] == ai.STATUS_FAIL and sub["ai_source"] is None
    reason = json.loads(sub["ai_check"])["reason"]
    assert reason.startswith("한도 초과(429)") and "Quota exceeded" in reason
    page = client.get("/submissions/1").text
    assert "한도 초과(429)" in page and "test-gemini-key" not in page
    assert _manual(client).status_code == 303                                   # 수동 경로 그대로
    assert _state(client)["submissions"][0]["ai_status"] == ai.STATUS_MANUAL


def test_gemini_without_key_is_manual(client, monkeypatch):
    calls = _gemini(monkeypatch, reply=lambda req: {}, key=None)
    assert _state(client)["ai_configured"] is False
    client.post("/submissions")
    sub = _state(client)["submissions"][0]
    assert sub["ai_status"] == ai.STATUS_FAIL and calls == []
    assert json.loads(sub["ai_check"])["reason"] == "API 키 없음(GEMINI_API_KEY 미설정)"


def test_gemini_other_errors_keep_manual_path(client, monkeypatch):
    _gemini(monkeypatch, status=404, body={"error": {"code": 404, "message": "models/x is not found"}})
    client.post("/submissions")
    assert json.loads(_state(client)["submissions"][0]["ai_check"])["reason"] == "API 오류 404: models/x is not found"


def test_claude_429_goes_to_manual_with_reason(client, monkeypatch):
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def create(**kw):
        resp = httpx.Response(429, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
        raise anthropic.RateLimitError("rate_limit_error: too many requests", response=resp, body=None)

    monkeypatch.setattr(ai.anthropic, "Anthropic",
                        lambda: SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create))))
    client.post("/submissions")
    sub = _state(client)["submissions"][0]
    assert sub["ai_status"] == ai.STATUS_FAIL
    assert json.loads(sub["ai_check"])["reason"].startswith("한도 초과(429)")
    assert "한도 초과(429)" in client.get("/submissions/1").text


# ---------- AI ② 회신 구조화 ----------

def _reply_task(c):
    c.post("/presets/small", data={"confirm_text": "프리셋"})       # 기준선(두 배치 의결) 위에서 고객 통지 Task 사용
    return next(t for t in _state(c)["tasks"] if t["type"] == "고객 통지" and "C-101" in t["title"])


def _intake(c, task, text):
    r = c.post(f"/tasks/{task['id']}/reply-text", data={"raw_text": text, "submitted_by": "고객(모의)"},
               follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers["location"].rsplit("/", 1)[1])


def _row(c, iid):
    return next(i for i in _state(c)["intakes"] if i["id"] == iid)


def test_gemini_reply_structuring_validated(client, monkeypatch):
    calls = _gemini(monkeypatch, reply=lambda req: {"customer_id": "C-101", "batch_id": B1, "held_qty": 12000,
                                                     "recall_agree": True, "pickup_date": "다음 주 화요일",
                                                     "evidence": "재고 12,000개 보유 중"})
    task = _reply_task(client)
    iid = _intake(client, task, presets.REPLY_OK)
    row = _row(client, iid)
    assert (row["source"], row["status"]) == ("gemini", "검증 통과")
    assert set(calls[-1]["json"]["generationConfig"]["responseJsonSchema"]["required"]) >= {"held_qty", "recall_agree"}
    assert "Gemini" in client.get(f"/intakes/{iid}").text


def test_gemini_reply_invented_qty_needs_human(client, monkeypatch):
    _gemini(monkeypatch, reply=lambda req: {"customer_id": "C-101", "batch_id": B1, "held_qty": 5000,
                                            "recall_agree": True, "pickup_date": None, "evidence": ""})
    iid = _intake(client, _reply_task(client), presets.REPLY_OK)
    row = _row(client, iid)
    assert row["status"] == "사람 확인 필요"
    assert any("원문에 없는 수량" in p for p in json.loads(row["validation_json"])["problems"])


def test_gemini_reply_429_goes_to_manual_form(client, monkeypatch):
    _gemini(monkeypatch, status=429, body={"error": {"code": 429, "message": "Resource has been exhausted"}})
    iid = _intake(client, _reply_task(client), presets.REPLY_OK)
    row = _row(client, iid)
    assert (row["source"], row["status"]) == (None, "사람 확인 필요")
    assert "한도 초과(429)" in json.loads(row["validation_json"])["problems"][0]
    page = client.get(f"/intakes/{iid}").text
    assert "한도 초과(429)" in page and "구조화 폼 직접 작성" in page


def test_gemini_timeout_finishes_before_gateway_and_goes_manual(client, monkeypatch):
    _gemini(monkeypatch, reply=lambda req: {})

    def slow(url, headers=None, json=None, timeout=None):
        assert timeout.read < 120                                            # 게이트웨이(120초)보다 먼저 끝난다
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(ai.httpx, "post", slow)
    client.post("/submissions")
    sub = _state(client)["submissions"][0]
    assert sub["ai_status"] == ai.STATUS_FAIL
    assert json.loads(sub["ai_check"])["reason"].startswith("응답 시간 초과")


# ---------- 503 자동 재시도 · 실패 라벨 ----------

def _gemini_sequence(monkeypatch, statuses, reply):
    """상태 코드 순서대로 응답한다(200이면 reply 출력). time.sleep은 기록만 한다."""
    calls = _gemini(monkeypatch, reply=reply)
    seq, sleeps = list(statuses), []
    ok_post = ai.httpx.post

    def post(url, headers=None, json=None, timeout=None):
        status = seq.pop(0) if seq else 200
        if status == 200:
            return ok_post(url, headers=headers, json=json, timeout=timeout)
        calls.append({"url": url, "json": json})
        return httpx.Response(status, request=httpx.Request("POST", url),
                              json={"error": {"code": status, "message": "This model is currently experiencing high demand."}})

    monkeypatch.setattr(ai.httpx, "post", post)
    monkeypatch.setattr(ai.time, "sleep", sleeps.append)
    return calls, sleeps


def test_gemini_503_retried_then_succeeds_and_shows_count(client, monkeypatch):
    calls, sleeps = _gemini_sequence(monkeypatch, [503], lambda req: _ai_output(_facts_from(req)))
    client.post("/submissions")
    sub = _state(client)["submissions"][0]
    assert (sub["ai_source"], sub["ai_status"]) == ("gemini", ai.STATUS_OK)
    assert json.loads(sub["ai_check"])["retries"] == 1 and sleeps == [ai.GEMINI_RETRY_DELAYS_SEC[0]]
    assert len(calls) == 2
    assert "503 자동 재시도 1회" in client.get("/submissions/1").text
    assert "503 자동 재시도 1회" in client.get("/history").text


def test_gemini_503_gives_up_after_two_retries(client, monkeypatch):
    calls, sleeps = _gemini_sequence(monkeypatch, [503, 503, 503, 503], lambda req: {})
    client.post("/submissions")
    sub = _state(client)["submissions"][0]
    chk = json.loads(sub["ai_check"])
    assert sub["ai_status"] == ai.STATUS_FAIL and len(calls) == 3 and sleeps == list(ai.GEMINI_RETRY_DELAYS_SEC)
    assert (chk["code"], chk["retries"]) == ("503", 2) and chk["reason"].endswith("(503 자동 재시도 2회 후)")
    assert "503 자동 재시도 2회 후" in client.get("/submissions/1").text
    assert _manual(client).status_code == 303


def test_gemini_503_not_retried_without_time_left(client, monkeypatch):
    calls, sleeps = _gemini_sequence(monkeypatch, [503, 503], lambda req: {})
    monkeypatch.setattr(ai, "GEMINI_TIMEOUT_SEC", 4)                  # 재시도할 시간이 없으면 바로 실패
    client.post("/submissions")
    chk = json.loads(_state(client)["submissions"][0]["ai_check"])
    assert len(calls) == 1 and sleeps == [] and chk["retries"] == 0


def test_gemini_other_errors_not_retried(client, monkeypatch):
    calls, sleeps = _gemini_sequence(monkeypatch, [429, 200], lambda req: {})
    client.post("/submissions")
    assert len(calls) == 1 and sleeps == []
    assert json.loads(_state(client)["submissions"][0]["ai_check"])["code"] == "429"


def _history_actors():
    from w03 import db
    conn = db.connect()
    rows = [r[0] for r in conn.execute("SELECT actor FROM history WHERE entity_type='reply' ORDER BY id")]
    conn.close()
    return rows


def test_preset_label_shows_actual_failure_reason(client, monkeypatch):
    _gemini_sequence(monkeypatch, [503] * 9, lambda req: {})
    client.post("/presets/reply_ok", data={"confirm_text": "프리셋"})
    actors = _history_actors()
    assert "영업 담당(시연 프리셋, AI 구조화 실패: 503, 재시도 2회)" in actors
    assert not any("AI 키 없음" in a for a in actors)
    client.post("/presets/reply_vague", data={"confirm_text": "프리셋"})
    assert "영업 담당(시연 프리셋, AI 구조화 실패: 503, 재시도 2회 — 원문 그대로 옮김)" in _history_actors()


def test_preset_label_without_key(client, monkeypatch):
    _gemini(monkeypatch, reply=lambda req: {}, key=None)
    client.post("/presets/reply_ok", data={"confirm_text": "프리셋"})
    assert "영업 담당(시연 프리셋, AI 구조화 실패: API 키 없음)" in _history_actors()


def test_default_gemini_model_is_flash_lite():
    from w03 import config
    assert "GEMINI_MODEL" in os.environ or config.GEMINI_MODEL == "gemini-3.5-flash-lite"
