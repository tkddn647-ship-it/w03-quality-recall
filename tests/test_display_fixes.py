"""표시·문구 수정: 원료 로트 시점 단서 · '원본 QC 결과' · AI 기준 표현/규칙 번호 · 보유량 필요량 구성 · 단위 · 업무일/기록 시각."""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("W03_DB", str(Path(tempfile.mkdtemp()) / "test_w03.db"))
for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GEMINI_API_KEY", "AI_PROVIDER"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from w03 import ai, db, presets, rules, workflow  # noqa: E402

from test_normal_scenario import _ai_output  # noqa: E402

LOT, B1 = "LOT-B-2602-P", presets.B1


@pytest.fixture
def c():
    with TestClient(main.app) as client:
        assert client.post("/admin/reset", data={"confirm_text": "초기화"}, follow_redirects=False).status_code == 303
        yield client


def _run(fn):
    conn = db.connect()
    out = fn(conn)
    conn.close()
    return out


def _st(c):
    return c.get("/api/state").json()


# ---------- 1. 원료 로트 시점 단서 ----------

def test_lot_clues_compare_consumption_with_receipt_and_inspection(c):
    clue = _run(lambda conn: rules.lot_clues(conn, LOT))
    assert (clue["receipt_date"], clue["inspection_date"], clue["inspection_result"]) == ("2026-02-06", "2026-02-09", "보류")
    timing = {u["batch_id"]: u["timing"] for u in clue["uses"]}
    assert timing == {"B-02-2602-01": "입고 전 투입", "B-02-2602-03": "입고검사 '보류' 이후 투입",
                      "B-02-2602-02": "입고검사 '보류' 이후 투입", "B-02-2603-01": "입고검사 '보류' 이후 투입"}
    first = clue["clues"][0]
    assert first.startswith("B-02-2602-01 투입일 2026-02-05이 로트 입고일 2026-02-06·입고검사일 2026-02-09보다 빠름")
    assert "검사 결과 전 투입 가능성" in first and "A05 알려진 한계" in first
    assert clue["consumed_kg"] > clue["received_kg"] and any("A05" in x for x in clue["limits"])
    assert clue["label"] == "단서(데이터 한계 가능성 포함, 확인 필요)"


def test_clues_reach_ai_facts_task_lot_and_submission_pages(c):
    c.post("/assess")
    c.post("/submissions")                                            # 키 없음 → 수동 작성 경로
    sub = _st(c)["submissions"][0]
    conn = db.connect()
    facts = json.loads(conn.execute("SELECT facts_json FROM submission WHERE id=1").fetchone()[0])
    conn.close()
    [lc] = facts["lot_clues"]
    assert lc["lot_id"] == LOT and lc["status"].startswith("단서") and len(lc["clues"]) == 4
    assert not any("kg" in x for x in lc["clues"])                                      # AI 입력 단서에는 kg 수치 없음
    draft = ai.draft_for_manual(facts)
    assert not any("원인 조사 단서" in q for q in draft["open_questions"])          # 단서는 '원료 로트' 칸에만
    assert sub["ai_status"] == ai.STATUS_FAIL
    page = c.get("/submissions/1").text
    assert "원인 조사 단서(데이터 한계 가능성 포함, 확인 필요)" in page and "검사 결과 전 투입 가능성" in page
    lot_page = c.get(f"/lots/{LOT}").text
    assert 'id="clues"' in lot_page and "입고 전 투입" in lot_page and "원인 판정이 아닙니다" in lot_page
    _run(presets.baseline)
    task = next(t for t in _st(c)["tasks"] if t["type"] == "원인 조사")
    page = c.get("/").text
    row = page.split(f'id="task-{task["id"]}"')[1].split("</tr>")[0]
    assert "단서(데이터 한계 가능성 포함, 확인 필요)" in row and "B-02-2602-01 투입일 2026-02-05" in row


# ---------- 2. '원본 QC 결과' ----------

def test_qc_result_column_renamed_everywhere(c):
    _run(presets.baseline)
    for path in ("/", f"/lots/{LOT}", f"/trace/{B1}", "/submissions/1", f"/batches/{B1}"):
        page = c.get(path).text
        assert "원본 QC 결과" in page and "품질판정" not in page, path


# ---------- 3. AI 요약: '10% 이상' · 규칙 번호 ----------

