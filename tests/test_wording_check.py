"""의결 전 상신 AI 문장의 표현 검사(R3): 회수를 진행·실행·결정된 것처럼 쓰면 '경고(표현)'. 원인 조사 진행은 제외."""
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("W03_DB", str(Path(tempfile.mkdtemp()) / "test_w03.db"))
for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GEMINI_API_KEY", "AI_PROVIDER"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from w03 import ai, db, presets  # noqa: E402

B1 = presets.B1
ACTIONS = [{"dept": "영업", "action": "승인 시 고객별 회수요청 통지"}, {"dept": "물류", "action": "승인 시 회수 진행 상황 공유"}]


@pytest.fixture
def c():
    with TestClient(main.app) as client:
        assert client.post("/admin/reset", data={"confirm_text": "초기화"}, follow_redirects=False).status_code == 303
        yield client


def _gemini(monkeypatch, summary):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    written = {"summary": summary, "actions": ACTIONS, "open_questions": []}

    def post(url, headers=None, json=None, timeout=None):
        text = __import__("json").dumps(written, ensure_ascii=False)
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]})

    monkeypatch.setattr(ai.httpx, "post", post)


def _submit(c):
    c.post("/assess")
    c.post("/submissions")
    conn = db.connect()
    row = dict(conn.execute("SELECT * FROM submission WHERE id=1").fetchone())
    conn.close()
    return row["ai_status"], json.loads(row["ai_check"])["problems"]


def test_prompt_forbids_writing_recall_as_decided():
    assert "회수 실행은 위원회 판단 대상으로만 씁니다" in ai.SYSTEM and "'원인 조사를 진행합니다'는 쓸 수 있습니다" in ai.SYSTEM


@pytest.mark.parametrize("text, hit", [
    ("원인 규명을 기다리지 않고 회수를 진행합니다 (R4).", "회수를 진행합니다"),
    ("회수가 결정되었습니다.", "회수가 결정되었"),
    ("즉시 회수를 실행해야 합니다.", "회수를 실행해야"),
    ("회수 결정을 내렸습니다.", "회수 결정을 내렸"),
    ("원인 조사는 회수와 별도 트랙으로 진행합니다 (R4).", None),         # 원인 조사 진행은 괜찮음
    ("회수 실행은 품질위원회 의결 사항입니다 (R3).", None),
    ("위원회가 회수 실행 여부를 판단해야 합니다.", None),
    ("승인 시 회수를 진행합니다.", None),                               # 조건부
    ("승인 시 회수 진행 상황 공유", None),
])
def test_preempting_phrases(text, hit):
    assert ai.preempting_phrases(text) == ([hit] if hit else [])


def test_decided_sounding_sentence_gives_wording_warning(c, monkeypatch):
    _gemini(monkeypatch, "두 배치는 회수대상입니다 (R1). 요건이 충족되었으므로 원인 규명을 기다리지 않고 회수를 진행합니다 (R4).")
    status, problems = _submit(c)
    assert status == ai.STATUS_WORDING == "경고(표현)"
    assert problems == ["표현 경고: 의결 전에 회수가 결정된 것처럼 읽힘(R3) — '회수를 진행합니다'"]
    page = c.get("/submissions/1").text
    assert "⚠ 표현 경고 1건" in page and "수치 불일치" not in page.split("상신 요약")[1].split("판정 근거")[0]
    r = c.post(f"/submissions/1/batches/{B1}/decide", data={"decision": "approve", "decider": "위원장", "reason": "기준 충족"},
               follow_redirects=False)
    assert r.status_code == 303                                         # 경고는 표시만, 의결은 위원회 판단


def test_cause_investigation_progress_is_fine(c, monkeypatch):
    _gemini(monkeypatch, "회수 실행 여부는 위원회가 판단합니다 (R3). 원인 조사는 회수와 별도 트랙으로 진행합니다 (R4).")
    assert _submit(c) == (ai.STATUS_OK, [])


def test_number_and_wording_problems_together(c, monkeypatch):
    _gemini(monkeypatch, "불량률 13.40%로 회수를 실행합니다.")
    status, problems = _submit(c)
    assert status == ai.STATUS_WARN and any(p.startswith("본문 숫자") for p in problems)
    assert any(p.startswith("표현 경고") for p in problems)
    page = c.get("/submissions/1").text
    assert "⚠ 수치 불일치 1건" in page and "⚠ 표현 경고 1건" in page


# ---------- 주어 없는 진행 · 자동 재생성 ----------

def test_prompt_requires_subject_for_r4():
    assert "원인 조사는 회수와 별도 트랙으로 진행합니다 (R4)" in ai.SYSTEM and "주어(원인 조사)를 밝혀" in ai.SYSTEM


