"""개선 효과 비교 — 모두 추정치다. 비교 기준과 가정을 함께 돌려준다(새 판정 로직 없음: 기존 R8·로트 비교 재사용).

(a) 과잉 대응: 현실적인 대안(같은 원료 로트 전부 · 언론 보도에 반응한 올해 생산분 전부) vs R1 회수대상 배치,
    전 기간 전면 회수는 상한(참고) (가정 A-30)
(b) 대응 지연: BPMN 대비 바뀐 구조와 지금 데이터에서 확인되는 지표(소요 시간 단축은 추정하지 않음)
(c) 로트 역추적 확장 있음/없음 (lots.compare, 의결 전이면 A-5 추정 재고로 같은 방식 비교)
"""
from . import ai, bizcal, decisions, lots, rules
from .config import AS_OF, RESUPPLY_LINE
from .db import current_business_date

RECALL_STATES = ("회수대상", "회수의결", "회수완료")


def _scope(conn, batch_ids: list[str]) -> dict:
    m = ",".join("?" * len(batch_ids))
    sh = conn.execute(f"SELECT COUNT(DISTINCT customer_id) n, COALESCE(SUM(shipped_qty), 0) q FROM shipment_state"
                      f" WHERE batch_id IN ({m}) AND source='SBL'", batch_ids).fetchone()
    produced = conn.execute(f"SELECT COALESCE(SUM(CAST(produced_qty AS INTEGER)), 0) FROM src_batches WHERE batch_id IN ({m})",
                            batch_ids).fetchone()[0]
    r8 = rules.resupply_check(conn, batch_ids)
    p = r8["production"]
    return {"batches": len(batch_ids), "customers": sh["n"], "shipped": sh["q"], "stock": produced - sh["q"],
            "need": p["upper_qty"], "spare": p["spare_qty"],
            "ratio": round(p["upper_qty"] / p["spare_qty"], 2) if p["spare_qty"] else None, "overall": r8["overall"]}


def target_batches(conn) -> list[str]:
    ids = [r[0] for r in conn.execute(
        f"SELECT batch_id FROM batch_state WHERE state IN ({','.join('?' * len(RECALL_STATES))}) ORDER BY batch_id",
        RECALL_STATES)]
    return ids or [r[0] for r in conn.execute("SELECT batch_id FROM assessment WHERE is_target=1 ORDER BY batch_id")]


def overreaction(conn) -> dict | None:
    targets = target_batches(conn)
    if not targets:
        return None
    product = conn.execute("SELECT product_id FROM src_batches WHERE batch_id=?", (targets[0],)).fetchone()[0]
    lot_ids = sorted({r[0] for r in conn.execute(
        f"SELECT input_lot_ids FROM src_batches WHERE batch_id IN ({','.join('?' * len(targets))})", targets)})
    same_lot = [r[0] for r in conn.execute(
        f"SELECT batch_id FROM src_batches WHERE input_lot_ids IN ({','.join('?' * len(lot_ids))}) ORDER BY batch_id", lot_ids)]
    year = AS_OF[:4]
    recent = [r[0] for r in conn.execute("SELECT batch_id FROM src_batches WHERE product_id=? AND production_date LIKE ?"
                                         " ORDER BY batch_id", (product, f"{year}-%"))]
    all_ids = [r[0] for r in conn.execute("SELECT batch_id FROM src_batches WHERE product_id=? ORDER BY batch_id", (product,))]
    span = conn.execute("SELECT MIN(production_date), MAX(production_date) FROM src_batches WHERE product_id=?",
                        (product,)).fetchone()
    actual = _scope(conn, targets)
    rows = [
        {"label": "대표 비교 1 — 같은 원료 로트 전부 회수",
         "basis": f"{', '.join(lot_ids)}을(를) 쓴 배치 전부({', '.join(same_lot)}) — 원인 로트를 의심해 확정 불량 없는 배치까지 회수한 경우",
         **_scope(conn, same_lot)},
        {"label": f"대표 비교 2 — {year}년 {product} 생산분 전부 회수",
         "basis": f"{year}년 생산 {product} 배치 전부 — 언론 보도(제품·배치 지목 없음)에 반응해 최근 생산분을 모두 회수한 경우",
         **_scope(conn, recent)},
        {"label": "실제 회수대상(R1)", "basis": f"확정 불량률 10% 이상 배치: {', '.join(targets)} — SBL 기준 · 확인 회신분 제외",
         **actual},
    ]
    upper = [{"label": f"{product} 전 기간 전면 회수(상한·참고)",
              "basis": f"{span[0]} ~ {span[1]} 생산 {product} 전 배치 — 실제 담당자가 고를 선택지가 아닌 상한",
              **_scope(conn, all_ids)}]
    for r in rows + upper:
        r["times"] = round(r["shipped"] / actual["shipped"], 1) if actual["shipped"] else None
    return {"product": product, "rows": rows, "upper": upper, "actual": actual, "line": RESUPPLY_LINE,
            "news": "ext_news_quality_incident: 단일 매체·미확정 표현, 특정 제품·배치 지목 없음 — 회수 근거 아님(R5)"}


