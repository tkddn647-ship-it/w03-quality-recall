"""상신 화면·의결 지연: 의결 처리 목표(A-32) · 요청 사항 한 줄 · 원인 조사 단서 한 곳 · 코드명 → 한글 · 용어집 집계."""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest
from markupsafe import escape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("W03_DB", str(Path(tempfile.mkdtemp()) / "test_w03.db"))
for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GEMINI_API_KEY", "AI_PROVIDER"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from w03 import ai, db, glossary, presets, workflow  # noqa: E402

B1, B2 = presets.B1, presets.B2
REQUEST = ("B-02-2602-01·B-02-2603-01 2개 배치 회수 실행 승인 요청 — 영향 고객 5곳·299,653EA, "
           "사내재고 추정 11,825EA 출하보류 · 승인 시 공급 판정 대안필요")
CODES = ("defect_confirmed", "approval_authority_policy", "quality_policy", "calculation_definitions", "produced_qty",
         "facts")


@pytest.fixture
def c():
    with TestClient(main.app) as client:
        assert client.post("/admin/reset", data={"confirm_text": "초기화"}, follow_redirects=False).status_code == 303
        yield client


def _st(c):
    return c.get("/api/state").json()


def _submit(c):
    c.post("/assess")
    c.post("/submissions")                                            # 키 없음 → 수동 작성 경로
    c.post("/submissions/1/summary", data={"author": "품질팀", "headline": REQUEST, "summary": "s", "actions": "-",
                                           "questions": "-"})


def _next_day(c):
    assert c.post("/business-day/next", data={"actor": "운영", "confirm_text": "넘기기"}, follow_redirects=False).status_code == 303


def _visible(html: str) -> str:
    """<main> 안의 보이는 글자만(툴팁 title 속성·태그 제외)."""
    body = html.split("<main", 1)[1].split("</main>", 1)[0]
    body = re.sub(r"<script.*?</script>", " ", body, flags=re.S)
    return re.sub(r"<[^>]+>", " ", body)


# ---------- 1. 위원회 의결 처리 목표 · 의결 지연 ----------

def test_decision_has_target_date_then_turns_into_delay_at_top(c):
    _submit(c)
    q = _st(c)["decision_queue"]
    item = next(i for i in q if i["kind"] == "위원회 의결")
    assert item["rank"] == 1 and "의결 처리 목표 2026-07-02(목)(상신 후 1영업일, A-32)" in item["why"]
    _next_day(c)                                                      # 07-02: 마감 당일 — 아직 지연 아님
    item = next(i for i in _st(c)["decision_queue"] if i["kind"] == "위원회 의결")
    assert "의결 처리 목표 오늘 2026-07-02(목)" in item["why"]

    conn = db.connect()                                               # 이미 기한이 지난 작업이 있어도 의결 지연이 맨 위
    conn.execute("INSERT INTO task (type, title, dept, business_date, due_date, state) VALUES "
                 "('추가검사', '지난 작업', '품질', '2026-07-01', '2026-07-01', '대기')")
    conn.commit()
    conn.close()
    _next_day(c)                                                      # 07-03: 1영업일 지연
    q = _st(c)["decision_queue"]
    top = q[0]
    assert (top["rank"], top["kind"], top["owner"]) == (0, "의결 지연", "품질위원회")
    assert top["title"] == f"회수상신 #1 의결 지연 1영업일 — {B1}, {B2}"
    assert "처리 목표 2026-07-02(목)(상신 2026-07-01(수) + 1영업일, A-32)에서 1영업일 지남" in top["why"]
    assert "고객 5곳 출하분 299,653EA" in top["why"] and top["href"] == "/submissions/1#decide"
    assert q[1]["kind"] == "지연 작업"
    conn = db.connect()
    h = conn.execute("SELECT reason FROM history WHERE entity_type='submission' AND to_state='의결 지연'").fetchall()
    conn.close()
    assert len(h) == 1 and "가정 A-32" in h[0][0]
    _next_day(c)                                                      # 07-06(월): 주말 건너뛰고 2영업일, 이력은 한 번만
    assert _st(c)["decision_queue"][0]["title"].startswith("회수상신 #1 의결 지연 2영업일")
    assert "의결 지연 2영업일" in c.get("/submissions/1").text
    c.post(f"/submissions/1/batches/{B1}/decide", data={"decision": "approve", "decider": "위원장", "reason": "기준 충족"})
    c.post(f"/submissions/1/batches/{B2}/decide", data={"decision": "approve", "decider": "위원장", "reason": "기준 충족"})
    assert not any(i["kind"] in ("의결 지연", "위원회 의결") for i in _st(c)["decision_queue"])


def test_missing_summary_also_becomes_decision_delay(c):
    c.post("/assess")
    c.post("/submissions")
    _next_day(c)
    _next_day(c)
    top = _st(c)["decision_queue"][0]
    assert top["kind"] == "의결 지연" and "요약 작성부터" in top["why"] and top["href"] == "/submissions/1"


def test_glossary_has_decision_target_row():
    terms = {t["code"]: t for t in glossary.TERMS}
    assert "상신 업무일 + 1영업일" in terms["A-32"]["short"]


