"""마지막 추가 3종: 클레임 접수 → 즉시 재판정 · 배치 추적(/trace) · 상신 화면 '승인 시 예상 공급 영향'."""
import hashlib
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
from w03 import db, presets, rules  # noqa: E402
from w03.config import SOURCE_DIR, SOURCE_TABLES  # noqa: E402

B1, B2 = presets.B1, presets.B2
T02, T03 = "B-02-2602-02", "B-02-2602-03"      # 같은 로트(LOT-B-2602-P)의 정상 배치 — 생산량 154,685 / 161,592


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


def _state_of(c, batch):
    return next((b["state"] for b in _st(c)["batches"] if b["batch_id"] == batch), "정상")


def _claim(c, batch=T02, qty="16000", confirmed="True", customer="C-101", product="PL-02", claim_type="규격",
           actor="품질팀 접수 담당", occurred="2026-06-20", applied=None):
    data = {"customer_id": customer, "product_id": product, "batch_id": batch, "qty": qty, "confirmed": confirmed,
            "claim_type": claim_type, "actor": actor, "occurred_date": occurred}
    if applied is not None:
        data["applied_date"] = applied
    return c.post("/claims", data=data, follow_redirects=False)


def _digest():
    return {rel: hashlib.sha256((SOURCE_DIR / rel).read_bytes()).hexdigest() for rel in SOURCE_TABLES.values()}


# ---------- 1. 클레임 접수 ----------

def test_new_confirmed_claim_creates_recall_target_and_pending(c):
    before = _digest()
    c.post("/assess")
    assert _state_of(c, T02) == "정상" and _st(c)["pending_targets"] == [B1, B2]
    r = _claim(c)                                                   # 16,000 ÷ 154,685 = 10.34% → 회수대상
    assert r.status_code == 303 and r.headers["location"] == "/claims?new=QC-N0001"
    add = _q("SELECT * FROM claim_addition WHERE claim_id='QC-N0001'")[0]
    assert (add["source"], add["batch_id"], add["qty_affected"], add["defect_confirmed"], add["status"], add["lot_id"]) == \
        ("클레임 접수", T02, "16000", "True", "확정", "LOT-B-2602-P")
    a = _q("SELECT * FROM assessment WHERE batch_id=?", T02)[0]
    assert a["confirmed_qty"] == 16000 and round(a["rate_prod"], 2) == 10.34 and a["is_target"] == 1
    assert _state_of(c, T02) == "회수대상"
    assert _st(c)["pending_targets"] == [B1, T02, B2]               # 상신 대기에 자동 추가
    hist = [h["reason"] for h in _q("SELECT reason FROM history WHERE entity_type='batch' AND entity_id=?", T02)]
    assert any(h.startswith("클레임 QC-N0001 접수 → 기준 충족") for h in hist)
    assert _q("SELECT actor FROM history WHERE entity_type='claim' AND entity_id='QC-N0001'")[0]["actor"] == "품질팀 접수 담당"
    page = c.get("/claims?new=QC-N0001").text
    assert "QC-N0001 접수 완료" in page and "회수대상 전환" in page and "상신 대기 목록" in page
    dash = c.get("/").text
    assert "상신 대기" in dash and f'href="/trace/{T02}"' in dash and "회수대상 3건 상신" in dash
    c.post("/submissions")
    assert T02 in _st(c)["submissions"][-1]["batch_ids"]          # 다음 상신에 포함
    assert _digest() == before                                      # 원본 CSV 불변


def test_claim_on_existing_target_keeps_decision(c):
    c.post("/assess")
    assert _claim(c, batch=B1, qty="500").status_code == 303
    assert _state_of(c, B1) == "회수대상"
    hist = [h["reason"] for h in _q("SELECT reason FROM history WHERE entity_type='batch' AND entity_id=?", B1)]
    assert any(h.startswith("판정 유지") and "18,400EA" in h for h in hist)


def test_small_or_unconfirmed_claim_keeps_normal_and_flags_r2(c):
    c.post("/assess")
    assert _claim(c, batch=T03, qty="300").status_code == 303       # 확정 300 → 0.19%: 기준 미달
    assert _state_of(c, T03) == "정상"
    assert _claim(c, batch=T03, qty="5000", confirmed="False").status_code == 303
    row = _q("SELECT * FROM batch_state WHERE batch_id=?", T03)[0]
    assert row["state"] == "정상" and "R2 미확정 클레임 진행 1건" in row["investigate_reason"]
    assert _q("SELECT status FROM claim_addition WHERE claim_id='QC-N0002'")[0]["status"] == "접수"


def test_claim_before_assessment_is_stored_then_applied(c):
    assert _claim(c).status_code == 303
    assert "판정 전" in c.get("/claims?new=QC-N0001").text
    assert _state_of(c, T02) == "정상"
    c.post("/assess")
    assert _state_of(c, T02) == "회수대상"


@pytest.mark.parametrize("kw, msg", [
    ({"product": "PL-01"}, "제품"),
    ({"qty": "200000"}, "생산량"),
    ({"qty": "0"}, "수량"),
    ({"qty": "abc"}, "정수"),
    ({"customer": "C-999"}, "고객"),
    ({"batch": "B-99-0000-00"}, "배치"),
    ({"claim_type": "기타"}, "유형"),
    ({"confirmed": ""}, "확정 여부"),
    ({"actor": " "}, "접수자"),
])
def test_claim_validation_rejects(c, kw, msg):
    c.post("/assess")
    r = _claim(c, **kw)
    assert r.status_code == 409 and msg in r.text
    assert _q("SELECT COUNT(*) n FROM claim_addition")[0]["n"] == 0