def test_prompt_and_facts_ask_for_fixed_wording_and_rule_numbers(c):
    c.post("/assess")
    facts = _run(lambda conn: workflow.build_facts(conn, [B1, presets.B2]))
    assert set(facts["policy"]) == {"R1", "R2", "R3", "R4", "R5", "R6"} and "10% 이상" in facts["rule"]
    assert "'10% 이상'" in ai.SYSTEM and "(R1)" in ai.SYSTEM and "lot_clues" in ai.SYSTEM
    assert "규칙 목록이나 규칙 문장을 따로 나열하지 않습니다" in ai.SYSTEM and "open_questions에 옮기지 않습니다" in ai.SYSTEM


@pytest.mark.parametrize("phrase, bad", [("불량률이 10% 이상으로 회수 기준(R1) 충족", False),
                                         ("불량률이 10% 초과", True), ("10%를 넘는 불량률", True), ("10%를 넘어", True)])
def test_verify_flags_threshold_wording(c, phrase, bad):
    c.post("/assess")
    facts = _run(lambda conn: workflow.build_facts(conn, [B1, presets.B2]))
    out = _ai_output(facts)
    out["summary"] = phrase
    problems = ai.verify(out, facts)
    assert any("'10% 이상'" in p for p in problems) == bad, problems


# ---------- 4. 표시 정리 ----------

def test_holding_task_shows_need_breakdown(c):
    _run(presets.baseline)
    t = next(x for x in _st(c)["tasks"] if x["type"] == "보유량 회신 수집")
    assert "필요량 299,653EA = 회신분 0 + 미회신분 상한 299,653(A-8)" in t["title"]
    c.post("/presets/reply_ok", data={"confirm_text": "프리셋"})                  # C-101 B-02-2602-01 보유 12,000 (출하 28,701)
    t = next(x for x in _st(c)["tasks"] if x["type"] == "보유량 회신 수집")
    assert "필요량 282,952EA = 회신분 12,000 + 미회신분 상한 270,952(A-8)" in t["title"]


def test_summary_cards_show_units(c):
    _run(presets.baseline)
    page = c.get("/").text
    assert "회수요청 출하 9건(고객 5곳)" in page and "회수의결 2개" in page and "대기 17건" in page


def test_submission_page_shows_business_date_and_record_time(c):
    _run(presets.baseline)
    page = c.get("/submissions/1").text
    assert "상신 업무일 <b>2026-07-01(수)</b> · 기록 시각(실제)" in page
    assert page.count("업무일 2026-07-01(수) · 기록 시각") == 2                      # 배치별 의결 2건


# ---------- 5차 표시 수정 ----------

def test_dashboard_separates_ai_check_from_decision_and_labels_record_time(c):
    c.post("/assess")
    c.post("/submissions")
    c.post("/submissions/1/summary", data={"author": "품질팀", "headline": "h", "summary": "s", "actions": "-", "questions": "-"})
    page = c.get("/").text
    line = page.split('href="/submissions/1">회수상신 #1</a>')[1].split("</div>")[0]
    assert "AI 요약 검증 수동 작성 완료" in line and "의결 대기" in line
    assert "업무일 2026-07-01(수) · 기록 시각(실제) " in line


def test_glossary_r1_split_source_and_a18_is_assumption(c):
    from w03 import glossary
    t = {x["code"]: x for x in glossary.TERMS}
    assert (t["R1"]["nature"], t["R1"]["also"]) == (glossary.FACT, glossary.ASSUME)
    assert t["R1"]["source"].startswith("원본 사실:") and "+ 가정 A-2:" in t["R1"]["source"]
    assert t["A-18"]["nature"] == glossary.ASSUME and "OPT-02 비고" in t["A-18"]["source"]
    sections = dict(glossary.grouped())
    assert all(x["nature"] == glossary.ASSUME for x in sections["가정"])          # 가정 섹션 = 가정만
    page = c.get("/glossary").text
    row = page.split('id="R1"')[1].split("</tr>")[0]
    assert ">원본 사실</span> + <span" in row and ">가정</span>" in row


def test_claim_form_batch_options_show_id_and_quantity(c):
    page = c.get("/claims").text
    assert '<option value="B-02-2602-02">B-02-2602-02 · PL-02 · 생산 2026-02-16 · 생산량 154,685EA</option>' in page


def test_actions_are_conditional_on_approval(c):
    c.post("/assess")
    facts = _run(lambda conn: workflow.build_facts(conn, [B1, presets.B2]))
    assert all(a["action"].startswith("승인 시") for a in ai.draft_for_manual(facts)["actions"])
    assert "'승인 시 …' 조건부" in ai.SYSTEM
    _run(presets.baseline)
    assert "부서별 조치안 — 위원회 승인 시" in c.get("/submissions/1").text
