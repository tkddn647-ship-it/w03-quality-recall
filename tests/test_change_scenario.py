"""3단계: 검사 정정(조건 변경) → 재검증 · 재상신, R8 대안필요 → 대안 (a)(b)(c) 선택·승인 회신, 시연 프리셋."""
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("W03_DB", str(Path(tempfile.mkdtemp()) / "test_w03.db"))
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from w03 import db, presets, resupply  # noqa: E402
from w03.config import SOURCE_DIR, SOURCE_TABLES  # noqa: E402
from w03.core import HOLD  # noqa: E402

B1, B2 = presets.B1, presets.B2


def _digest():
    return {rel: hashlib.sha256((SOURCE_DIR / rel).read_bytes()).hexdigest() for rel in SOURCE_TABLES.values()}


@pytest.fixture
def c():
    with TestClient(main.app) as client:
        yield client


def _st(c):
    return c.get("/api/state").json()


def _baseline(c):
    conn = db.connect()
    sid = presets.baseline(conn)
    conn.close()
    return sid


def _state_of(st, batch):
    return next(b for b in st["batches"] if b["batch_id"] == batch)


def _tasks(st, type_):
    return [t for t in st["tasks"] if t["type"] == type_]


def _correct(c, claim, confirmed="", qty="", corrector="품질팀", reason="재검사", evidence="성적서 #1"):
    return c.post("/corrections", data={"claim_id": claim, "new_confirmed": confirmed, "new_qty": qty,
                                        "corrector": corrector, "reason": reason, "evidence": evidence,
                                        "next": "/"}, follow_redirects=False)


# ---------- 1·2. 정정 입력과 자동 재검증 ----------

def test_correction_requires_fields_and_change(c):
    _baseline(c)
    assert _correct(c, "QC-00006", qty="600", evidence=" ").status_code == 409        # 근거 필수
    assert _correct(c, "QC-00006", confirmed="True").status_code == 409              # 이미 True — 변경 없음
    assert _correct(c, "QC-00006", qty="-5").status_code == 400


def test_small_correction_keeps_decision_with_reason(c):
    before = _digest()
    r = c.post("/presets/small", data={"confirm_text": "프리셋"}, follow_redirects=False)
    assert r.headers["location"] == f"/batches/{B1}"
    st = _st(c)
    assert _state_of(st, B1)["state"] == "회수의결"
    k = st["corrections"][0]
    assert (k["claim_id"], k["field"], k["original_value"], k["previous_value"], k["corrected_value"]) == \
        ("QC-00006", "qty_affected", "1100", "1100", "600")
    conn = db.connect()
    keep = conn.execute("SELECT reason, evidence_json FROM history WHERE entity_type='batch' AND entity_id=?"
                        " AND reason LIKE '판정 유지%'", (B1,)).fetchone()
    src = conn.execute("SELECT qty_affected FROM src_claims WHERE claim_id='QC-00006'").fetchone()[0]
    conn.close()
    assert "11.54%" in keep["reason"] and "11.93%" in keep["reason"] and "유지 근거" in keep["reason"]
    assert json.loads(keep["evidence_json"])["confirmed_qty"] == 17400
    assert src == "1100"                                                         # SQLite 원본 복사본도 그대로
    assert "정정 내역" in c.get(f"/batches/{B1}").text
    assert _digest() == before


def test_large_correction_review_holds_followups_and_recomputes(c):
    r = c.post("/presets/large", data={"confirm_text": "프리셋"}, follow_redirects=False)
    assert r.headers["location"] == "/submissions/2"
    st = _st(c)
    b2 = _state_of(st, B2)
    assert (b2["state"], b2["review_from"]) == ("재검토", "회수의결")
    a = next(x for x in st["assessment"] if x["batch_id"] == B2)
    assert a["confirmed_qty"] == 15326 and a["rate_prod"] < 10 and a["rate_ship"] < 10
    held = [t for t in st["tasks"] if t["state"] == HOLD]
    assert {t["type"] for t in held} == {"사내재고 격리", "고객 통지", "출하 경로 확인 요청"}   # B2 격리 + C-108 + C-105(B2) 확인
    c101 = next(t for t in _tasks(st, "고객 통지") if "C-101" in t["title"])
    assert c101["state"] == "대기" and "28,701EA" in c101["title"] and "재검토 대기" in c101["title"]
    req = _tasks(st, "재공급 요청")[0]
    assert "145,860EA" in req["title"]                                           # B2 빠진 필요량으로 재판정
    sub = next(s for s in st["submissions"] if s["id"] == 2)
    assert sub["kind"] == "재상신" and json.loads(sub["batch_ids"]) == [B2]
    assert "해제 승인" in c.get("/submissions/2").text

    # 재상신 해제 승인 → 정상 · 요청철회 · 보류해제 · Task 취소
    r = c.post(f"/submissions/2/batches/{B2}/decide",
               data={"decision": "approve", "decider": "위원장", "reason": "정정 후 기준 미달"}, follow_redirects=False)
    assert r.status_code == 303
    st = _st(c)
    assert _state_of(st, B2)["state"] == "정상"
    assert {s["state"]: s["n"] for s in st["shipments"]}["요청철회"] == 5
    assert next(i for i in st["inventory"] if i["batch_id"] == B2)["state"] == "보류해제"
    assert {t["state"] for t in st["tasks"] if t["state"] in (HOLD, "취소")} == {"취소"}
    assert "B-02-2603-01" not in next(t for t in _tasks(st, "고객 통지") if "C-101" in t["title"])["title"].split("(")[1]


