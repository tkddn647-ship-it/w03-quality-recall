"""개선 효과 비교(추정치·기준·가정) · 추가 데이터 추적성(식별자·적용 시점·원본 연결·생성 이유)."""
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
from w03 import db, effects, glossary, presets, provenance  # noqa: E402

T02 = "B-02-2602-02"


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


def _q(sql, *args):
    conn = db.connect()
    rows = [dict(r) for r in conn.execute(sql, args)]
    conn.close()
    return rows


# ---------- (a)(b)(c) ----------

def test_effects_before_assessment(c):
    page = c.get("/effects").text
    assert page.count("아직 판정 전") == 2 and "모두 추정치입니다" in page


def test_overreaction_uses_realistic_alternatives_and_full_recall_as_upper(c):
    c.post("/assess")
    o = _run(effects.overreaction)
    lot, recent, actual = o["rows"]
    [upper] = o["upper"]
    assert lot["label"].startswith("대표 비교 1") and (lot["batches"], lot["shipped"], lot["times"]) == (4, 602234, 2.0)
    assert "LOT-B-2602-P" in lot["basis"] and all(b in lot["basis"] for b in ("B-02-2602-02", "B-02-2602-03"))
    assert recent["label"].startswith("대표 비교 2") and (recent["batches"], recent["shipped"], recent["times"]) == (18, 2731634, 9.1)
    assert (actual["batches"], actual["customers"], actual["shipped"], actual["ratio"]) == (2, 5, 299653, 5.12)
    assert (upper["batches"], upper["shipped"], upper["times"]) == (90, 13709523, 45.8) and "상한" in upper["label"]
    page = c.get("/effects").text
    main_table = page.split("<summary>상한(참고)")[0]
    assert "602,234" in main_table and "2,731,634" in main_table and "13,709,523" not in main_table
    assert "13,709,523" in page.split("<summary>상한(참고)")[1]                  # 상한은 접힌 참고 표에만
    for text in ("가정 A-30", "단가·회수율 근거가 없어 계산하지 않았습니다"):
        assert text in page


def test_delay_lists_structural_changes_without_claimed_savings(c):
    c.post("/assess")
    rows = _run(effects.delay)
    assert [r["step"] for r in rows] == ["클레임 접수", "배치·검사자료 취합 · 회수 안건 작성", "위원회 의결 전 공급 영향",
                                         "의결 범위 담당자 전달", "공급 영향·고객 회신", "처리 기한 · 지연"]
    page = c.get("/effects").text
    assert "추정하지 않습니다" in page and "일 단축" not in page


def test_lot_effect_before_and_after_decision_use_same_basis(c):
    c.post("/assess")
    before = {r["label"]: r for r in _run(effects.lot_effect)["rows"]}
    assert (before["같은 로트 재고 출하 가능 수량"]["without"], before["같은 로트 재고 출하 가능 수량"]["with"]) == (13696, 0)
    assert (before["재공급 필요량"]["without"], before["재공급 필요량"]["with"]) == (285957, 299653)
    _run(presets.lot_inspection)
    after = _run(effects.lot_effect)
    assert after["mode"].startswith("의결 후")
    rows = {r["label"]: r for r in after["rows"]}
    assert rows["재공급 필요량"]["without"] == before["재공급 필요량"]["without"]       # 같은 비교 기준
    assert rows["같은 로트 재고 출하 가능 수량"]["with"] == 10483
    assert 'href="/lots/LOT-B-2602-P"' in c.get("/effects").text


# ---------- 추가 데이터 추적성 ----------

def _assert_audit_ok(kinds_with_rows=()):
    audit = {a["kind"]: a for a in _run(provenance.audit)}
    bad = {k: a["missing"] for k, a in audit.items() if not a["ok"]}
    assert not bad, bad
    for k in kinds_with_rows:
        assert audit[k]["rows"] > 0, k


@pytest.mark.parametrize("preset, kinds", [
    ("small", ["검사 정정", "위원회 의결"]),
    ("large", ["검사 정정"]),
    ("deferral", ["납기 연기 동의 회신"]),
    ("lot", ["추가 클레임", "격리 실측·검사 수량", "표본 불량률 경보", "클레임 모니터링 강화"]),
    ("reply_ok", ["회신 접수(구조화)", "고객 보유량 회신"]),
    ("reply_vague", ["회신 접수(구조화)"]),
])
def test_every_preset_leaves_traceable_data(c, preset, kinds):
    assert c.post(f"/presets/{preset}", data={"confirm_text": "프리셋"}, follow_redirects=False).status_code == 303
    _assert_audit_ok(kinds)


def test_manual_flows_leave_traceable_data(c):
    _run(presets.baseline)
    c.post("/business-day/next", data={"confirm_text": "넘기기"})
    quarantine = _q("SELECT id FROM task WHERE type='사내재고 격리' ORDER BY id")[0]["id"]
    c.post(f"/tasks/{quarantine}/quarantine", data={"qty": "4000", "actor": "물류"})
    route = _q("SELECT id FROM task WHERE type='출하 경로 확인 요청' ORDER BY id")[0]["id"]
    assert c.post(f"/tasks/{route}/shipment-route", data={"decision": "shipped", "qty": "1000", "actor": "영업"},
                  follow_redirects=False).status_code == 303
    assert c.post("/claims", data={"customer_id": "C-101", "product_id": "PL-02", "batch_id": T02, "qty": "100",
                                   "confirmed": "True", "claim_type": "규격", "actor": "품질팀",
                                   "occurred_date": "2026-06-30"}, follow_redirects=False).status_code == 303
    _assert_audit_ok(["격리 실측·검사 수량", "확인 회신 출하", "추가 클레임", "위원회 의결"])
    inv = _q("SELECT * FROM inventory_hold WHERE hold_id IN (SELECT related_id FROM task WHERE id=?)", quarantine)[0]
    assert inv["measured_business_date"] == "2026-07-02" and f"Task #{quarantine}" in inv["measured_source"]
    shp = _q("SELECT * FROM shipment_state WHERE source='확인 회신'")[0]
    assert shp["business_date"] == "2026-07-02" and f"Task #{route}" in shp["origin_ref"] and "A-21" in shp["reason"]
    add = _q("SELECT * FROM claim_addition WHERE source='클레임 접수'")[0]
    assert "화면 클레임 접수" in add["origin_ref"] and add["reason"].startswith("새 클레임 접수")
    page = c.get("/effects").text
    assert "빠짐" not in page and page.count('<span class="pill t-ok">있음</span>') >= 4 * 4


def test_original_sbl_rows_are_not_app_data(c):
    c.post("/assess")
    assert {a["kind"]: a["rows"] for a in _run(provenance.audit)}["확인 회신 출하"] == 0


def test_glossary_has_a30():
    t = {x["code"]: x for x in glossary.TERMS}
    assert "A-30" in t and t["A-30"]["nature"] == glossary.ASSUME
