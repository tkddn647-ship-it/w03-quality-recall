"""시간 축: A05 업무 달력(월~금) · A-28 처리 목표일(영업일) · 업무일 넘기기와 지연 · 클레임 발생일/적용일."""
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
from w03 import bizcal, db, glossary, presets  # noqa: E402

B1, B2 = presets.B1, presets.B2
T02 = "B-02-2602-02"


@pytest.fixture
def c():
    with TestClient(main.app) as client:
        assert client.post("/admin/reset", data={"confirm_text": "초기화"}, follow_redirects=False).status_code == 303
        yield client


def _st(c):
    return c.get("/api/state").json()


def _q(sql, *args):
    conn = db.connect()
    rows = [dict(r) for r in conn.execute(sql, args)]
    conn.close()
    return rows


def _baseline():
    conn = db.connect()
    sid = presets.baseline(conn)
    conn.close()
    return sid


def _next_day(c, confirm="넘기기"):
    return c.post("/business-day/next", data={"confirm_text": confirm, "actor": "품질팀"}, follow_redirects=False)


# ---------- 달력 · A-28 ----------

def test_a05_calendar_skips_weekends():
    assert bizcal.label("2026-07-01") == "2026-07-01(수)"
    assert bizcal.next_business_day("2026-07-03") == "2026-07-06"           # 금 → 월
    assert bizcal.add_business_days("2026-07-01", 3) == "2026-07-06"
    assert bizcal.add_business_days("2026-07-04", 0) == "2026-07-06"        # 토요일에 생기면 월요일부터
    assert bizcal.business_days_between("2026-07-01", "2026-07-15") == 10
    assert bizcal.business_days_between("2026-07-06", "2026-07-02") == -2


def test_a28_user_set_and_converted_values():
    assert {t: bizcal.due_rule(t)[0] for t in bizcal.USER_SET} == {
        "사내재고 격리": 0, "고객 통지": 1, "보유량 회신 수집": 3, "원인 조사": 10}
    rows = {r["type"]: r for r in bizcal.due_table("2026-07-01")}
    for r in rows.values():                                                   # 환산: 같은 마감일(주말이면 다음 월요일)
        if not r["user_set"] and r["prev_cal_days"] is not None:
            assert r["due"] == bizcal.add_business_days(r["prev_due"], 0), r
    assert (rows["회수 범위 확인"]["bdays"], rows["회수 범위 확인"]["prev_cal_days"]) == (1, None)   # 신규 유형
    assert (rows["재공급 요청"]["bdays"], rows["재공급 요청"]["due"]) == (3, "2026-07-06")
    assert (rows["폐기 검토"]["bdays"], rows["폐기 검토"]["due"]) == (5, "2026-07-08")
    assert rows["원인 조사"]["due"] == "2026-07-15"


def test_tasks_get_due_dates_from_business_date(c):
    _baseline()
    due = {t["type"]: t["due_date"] for t in _q("SELECT type, due_date, business_date FROM task")}
    assert due["사내재고 격리"] == "2026-07-01" and due["고객 통지"] == "2026-07-02"
    assert due["보유량 회신 수집"] == "2026-07-06" and due["원인 조사"] == "2026-07-15"
    assert {r["business_date"] for r in _q("SELECT business_date FROM task")} == {"2026-07-01"}
    page = c.get("/").text
    assert "처리 목표(A-28)" in page and "2026-07-15(수)" in page


# ---------- 업무일 넘기기 · 지연 ----------

def test_advance_marks_overdue_tasks_delayed_and_tops_queue(c):
    _baseline()
    assert _next_day(c, confirm="").status_code == 400
    assert _next_day(c).status_code == 303
    assert _st(c)["business_date"] == "2026-07-02"
    assert "2026-07-02(목)" in c.get("/").text.split("</header>")[0]          # 화면 상단 업무일
    quarantine = _q("SELECT * FROM task WHERE type='사내재고 격리'")
    assert quarantine and all(t["delayed_at"] == "2026-07-02" and t["state"] == "대기" for t in quarantine)
    assert not _q("SELECT 1 FROM task WHERE type='고객 통지' AND delayed_at IS NOT NULL")
    hist = _q("SELECT * FROM history WHERE entity_type='task' AND to_state='지연'")
    assert len(hist) == len(quarantine) and all("2026-07-01(수) 경과(1영업일)" in h["reason"] for h in hist)
    assert all(h["business_date"] == "2026-07-02" for h in hist)
    q = _st(c)["decision_queue"]
    assert (q[0]["rank"], q[0]["kind"], q[0]["urgency"]) == (0, "지연 작업", "지연")
    assert q[0]["title"].startswith("[물류] 사내재고 격리") and "1영업일 지남" in q[0]["why"]
    notice = next(i for i in q if "고객 통지" in i["title"])
    assert "처리 목표 오늘 2026-07-02(목)" in notice["why"]
    assert '<span class="pill t-bad">지연</span>' in c.get("/").text


def test_advance_skips_weekend_and_records_history(c):
    _baseline()
    for expected in ("2026-07-02", "2026-07-03", "2026-07-06"):
        _next_day(c)
        assert _st(c)["business_date"] == expected
    moves = [(h["from_state"], h["to_state"]) for h in _q("SELECT * FROM history WHERE entity_type='business_date' ORDER BY id")]
    assert moves == [("2026-07-01", "2026-07-02"), ("2026-07-02", "2026-07-03"), ("2026-07-03", "2026-07-06")]
    delayed = {t["type"] for t in _q("SELECT type FROM task WHERE delayed_at IS NOT NULL")}
    assert {"사내재고 격리", "고객 통지", "주문 상태 확인 요청"} <= delayed and "원인 조사" not in delayed
    assert "보유량 회신 수집" not in delayed                                   # 기한 07-06 당일 → 아직 지연 아님