def test_review_rejected_resumes_followups(c):
    c.post("/presets/large", data={"confirm_text": "프리셋"})
    c.post(f"/submissions/2/batches/{B2}/decide",
           data={"decision": "reject", "decider": "위원장", "reason": "원인 조사 결과 전까지 회수 유지"})
    st = _st(c)
    assert _state_of(st, B2)["state"] == "회수의결"
    assert not [t for t in st["tasks"] if t["state"] == HOLD]
    assert "299,653EA" in _tasks(st, "재공급 요청")[0]["title"]


def test_correction_before_decision_moves_batch_out(c):
    c.post("/admin/reset", data={"confirm_text": "초기화"})
    c.post("/assess")
    c.post("/submissions")
    for claim in presets.LARGE_UNCONFIRM:
        _correct(c, claim, confirmed="False")
    st = _st(c)
    assert _state_of(st, B2)["state"] == "재검토"
    assert any(d["batch_id"] == B2 and d["result"] == "재검토 이관" for d in st["decisions"])
    assert c.post(f"/submissions/1/batches/{B2}/decide",
                  data={"decision": "approve", "decider": "x", "reason": "y"}).status_code == 409


def test_upward_correction_targets_normal_batch(c):
    c.post("/admin/reset", data={"confirm_text": "초기화"})
    c.post("/assess")
    conn = db.connect()
    claim = conn.execute("SELECT claim_id FROM src_claims WHERE batch_id='B-02-2508-03' AND defect_confirmed='True'"
                         ).fetchone()[0]
    conn.close()
    _correct(c, claim, qty="20000")                                           # 16 → 20,000EA → 12.93%
    assert _state_of(_st(c), "B-02-2508-03")["state"] == "회수대상"


# ---------- 3. R8 대안필요 → (b) → (a) → (c) 순차 적용 ----------

def _plan(c):
    return _st(c)["plans"][0]


def _comp(sid):
    conn = db.connect()
    comp = resupply.compare(conn, sid)
    conn.close()
    return comp


def _reply(c, task, path, decision, actor="담당", reason="모의 회신"):
    return c.post(f"/tasks/{task['id']}/{path}", data={"decision": decision, "actor": actor, "reason": reason},
                  follow_redirects=False)


def _open(st, type_):
    return [t for t in _tasks(st, type_) if t["state"] in ("대기", "진행")]


def test_r8_starts_sequential_plan_with_stage_b(c):
    sid = _baseline(c)
    comp = _comp(sid)
    p = comp["resupply"]["production"]
    assert (p["spare_qty"], p["need_qty"], p["periods_needed"], p["result"]) == (58484, 299653, 5.12, "대안필요")
    assert p["capacity_period"].startswith("단위 미확정(실생산 2025년 578만EA와 불일치) — 판정은 단위와 무관")
    assert _plan(c)["state"] == "(b) 회신대기"
    st = _st(c)
    b_task = _open(st, "보유량 회신 수집")[0]
    assert "9/9건" in b_task["title"] and "299,653EA" in b_task["title"]        # 두 배치 승인 후 범위로 갱신
    # 단계별: 시작 241,169 → (b) 회신 없음 241,169 → (a) 예상 0 → (c) 대상 0
    assert comp["start"]["shortfall"] == comp["b"]["after"] == 241169
    assert comp["a"]["projected"] and comp["a"]["after"] == 0 and comp["a"]["selected_amount"] > 0
    assert comp["a"]["warning"]["periods"] == round(comp["a"]["applied_qty"] / 58484, 2)
    page = c.get(f"/plans/{sid}").text
    assert "대안 순차 적용" in page and "단계 후 남는 부족분" in page and "지연 금액" in page
    assert "연기는 생산능력을 만들지 않고 순서를 바꾸는 것" in page


