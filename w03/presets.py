"""시연용 프리셋. 각 프리셋은 초기화 → 정상 시나리오(두 배치 승인) 기준선 → 해당 조건 변경을 실행한다.
상신 요약은 AI를 호출하지 않는다(규칙 초안을 "시연 프리셋" 이름으로 수동 저장). 회신 프리셋은 키가 있으면 AI를 1회씩 호출한다."""
import json

from . import ai, corrections, db, replies, resupply, workflow

B1, B2 = "B-02-2602-01", "B-02-2603-01"
ACTOR = "시연 프리셋"
EVIDENCE = "재검사 성적서(모의) #RT-0701"
# 3,000EA 미확정으로는 출하 분모가 10.16%로 남아(A-2: 어느 한쪽 ≥10% → 회수대상) 재검토가 되지 않는다.
# 두 분모가 모두 10% 미만이 되는 최소 묶음: 4건 3,296EA → 생산 9.54% / 출하 9.97%.
LARGE_UNCONFIRM = ["QC-00042", "QC-00038", "QC-00029", "QC-00030"]
# 정확히 3,000EA가 되는 확정 3건(883 + 1,041 + 1,076)
EXACT_3000 = ["QC-00032", "QC-00035", "QC-00038"]


def baseline(conn) -> int:
    db.reset(conn)
    workflow.run_assessment(conn)
    sid = workflow.create_submission(conn, use_ai=False)
    draft = ai.draft_for_manual(json.loads(conn.execute("SELECT facts_json FROM submission WHERE id=?",
                                                        (sid,)).fetchone()[0]))
    workflow.save_manual_summary(conn, sid, f"품질팀({ACTOR})", draft["headline"], draft["summary"],
                                 "\n".join(f"{a['dept']}: {a['action']}" for a in draft["actions"]),
                                 "\n".join(draft["open_questions"]))
    for b in (B1, B2):
        workflow.decide_batch(conn, sid, b, True, f"품질위원회 위원장({ACTOR})", "확정 불량률 기준 충족 — 회수 의결")
    return sid


def small_correction(conn) -> str:
    """B-02-2602-01 소량 정정(QC-00006 수량 1,100 → 600) → 17,400EA, 11.54% / 11.93% → 판정 유지."""
    baseline(conn)
    corrections.apply_correction(conn, "QC-00006", None, 600, f"품질팀({ACTOR})",
                                 "재검사 결과 영향 수량 정정(1,100 → 600)", EVIDENCE + "-01")
    return f"/batches/{B1}"


def large_correction(conn) -> str:
    """B-02-2603-01 대량 정정(확정 4건 3,296EA → 미확정) → 두 분모 모두 10% 미만 → 재검토 → 재상신."""
    baseline(conn)
    for cid in LARGE_UNCONFIRM:
        corrections.apply_correction(conn, cid, "False", None, f"품질팀({ACTOR})",
                                     "재검사 결과 규격이탈 아님 — 확정 취소", EVIDENCE + "-02")
    sid = workflow.create_resubmission(conn)
    return f"/submissions/{sid}"


def exact_3000(conn) -> str:
    """B-02-2603-01 확정 3건 정확히 3,000EA → 미확정: 15,622EA → 생산 9.72% / 출하 10.16% → A-2(어느 한쪽 ≥10%)로 판정 유지."""
    baseline(conn)
    for cid in EXACT_3000:
        corrections.apply_correction(conn, cid, "False", None, f"품질팀({ACTOR})",
                                     "재검사 결과 규격이탈 아님 — 확정 취소(3,000EA)", EVIDENCE + "-03")
    return f"/batches/{B2}"


def deferral_reject(conn) -> str:
    """(b) 회신 마감(회신 없음) → (a) 영업 승인 → 연기 1순위 고객이 거절 → 그 고객 주문 제외 후 다음 순번 재선정."""
    sid = baseline(conn)
    resupply.close_b(conn, sid, f"영업({ACTOR})")
    task = conn.execute("SELECT id FROM task WHERE submission_id=? AND type='영업 승인'", (sid,)).fetchone()
    resupply.reply_approval(conn, task["id"], True, f"영업 임원({ACTOR})", "기존 주문 일부 납기 연기 승인")
    first = conn.execute(
        "SELECT t.id FROM task t WHERE t.submission_id=? AND t.type='납기 연기 동의 요청' ORDER BY t.id LIMIT 1",
        (sid,)).fetchone()
    resupply.reply_customer(conn, first["id"], False, f"고객 구매담당({ACTOR})", "판촉 일정상 납기 연기 불가")
    return f"/plans/{sid}"