def test_finished_task_is_not_delayed(c):
    _baseline()
    t = _q("SELECT id, detail_json FROM task WHERE type='사내재고 격리' ORDER BY id")[0]
    assert c.post(f"/tasks/{t['id']}/quarantine", data={"qty": "4000", "actor": "물류"}).status_code == 200
    _next_day(c)
    assert _q("SELECT delayed_at FROM task WHERE id=?", t["id"])[0]["delayed_at"] is None
    assert len(_q("SELECT 1 FROM task WHERE type='사내재고 격리' AND delayed_at IS NOT NULL")) == 1


def test_tasks_created_later_use_that_business_date(c):
    c.post("/assess")
    _next_day(c)                                                              # 07-02(목)에 상신·의결
    c.post("/submissions")
    c.post("/submissions/1/summary", data={"author": "품질팀", "headline": "h", "summary": "s", "actions": "-", "questions": "-"})
    c.post(f"/submissions/1/batches/{B1}/decide", data={"decision": "approve", "decider": "위원장", "reason": "기준 충족"})
    t = {r["type"]: r for r in _q("SELECT * FROM task")}
    assert t["사내재고 격리"]["business_date"] == "2026-07-02" and t["사내재고 격리"]["due_date"] == "2026-07-02"
    assert t["원인 조사"]["due_date"] == "2026-07-16"
    dec = _q("SELECT business_date FROM history WHERE entity_type='batch' AND to_state='회수의결'")
    assert dec and dec[0]["business_date"] == "2026-07-02"


# ---------- 클레임 발생일 · 적용일 (A05) ----------

def _claim(c, occurred="2026-06-20", applied=None, batch=T02, qty="100"):
    data = {"customer_id": "C-101", "product_id": "PL-02", "batch_id": batch, "qty": qty, "confirmed": "True",
            "claim_type": "규격", "actor": "품질팀", "occurred_date": occurred}
    if applied is not None:
        data["applied_date"] = applied
    return c.post("/claims", data=data, follow_redirects=False)


def test_claim_records_occurred_and_applied_dates(c):
    c.post("/assess")
    assert _claim(c).status_code == 303
    row = _q("SELECT * FROM claim_addition WHERE claim_id='QC-N0001'")[0]
    assert (row["claim_date"], row["applied_date"]) == ("2026-06-20", "2026-07-01")   # 적용일 기본 = 현재 업무일
    page = c.get("/claims").text
    assert 'name="applied_date" value="2026-07-01"' in page and "발생일" in page and "2026-06-20" in page
    _next_day(c)
    _next_day(c)
    _next_day(c)                                                              # 07-06(월)
    assert _claim(c, occurred="2026-07-03", applied="2026-07-02").status_code == 409   # 발생일 > 적용일
    assert _claim(c, occurred="2026-07-01", applied="2026-07-02").status_code == 303   # 소급 적용
    row = _q("SELECT * FROM claim_addition WHERE claim_id='QC-N0002'")[0]
    assert (row["claim_date"], row["applied_date"]) == ("2026-07-01", "2026-07-02")


@pytest.mark.parametrize("occurred, applied, msg", [
    ("", None, "발생일"),
    ("2026-01-10", None, "배치 생산일"),                                      # B-02-2602-02 생산 2026-02-16
    ("2026-06-20", "2026-07-02", "현재 업무일"),                               # 미래 적용
    ("2026-06-25", "2026-06-24", "발생일 ≤ 적용일"),
    ("2026-06-20", "2026-06-27", "영업일"),                                    # 토요일 적용
])
def test_claim_date_validation(c, occurred, applied, msg):
    c.post("/assess")
    r = _claim(c, occurred=occurred, applied=applied)
    assert r.status_code == 409 and msg in r.text


def test_retroactive_applied_date_is_noted(c):
    c.post("/assess")
    _next_day(c)
    assert _claim(c, occurred="2026-06-30", applied="2026-07-01").status_code == 303
    h = _q("SELECT reason, business_date FROM history WHERE entity_type='claim' AND entity_id='QC-N0001'")[0]
    assert "발생 2026-06-30 · 적용 2026-07-01 (소급 적용)" in h["reason"] and h["business_date"] == "2026-07-02"


def test_inspection_claim_uses_business_date(c):
    conn = db.connect()
    presets.baseline(conn)
    conn.close()
    _next_day(c)
    conn = db.connect()
    tasks = {r["related_id"]: r["id"] for r in conn.execute("SELECT id, related_id FROM task WHERE type='추가검사'")}
    from w03 import workflow
    workflow.record_inspection(conn, tasks[f"INV-{T02}"], False, 3213, 900, "검사원")
    conn.close()
    row = _q("SELECT claim_date, applied_date FROM claim_addition WHERE source='추가검사'")[0]
    assert (row["claim_date"], row["applied_date"]) == ("2026-07-02", "2026-07-02")


# ---------- 용어집 ----------

def test_glossary_a05_a28_a29(c):
    terms = {t["code"]: t for t in glossary.TERMS}
    assert "데이터 이용 및 확장 기준" in terms["A05"]["name"] and "월~금" in terms["A05"]["short"]
    assert "달력일" in terms["A-11"]["short"] and "43일" in terms["A-11"]["short"]
    assert "A-29" in terms
    page = c.get("/glossary").text
    assert 'id="A-28-table"' in page and "<b>10영업일</b>" in page and "<b>당일</b>" in page
    assert page.count('<span class="pill t-ok">지정</span>') == len(bizcal.USER_SET) + 1    # + 위원회 의결(A-32)