def test_claim_from_customer_without_sbl_is_flagged(c):
    c.post("/assess")
    assert not _q("SELECT 1 FROM src_sbl WHERE batch_id=? AND customer_id='C-105'", T02)
    assert _claim(c, customer="C-105", qty="100").status_code == 303
    assert "SBL 출하 기록이 없습니다" in c.get("/claims?new=QC-N0001").text


# ---------- 2. 배치 추적 ----------

def test_trace_shows_chain_with_link_basis(c):
    conn = db.connect()
    presets.baseline(conn)
    conn.close()
    page = c.get(f"/trace/{B1}").text
    for text in ("배치 추적 B-02-2602-01", "LOT-B-2602-P", T02, T03, "qc_claims_cv.batch_id",
                 "shipment_batch_link_cv.batch_id", "prod_batch_log_cv.input_lot_ids", "같은 input_lot_ids",
                 "같은 로트 → 조사대상(A-3, R6)", "생산량 − SBL 출하 합 추정(A-5)",
                 "출하 연결 미확인", "C-105", "고객 통지", "사내재고 격리", "회수상신 #1"):
        assert text in page, text
    assert page.count(">원본 키<") >= 3 and page.count(">가정<") >= 2
    linked = {t["id"] for t in _st(c)["tasks"] if t["type"] == "사내재고 격리" and B1 in t["title"]}
    assert linked and all(f"<td>{tid}</td>" in page for tid in linked)


def test_trace_marks_app_added_links(c):
    c.post("/assess")
    _claim(c, batch=T02, qty="100")
    page = c.get(f"/trace/{T02}").text
    assert "QC-N0001" in page and ">앱 추가<" in page and "claim_addition — 화면 &#39;클레임 접수&#39;" in page
    flow = page.split("<h2>① 클레임</h2>")[0]
    assert "claim_addition.batch_id" in flow and "qc_claims_cv.batch_id" not in flow   # 원본 클레임이 없으면 원본 키 표시 안 함
    lot_table = page.split("<h2>③④ 원료 로트 · 같은 로트 배치</h2>")[1].split("</table>")[0]
    b1_row = lot_table.split(f'href="/trace/{B1}">{B1}</a>')[1].split("</tr>")[0]
    assert "R2 원본 QC 결과 보류" in b1_row and "A-3, R6" not in b1_row                    # R6 배지는 R6 사유일 때만
    t03_row = lot_table.split(f'href="/trace/{T03}">{T03}</a>')[1].split("</tr>")[0]
    assert "A-3, R6" in t03_row
    assert c.get("/trace/B-99-0000-00").status_code == 404


# ---------- 3. 승인 시 예상 공급 영향 ----------

def _manual_submission(c):
    c.post("/assess")
    c.post("/submissions")
    c.post("/submissions/1/summary", data={"author": "품질팀", "headline": "h", "summary": "s",
                                           "actions": "영업: 통지", "questions": "-"})


def test_preview_reuses_r8_and_is_marked_estimate(c):
    _manual_submission(c)
    conn = db.connect()
    expected = rules.resupply_check(conn, [B1, B2])
    conn.close()
    page = c.get("/submissions/1").text
    head, _, rest = page.partition('id="preview"')
    assert rest and "id=\"decide\"" in head                         # 의결 표(버튼) 위
    preview = rest.split("</table>")[0]
    assert "승인 시 예상 공급 영향" in preview and "예상치" in preview
    p = expected["production"]
    assert (p["need_qty"], p["spare_qty"], expected["overall"]) == (299653, 58484, "대안필요")
    for text in ("299,653 EA", "58,484 EA", "생산 대안필요", f"조달 {expected['material']['result']}", "<b>대안필요</b>",
                 "5.12", "상한값(가정 A-8"):
        assert text in preview, text
    assert preview.index("승인 시 예상 공급 영향") < preview.index('action="/submissions/1/batches/')


def test_preview_tracks_partial_decisions_and_matches_plan(c):
    _manual_submission(c)
    c.post(f"/submissions/1/batches/{B1}/decide", data={"decision": "approve", "decider": "위원장", "reason": "기준 충족"})
    page = c.get("/submissions/1").text
    assert "의결 전 배치(B-02-2603-01)" in page and "299,653 EA" in page
    c.post(f"/submissions/1/batches/{B2}/decide", data={"decision": "approve", "decider": "위원장", "reason": "기준 충족"})
    page = c.get("/submissions/1").text
    assert "승인 시 예상 공급 영향" not in page                      # 의결이 끝나면 미리보기 없음
    plan = next(t for t in _st(c)["tasks"] if t["type"] == "재공급 요청")
    assert "299,653EA" in plan["title"]                               # 의결 후 실제 계획과 같은 값


def test_preview_not_on_resubmission(c):
    conn = db.connect()
    sid = presets.large_correction(conn)
    conn.close()
    assert "승인 시 예상 공급 영향" not in c.get(f"/submissions/{sid}").text
