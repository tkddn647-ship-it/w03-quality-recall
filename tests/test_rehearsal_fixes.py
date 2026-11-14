"""녹화 전 전체 점검에서 고친 표시·문구: 입력 오류 화면 · 이력 대상 한글 · 확정 여부 한글 · 상신 이후 수치 변경 안내 ·
(b) 축소 내역 · 연기 거절 표시 · 개선 효과 상신 행."""
import os
import re
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
from w03 import db, presets, workflow  # noqa: E402

B1, B2 = presets.B1, presets.B2
HTML = {"accept": "text/html,application/xhtml+xml"}


@pytest.fixture
def c():
    with TestClient(main.app) as client:
        assert client.post("/admin/reset", data={"confirm_text": "초기화"}, follow_redirects=False).status_code == 303
        yield client


def _run(fn):
    conn = db.connect()
    try:
        return fn(conn)
    finally:
        conn.close()


def _visible(html: str) -> str:
    body = html.split("<main", 1)[1].split("</main>", 1)[0]
    body = re.sub(r"<details>.*?</details>", " ", body, flags=re.S)     # 접힌 시스템 기록 원문(JSON)은 제외
    body = re.sub(r"<script.*?</script>|<(textarea|select)[^>]*>.*?</\1>", " ", body, flags=re.S)
    return re.sub(r"<[^>]+>", " ", body)


def _st(c):
    return c.get("/api/state").json()


def test_form_error_is_an_html_page_for_browsers_and_json_for_api(c):
    data = {"customer_id": "C-999", "product_id": "PL-02", "batch_id": B1, "qty": "10", "confirmed": "True",
            "claim_type": "규격", "actor": "품질팀", "occurred_date": "2026-06-30"}
    r = c.post("/claims", data=data, headers=HTML)
    assert r.status_code == 409 and "처리할 수 없습니다" in r.text and "고객 C-999 없음" in r.text
    assert "m_customer_cv" not in _visible(r.text) and "고객 원본" in r.text and '"detail"' not in r.text
    assert c.post("/claims", data=data).json()["detail"].startswith("고객 C-999 없음")    # API·테스트는 JSON 그대로


def test_history_target_and_booleans_shown_in_korean(c):
    _run(presets.large_correction)
    for path in ("/", "/history", f"/batches/{B2}", f"/trace/{B2}", "/submissions/2", "/claims"):
        text = _visible(c.get(path).text)
        for word in ("True", "False", "batch_flag", "submission_summary", "qc_claims_cv", "shipment_batch_link_cv",
                     "input_lot_ids", "claim_addition"):
            assert not re.search(rf"(?<![A-Za-z_]){word}(?![A-Za-z_])", text), (path, word)
    page = c.get(f"/trace/{B2}").text
    assert "확정 여부(정정 반영)" in page and "원본 처리 상태" in page and "QC 클레임 원본의 배치 번호" in page
    assert 'title="원래 이름: batch_flag">조사대상 표시' in c.get("/history").text
    assert "QC-00042 확정 → 미확정" in c.get("/submissions/2").text


def test_decided_submission_warns_when_numbers_changed_later(c):
    _run(presets.baseline)                                           # 의결 완료된 상신은 상신 시점 값 그대로 + 안내
    assert "상신 이후 수치가 바뀌었습니다" not in c.get("/submissions/1").text
    c.post("/claims", data={"customer_id": "C-101", "product_id": "PL-02", "batch_id": B1, "qty": "500",
                            "confirmed": "True", "claim_type": "규격", "actor": "품질팀", "occurred_date": "2026-06-30"})
    page = c.get("/submissions/1").text
    assert "상신 이후 수치가 바뀌었습니다" in page and "17,900 → <b>18,400EA</b>" in page and "12.21% / 12.61%" in page


def test_shipment_without_date_and_plan_reduction_breakdown(c):
    sid = _run(presets.baseline)
    st = _st(c)
    route = next(t["id"] for t in st["tasks"] if t["type"] == "출하 경로 확인 요청" and B1 in t["title"])
    c.post(f"/tasks/{route}/shipment-route", data={"decision": "shipped", "qty": "3000", "actor": "영업"})
    assert "- (확인 회신)" in c.get(f"/batches/{B1}").text and "None" not in _visible(c.get(f"/batches/{B1}").text)
    insp = next(t["id"] for t in st["tasks"] if t["type"] == "추가검사" and "B-02-2602-03" in t["title"])
    c.post(f"/tasks/{insp}/inspection", data={"result": "pass", "inspected_qty": "10483", "actor": "검사원"})
    page = c.get(f"/plans/{sid}").text
    assert "(보유량 회신 0 + 추가검사 합격 재고 10,483, A-22)" in page
    assert "생산능력 원본" in page and "strat_capacity_cv" not in _visible(page) and "snapshot" not in page


def test_rejected_deferral_title_is_marked(c):
    _run(presets.deferral_reject)
    titles = [t["title"] for t in _st(c)["tasks"] if t["type"] == "납기 연기 동의 요청"]
    assert sum("연기순번 1 " in t for t in titles) == 2
    assert any(t.endswith("→ 거절(후보 제외, 이후 순번은 재선정 기준)") for t in titles)


def test_effects_rows_point_at_recall_submission(c):
    _run(presets.large_correction)                                   # 재상신 #2가 열려 있어도 AI 행은 회수상신 #1
    rows = {r["step"]: r["now"] for r in _run(lambda conn: __import__("w03.effects", fromlist=["x"]).delay(conn))}
    assert rows["배치·검사자료 취합 · 회수 안건 작성"] == "회수상신 #1 요약 수동 작성 완료(사람 작성)"
    assert rows["위원회 의결 전 공급 영향"] == "회수상신 #1 의결 완료 — 다음 회수상신 때 표시"
    text = _visible(c.get("/effects").text)
    assert "(rule)" not in text and "ext_news_quality_incident" not in text and "claim_correction" not in text
