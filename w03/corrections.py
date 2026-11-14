"""조건 변경 시나리오: 검사 정정 입력 → 불량률 재계산 → 판정 유지 / 재검토 (DESIGN.md §2 R7, §6).

원본 qc_claims_cv는 건드리지 않는다. 정정은 claim_correction에 누적되고 claims_effective 뷰가 최신값을 쓴다(A-9).
"""
import json

from . import workflow
from . import bizcal
from .config import RECALL_THRESHOLD_PCT
from .core import SYSTEM_ACTOR, TransitionError, set_state
from .db import current_business_date, now, record


def _shown(field, value) -> str:
    """정정 값을 화면·이력용 한글로: 확정 여부 True/False → 확정/미확정, 수량은 천 단위 쉼표."""
    if field == "defect_confirmed":
        return {"True": "확정", "False": "미확정"}.get(str(value), str(value))
    try:
        return f"수량 {int(float(value)):,}"
    except (TypeError, ValueError):
        return f"수량 {value}"


def apply_correction(conn, claim_id: str, new_confirmed: str | None, new_qty: int | None,
                     corrector: str, reason: str, evidence: str) -> dict:
    corrector, reason, evidence = corrector.strip(), reason.strip(), evidence.strip()
    if not corrector or not reason or not evidence:
        raise TransitionError("정정자·사유·근거는 필수")
    cur = conn.execute("SELECT * FROM claims_effective WHERE claim_id = ?", (claim_id,)).fetchone()
    if cur is None:
        raise TransitionError(f"클레임 {claim_id} 없음")
    changes = []
    if new_confirmed is not None:
        if new_confirmed not in ("True", "False"):
            raise TransitionError("확정 여부는 True/False")
        if new_confirmed != cur["defect_confirmed"]:
            changes.append(("defect_confirmed", cur["src_defect_confirmed"], cur["defect_confirmed"], new_confirmed))
    if new_qty is not None:
        if new_qty < 0:
            raise TransitionError("수량은 0 이상")
        if str(new_qty) != cur["qty_affected"]:
            changes.append(("qty_affected", cur["src_qty_affected"], cur["qty_affected"], str(new_qty)))
    if not changes:
        raise TransitionError("현재 유효값과 다른 정정값이 없음")
    for field, original, previous, corrected in changes:
        conn.execute(
            "INSERT INTO claim_correction (claim_id, batch_id, field, original_value, previous_value, corrected_value,"
            " corrector, reason, evidence, created_at, business_date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (claim_id, cur["batch_id"], field, original, previous, corrected, corrector, reason, evidence, now(),
             current_business_date(conn)))
    before = " · ".join(_shown(f, p) for f, _, p, _ in changes)
    after = " · ".join(_shown(f, c) for f, _, _, c in changes)
    record(conn, "claim", claim_id, before, after, "사람", corrector, f"검사 정정: {reason}",
           {"batch_id": cur["batch_id"], "evidence": evidence,
            "changes": [{"field": f, "original": o, "previous": p, "corrected": c} for f, o, p, c in changes]})
    outcome = revalidate(conn, cur["batch_id"], f"{claim_id} 정정({after})")
    conn.commit()
    return outcome