def test_overdue_orders_excluded_and_check_task(c):
    sid = _baseline(c)
    a = _comp(sid)["a"]
    assert len(a["data_check"]) == 24 and a["pool_orders"] == 237 - 24
    assert all(o["est_due"] >= "2026-07-01" for o in a["selected"])
    assert not {o["order_id"] for o in a["selected"]} & {o["order_id"] for o in a["data_check"]}
    assert {t: s["median_days"] for t, s in a["lead_stats"].items()} == {"국내1": 60, "국내2": 61, "해외": 61}
    dues = [o["est_due"] for o in a["selected"]]
    assert dues == sorted(dues, reverse=True)
    assert a["selected_qty"] >= a["target"] > a["selected_qty"] - a["selected"][-1]["qty"]
    st = _st(c)
    check = _tasks(st, "주문 상태 확인 요청")
    assert len(check) == 1 and check[0]["dept"] == "영업" and "24건" in check[0]["title"]
    conn = db.connect()
    detail = json.loads(conn.execute("SELECT detail_json FROM task WHERE id=?", (check[0]["id"],)).fetchone()[0])
    conn.close()
    assert len(detail["orders"]) == 24


def test_holdings_partial_then_a_targets_remaining(c):
    sid = _baseline(c)
    c.post(f"/plans/{sid}/holding", data={"pair": f"{B1}|C-103", "held_qty": 5211, "actor": "영업"})  # 55,211 → 5,211
    comp = _comp(sid)
    assert comp["b"]["reduction"] == 50000 and comp["b"]["after"] == 241169 - 50000
    c.post(f"/plans/{sid}/close-b", data={"actor": "영업"})
    st = _st(c)
    assert st["plans"][0]["state"] == "(a) 승인대기"
    assert "남은 부족분 191,169EA" in _open(st, "영업 승인")[0]["title"]
    # 영업 거절 → (a) 적용분 0, 남은 부족분 전량 (c)
    _reply(c, _open(st, "영업 승인")[0], "approval", "reject", reason="주요 고객 납기 연기 불가")
    assert _plan(c)["state"] == "(c) 선택대기"
    assert _comp(sid)["c"]["target"] == 191169


def test_holdings_resolve_need_in_stage_b(c):
    sid = _baseline(c)
    conn = db.connect()
    pairs = conn.execute("SELECT DISTINCT batch_id, customer_id FROM shipment_state WHERE batch_id IN (?,?)",
                         (B1, B2)).fetchall()
    conn.close()
    for p in pairs:
        c.post("/api/customer-holdings", json={"batch_id": p[0], "customer_id": p[1], "held_qty": 5000, "actor": "영업"})
    st = _st(c)
    # 9쌍 × 5,000 = 45,000 ≤ 여유 58,484 → (b)에서 해소
    assert (st["plans"][0]["state"], st["plans"][0]["alternative"]) == ("대안확정", "b")
    assert _tasks(st, "재공급 생산")[0]["title"].startswith("[가능]")


def test_deferral_reject_then_reselect_then_confirm(c):
    r = c.post("/presets/deferral", data={"confirm_text": "프리셋"}, follow_redirects=False)
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    st = _st(c)
    assert (_plan(c)["state"], _plan(c)["alternative"]) == ("(a) 고객동의대기", "a")
    rejected = [d for d in st["deferrals"] if d["state"] == "거절"]
    assert len(rejected) == 1
    comp = _comp(sid)
    assert rejected[0]["customer_id"] in comp["a"]["excluded_customers"]
    assert rejected[0]["customer_id"] not in {o["customer_id"] for o in comp["a"]["selected"]}
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM history WHERE reason LIKE '%다음 순번 재선정%'").fetchone()[0] == 1
    conn.close()
    for t in _open(st, "납기 연기 동의 요청"):
        _reply(c, t, "customer-reply", "agree")
    st = _st(c)
    assert st["plans"][0]["state"] == "대안확정"
    title = _tasks(st, "재공급 생산")[0]["title"]
    assert "대안(a) 확정" in title and "A-17" in title


def test_deferral_partial_agreement_moves_remaining_to_c(c):
    sid = _baseline(c)
    c.post(f"/plans/{sid}/close-b", data={"actor": "영업"})
    _reply(c, _open(_st(c), "영업 승인")[0], "approval", "approve")
    first = True
    while _plan(c)["state"] == "(a) 고객동의대기":
        t = _open(_st(c), "납기 연기 동의 요청")[0]
        _reply(c, t, "customer-reply", "agree" if first else "reject")
        first = False
    comp = _comp(sid)
    agreed = comp["a"]["agreed"]["qty"]
    assert _plan(c)["state"] == "(c) 선택대기" and 0 < agreed < 241169
    assert comp["c"]["target"] == 241169 - agreed                          # (a) 동의분만큼만 줄어든 부족분


def _to_stage_c(c):
    sid = _baseline(c)
    c.post(f"/plans/{sid}/close-b", data={"actor": "영업"})
    _reply(c, _open(_st(c), "영업 승인")[0], "approval", "reject", reason="연기 불가")
    return sid


