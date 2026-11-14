"""사용자 결정 3건: ① 의결 전 상신의 판정 근거 재계산 + '요약 갱신 필요'·'요약 다시 만들기'(사람이 누를 때만 AI)
② AI는 문장만 쓰고 제목·수치·목록은 계산값을 그대로 넣음(수치 대조는 유지) ③ 개선 효과 '실제 회수대상' SBL 기준 표시."""
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
from w03 import ai, db, effects, presets  # noqa: E402

B1, B2 = presets.B1, presets.B2
SENTENCES = {"summary": "두 배치는 규격이탈 확정 클레임 기준 불량률이 10% 이상이라 회수대상입니다 (R1). 회수 실행은 위원회 의결 사항입니다 (R3).",
             "actions": [{"dept": "영업", "action": "승인 시 고객별 회수요청 통지"},
                         {"dept": "품질", "action": "승인 시 원인 조사 착수 — 원료 로트 단서 확인"}],
             "open_questions": ["재공급 일정에 대해 영업과 고객 협의가 필요한가"]}


@pytest.fixture
def c():
    with TestClient(main.app) as client:
        assert client.post("/admin/reset", data={"confirm_text": "초기화"}, follow_redirects=False).status_code == 303
        yield client


def _st(c):
    return c.get("/api/state").json()


def _sub(n=1):
    conn = db.connect()
    row = dict(conn.execute("SELECT * FROM submission WHERE id=?", (n,)).fetchone())
    conn.close()
    return row


def _gemini(monkeypatch, written):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        text = __import__("json").dumps(written, ensure_ascii=False)
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}], "modelVersion": "test"})

    monkeypatch.setattr(ai.httpx, "post", post)
    return calls


def _claim(c, qty="500"):
    return c.post("/claims", data={"customer_id": "C-101", "product_id": "PL-02", "batch_id": B1, "qty": qty,
                                   "confirmed": "True", "claim_type": "규격", "actor": "품질팀", "occurred_date": "2026-06-30"})


# ---------- ② AI는 문장만 ----------

def test_ai_writes_sentences_only_and_lists_come_from_computed_values(c, monkeypatch):
    calls = _gemini(monkeypatch, SENTENCES)
    c.post("/assess")
    c.post("/submissions")
    sub = _sub()
    assert (sub["ai_source"], sub["ai_status"]) == ("gemini", ai.STATUS_OK)
    sent = calls[0]["generationConfig"]["responseJsonSchema"]
    assert set(sent["properties"]) == {"summary", "actions", "open_questions"}          # 수치·목록 필드 없음
    out, facts = json.loads(sub["ai_json"]), json.loads(sub["facts_json"])
    assert out["headline"] == facts["request"]["headline"]
    assert [b["batch_id"] for b in out["cited_batches"]] == [B1, B2]
    assert out["cited_batches"][0]["confirmed_qty"] == 17900 and out["same_lot_batches"] == facts["same_lot_batches"]
    assert out["basis_lines"][0].startswith(f"{B1}: 확정 불량 17,900EA") and out["summary"] == SENTENCES["summary"]
    assert out["open_questions"][-1] == SENTENCES["open_questions"][0] and facts["gaps"][0] in out["open_questions"]
    page = c.get("/submissions/1").text
    assert "계산값 (규칙 엔진" in page and "AI 문장" in page and "수치·목록은 계산값을 그대로 넣었고" in page


def test_wrong_number_in_ai_sentence_is_still_caught(c, monkeypatch):
    _gemini(monkeypatch, {**SENTENCES, "summary": "불량률이 13.40%로 10% 이상입니다 (R1)."})
    c.post("/assess")
    c.post("/submissions")
    sub = _sub()
    problems = json.loads(sub["ai_check"])["problems"]
    assert sub["ai_status"] == ai.STATUS_WARN and problems == ["본문 숫자 중 계산값에 없는 값: 13.40"]


# ---------- ① 의결 전 상신: 판정 근거 재계산 · 요약 갱신 필요 ----------