def delay(conn) -> list[dict]:
    """BPMN 대비 바뀐 구조. '현재 데이터' 열은 지금 DB에서 센 값이고, 시간 단축량은 추정하지 않는다."""
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    today = current_business_date(conn)
    # AI 요약·공급 미리보기는 회수상신에만 있다(재상신은 규칙 요약) — 가장 최근 회수상신 기준
    recall_sub = conn.execute("SELECT id, status, ai_status, ai_source FROM submission WHERE kind='회수상신' ORDER BY id DESC").fetchone()
    by = {"manual": "사람 작성", "rule": "규칙"}
    tasks = q("SELECT COUNT(*) FROM task")
    first_decision_tasks = q("SELECT COUNT(*) FROM task WHERE submission_id IS NOT NULL")
    holdings = q("SELECT COUNT(*) FROM customer_holding")
    intake_claims = q("SELECT COUNT(*) FROM claim_addition WHERE source='클레임 접수'")
    delayed = q("SELECT COUNT(*) FROM task WHERE delayed_at IS NOT NULL AND state IN ('대기','진행')")
    queue = decisions.build(conn)
    return [
        {"step": "클레임 접수", "bpmn": "접수 후 다음 판정 때 반영(정기 취합)",
         "system": "접수 즉시 해당 배치 두 분모 불량률 재계산·판정 갱신, 새 회수대상은 상신 대기에 자동 추가",
         "now": f"앱 접수 클레임 {intake_claims}건",
         "basis": "자동(규칙 R1)"},
        {"step": "배치·검사자료 취합 · 회수 안건 작성", "bpmn": "담당자가 클레임·생산·출하 자료를 손으로 대조해 안건 작성",
         "system": "규칙 엔진이 두 분모 불량률·영향 고객·같은 로트·재공급 판정을 계산, AI가 그 값만으로 요약(수치 대조, 실패 시 수동)",
         "now": (f"회수상신 #{recall_sub['id']} 요약 {recall_sub['ai_status']}"
                 + (f"({by.get(recall_sub['ai_source']) or ai.label(recall_sub['ai_source'])})" if recall_sub["ai_source"] else ""))
         if recall_sub else "상신 없음",
         "basis": "자동(규칙) + AI(요약)"},
        {"step": "위원회 의결 전 공급 영향", "bpmn": "의결 후 '공급 영향·일정 부서별 확인' 단계에서 처음 확인",
         "system": "상신 화면 의결 버튼 위에 '승인 시 예상 공급 영향'(R8: 필요량 상한·L-02 여유·판정, 예상치)",
         "now": (f"회수상신 #{recall_sub['id']} 화면에서 확인 가능" if recall_sub and recall_sub["status"] == "상신"
                 else f"회수상신 #{recall_sub['id']} 의결 완료 — 다음 회수상신 때 표시" if recall_sub else "상신 후 표시"),
         "basis": "자동(R8 재사용)"},
        {"step": "의결 범위 담당자 전달", "bpmn": "의결 결과를 부서별로 전달(수작업)",
         "system": "배치별 승인 즉시 고객 통지·사내재고 격리·재공급·원인 조사 Task 생성, 처리 목표일(A-28) 부여",
         "now": f"Task {tasks}건(의결로 생성 {first_decision_tasks}건)", "basis": "자동"},
        {"step": "공급 영향·고객 회신", "bpmn": "회신은 '기록'으로 끝남",
         "system": "보유량·격리 실측·검사·조달 회신이 들어오면 재공급 필요량·R8·대안 계획을 바로 다시 계산",
         "now": f"보유량 회신 반영 {holdings}건", "basis": "자동(회신 → R8)"},
        {"step": "처리 기한 · 지연", "bpmn": "기한 개념 없음",
         "system": "작업별 처리 목표일(A-28, A05 영업일)·위원회 의결은 상신 후 1영업일(A-32), 업무일을 넘기면 기한 지난 작업·의결을 '지연'으로 표시해 결정 대기 맨 위로",
         "now": f"업무일 {bizcal.label(today)} · 결정 대기 {len(queue)}건 · 지연 {delayed}건",
         "basis": "가정 A-28·A-29"},
    ]