def lot_inspection(conn) -> str:
    """같은 로트 배치 추가검사: B-02-2602-03 합격(10,483EA) → 출하 가능 / B-02-2602-02 불합격(불량 900/3,213EA)
    → 확정 클레임 등록 → R1 재판정(900 ÷ 154,685 = 0.58% → 기준 미달, 정상 유지) · 재고 격리 유지·폐기 검토 ·
    표본 불량률 경보 28.01% → 품질위원회 보고 · 기출하 고객 모니터링 강화. 검사 수치는 프리셋 모의 데이터."""
    baseline(conn)
    tasks = {r["related_id"]: r["id"] for r in conn.execute("SELECT id, related_id FROM task WHERE type='추가검사'")}
    workflow.record_inspection(conn, tasks["INV-B-02-2602-03"], True, 10483, None, f"품질 검사원({ACTOR})")
    workflow.record_inspection(conn, tasks["INV-B-02-2602-02"], False, 3213, 900, f"품질 검사원({ACTOR})")
    return "/lots/LOT-B-2602-P"


REPLY_OK = "C-101 뷰티메가리테일입니다. B-02-2602-01 배치 재고 12,000개 보유 중이고 회수 동의합니다. 다음 주 화요일 수거 가능합니다."
REPLY_VAGUE = "C-102입니다. 해당 배치 제품이 일부 남아 있는 것 같은데 수량은 확인 후 다시 알려드리겠습니다. 회수에는 협조하겠습니다."


def _notice_task(conn, customer):
    return conn.execute("SELECT id FROM task WHERE type='고객 통지' AND related_id=?", (customer,)).fetchone()[0]


def reply_ok(conn) -> str:
    """정상 회신 → AI 구조화 → 검증 통과 → 반영(보유량 → 필요량·R8 재계산).
    AI 구조화가 실패하면(키 없음·503·429 …) 사람이 구조화 폼을 채우는 경로로 같은 값을 넣고, 작성자 라벨에 실패 사유를 남긴다."""
    baseline(conn)
    iid = replies.intake(conn, _notice_task(conn, "C-101"), REPLY_OK, f"C-101 구매담당({ACTOR})")
    row = conn.execute("SELECT source, ai_info FROM reply_intake WHERE id=?", (iid,)).fetchone()
    if row["source"] is None:
        replies.manual_structure(conn, iid, {"customer_id": "C-101", "batch_id": B1, "held_qty": "12000",
                                             "recall_agree": "true", "pickup_date": "다음 주 화요일"},
                                 f"영업 담당({ACTOR}, {ai.failure_label(json.loads(row['ai_info']))})")
    replies.apply(conn, iid, f"영업 담당({ACTOR})")
    return f"/intakes/{iid}"


def reply_vague(conn) -> str:
    """모호한 회신(수량 없음) → 구조화 → 검증 실패(필수 항목 누락) → 사람 확인 필요."""
    baseline(conn)
    iid = replies.intake(conn, _notice_task(conn, "C-102"), REPLY_VAGUE, f"C-102 구매담당({ACTOR})")
    row = conn.execute("SELECT source, ai_info FROM reply_intake WHERE id=?", (iid,)).fetchone()
    if row["source"] is None:
        replies.manual_structure(conn, iid, {"customer_id": "C-102", "recall_agree": "true"},
                                 f"영업 담당({ACTOR}, {ai.failure_label(json.loads(row['ai_info']))} — 원문 그대로 옮김)")
    return f"/intakes/{iid}"


PRESETS = {
    "small": ("B-02-2602-01 소량 정정 → 판정 유지", small_correction),
    "exact3000": ("B-02-2603-01 3,000EA 정정 → 생산 9.72% / 출하 10.16% → A-2 규칙으로 판정 유지", exact_3000),
    "large": ("B-02-2603-01 대량 정정(3,296EA 확정 → 미확정) → 두 분모 10% 미만, 재검토", large_correction),
    "deferral": ("(b) 마감 → (a) 영업 승인 → 고객 1곳 연기 거절 → 다음 순번 재선정", deferral_reject),
    "lot": ("같은 로트 배치 추가검사(모의 수치) → 1건 합격, 1건 불합격 → 재판정·표본 경보", lot_inspection),
    "reply_ok": ("정상 회신 → AI 구조화 → 반영", reply_ok),
    "reply_vague": ("모호한 회신(수량 없음) → 검증 실패 → 사람 확인", reply_vague),
}
