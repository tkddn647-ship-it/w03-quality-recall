"""마지막 보완: 회수의결 이후 클레임 · 위원회 거절 시 원인 조사(R4) · 외부 정보(언론 보도) 접수(R5)."""
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
from w03 import db, glossary, presets, provenance  # noqa: E402

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
    presets.baseline(conn)
    conn.close()


def _claim(c, customer, batch=B1, qty="500"):
    return c.post("/claims", data={"customer_id": customer, "product_id": "PL-02", "batch_id": batch, "qty": qty,
                                   "confirmed": "True", "claim_type": "규격", "actor": "품질팀",
                                   "occurred_date": "2026-06-30"}, follow_redirects=False)


def _manual_submission(c):
    c.post("/assess")
    c.post("/submissions")
    c.post("/submissions/1/summary", data={"author": "품질팀", "headline": "h", "summary": "s", "actions": "-", "questions": "-"})


# ---------- 1. 회수의결 이후 같은 배치 클레임 ----------

def test_claim_on_recalled_batch_logs_in_progress_and_checks_scope(c):
    _baseline()
    subs, tasks = len(_st(c)["submissions"]), len(_st(c)["tasks"])
    assert _claim(c, "C-101").status_code == 303                       # 기존 회수 범위 안 고객
    assert _claim(c, "C-106").status_code == 303                       # 이 배치 SBL 출하 기록 없는 고객
    st = _st(c)
    assert len(st["submissions"]) == subs and st["pending_targets"] == []           # 새 상신 없음
    assert next(b["state"] for b in st["batches"] if b["batch_id"] == B1) == "회수의결"
    scope = [t for t in st["tasks"] if t["type"] == "회수 범위 확인"]
    assert len(st["tasks"]) == tasks + 2 and len(scope) == 2
    assert "C-101" in scope[0]["title"] and "기존 회수 범위 안(회수요청" in scope[0]["title"]
    assert "C-106" in scope[1]["title"] and "새 고객(SBL 출하 기록 없음)" in scope[1]["title"]
    hist = [h["reason"] for h in _q("SELECT reason FROM history WHERE entity_type='batch' AND entity_id=? ORDER BY id", B1)]
    assert any(h.startswith("이미 회수 진행 중 — 클레임 QC-N0001(C-101") and "새 상신을 만들지 않음" in h for h in hist)
    assert any("회수 범위 밖 고객(새 고객)" in h for h in hist)
    assert any(h.startswith("판정 유지 — 클레임 QC-N0001 접수 → 재계산") for h in hist)        # '정정 후'가 아님
    due = {t["due_date"] for t in _q("SELECT due_date FROM task WHERE type='회수 범위 확인'")}
    assert due == {"2026-07-02"}                                                      # A-28: 1영업일
    assert any("회수 범위 확인" in q["title"] for q in st["decision_queue"])


def test_claim_before_decision_still_goes_to_submission(c):
    c.post("/assess")
    assert _claim(c, "C-101", batch=T02, qty="16000").status_code == 303
    st = _st(c)
    assert T02 in st["pending_targets"] and not any(t["type"] == "회수 범위 확인" for t in st["tasks"])


# ---------- 2. 위원회 거절 → 원인 조사는 계속(R4) ----------

def test_rejection_creates_cause_investigation(c):
    _manual_submission(c)
    c.post(f"/submissions/1/batches/{B1}/decide", data={"decision": "reject", "decider": "위원장", "reason": "추가 근거 필요"})
    tasks = {t["type"]: t for t in _st(c)["tasks"]}
    assert "재검토" in tasks and "원인 조사" in tasks
    assert "위원회 거절 B-02-2602-01(조사는 계속)" in tasks["원인 조사"]["title"] and "R4" in tasks["원인 조사"]["title"]
    assert next(b["state"] for b in _st(c)["batches"] if b["batch_id"] == B1) == "보류"
    c.post(f"/submissions/1/batches/{B2}/decide", data={"decision": "approve", "decider": "위원장", "reason": "기준 충족"})
    cause = [t for t in _st(c)["tasks"] if t["type"] == "원인 조사"]
    assert len(cause) == 1                                                             # 같은 로트 → 한 Task로 합침
    assert "회수의결 B-02-2603-01" in cause[0]["title"] and "위원회 거절 B-02-2602-01" in cause[0]["title"]


# ---------- 3. 외부 정보(언론 보도) 접수 ----------

def _news(c, batch_ids="", product_id="", published="2026-06-28", summary="일부 소비자 트러블 사례가 있는 것으로 알려졌다(미확정)"):
    return c.post("/external", data={"source": "온라인 매체 A", "published_date": published, "summary": summary,
                                     "product_id": product_id, "batch_ids": batch_ids, "actor": "품질팀"},
                  follow_redirects=False)


def test_news_without_named_batch_keeps_verdict_and_creates_monitoring_only(c):
    c.post("/assess")
    before = {b["batch_id"]: b["state"] for b in _st(c)["batches"]}
    r = _news(c)
    assert r.status_code == 303 and r.headers["location"] == "/external?new=EXT-0001"
    st = _st(c)
    assert {b["batch_id"]: b["state"] for b in st["batches"]} == before                  # 판정 그대로
    assert [t["type"] for t in st["tasks"]] == ["외부 정보 모니터링"] and st["tasks"][0]["dept"] == "품질"
    for b in (B1, B2):
        h = _q("SELECT reason FROM history WHERE entity_type='batch' AND entity_id=? AND reason LIKE '외부 정보 EXT-0001%'", b)
        assert h and "회수 기준 충족 — 판정 유지" in h[0]["reason"] and "R5: 언론 보도는 회수 근거 아님" in h[0]["reason"]
    assert st["submissions"] == []                                                      # 상신을 새로 만들지 않음
    page = c.get("/external?new=EXT-0001").text
    assert "EXT-0001 접수 완료" in page and "외부 정보 모니터링" in page


def test_news_naming_a_batch_creates_fact_check_without_recall(c):
    c.post("/assess")
    assert _news(c, batch_ids=T02, product_id="PL-02").status_code == 303
    st = _st(c)
    assert next((b["state"] for b in st["batches"] if b["batch_id"] == T02), "정상") == "정상"
    assert [t["type"] for t in st["tasks"]] == ["보도 지목 배치 사실 확인"]
    h = _q("SELECT reason FROM history WHERE entity_type='batch' AND entity_id=? AND reason LIKE '외부 정보%'", T02)
    assert "회수 기준 미달 — 보도만으로 회수대상이 되지 않음" in h[0]["reason"]


@pytest.mark.parametrize("kw, msg", [({"summary": ""}, "필수"), ({"published": "2026-07-05"}, "현재 업무일"),
                                     ({"batch_ids": "B-99-0000-00"}, "배치")])
def test_news_validation(c, kw, msg):
    r = _news(c, **kw)
    assert r.status_code == 409 and msg in r.text


def test_news_is_traceable_and_in_glossary(c):
    c.post("/assess")
    _news(c)
    audit = {a["kind"]: a for a in _q_audit()}
    assert audit["외부 정보(언론 보도)"]["rows"] == 1 and audit["외부 정보(언론 보도)"]["ok"]
    assert "A-31" in {t["code"] for t in glossary.TERMS}
    assert 'href="/external"' in c.get("/").text


def _q_audit():
    conn = db.connect()
    out = provenance.audit(conn)
    conn.close()
    return out
