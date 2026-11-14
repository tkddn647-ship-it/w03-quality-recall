"""필수 확장: 원료 로트 역추적 — 확장이 없을 때와 있을 때의 차이 (DESIGN.md §8)."""
from . import resupply, rules
from .workflow import inspection_source


def compare(conn, lot_id: str) -> dict | None:
    lot = conn.execute("SELECT * FROM lot_status WHERE lot_id=?", (lot_id,)).fetchone()
    src = conn.execute("SELECT * FROM src_lots WHERE lot_id=?", (lot_id,)).fetchone()
    if src is None:
        return None
    batches = [dict(r) for r in conn.execute(
        """SELECT b.batch_id, b.production_date, CAST(b.produced_qty AS INTEGER) produced_qty, b.qc_result,
                  s.state batch_state, s.investigate_reason, i.state inv_state, i.kind, i.est_qty, i.measured_qty,
                  i.measured_by, a.confirmed_qty, a.rate_prod, a.rate_ship,
                  sa.inspected_qty, sa.defect_qty, sa.rate_pct alert_rate
           FROM src_batches b JOIN batch_state s USING (batch_id)
           LEFT JOIN inventory_hold i ON i.hold_id = 'INV-' || b.batch_id
           LEFT JOIN assessment a USING (batch_id)
           LEFT JOIN sample_alert sa USING (batch_id)
           WHERE b.input_lot_ids=? ORDER BY b.batch_id""", (lot_id,))]
    for b in batches:
        b["data_source"] = inspection_source(b["measured_by"]) if b["kind"] == "trace" and b["measured_qty"] is not None else None
    trace = [b for b in batches if b["kind"] == "trace"]
    qty = lambda b: b["measured_qty"] if b["measured_qty"] is not None else b["est_qty"]
    out = {"lot": dict(lot) if lot else None, "src": dict(src), "batches": batches, "trace": trace}
    if lot is None:
        return out
    sid = lot["submission_id"]
    active = resupply.active_batches(conn, sid)
    if active:
        with_ = rules.resupply_check(conn, active, "actual")
        without = rules.resupply_check(conn, active, "no_trace")
        out["rows"] = [
            {"label": "같은 로트 재고 출하 가능 수량",
             "without": sum(qty(b) for b in trace),
             "with": sum(qty(b) for b in trace if b["inv_state"] == "출하가능"),
             "note": "확장 없음: 검사 없이 정상 재고 / 확장: 추가검사 합격분만"},
            {"label": "출하 보류(추가검사 대기·불합격)",
             "without": 0, "with": sum(qty(b) for b in trace if b["inv_state"] in ("추가검사필요", "검사불합격")),
             "note": "같은 로트 의심 재고가 고객에게 나가지 않도록 막은 수량"},
            {"label": "재공급 필요량", "without": without["production"]["need_qty"], "with": with_["production"]["need_qty"],
             "note": "재공급 가능 재고(A-22)를 뺀 값"},
            {"label": "필요 ÷ 여유", "without": without["production"]["periods_needed"],
             "with": with_["production"]["periods_needed"], "note": "> 1 → 대안필요"},
            {"label": "R8 생산 판정", "without": without["production"]["result"], "with": with_["production"]["result"], "note": ""},
            {"label": "조달 판정 제외 로트", "without": ", ".join(without["material"]["excluded_lots"]),
             "with": ", ".join(with_["material"]["excluded_lots"]),
             "note": f"확장: {lot_id} 사용정지 — M-B 가용 로트에서 제외"},
            {"label": "R8 조달 판정", "without": without["material"]["result"], "with": with_["material"]["result"],
             "note": "이 로트는 이미 투입 기록이 있어 결과는 같지만, 사용정지로 이후 배정을 막는다"},
        ]
        out["sid"] = sid
    return out