def test_opt02_conditional_until_procurement_reply(c):
    sid = _to_stage_c(c)
    opts = {o["option_id"]: o for o in _comp(sid)["c"]["options"]}
    assert opts["OPT-01"]["status"] == "충족" and opts["OPT-01"]["conditions"] == []
    assert opts["OPT-02"]["status"] == "조건부 충족(조달 확인 전)"
    assert opts["OPT-02"]["conditions"] == ["원료(M-B) 재계약", "처방 재검증"]
    c.post(f"/plans/{sid}/choose-c", data={"option_id": "OPT-02", "actor": "생산기획"})
    st = _st(c)
    assert st["plans"][0]["state"] == "(c) 조달확인대기"
    assert not _tasks(st, "투자 승인")                                     # 조달 회신 전에는 투자 승인으로 안 감
    proc = _open(st, "조달 확인")[0]
    assert proc["dept"] == "조달"
    assert c.post(f"/tasks/{proc['id']}/advance", data={"actor": "x"}, follow_redirects=False).status_code == 303
    assert c.post(f"/tasks/{proc['id']}/advance", data={"actor": "x"}).status_code == 409   # 회신으로만 완료
    _reply(c, proc, "procurement-reply", "infeasible", reason="V-205 재계약 불가")
    assert _plan(c)["state"] == "(c) 선택대기"
    assert {o["option_id"]: o["status"] for o in _comp(sid)["c"]["options"]}["OPT-02"] == "미달(조달 불가)"
    assert c.post(f"/plans/{sid}/choose-c", data={"option_id": "OPT-02", "actor": "x"}).status_code == 409


def test_opt02_feasible_then_investment(c):
    sid = _to_stage_c(c)
    c.post(f"/plans/{sid}/choose-c", data={"option_id": "OPT-02", "actor": "생산기획"})
    _reply(c, _open(_st(c), "조달 확인")[0], "procurement-reply", "feasible", reason="재계약·재검증 일정 확보")
    st = _st(c)
    assert st["plans"][0]["state"] == "(c) 승인대기"
    assert {o["option_id"]: o["status"] for o in _comp(sid)["c"]["options"]}["OPT-02"] == "충족(조달 가능 회신)"
    _reply(c, _open(st, "투자 승인")[0], "approval", "approve", reason="F2 전환 승인")
    st = _st(c)
    assert st["plans"][0]["state"] == "대안확정"
    assert "대안(c) 승인: OPT-02" in _tasks(st, "재공급 생산")[0]["title"]


def test_investment_rejections_exhaust_options(c):
    sid = _to_stage_c(c)
    c.post(f"/plans/{sid}/choose-c", data={"option_id": "OPT-01", "actor": "생산기획"})
    _reply(c, _open(_st(c), "투자 승인")[0], "approval", "reject", reason="투자 여력 없음")
    assert _plan(c)["state"] == "(c) 선택대기"
    c.post(f"/plans/{sid}/choose-c", data={"option_id": "OPT-02", "actor": "생산기획"})
    _reply(c, _open(_st(c), "조달 확인")[0], "procurement-reply", "infeasible", reason="재계약 불가")
    assert _plan(c)["state"] == "대안소진"
    assert "경영 판단 필요" in c.get(f"/plans/{sid}").text


def test_exact_3000_preset_keeps_decision_by_a2(c):
    r = c.post("/presets/exact3000", data={"confirm_text": "프리셋"}, follow_redirects=False)
    assert r.headers["location"] == f"/batches/{B2}"
    st = _st(c)
    assert _state_of(st, B2)["state"] == "회수의결"
    a = next(x for x in st["assessment"] if x["batch_id"] == B2)
    assert a["confirmed_qty"] == 15622 and round(a["rate_prod"], 2) == 9.72 and round(a["rate_ship"], 2) == 10.16
    assert sum(1 for k in st["corrections"] if k["corrected_value"] == "False") == 3
    conn = db.connect()
    last = conn.execute("SELECT reason FROM history WHERE entity_type='batch' AND entity_id=? ORDER BY id DESC LIMIT 1",
                        (B2,)).fetchone()[0]
    conn.close()
    assert "판정 유지" in last and "9.72%" in last and "10.16%" in last


def test_presets_need_confirmation(c):
    assert c.post("/presets/small").status_code == 400
    assert c.post("/presets/nope", data={"confirm_text": "프리셋"}).status_code == 404


def test_all_pages_render_after_presets(c):
    for key in presets.PRESETS:
        url = c.post(f"/presets/{key}", data={"confirm_text": "프리셋"}, follow_redirects=False).headers["location"]
        for path in (url, "/", "/history", "/plans/1", "/submissions/1", f"/batches/{B2}"):
            assert c.get(path).status_code == 200, (key, path)