def test_new_claim_refreshes_open_submission_and_flags_summary(c, monkeypatch):
    _gemini(monkeypatch, SENTENCES)
    c.post("/assess")
    c.post("/submissions")
    calls = _gemini(monkeypatch, SENTENCES)                               # 이후 AI 호출 횟수만 센다
    _claim(c)
    sub = _sub()
    facts, check = json.loads(sub["facts_json"]), json.loads(sub["ai_check"])
    assert facts["targets"][0]["confirmed_qty"] == 18400 and facts["targets"][0]["rate_prod"] == 12.21   # 최신 사실
    assert check["stale"]["basis"][B1]["confirmed_qty"] == 17900 and check["stale"]["causes"] == ["클레임 QC-N0001 접수"]
    assert sub["ai_status"] == ai.STATUS_OK and calls == []                 # AI는 다시 부르지 않음
    page = c.get("/submissions/1").text
    assert "수치 변경으로 요약 갱신 필요" in page and "17,900 → <b>18,400EA</b>" in page and "요약 다시 만들기 (AI 호출)" in page
    table = page.split("판정 근거 (규칙 엔진 계산값)")[1].split("</table>")[0]
    assert "18,400" in table and "12.21%" in table and "17,900" not in table
    q = next(i for i in _st(c)["decision_queue"] if i["kind"] == "위원회 의결")
    assert "수치 변경으로 요약 갱신 필요" in q["why"]
    assert c.post("/submissions/1/summary/regenerate", data={"actor": "품질팀"}, follow_redirects=False).status_code == 303
    sub = _sub()
    out = json.loads(sub["ai_json"])
    assert len(calls) == 1 and sub["ai_status"] == ai.STATUS_OK and "stale" not in json.loads(sub["ai_check"])
    assert out["basis_lines"][0].startswith(f"{B1}: 확정 불량 18,400EA")
    assert "수치 변경으로 요약 갱신 필요" not in c.get("/submissions/1").text
    r = c.post(f"/submissions/1/batches/{B1}/decide", data={"decision": "approve", "decider": "위원장", "reason": "기준 충족"},
               follow_redirects=False)
    assert r.status_code == 303


def test_regenerate_without_key_falls_back_to_manual_with_new_numbers(c):
    c.post("/assess")
    c.post("/submissions")
    c.post("/submissions/1/summary", data={"author": "품질팀", "headline": "h", "summary": "s", "actions": "-", "questions": "-"})
    _claim(c)
    assert "stale" in json.loads(_sub()["ai_check"])
    c.post("/submissions/1/summary/regenerate", data={"actor": "품질팀"})
    sub = _sub()
    assert sub["ai_status"] == ai.STATUS_FAIL and "stale" not in json.loads(sub["ai_check"])
    assert "18,400EA" in c.get("/submissions/1").text.split("수동 요약 저장")[0]          # 초안이 최신 값
    assert c.post("/submissions/1/summary/regenerate", data={"actor": ""}).status_code == 409


def test_failed_summary_has_no_stale_flag_and_decided_submission_is_not_refreshed(c):
    c.post("/assess")
    c.post("/submissions")                                                # 키 없음 → 요약 실패(수동 작성 대기)
    _claim(c)
    sub = _sub()
    assert "stale" not in json.loads(sub["ai_check"]) and json.loads(sub["facts_json"])["targets"][0]["confirmed_qty"] == 18400
    c.post("/admin/reset", data={"confirm_text": "초기화"})
    conn = db.connect()
    presets.baseline(conn)
    conn.close()
    _claim(c)
    assert json.loads(_sub()["facts_json"])["targets"][0]["confirmed_qty"] == 17900    # 의결 완료 상신은 상신 시점 값


# ---------- ③ 개선 효과: SBL 기준 표시 ----------

def test_effects_actual_row_says_sbl_basis(c):
    c.post("/assess")
    conn = db.connect()
    o = effects.overreaction(conn)
    conn.close()
    actual = next(r for r in o["rows"] if r["label"] == "실제 회수대상(R1)")
    assert actual["basis"].endswith("SBL 기준 · 확인 회신분 제외") and actual["shipped"] == 299653    # 로직 그대로
    assert "SBL 기준 · 확인 회신분 제외" in c.get("/effects").text


# ---------- 부서 이름은 코드가 고정 ----------

def test_department_names_are_fixed_by_code(c, monkeypatch):
    calls = _gemini(monkeypatch, {**SENTENCES, "actions": {
        "sales": "영향 고객 — 승인 시 고객별 회수요청 통지", "logistics": "승인 시 사내 잔여재고 출하보류",
        "quality": "승인 시 원인 조사 착수 — 원료 로트 단서 확인", "production": "승인 시 재생산 일정 검토",
        "procurement": "승인 시 원료 확보 검토"}})
    c.post("/assess")
    c.post("/submissions")
    schema = calls[0]["generationConfig"]["responseJsonSchema"]["properties"]["actions"]
    assert list(schema["properties"]) == ["sales", "logistics", "quality", "production", "procurement"]
    assert "dept" not in str(schema)                                     # AI는 부서 이름을 쓰지 않음
    out = json.loads(_sub()["ai_json"])
    assert [a["dept"] for a in out["actions"]] == ["영업", "물류", "품질", "생산", "조달"]
    assert out["actions"][0]["action"].startswith("영향 고객")              # 문장은 그대로, 부서 칸은 고정


def test_list_shaped_actions_still_get_fixed_names():
    acts = ai.dept_actions([{"dept": "영향 고객", "action": "승인 시 통지"}, {"dept": "?", "action": "승인 시 출하보류"}])
    assert [a["dept"] for a in acts] == ["영업", "물류", "품질", "생산", "조달"]
    assert [a["action"] for a in acts] == ["승인 시 통지", "승인 시 출하보류", "—", "—", "—"]