def revalidate(conn, batch_id: str, cause: str) -> dict:
    """정정 후 자동 재검증. 기준 이상이면 판정 유지(근거 기록), 미달이면 재검토 → 재상신 대상."""
    results = workflow.refresh_assessment(conn)
    r = results[batch_id]
    ev = {**workflow.evidence(r), "cause": cause}
    state = conn.execute("SELECT state FROM batch_state WHERE batch_id=?", (batch_id,)).fetchone()["state"]
    nums = f"생산 {_pct(r['rate_prod'])} / 출하 {_pct(r['rate_ship'])} (확정 {r['confirmed_qty']:,}EA)"
    outcome = {"batch_id": batch_id, "before": state, "rate_prod": r["rate_prod"], "rate_ship": r["rate_ship"],
               "confirmed_qty": r["confirmed_qty"], "is_target": bool(r["is_target"])}

    if r["is_target"]:
        if state in ("회수대상", "회수의결"):
            record(conn, "batch", batch_id, state, state, *SYSTEM_ACTOR,
                   f"판정 유지 — {cause} → 재계산 {nums}, 기준 {RECALL_THRESHOLD_PCT:g}% 이상 [유지 근거]", ev)
            outcome["result"] = "판정 유지"
        elif state == "정상":
            set_state(conn, "batch", batch_id, "회수대상", *SYSTEM_ACTOR, f"{cause} → 기준 충족 — {nums} (R1)", ev)
            outcome["result"] = "회수대상 전환"
        else:
            record(conn, "batch", batch_id, state, state, *SYSTEM_ACTOR,
                   f"{cause} → 재계산 {nums} — 기준 이상, 현재 {state} 절차에서 처리", ev)
            outcome["result"] = f"{state} 유지(기준 이상)"
    elif state in ("회수대상", "회수의결"):
        conn.execute("UPDATE batch_state SET review_from=? WHERE batch_id=?", (state, batch_id))
        set_state(conn, "batch", batch_id, "재검토", *SYSTEM_ACTOR,
                  f"정정 후 기준 미달 — {nums} < {RECALL_THRESHOLD_PCT:g}% (R7) → 위원회 재상신 필요", ev)
        if state == "회수의결":
            workflow.hold_batch_followups(conn, batch_id, f"{batch_id} 재검토 대기 — {cause}")
        else:
            _move_out_of_open_submission(conn, batch_id, cause)
        outcome["result"] = "재검토"
    else:
        record(conn, "batch", batch_id, state, state, *SYSTEM_ACTOR, f"{cause} → 재계산 {nums} — 기준 미달(R1 비해당), 상태 변경 없음", ev)
        outcome["result"] = "변경 없음"
    outcome["after"] = conn.execute("SELECT state FROM batch_state WHERE batch_id=?", (batch_id,)).fetchone()["state"]
    outcome["refreshed_submissions"] = workflow.refresh_open_submissions(conn, batch_id, cause)
    return outcome


CLAIM_INTAKE = "클레임 접수"


