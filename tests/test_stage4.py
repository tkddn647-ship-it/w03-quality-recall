"""4단계: (a) 표시 · 원료 로트 역추적(필수 확장) · 출하 연결 미확인 고객(정보 부족) · AI 회신 구조화."""
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
os.environ.setdefault("W03_DB", str(Path(tempfile.mkdtemp()) / "test_w03.db"))
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from w03 import ai, db, lots, presets, replies, resupply  # noqa: E402
from w03.config import SOURCE_DIR, SOURCE_TABLES  # noqa: E402

B1, B2 = presets.B1, presets.B2
LOT = "LOT-B-2602-P"
T02, T03 = "B-02-2602-02", "B-02-2602-03"


@pytest.fixture
def c():
    with TestClient(main.app) as client:
        yield client


def _st(c):
    return c.get("/api/state").json()


def _baseline():
    conn = db.connect()
    sid = presets.baseline(conn)
    conn.close()
    return sid


def _q(sql, *args):
    conn = db.connect()
    rows = [dict(r) for r in conn.execute(sql, args)]
    conn.close()
    return rows


def _task(type_, related=None):
    sql = "SELECT * FROM task WHERE type=?" + (" AND related_id=?" if related else "") + " ORDER BY id"
    return _q(sql, type_, *([related] if related else []))


def _digest():
    return {rel: hashlib.sha256((SOURCE_DIR / rel).read_bytes()).hexdigest() for rel in SOURCE_TABLES.values()}


# ---------- 1. (a) 표시 ----------

def test_stage_a_cell_shows_this_period_and_later_erosion(c):
    sid = _baseline()
    conn = db.connect()
    a = resupply.compare(conn, sid)["a"]
    conn.close()
    periods = round(a["selected_qty"] / 58484, 2)            # 242,973 ÷ 58,484 = 4.15
    assert a["warning"]["periods"] == periods == 4.15
    page = c.get(f"/plans/{sid}").text
    assert "이번 기간 0" in page and f"이후 {periods}기간 여유량 잠식" in page


# ---------- 2. 원료 로트 역추적 ----------

def test_lot_trace_blocks_same_lot_stock_and_stops_lot(c):
    _baseline()
    st = _st(c)
    assert [(l["lot_id"], l["state"]) for l in st["lots"]] == [(LOT, "사용정지")]
    trace = {i["batch_id"]: i for i in st["inventory"] if i["kind"] == "trace"}
    assert {b: (i["state"], i["est_qty"]) for b, i in trace.items()} == {T02: ("추가검사필요", 3213), T03: ("추가검사필요", 10483)}
    assert {t["related_id"] for t in _task("추가검사")} == {f"INV-{T02}", f"INV-{T03}"}
    assert all(t["dept"] == "품질" for t in _task("추가검사"))
    conn = db.connect()
    rs = resupply.compare(conn, 1)["resupply"]
    conn.close()
    assert LOT in rs["material"]["stopped_lots"] and LOT in rs["material"]["excluded_lots"]
    assert all(b["state"] == "정상" for b in st["batches"] if b["batch_id"] in (T02, T03))   # 배치 상태는 그대로(R6)


def test_lot_compare_without_vs_with_extension(c):
    _baseline()
    conn = db.connect()
    rows = {r["label"]: r for r in lots.compare(conn, LOT)["rows"]}
    conn.close()
    assert (rows["같은 로트 재고 출하 가능 수량"]["without"], rows["같은 로트 재고 출하 가능 수량"]["with"]) == (13696, 0)
    assert (rows["재공급 필요량"]["without"], rows["재공급 필요량"]["with"]) == (299653 - 13696, 299653)
    assert (rows["필요 ÷ 여유"]["without"], rows["필요 ÷ 여유"]["with"]) == (4.89, 5.12)
    page = c.get(f"/lots/{LOT}").text
    assert "확장이 없을 때 vs 있을 때" in page and "추가검사필요" in page


def test_inspection_pass_and_fail_then_rejudge(c):
    r = c.post("/presets/lot", data={"confirm_text": "프리셋"}, follow_redirects=False)
    assert r.headers["location"] == f"/lots/{LOT}"
    st = _st(c)
    trace = {i["batch_id"]: i for i in st["inventory"] if i["kind"] == "trace"}
    assert (trace[T03]["state"], trace[T03]["measured_qty"]) == ("출하가능", 10483)
    assert (trace[T02]["state"], trace[T02]["measured_qty"]) == ("검사불합격", 3213)
    add = st["claim_additions"]
    assert len(add) == 1 and (add[0]["batch_id"], add[0]["qty_affected"], add[0]["defect_confirmed"]) == (T02, "900", "True")
    a = next(x for x in _q("SELECT * FROM assessment WHERE batch_id=?", T02))
    assert a["confirmed_qty"] == 900 and round(a["rate_prod"], 2) == 0.58
    hist = _q("SELECT reason FROM history WHERE entity_type='batch' AND entity_id=?", T02)
    assert any("기준 미달(R1 비해당)" in h["reason"] for h in hist)
    # 합격분 10,483EA가 재공급 가능 재고로 빠져 필요량 감소
    conn = db.connect()
    p = resupply.compare(conn, 1)["resupply"]["production"]
    conn.close()
    assert (p["stock_offset_qty"], p["need_qty"], p["stock_blocked_qty"]) == (10483, 299653 - 10483, 3213)