@pytest.mark.parametrize("text, hit", [
    ("요건이 충족되었으므로 원인 규명을 기다리지 않고 진행합니다 (R4).", "기다리지 않고 진행합니다"),
    ("원인 규명을 기다리지 않고 판단합니다.", "기다리지 않고 판단합니다"),
    ("대기 없이 진행됩니다.", "대기 없이 진행됩니다"),
    ("원인 규명을 기다리지 않고 원인 조사를 진행합니다.", None),           # 주체가 밝혀짐
    ("원인 조사는 회수와 별도 트랙으로 진행합니다 (R4).", None),
    ("승인 시 대기 없이 진행합니다.", None),                              # 조건부
])
def test_subjectless_phrases(text, hit):
    assert ai.subjectless_phrases(text) == ([hit] if hit else [])


def _gemini_sequence(monkeypatch, summaries):
    """호출마다 summaries에서 하나씩 꺼내 답한다(마지막 것은 반복). 호출 수를 돌려준다."""
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        summary = summaries[min(len(calls), len(summaries) - 1)]
        calls.append(summary)
        text = __import__("json").dumps({"summary": summary, "actions": ACTIONS, "open_questions": []}, ensure_ascii=False)
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]})

    monkeypatch.setattr(ai.httpx, "post", post)
    return calls


BAD = "두 배치는 회수대상입니다 (R1). 원인 규명을 기다리지 않고 진행합니다 (R4)."
GOOD = "두 배치는 회수대상입니다 (R1). 원인 조사는 회수와 별도 트랙으로 진행합니다 (R4)."


def _check():
    conn = db.connect()
    row = dict(conn.execute("SELECT ai_status, ai_json, ai_check FROM submission WHERE id=1").fetchone())
    hist = [r[0] for r in conn.execute("SELECT reason FROM history WHERE entity_type='submission_summary'")]
    conn.close()
    return row["ai_status"], json.loads(row["ai_json"]), json.loads(row["ai_check"]), hist


def test_wording_warning_is_regenerated_before_anyone_sees_it(c, monkeypatch):
    calls = _gemini_sequence(monkeypatch, [BAD, GOOD])
    c.post("/assess")
    c.post("/submissions")
    status, out, check, hist = _check()
    assert len(calls) == 2 and status == ai.STATUS_OK and out["summary"] == GOOD
    assert check["wording_retries"] == 1 and "기다리지 않고 진행합니다" in check["wording_retry_reasons"][0][0]
    assert "표현 경고로 자동 재생성 1회" in hist[-1]
    assert "표현 경고로 자동 재생성 1회" in c.get("/submissions/1").text


def test_wording_left_after_two_retries_is_shown_as_warning(c, monkeypatch):
    calls = _gemini_sequence(monkeypatch, [BAD])
    c.post("/assess")
    c.post("/submissions")
    status, out, check, _ = _check()
    assert len(calls) == 3 and status == ai.STATUS_WORDING and check["wording_retries"] == 2
    assert check["problems"][0].startswith("표현 경고: 주어 없이 진행을 말해")
    assert "⚠ 표현 경고 1건" in c.get("/submissions/1").text


def test_number_mismatch_alone_does_not_retry(c, monkeypatch):
    calls = _gemini_sequence(monkeypatch, ["불량률 13.40%로 기준 10% 이상입니다 (R1)."])
    c.post("/assess")
    c.post("/submissions")
    status, _, check, _ = _check()
    assert len(calls) == 1 and status == ai.STATUS_WARN and check["wording_retries"] == 0


def test_no_retry_when_time_budget_is_used_up(c, monkeypatch):
    calls = _gemini_sequence(monkeypatch, [BAD, GOOD])
    monkeypatch.setattr(ai, "SUMMARY_BUDGET_SEC", 0)                        # 게이트웨이 시간 안에서만 다시 부름
    c.post("/assess")
    c.post("/submissions")
    status, _, check, _ = _check()
    assert len(calls) == 1 and status == ai.STATUS_WORDING and check["wording_retries"] == 0


def test_start_state_seed_uses_the_same_rule(c, monkeypatch):
    calls = _gemini_sequence(monkeypatch, [BAD, BAD, GOOD])
    from w03 import workflow
    conn = db.connect()
    workflow.run_assessment(conn)
    workflow.seed_submission(conn)                                          # 시작 상태 자동 생성 경로
    conn.close()
    status, out, check, _ = _check()
    assert len(calls) == 3 and status == ai.STATUS_OK and out["summary"] == GOOD and check["wording_retries"] == 2