def claim_types(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT claim_type FROM src_claims ORDER BY claim_type")]


def register_claim(conn, customer_id: str, product_id: str, batch_id: str, qty, confirmed, claim_type: str,
                   actor: str, occurred_date: str = "", applied_date: str = "") -> dict:
    """새 클레임 접수(BPMN '클레임 접수'): 원본은 그대로 두고 claim_addition에 저장 → 배치 불량률 즉시 재계산 →
    판정 갱신(회수대상 신규 전환/유지)·이력. 새 회수대상은 대시보드 '상신 대기'에 자동으로 잡힌다."""
    customer_id, product_id, batch_id = customer_id.strip(), product_id.strip(), batch_id.strip()
    claim_type, actor = claim_type.strip(), actor.strip()
    if not actor:
        raise TransitionError("접수자는 필수")
    cust = conn.execute("SELECT customer_id, customer_name FROM src_customers WHERE customer_id=?", (customer_id,)).fetchone()
    if cust is None:
        raise TransitionError(f"고객 {customer_id or '(빈 값)'} 없음 — m_customer_cv에 있는 고객만 접수")
    if conn.execute("SELECT 1 FROM src_products WHERE product_id=?", (product_id,)).fetchone() is None:
        raise TransitionError(f"제품 {product_id or '(빈 값)'} 없음 — m_product_cv에 있는 제품만 접수")
    b = conn.execute("SELECT * FROM src_batches WHERE batch_id=?", (batch_id,)).fetchone()
    if b is None:
        raise TransitionError(f"배치 {batch_id or '(빈 값)'} 없음 — prod_batch_log_cv에 있는 배치만 접수")
    if b["product_id"] != product_id:
        raise TransitionError(f"배치 {batch_id}는 {b['product_id']} 제품 — 입력한 제품 {product_id}와 다름")
    produced = int(b["produced_qty"])
    if not isinstance(qty, int) or not 1 <= qty <= produced:
        raise TransitionError(f"수량은 1 이상 배치 생산량({produced:,}) 이하 정수")
    if confirmed not in (True, False):
        raise TransitionError("확정 여부(확정/미확정)는 필수")
    if claim_type not in claim_types(conn):
        raise TransitionError(f"클레임 유형은 {', '.join(claim_types(conn))} 중 하나")
    # A05: 사건의 발생일과 적용일을 따로 기록한다. 적용일 = 판정에 반영되는 업무일(기본 현재 업무일).
    today = current_business_date(conn)
    occurred_date, applied_date = (occurred_date or "").strip(), (applied_date or "").strip() or today
    for name, v in (("발생일", occurred_date), ("적용일", applied_date)):
        try:
            bizcal.label(v)
        except ValueError:
            raise TransitionError(f"{name}은 YYYY-MM-DD 날짜")
    if occurred_date < b["production_date"]:
        raise TransitionError(f"발생일 {occurred_date}이 배치 생산일 {b['production_date']}보다 빠름")
    if not occurred_date <= applied_date <= today:
        raise TransitionError(f"발생일 ≤ 적용일 ≤ 현재 업무일({today})이어야 함")
    if not bizcal.is_business_day(applied_date):
        raise TransitionError(f"적용일 {bizcal.label(applied_date)}은 영업일(월~금)이어야 함 — {bizcal.BASIS}")

    n = conn.execute("SELECT COUNT(*) FROM claim_addition WHERE claim_id LIKE 'QC-N%'").fetchone()[0] + 1
    cid = f"QC-N{n:04d}"
    status = "확정" if confirmed else "접수"
    conn.execute("INSERT INTO claim_addition VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (cid, occurred_date, customer_id, product_id, batch_id, b["input_lot_ids"], claim_type, str(confirmed),
                  str(qty), status, CLAIM_INTAKE, now(), applied_date, f"화면 클레임 접수(모의) — 접수자 {actor}",
                  f"새 클레임 접수: {customer_id} · {claim_type} · {'확정' if confirmed else '미확정'} {qty:,}EA"))
    shipped = conn.execute("SELECT 1 FROM shipment_state WHERE batch_id=? AND customer_id=?",
                           (batch_id, customer_id)).fetchone() is not None
    note = "" if shipped else " · 이 고객의 해당 배치 SBL 출하 기록 없음(출하 경로 확인 필요)"
    record(conn, "claim", cid, None, f"{status} · 수량 {qty}", "사람", actor,
           f"새 클레임 접수(모의) — {customer_id} {cust['customer_name']} · {batch_id} · {claim_type} · "
           f"{'확정' if confirmed else '미확정'} {qty:,}EA · 발생 {occurred_date} · 적용 {applied_date}"
           f"{' (소급 적용)' if applied_date < today else ''} (원본 불변, claim_addition){note}",
           {"customer_id": customer_id, "product_id": product_id, "batch_id": batch_id, "qty": qty,
            "occurred_date": occurred_date, "applied_date": applied_date,
            "defect_confirmed": confirmed, "claim_type": claim_type, "sbl_shipped_to_customer": shipped})

    out = {"claim_id": cid, "batch_id": batch_id, "sbl_shipped_to_customer": shipped,
           "occurred_date": occurred_date, "applied_date": applied_date}
    if conn.execute("SELECT COUNT(*) FROM assessment").fetchone()[0] == 0:
        out["result"] = "판정 전 — '판정 실행' 때 반영"
    else:
        out.update(revalidate(conn, batch_id, f"클레임 {cid} 접수"))
        if out["before"] in ("회수의결", "회수완료"):
            out["scope_task"] = workflow.claim_on_recalled_batch(conn, batch_id, customer_id, cid, qty, confirmed)
        targets = {r[0] for r in conn.execute("SELECT batch_id FROM assessment WHERE is_target=1")}
        workflow.update_flags(conn, targets)
        out["pending_submission"] = out["after"] == "회수대상" and batch_id not in workflow._open_submission_batches(conn)
    conn.commit()
    return out


def _move_out_of_open_submission(conn, batch_id, cause):
    """의결 전 상신에 들어 있던 배치가 재검토로 가면 그 상신에서는 '재검토 이관'으로 닫는다."""
    for s in conn.execute("SELECT id, batch_ids FROM submission WHERE status='상신' AND kind='회수상신'").fetchall():
        if batch_id in json.loads(s["batch_ids"]) and not conn.execute(
                "SELECT 1 FROM decision WHERE submission_id=? AND batch_id=?", (s["id"], batch_id)).fetchone():
            conn.execute("INSERT INTO decision VALUES (?,?,?,?,?,?,?)",
                         (s["id"], batch_id, "재검토 이관", "시스템", f"정정 후 기준 미달: {cause}", now(),
                          current_business_date(conn)))
            record(conn, "submission", str(s["id"]), "상신", "상신", *SYSTEM_ACTOR,
                   f"{batch_id} 재검토 이관 — 이 상신에서 의결하지 않음")
            workflow._close_if_complete(conn, s["id"])


def _pct(v):
    return "계산 불가" if v is None else f"{v:.2f}%"