def test_inspection_fail_over_threshold_targets_batch(c):
    _baseline()
    t = _task("추가검사", f"INV-{T02}")[0]
    r = c.post(f"/tasks/{t['id']}/inspection", data={"result": "fail", "inspected_qty": "20000", "defect_qty": "16000",
                                                     "actor": "검사원"}, follow_redirects=False)
    assert r.status_code == 303
    assert next(b for b in _st(c)["batches"] if b["batch_id"] == T02)["state"] == "회수대상"   # 16,000 ÷ 154,685 = 10.3%
    assert c.post(f"/tasks/{_task('추가검사', f'INV-{T03}')[0]['id']}/inspection",
                  data={"result": "fail", "inspected_qty": "100", "actor": "x"}).status_code == 409   # 불량 수량 필수
    # R1 회수대상이 돼도 표본 경보 후속은 함께 남는다. 화면 입력 회신은 '모의 회신(화면 입력)'
    alert = next(a for a in _st(c)["sample_alerts"] if a["batch_id"] == T02)
    assert (alert["rate_pct"], alert["data_source"]) == (80.0, "모의 회신(화면 입력)")


def test_inspection_fail_alert_keeps_r1_and_creates_followups(c):
    """불합격: R1 유지(0.58%, 정상) · 재고 격리 유지 + 폐기 검토 · 표본 불량률 경보 + 위원회 보고 · 기출하 고객 모니터링 강화."""
    c.post("/presets/lot", data={"confirm_text": "프리셋"})
    st = _st(c)
    assert next((b["state"] for b in st["batches"] if b["batch_id"] == T02), "정상") == "정상"
    assert [s for s in st["submissions"] if T02 in (s["batch_ids"] or "")] == []          # 회수 상신 없음
    assert next(i for i in st["inventory"] if i["batch_id"] == T02)["state"] == "검사불합격"  # 격리 유지(출하 불가)

    [alert] = st["sample_alerts"]
    assert (alert["batch_id"], alert["defect_qty"], alert["inspected_qty"]) == (T02, 900, 3213)
    assert alert["rate_pct"] == 28.01 and alert["data_source"] == "프리셋 모의 데이터"

    [dispose] = _task("폐기 검토", f"INV-{T02}")
    [report] = _task("품질위원회 보고", T02)
    assert (dispose["dept"], dispose["state"], dispose["id"]) == ("품질", "대기", alert["dispose_task_id"])
    assert (report["dept"], report["id"]) == ("품질위원회", alert["report_task_id"])
    assert "회수 의결 아님" in report["title"] and "28.01%" in report["title"]

    sbl = {r["customer_id"]: r["qty"] for r in _q(
        "SELECT customer_id, SUM(CAST(shipped_qty AS INTEGER)) qty FROM src_sbl WHERE batch_id=? GROUP BY customer_id", T02)}
    assert sbl and {w["customer_id"]: w["shipped_qty"] for w in st["claim_watch"]} == sbl
    assert {r["state"] for r in _q("SELECT state FROM shipment_state WHERE batch_id=?", T02)} == {"정상"}   # 회수요청 아님

    # 위원회 보고는 일반 Task로 완료되고 배치 상태를 바꾸지 않는다
    for _ in range(2):
        assert c.post(f"/tasks/{report['id']}/advance", data={"actor": "품질위원회"}, follow_redirects=False).status_code == 303
    assert _task("품질위원회 보고", T02)[0]["state"] == "완료"
    assert next((b["state"] for b in _st(c)["batches"] if b["batch_id"] == T02), "정상") == "정상"

    batch_page = c.get(f"/batches/{T02}").text
    for text in ("표본 불량률 경보", "28.01%", "클레임 모니터링 강화", "프리셋 모의 데이터", "회수 의결 아님"):
        assert text in batch_page
    lot_page = c.get(f"/lots/{LOT}").text
    for text in ("모의 데이터", "프리셋 모의 데이터", "격리 유지", "경보 28.01%", "폐기 검토", "품질위원회 보고"):
        assert text in lot_page
    assert "표본 불량률 경보" not in c.get(f"/batches/{T03}").text      # 합격 배치에는 경보 없음


