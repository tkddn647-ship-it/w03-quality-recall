"""정상 시나리오 E2E: 적재 → 판정 → 상신(AI 요약 또는 수동 작성) → 배치별 모의 의결 → 후속 상태·Task·이력.

실행: python -m pytest tests -q
Claude 호출은 가짜 클라이언트로 대체한다(실제 API 비용·키 불필요).
"""
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["W03_DB"] = str(Path(tempfile.mkdtemp()) / "test_w03.db")
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from w03 import ai, db, workflow  # noqa: E402
from w03.config import SOURCE_DIR, SOURCE_TABLES  # noqa: E402

B1, B2 = "B-02-2602-01", "B-02-2603-01"
REASON = "확정 불량률 기준 충족"


def _digest():
    return {rel: hashlib.sha256((SOURCE_DIR / rel).read_bytes()).hexdigest() for rel in SOURCE_TABLES.values()}


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        assert c.post("/admin/reset", data={"confirm_text": "초기화"}, follow_redirects=False).status_code == 303
        c.post("/assess")
        yield c


def _state(c):
    return c.get("/api/state").json()


def _decide(c, batch, decision, reason=REASON):
    return c.post(f"/submissions/1/batches/{batch}/decide",
                  data={"decision": decision, "decider": "품질위원회 위원장", "reason": reason},
                  follow_redirects=False)


def _manual(c):
    return c.post("/submissions/1/summary", data={
        "author": "품질팀 담당자", "headline": "회수대상 2건", "summary": "확정 불량률 기준 충족",
        "actions": "영업: 고객 통지\n품질: 원인 조사", "questions": "C-105 출하 기록 확인"}, follow_redirects=False)


def _ai_output(facts, rate_delta=0.0):
    return {
        "headline": facts["request"]["headline"],
        "summary": " ".join(f"{t['batch_id']} 불량률 {t['rate_prod'] + rate_delta:.2f}%" for t in facts["targets"]),
        "cited_batches": [{"batch_id": t["batch_id"], "confirmed_qty": t["confirmed_qty"],
                           "rate_prod_pct": t["rate_prod"] + rate_delta, "rate_ship_pct": t["rate_ship"]}
                          for t in facts["targets"]],
        "cited_customers": [{"customer_id": c["customer_id"], "qty": c["qty"]} for c in facts["impact"]["customers"]],
        "same_lot_batches": facts["same_lot_batches"],
        "actions": [{"dept": "영업", "action": "고객 통지"}],
        "open_questions": ["C-105 출하 기록 확인"],
    }


def _fake_claude(monkeypatch, rate_delta=0.0):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def create(**kw):
        facts = json.loads(kw["messages"][0]["content"].split("\n", 1)[1])
        return SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5", _request_id="req_test",
                               content=[SimpleNamespace(type="text", text=json.dumps(_ai_output(facts, rate_delta)))])

    fake = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setattr(ai.anthropic, "Anthropic", lambda: fake)


def test_reset_requires_confirmation():
    with TestClient(main.app) as c:
        assert c.post("/admin/reset", data={"confirm_text": ""}).status_code == 400
        assert c.post("/admin/reset").status_code == 400