def lot_effect(conn) -> dict | None:
    """확장 있음/없음. 의결 후에는 기존 로트 비교(lots.compare), 의결 전이면 A-5 추정 재고로 같은 기준 예상."""
    targets = target_batches(conn)
    if not targets:
        return None
    lot_id = conn.execute("SELECT input_lot_ids FROM src_batches WHERE batch_id=?", (targets[0],)).fetchone()[0]
    comp = lots.compare(conn, lot_id)
    if comp and comp.get("rows"):
        return {"lot": lot_id, "mode": "의결 후 실제 상태(로트 역추적 화면과 같은 값)", "rows": comp["rows"]}
    m = ",".join("?" * len(targets))
    same = conn.execute(
        f"""SELECT b.batch_id, CAST(b.produced_qty AS INTEGER) - COALESCE(SUM(s.shipped_qty), 0) est
            FROM src_batches b JOIN batch_state st USING (batch_id) LEFT JOIN shipment_state s USING (batch_id)
            WHERE b.input_lot_ids=? AND b.batch_id NOT IN ({m}) AND st.state='정상' GROUP BY b.batch_id""",
        (lot_id, *targets)).fetchall()
    stock = sum(r["est"] for r in same)
    p = rules.resupply_check(conn, targets)["production"]
    upper, spare = p["upper_qty"], p["spare_qty"]
    verdict = lambda need: "가능" if spare and need / spare <= 1 else "대안필요"
    rows = [
        {"label": "같은 로트 재고 출하 가능 수량", "without": stock, "with": 0,
         "note": f"같은 로트 정상 배치 {', '.join(r['batch_id'] for r in same)} 사내 재고 추정(A-5) — 확장: 추가검사 전 출하 보류"},
        {"label": "재공급 필요량", "without": upper - stock, "with": upper, "note": "확장 없음: 같은 로트 재고를 재공급에 씀(A-26)"},
        {"label": "필요 ÷ 여유", "without": round((upper - stock) / spare, 2), "with": round(upper / spare, 2), "note": "> 1 → 대안필요"},
        {"label": "R8 생산 판정", "without": verdict(upper - stock), "with": verdict(upper), "note": ""},
    ]
    return {"lot": lot_id, "mode": "의결 전 예상(같은 기준, A-5 추정 재고) — 의결 후에는 실제 상태로 바뀜", "rows": rows}


def build(conn) -> dict:
    return {"overreaction": overreaction(conn), "delay": delay(conn), "lot": lot_effect(conn),
            "business_date": current_business_date(conn), "as_of": AS_OF}
