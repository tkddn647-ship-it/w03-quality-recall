"""대시보드 '결정 대기'(급한 순서·이유·바로가기) · 용어집/툴팁(출처 성격) · 시연 프리셋 접힘."""
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
from w03 import db, glossary, presets, resupply  # noqa: E402

B1, B2 = presets.B1, presets.B2


@pytest.fixture
def c():
    with TestClient(main.app) as client:
        assert client.post("/admin/reset", data={"confirm_text": "초기화"}, follow_redirects=False).status_code == 303
        yield client


def _queue(c):
    return c.get("/api/state").json()["decision_queue"]


def _run(fn):
    conn = db.connect()
    out = fn(conn)
    conn.close()
    return out


# ---------- 결정 대기 ----------

def test_empty_queue_before_assessment(c):
    assert _queue(c) == []
    assert "지금 사람이 결정할 일은 없습니다" in c.get("/").text


def test_queue_is_first_on_dashboard_and_starts_with_submission(c):
    c.post("/assess")
    [item] = _queue(c)
    assert (item["rank"], item["kind"], item["href"]) == (1, "회수대상 상신", "/#submit")
    assert "B-02-2602-01 11.87%/12.27%" in item["why"] and "299,653EA" in item["why"]
    page = c.get("/").text
    assert page.index('id="decide-queue"') < page.index("<h2>0. 클레임 접수</h2>") < page.index("<h2>1. 판정</h2>")
    assert "왜 지금:" in page and "회수대상 상신 2건" in page


def test_queue_blocks_on_summary_then_committee_decision(c):
    c.post("/assess")
    c.post("/submissions")                                            # 키 없음 → 요약 실패
    [item] = _queue(c)
    assert item["kind"] == "상신 요약 작성" and "API 키 없음" in item["why"] and item["href"] == "/submissions/1"
    c.post("/submissions/1/summary", data={"author": "품질팀", "headline": "h", "summary": "s", "actions": "-", "questions": "-"})
    [item] = _queue(c)
    assert (item["kind"], item["owner"], item["href"]) == ("위원회 의결", "품질위원회", "/submissions/1#decide")
    assert "회수요청·사내재고 출하보류가 시작되지 않음" in item["why"] and "299,653EA" in item["why"]


def test_queue_after_decision_orders_deadlines_then_plan(c):
    _run(presets.baseline)
    q = _queue(c)
    assert [i["kind"] for i in q] == ["기한 임박 작업", "기한 임박 작업", "기한 임박 작업", "재공급 대안 선택"]
    assert q[0]["title"].startswith("[물류] 사내재고 격리") and "처리 목표 오늘 2026-07-01(수)(A-28)" in q[0]["why"]
    assert q[1]["title"].startswith("[영업] 고객 통지") and "처리 목표 다음 영업일 2026-07-02(목)" in q[1]["why"]
    assert "추정 납기 이미 지난 주문 24건" in q[2]["why"] and "2026-07-03(금)" in q[2]["why"]
    assert "보유량 회신 0/9" in q[3]["why"] and q[3]["href"] == "/plans/1"
    page = c.get("/").text
    for i in q[:3]:
        assert f'id="{i["href"].split("#")[1]}"' in page                # 바로가기 앵커가 실제 Task 행
    assert [i["no"] for i in q] == [1, 2, 3, 4]
    # 원인 조사(D+14)처럼 급하지 않은 Task는 결정 대기에 넣지 않는다
    assert not any("원인 조사" in i["title"] for i in q)


def test_queue_shows_reply_needing_human(c):
    _run(presets.reply_vague)
    item = next(i for i in _queue(c) if i["kind"] == "회신 확인")
    assert item["rank"] == 3 and item["href"] == "/intakes/1"
    assert "배치 특정 불가" in item["why"] and "상한값(A-8)" in item["why"]


def test_queue_shows_capex_choice(c):
    sid = _run(presets.baseline)

    def to_c(conn):
        resupply.close_b(conn, sid, "영업")
        task = conn.execute("SELECT id FROM task WHERE submission_id=? AND type='영업 승인'", (sid,)).fetchone()
        resupply.reply_approval(conn, task["id"], False, "영업 임원", "연기 불가")
        return conn.execute("SELECT state FROM resupply_plan WHERE submission_id=?", (sid,)).fetchone()[0]

    assert _run(to_c) == "(c) 선택대기"
    item = next(i for i in _queue(c) if i["kind"] == "재공급 대안 선택")
    assert item["title"] == f"재공급 계획 #{sid} 증설안 선택" and "EA 부족" in item["why"]


# ---------- 용어집 · 툴팁 ----------

def test_glossary_covers_every_rule_and_assumption_in_design():
    design = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
    used = set(re.findall(r"(?<![A-Za-z0-9-])(R\d{1,2}|A-\d{1,2}|A05)(?![0-9])", design))
    codes = {t["code"] for t in glossary.TERMS}
    assert used and used <= codes, sorted(used - codes)


def test_glossary_page_with_natures_and_department_claims(c):
    page = c.get("/glossary").text
    for n in glossary.NATURES:
        assert n in page
    for code in ["R1", "R8", "R10", "A-5", "A-8", "A-28", "OPT-01 비고", "OPT-02 비고", "RFP 라인 제약", "시연 프리셋"]:
        assert f'id="{code.replace(" ", "_")}"' in page
    nature = {t["code"]: t["nature"] for t in glossary.TERMS}
    assert nature["OPT-02 비고"] == nature["RFP 라인 제약"] == glossary.CLAIM
    assert nature["A-18"] == glossary.ASSUME                                     # 판단은 가정, 근거(OPT-02 비고)는 부서 주장
    assert nature["R1"] == glossary.FACT and nature["A-5"] == glossary.ASSUME and nature["모의 의결"] == glossary.MOCK
    claims = c.get("/glossary?nature=부서 주장").text
    assert "OPT-02 비고" in claims and 'id="R1"' not in claims


def test_tooltips_embedded_for_codes_only(c):
    page = c.get("/").text
    tips = re.search(r"const TIPS = (\{.*?\});", page).group(1)
    assert '"R1"' in tips and '"A-5"' in tips and '"A-28"' in tips and '"SBL"' in tips
    assert "OPT-01" not in tips and "모의 의결" not in tips                 # 공백·한글 용어는 자동 툴팁 대상 아님
    assert '<a href="/glossary">용어집</a>' in page


# ---------- 시연 프리셋 ----------

def test_presets_folded_at_bottom(c):
    page = c.get("/").text
    details = page.index('<details class="card" id="presets"')
    assert details > page.index('id="tasks"') and details > page.index("최근 처리 이력")
    assert page.count('action="/presets/') == len(presets.PRESETS)
    assert page[details:].count('action="/presets/') == len(presets.PRESETS)