def test_no_key_manual_summary_then_approve_all(client):
    before = _digest()
    c = client
    st = _state(c)
    assert sorted(b["batch_id"] for b in st["batches"] if b["state"] == "회수대상") == [B1, B2]

    assert c.post("/submissions", follow_redirects=False).headers["location"] == "/submissions/1"
    sub = _state(c)["submissions"][0]
    assert sub["ai_status"] == ai.STATUS_FAIL and sub["ai_source"] is None
    assert "수동 요약 저장" in c.get("/submissions/1").text

    assert _decide(c, B1, "approve").status_code == 409            # 요약 전에는 의결 불가
    assert _manual(c).status_code == 303
    assert _state(c)["submissions"][0]["ai_status"] == ai.STATUS_MANUAL
    assert _decide(c, B1, "approve", reason="  ").status_code == 409   # 사유 필수
    page = c.get("/submissions/1").text
    assert "모의 의결" in page and 'name="reason" required' in page

    assert _decide(c, B1, "approve").status_code == 303
    first = {t["type"]: t for t in _state(c)["tasks"]}
    # R8: 필요 145,860 ÷ 여유 58,484 = 2.49 > 1 → 대안필요
    assert first["재공급 요청"]["title"].startswith("[대안필요]") and "145,860EA" in first["재공급 요청"]["title"]
    assert _decide(c, B1, "approve").status_code == 409             # 같은 배치 재의결 불가
    assert _decide(c, B2, "approve").status_code == 303

    st = _state(c)
    states = {b["batch_id"]: b["state"] for b in st["batches"]}
    assert states[B1] == states[B2] == "회수의결"
    assert states["B-02-2602-02"] == states["B-02-2602-03"] == "정상"   # 같은 로트(R6)는 회수 아님
    assert {s["state"]: s["n"] for s in st["shipments"]}["회수요청"] == 9
    assert {i["hold_id"]: (i["state"], i["est_qty"]) for i in st["inventory"] if i["kind"] == "recall"} == {
        f"INV-{B1}": ("출하보류", 4885), f"INV-{B2}": ("출하보류", 6940)}
    assert all("가정 A-5" in i["basis"] for i in st["inventory"])
    assert st["submissions"][0]["status"] == "의결완료"

    types = [t["type"] for t in st["tasks"]]
    assert types.count("고객 통지") == 5                            # 고객별(배치가 겹치면 합쳐짐)
    assert types.count("사내재고 격리") == 2                         # 물류, 배치별
    assert types.count("원인 조사") == 1 and types.count("재공급 요청") == 1
    assert types.count("재공급 생산") == 1 and types.count("재공급 원료 확보") == 1
    c101 = next(t for t in st["tasks"] if t["type"] == "고객 통지" and "C-101" in t["title"])
    assert "56,993EA" in c101["title"] and B1 in c101["title"] and B2 in c101["title"]
    resupply = next(t for t in st["tasks"] if t["type"] == "재공급 요청")
    assert "299,653EA" in resupply["title"] and "상한값" in resupply["title"]   # 두 번째 의결로 재판정 갱신
    assert "필요÷여유 5.12" in next(t for t in st["tasks"] if t["type"] == "재공급 생산")["title"]
    assert "별도 트랙" in next(t for t in st["tasks"] if t["type"] == "원인 조사")["title"]

    conn = db.connect()
    rows = conn.execute("SELECT at, business_date, actor_type FROM history").fetchall()
    assert rows and all(r["business_date"] == "2026-07-01" and r["at"].startswith("20") for r in rows)
    assert conn.execute("SELECT COUNT(*) FROM history WHERE reason LIKE '재판정 갱신%'").fetchone()[0] >= 1
    conn.close()
    for path in ("/", "/history", f"/batches/{B1}", "/submissions/1"):
        assert c.get(path).status_code == 200
    assert _digest() == before, "원본 CSV가 변경됨"


def test_reject_one_batch(client):
    c = client
    c.post("/submissions")
    _manual(c)
    _decide(c, B1, "approve")
    assert _decide(c, B2, "reject", reason="추가 검사 결과 대기").status_code == 303
    st = _state(c)
    states = {b["batch_id"]: b["state"] for b in st["batches"]}
    assert states[B1] == "회수의결" and states[B2] == "보류"
    review = [t for t in st["tasks"] if t["type"] == "재검토"]
    assert len(review) == 1 and "추가 검사 결과 대기" in review[0]["title"]
    conn = db.connect()
    by_batch = dict(conn.execute("SELECT batch_id, GROUP_CONCAT(DISTINCT state) FROM shipment_state"
                                 " WHERE batch_id IN (?,?) GROUP BY batch_id", (B1, B2)).fetchall())
    conn.close()
    assert by_batch == {B1: "회수요청", B2: "정상"}
    assert [i["hold_id"] for i in st["inventory"] if i["kind"] == "recall"] == [f"INV-{B1}"]