# ---------- 2. 요청 사항 한 줄 ----------

def test_request_line_on_top_of_submission_and_in_headline(c):
    c.post("/assess")
    c.post("/submissions")
    conn = db.connect()
    facts = json.loads(conn.execute("SELECT facts_json FROM submission WHERE id=1").fetchone()[0])
    conn.close()
    assert facts["request"]["headline"] == REQUEST
    assert ai.draft_for_manual(facts)["headline"] == REQUEST
    assert ai.assemble({"summary": "s", "actions": [], "open_questions": []}, facts)["headline"] == REQUEST
    assert "headline" not in ai.SCHEMA["properties"]                  # 제목은 AI가 쓰지 않고 계산값
    page = c.get("/submissions/1").text
    assert page.index('id="request"') < page.index("상신 요약") and REQUEST in page
    out = {"headline": "회수대상 2개 배치 의결 요청", "summary": "", "actions": [], "open_questions": [],
           "cited_batches": [], "cited_customers": [], "same_lot_batches": []}
    assert any("요청 사항" in p and "299,653" in p for p in ai.verify(out, facts))


# ---------- 3. 원인 조사 단서는 원료 로트 칸에만 ----------

def test_clues_only_in_lot_card_with_link_from_gaps(c):
    c.post("/assess")
    c.post("/submissions")
    conn = db.connect()
    facts = json.loads(conn.execute("SELECT facts_json FROM submission WHERE id=1").fetchone()[0])
    clue = facts["lot_clues"][0]["clues"][0]
    shown = str(escape(clue))
    out = ai.draft_for_manual(facts)
    out["open_questions"] = out["open_questions"] + [f"[원인 조사 단서 — 확인 필요] {clue}"]   # 예전 형식 요약도 걸러 낸다
    conn.execute("UPDATE submission SET ai_json=?, ai_status=?, ai_source='manual' WHERE id=1",
                 (json.dumps(out, ensure_ascii=False), ai.STATUS_MANUAL))
    conn.commit()
    conn.close()
    page = c.get("/submissions/1").text
    gaps = page.split("<b>정보 공백·확인 필요</b>")[1].split("</ul>")[0]
    assert shown not in gaps and "투입 가능성" not in gaps and 'href="#lot"' in gaps and "원인 조사 단서 4건" in gaps
    assert page.count(shown) == 1 and 'id="lot"' in page


# ---------- 4. 코드명 → 한글 업무 용어(원래 이름은 툴팁) ----------

def test_code_names_only_in_tooltips(c):
    def check(paths):
        for path in paths:
            text = _visible(c.get(path).text)
            for code in CODES:
                assert not re.search(rf"(?<![A-Za-z_]){code}(?![A-Za-z_])", text), (path, code)

    _submit(c)
    check(("/submissions/1", "/glossary", "/claims", "/"))
    page = c.get("/submissions/1").text
    assert 'title="원래 이름: approval_authority_policy">승인권한 규정' in page and "규격이탈 확정" in page
    c.post("/admin/reset", data={"confirm_text": "초기화"})
    conn = db.connect()
    presets.baseline(conn)                                            # 의결 후 작업·이력까지
    conn.close()
    check(("/", "/submissions/1", "/history", f"/batches/{B1}"))


def test_terms_filter_and_prompt():
    html = str(glossary.terms("회수대상 = defect_confirmed=True, produced_qty 기준 (approval_authority_policy) facts.gaps <x>"))
    assert 'title="원래 이름: defect_confirmed=True">규격이탈 확정</span>' in html
    assert ">생산량</span>" in html and ">승인권한 규정</span>" in html and ">정보 공백</span>" in html and "&lt;x&gt;" in html
    assert glossary.plain("facts의 produced_qty") == "계산값의 생산량"
    assert "본문에 쓰지 않습니다" in ai.SYSTEM and "approval_authority_policy" in ai.SYSTEM


def test_facts_text_uses_business_terms(c):
    c.post("/assess")
    conn = db.connect()
    facts = workflow.build_facts(conn, [B1, B2])
    conn.close()
    blob = json.dumps({k: facts[k] for k in ("rule", "gaps", "policy")}, ensure_ascii=False)
    assert "규격이탈 확정" in facts["rule"] and "승인권한 규정" in facts["policy"]["R3"]
    for code in CODES:
        assert code not in blob, code


# ---------- 5. 용어집 집계: 규칙은 따로 ----------

def test_glossary_counts_exclude_rules(c):
    a_codes = [t for t in glossary.TERMS if t["code"].startswith("A-")]
    assert len(a_codes) == 32 and all(t["nature"] == glossary.ASSUME for t in a_codes)
    page = c.get("/glossary").text
    assert "32개 (A-1~A-32)" in page and "36개" not in page
    assert "R1~R10 — 위 집계에 넣지 않고 따로 셉니다" in page and "10개 = 원본 사실 근거 5 · 가정 근거 5" in page
    codes = [t["code"] for t in a_codes]
    assert codes == [f"A-{n}" for n in range(1, 33)]                    # 번호 순서