# ---------- 3. 출하 연결 미확인 고객 ----------

def test_unlinked_customer_held_and_task(c):
    _baseline()
    st = _st(c)
    un = {(u["batch_id"], u["customer_id"]): u for u in st["unlinked"]}
    assert set(un) == {(B1, "C-105"), (B2, "C-105")}
    assert all(u["state"] == "출하 연결 미확인" for u in un.values())
    assert not _q("SELECT * FROM shipment_state WHERE customer_id='C-105' AND batch_id IN (?,?)", B1, B2)   # 회수요청 안 보냄
    assert len(_task("출하 경로 확인 요청")) == 2 and all(t["dept"] == "영업" for t in _task("출하 경로 확인 요청"))


def test_unlinked_confirmed_adds_recall_and_need(c):
    _baseline()
    t = _task("출하 경로 확인 요청", f"{B1}|C-105")[0]
    assert c.post(f"/tasks/{t['id']}/shipment-route", data={"decision": "shipped", "qty": "6000", "actor": "영업"}).status_code == 409
    r = c.post(f"/tasks/{t['id']}/shipment-route", data={"decision": "shipped", "qty": "3000", "actor": "영업"},
               follow_redirects=False)
    assert r.status_code == 303
    shp = _q("SELECT * FROM shipment_state WHERE shipment_id=?", f"CNF-{B1}-C-105")[0]
    assert (shp["state"], shp["shipped_qty"], shp["source"]) == ("회수요청", 3000, "확인 회신")
    assert _q("SELECT est_qty FROM inventory_hold WHERE hold_id=?", f"INV-{B1}")[0]["est_qty"] == 4885 - 3000
    notice = _task("고객 통지", "C-105")
    assert notice and "3,000EA" in notice[0]["title"]
    conn = db.connect()
    p = resupply.compare(conn, 1)["resupply"]["production"]
    conn.close()
    assert p["upper_qty"] == 299653 + 3000


def test_unlinked_none_creates_batch_recheck(c):
    _baseline()
    t = _task("출하 경로 확인 요청", f"{B2}|C-105")[0]
    c.post(f"/tasks/{t['id']}/shipment-route", data={"decision": "none", "actor": "영업"})
    assert _q("SELECT state FROM unlinked_customer WHERE batch_id=? AND customer_id='C-105'", B2)[0]["state"] == "출하 없음"
    chk = _task("클레임 배치 재확인")
    assert len(chk) == 1 and chk[0]["dept"] == "품질"


# ---------- 4. AI 회신 구조화 ----------

def _fake_ai(monkeypatch, payload):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    calls = []

    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5", _request_id="req_test",
                               content=[SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))])

    monkeypatch.setattr(ai.anthropic, "Anthropic",
                        lambda: SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create))))
    return calls


def _intake(c, task, text):
    r = c.post(f"/tasks/{task['id']}/reply-text", data={"raw_text": text, "submitted_by": "고객(모의)"},
               follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers["location"].rsplit("/", 1)[1])


def _intake_row(iid):
    return _q("SELECT * FROM reply_intake WHERE id=?", iid)[0]


def test_ai_reply_valid_then_apply_updates_need(c, monkeypatch):
    calls = _fake_ai(monkeypatch, {"customer_id": "C-101", "batch_id": B1, "held_qty": 12000, "recall_agree": True,
                                   "pickup_date": "다음 주 화요일", "evidence": "재고 12,000개 보유 중"})
    _baseline()
    iid = _intake(c, _task("고객 통지", "C-101")[0], presets.REPLY_OK)
    row = _intake_row(iid)
    assert (row["source"], row["status"]) == ("claude", "검증 통과")
    sch = calls[0]["output_config"]["format"]["schema"]
    assert set(sch["required"]) >= {"customer_id", "batch_id", "held_qty", "recall_agree", "pickup_date"}
    assert c.post(f"/intakes/{iid}/apply", data={"actor": "영업 담당"}, follow_redirects=False).status_code == 303
    assert _intake_row(iid)["status"] == "반영"
    held = _q("SELECT * FROM customer_holding WHERE batch_id=? AND customer_id='C-101'", B1)[0]
    assert held["held_qty"] == 12000 and held["source"] == f"회신 #{iid} 반영"
    reasons = [h["to_state"] for h in _q("SELECT to_state FROM history WHERE entity_type='reply' AND entity_id=? ORDER BY id",
                                         str(iid))]
    assert reasons == ["접수", "AI 구조화", "검증 통과", "반영"]
    conn = db.connect()
    p = resupply.compare(conn, 1)["resupply"]["production"]
    conn.close()
    assert p["need_qty"] == 299653 - (28701 - 12000)


