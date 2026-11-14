"""앱이 빈 DB로 시작하면 시작 상태(업무일 07-01 · 판정 완료 · 회수상신 #1)를 자동으로 만든다."""
import json
import json as _json
import os
import sys
import tempfile
import threading
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("W03_DB", str(Path(tempfile.mkdtemp()) / "test_w03.db"))
for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GEMINI_API_KEY", "AI_PROVIDER"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from w03 import ai, db  # noqa: E402

from test_normal_scenario import _ai_output  # noqa: E402


def _start_fresh(monkeypatch):
    """DB를 '빈 상태'(스키마 버전 0)로 만들고 자동 생성을 켠 채 앱을 시작 → 백그라운드 상신이 끝날 때까지 기다린다."""
    conn = db.connect()
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    monkeypatch.setenv("W03_AUTOSEED", "1")
    client = TestClient(main.app)
    client.__enter__()
    for t in threading.enumerate():
        if t.name == "w03-seed":
            t.join(timeout=30)
    return client


def _state(c):
    return c.get("/api/state").json()


def test_fresh_start_builds_start_state_with_ai_summary(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")

    def post(url, headers=None, json=None, timeout=None):
        facts = _json.loads(json["contents"][0]["parts"][0]["text"].split("\n", 1)[1])
        text = _json.dumps(_ai_output(facts), ensure_ascii=False)
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}], "modelVersion": "gemini-test"})

    monkeypatch.setattr(ai.httpx, "post", post)
    c = _start_fresh(monkeypatch)
    try:
        s = _state(c)
        assert s["business_date"] == "2026-07-01"
        assert [(b["batch_id"], b["state"]) for b in s["batches"] if b["state"] != "정상"] == [
            ("B-02-2602-01", "회수대상"), ("B-02-2603-01", "회수대상")]
        [sub] = s["submissions"]
        assert (sub["id"], sub["kind"], sub["status"], sub["ai_source"], sub["ai_status"]) == (1, "회수상신", "상신", "gemini", ai.STATUS_OK)
        assert s["tasks"] == [] and s["claim_additions"] == [] and s["intakes"] == []
        assert main.SEED["state"] == "done" and "회수상신 #1" in main.SEED["detail"]
        assert [q["kind"] for q in s["decision_queue"]] == ["위원회 의결"]
        hist = c.get("/history").text
        assert "빈 DB로 시작" in hist
    finally:
        c.__exit__(None, None, None)


def test_fresh_start_ai_failure_keeps_manual_path_with_reason(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setattr(ai.httpx, "post", lambda url, **kw: httpx.Response(
        429, request=httpx.Request("POST", url), json={"error": {"code": 429, "message": "Quota exceeded"}}))
    c = _start_fresh(monkeypatch)
    try:
        [sub] = _state(c)["submissions"]
        assert sub["ai_status"] == ai.STATUS_FAIL
        assert json.loads(sub["ai_check"])["reason"].startswith("한도 초과(429)")
        assert "한도 초과(429)" in c.get("/submissions/1").text
        assert c.post("/submissions/1/summary", data={"author": "품질팀", "headline": "h", "summary": "s", "actions": "-",
                                                      "questions": "-"}, follow_redirects=False).status_code == 303
    finally:
        c.__exit__(None, None, None)


def test_existing_db_is_not_reseeded(monkeypatch):
    with TestClient(main.app) as c:
        c.post("/admin/reset", data={"confirm_text": "초기화"})
    monkeypatch.setenv("W03_AUTOSEED", "1")
    with TestClient(main.app) as c:                                  # 스키마 그대로 → 빈 DB 아님 → 자동 생성 안 함
        assert _state(c)["submissions"] == [] and _state(c)["assessment"] == []