def _approve_both(c):
    c.post("/submissions")
    _manual(c)
    _decide(c, B1, "approve")
    _decide(c, B2, "approve")
    return _state(c)


def test_quarantine_reply_replaces_estimate(client):
    st = _approve_both(client)
    task = next(t for t in st["tasks"] if t["type"] == "사내재고 격리" and B1 in t["title"])
    # 일반 완료 버튼으로는 닫을 수 없음 — 수량 회신 필수
    client.post(f"/tasks/{task['id']}/advance", data={"actor": "물류"})
    assert client.post(f"/tasks/{task['id']}/advance", data={"actor": "물류"}).status_code == 409
    assert client.post(f"/tasks/{task['id']}/quarantine",
                       data={"qty": 999999, "actor": "물류 담당자"}).status_code == 409   # 생산량 초과
    assert client.post(f"/tasks/{task['id']}/quarantine", data={"qty": 4700, "actor": "물류 담당자"},
                       follow_redirects=False).status_code == 303
    st = _state(client)
    inv = next(i for i in st["inventory"] if i["batch_id"] == B1)
    assert (inv["qty_basis"], inv["measured_qty"], inv["est_qty"]) == ("실측", 4700, 4885)
    assert next(t for t in st["tasks"] if t["id"] == task["id"])["state"] == "완료"
    conn = db.connect()
    h = conn.execute("SELECT from_state, to_state, actor_type FROM history WHERE entity_type='inventory_qty'").fetchone()
    conn.close()
    assert tuple(h) == ("추정 4,885", "실측 4,700", "사람")


def test_customer_holding_reply_recomputes_need(client):
    _approve_both(client)
    # C-101은 B1을 28,701EA 받음 → 보유량 20,000 회신 시 필요 수량 8,701 감소
    r = client.post("/api/customer-holdings", json={"batch_id": B1, "customer_id": "C-101", "held_qty": 20000,
                                                    "actor": "영업 담당자"})
    assert r.status_code == 200
    resupply = next(t for t in _state(client)["tasks"] if t["type"] == "재공급 요청")
    assert "290,952EA" in resupply["title"] and "일부 회신 반영(1/9)" in resupply["title"]
    assert client.post("/api/customer-holdings", json={"batch_id": B1, "customer_id": "C-101",
                                                       "held_qty": 30000, "actor": "x"}).status_code == 409


def test_ai_summary_pass(client, monkeypatch):
    _fake_claude(monkeypatch)
    client.post("/submissions")
    sub = _state(client)["submissions"][0]
    assert sub["ai_source"] == "claude" and sub["ai_status"] == ai.STATUS_OK
    assert _decide(client, B1, "approve").status_code == 303


def test_ai_summary_mismatch_warns(client, monkeypatch):
    _fake_claude(monkeypatch, rate_delta=1.5)
    client.post("/submissions")
    sub = _state(client)["submissions"][0]
    assert sub["ai_status"] == ai.STATUS_WARN
    problems = json.loads(sub["ai_check"])["problems"]
    assert not any("rate_prod_pct" in p for p in problems)        # AI가 옮긴 목록은 쓰지 않고 계산값을 넣음
    assert any("본문 숫자" in p for p in problems)
    page = client.get("/submissions/1").text
    assert "수치 불일치" in page
    assert _decide(client, B1, "approve").status_code == 303         # 경고는 표시만, 의결은 위원회 판단


def test_verify_detects_missing_customer(client):
    conn = db.connect()
    facts = workflow.build_facts(conn, [B1, B2])
    conn.close()
    out = _ai_output(facts)
    assert ai.verify(out, facts) == []
    out["cited_customers"] = out["cited_customers"][1:]
    assert any("영향 고객" in p and "누락" in p for p in ai.verify(out, facts))