@pytest.mark.parametrize("payload,expect", [
    ({"customer_id": "C-101", "batch_id": B1, "held_qty": None, "recall_agree": True, "pickup_date": None,
      "evidence": ""}, "필수 항목 누락: 보유수량"),
    ({"customer_id": "C-101", "batch_id": B1, "held_qty": 99999, "recall_agree": True, "pickup_date": None,
      "evidence": ""}, "수량 초과"),
    ({"customer_id": "C-104", "batch_id": B1, "held_qty": 100, "recall_agree": True, "pickup_date": None,
      "evidence": ""}, "고객 불일치"),
    ({"customer_id": "C-101", "batch_id": None, "held_qty": 100, "recall_agree": True, "pickup_date": None,
      "evidence": ""}, "배치 특정 불가"),
    ({"customer_id": "C-101", "batch_id": B1, "held_qty": 5555, "recall_agree": True, "pickup_date": None,
      "evidence": ""}, "원문에 없는 수량"),
])
def test_ai_reply_validation_failures(c, monkeypatch, payload, expect):
    _fake_ai(monkeypatch, payload)
    _baseline()
    raw = "C-101입니다. B-02-2602-01 재고 100개 남았고 99999 아님, 회수 동의합니다."
    iid = _intake(c, _task("고객 통지", "C-101")[0], raw)
    row = _intake_row(iid)
    assert row["status"] == "사람 확인 필요"
    assert any(expect in p for p in json.loads(row["validation_json"])["problems"])
    assert c.post(f"/intakes/{iid}/apply", data={"actor": "x"}).status_code == 409        # 검증 실패는 반영 불가
    assert "사람 확인 필요" in c.get(f"/intakes/{iid}").text


def test_no_key_goes_to_manual_form_then_apply(c):
    _baseline()
    iid = _intake(c, _task("고객 통지", "C-101")[0], presets.REPLY_OK)
    row = _intake_row(iid)
    assert row["source"] is None and row["status"] == "사람 확인 필요"
    assert "AI 구조화 실패" in json.loads(row["validation_json"])["problems"][0]
    assert "구조화 폼 직접 작성" in c.get(f"/intakes/{iid}").text
    c.post(f"/intakes/{iid}/manual", data={"customer_id": "C-101", "batch_id": B1, "held_qty": "12,000",
                                           "recall_agree": "true", "pickup_date": "", "actor": "영업 담당"})
    assert (_intake_row(iid)["source"], _intake_row(iid)["status"]) == ("manual", "검증 통과")
    c.post(f"/intakes/{iid}/apply", data={"actor": "영업 담당"})
    assert _intake_row(iid)["status"] == "반영"


def test_ai_reply_for_inspection_and_procurement(c, monkeypatch):
    _fake_ai(monkeypatch, {"batch_id": None, "result": "합격", "inspected_qty": 10483, "defect_qty": None,
                           "evidence": "전수 검사 결과 합격"})
    _baseline()
    iid = _intake(c, _task("추가검사", f"INV-{T03}")[0], "잔여 10,483개 전수 검사 결과 규격 이상 없음, 합격입니다.")
    row = _intake_row(iid)
    v = json.loads(row["validation_json"])
    assert row["status"] == "검증 통과" and v["data"]["batch_id"] == T03 and "가정 A-24" in v["notes"][0]
    c.post(f"/intakes/{iid}/apply", data={"actor": "품질"})
    assert _q("SELECT state FROM inventory_hold WHERE hold_id=?", f"INV-{T03}")[0]["state"] == "출하가능"


def test_reply_presets_and_pages(c):
    for key in ("reply_ok", "reply_vague", "lot"):
        url = c.post(f"/presets/{key}", data={"confirm_text": "프리셋"}, follow_redirects=False).headers["location"]
        assert c.get(url).status_code == 200
        if key == "reply_ok":
            assert _intake_row(1)["status"] == "반영"
        if key == "reply_vague":
            row = _intake_row(1)
            assert row["status"] == "사람 확인 필요"
            assert any("보유수량" in p for p in json.loads(row["validation_json"])["problems"])
    for path in ("/", "/history", "/plans/1", f"/lots/{LOT}", "/submissions/1"):
        assert c.get(path).status_code == 200
    assert _st(c)["ai_configured"] is False


def test_originals_untouched_after_stage4_flows(c):
    before = _digest()
    for key in ("lot", "reply_ok", "reply_vague"):
        c.post(f"/presets/{key}", data={"confirm_text": "프리셋"})
    assert _digest() == before
