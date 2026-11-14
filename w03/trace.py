"""배치 추적: 클레임 → 배치 → 원료 로트 → 같은 로트 배치 → 출하(SBL) → 고객 → 관련 Task·상태.

각 연결에 근거를 붙인다 — '원본 키'(원본 CSV의 키로 직접 연결) / '가정'(DESIGN.md 가정 A-x로 만든 연결) /
'앱 추가'(화면 접수·회신으로 앱이 추가한 연결, 원본은 그대로).
"""
import json

ORIGIN, ASSUMED, ADDED = "원본 키", "가정", "앱 추가"


def _link(kind: str, basis: str) -> dict:
    return {"kind": kind, "basis": basis}


def _claim_link(origin: str) -> dict:
    if origin == "원본":
        return _link(ORIGIN, "qc_claims_cv.batch_id")
    if "클레임 접수" in origin:
        return _link(ADDED, "claim_addition — 화면 '클레임 접수'")
    return _link(ADDED, "claim_addition — 추가검사 불합격 등록(A-23)")


def build(conn, batch_id: str) -> dict | None:
    b = conn.execute("SELECT * FROM src_batches WHERE batch_id=?", (batch_id,)).fetchone()
    if b is None:
        return None
    state = conn.execute("SELECT * FROM batch_state WHERE batch_id=?", (batch_id,)).fetchone()
    a = conn.execute("SELECT * FROM assessment WHERE batch_id=?", (batch_id,)).fetchone()
    lot_id = b["input_lot_ids"]

    claims = [{**dict(c), "link": _claim_link(c["origin"])} for c in conn.execute(
        "SELECT * FROM claims_effective WHERE batch_id=? ORDER BY origin != '원본', claim_date, claim_id", (batch_id,))]
    for c in claims:
        if c["corrections"]:
            c["link"] = {**c["link"], "basis": c["link"]["basis"] + " · 정정 반영(A-9)"}

    lot = conn.execute("SELECT * FROM src_lots WHERE lot_id=?", (lot_id,)).fetchone()
    lot_state = conn.execute("SELECT * FROM lot_status WHERE lot_id=?", (lot_id,)).fetchone()
    same_lot = [dict(r) for r in conn.execute(
        """SELECT b.batch_id, b.production_date, CAST(b.produced_qty AS INTEGER) produced_qty, b.qc_result,
                  s.state, s.investigate_flag, s.investigate_reason, i.state inv_state, sa.rate_pct alert_rate
           FROM src_batches b JOIN batch_state s USING (batch_id)
           LEFT JOIN inventory_hold i ON i.hold_id = 'INV-' || b.batch_id
           LEFT JOIN sample_alert sa USING (batch_id)
           WHERE b.input_lot_ids=? AND b.batch_id != ? ORDER BY b.batch_id""", (lot_id, batch_id))]
    for r in same_lot:
        r["link"] = _link(ORIGIN, "같은 input_lot_ids")
        r["flag_link"] = (_link(ASSUMED, "같은 로트 → 조사대상(A-3, R6)")
                          if r["investigate_flag"] and "R6" in (r["investigate_reason"] or "") else None)

    ships = [dict(r) for r in conn.execute(
        """SELECT s.*, c.customer_name, w.reason watch FROM shipment_state s
           LEFT JOIN src_customers c USING (customer_id)
           LEFT JOIN claim_watch w USING (batch_id, customer_id)
           WHERE s.batch_id=? ORDER BY s.ship_date, s.shipment_id""", (batch_id,))]
    for s in ships:
        s["link"] = (_link(ORIGIN, "shipment_batch_link_cv.batch_id") if s["source"] == "SBL"
                     else _link(ADDED, "출하 경로 확인 회신(A-21)"))

    # 고객: SBL 출하로 연결된 고객 + 클레임만 있고 SBL 출하가 없는 고객(R10)
    customers: dict[str, dict] = {}
    for s in ships:
        c = customers.setdefault(s["customer_id"], {"customer_id": s["customer_id"], "customer_name": s["customer_name"],
                                                    "shipped_qty": 0, "shipments": 0, "states": set(), "claims": 0,
                                                    "claim_qty": 0, "watch": s["watch"]})
        c["shipped_qty"] += s["shipped_qty"]
        c["shipments"] += 1
        c["states"].add(s["state"])
        if s["source"] == "SBL":
            c["link"] = _link(ORIGIN, "shipment_batch_link_cv.customer_id")
        else:
            c.setdefault("link", _link(ADDED, "출하 경로 확인 회신으로 연결(A-21)"))
    for cl in claims:
        if cl["defect_confirmed"] != "True":
            continue
        c = customers.get(cl["customer_id"])
        if c is None:
            name = conn.execute("SELECT customer_name FROM src_customers WHERE customer_id=?",
                                (cl["customer_id"],)).fetchone()
            c = customers.setdefault(cl["customer_id"], {
                "customer_id": cl["customer_id"], "customer_name": name[0] if name else None, "shipped_qty": 0,
                "shipments": 0, "states": set(), "claims": 0, "claim_qty": 0, "watch": None,
                "link": _link(ASSUMED, "클레임만 있고 SBL 출하 없음 — 출하 연결 미확인(R10, A-21)")})
        c["claims"] += 1
        c["claim_qty"] += int(cl["qty_affected"])
    holdings = {r["customer_id"]: r for r in conn.execute("SELECT * FROM customer_holding WHERE batch_id=?", (batch_id,))}
    unlinked = {r["customer_id"]: r for r in conn.execute("SELECT * FROM unlinked_customer WHERE batch_id=?", (batch_id,))}
    for cid, c in customers.items():
        c["states"] = sorted(c["states"])
        h = holdings.get(cid)
        c["holding"] = h["held_qty"] if h else None
        c["holding_link"] = _link(ADDED, f"고객 보유량 회신({h['source']})") if h else None
        u = unlinked.get(cid)
        c["unlinked_state"] = u["state"] if u else None
    customers_list = sorted(customers.values(), key=lambda c: (-c["shipped_qty"], c["customer_id"]))

    inv = conn.execute("SELECT * FROM inventory_hold WHERE hold_id=?", (f"INV-{batch_id}",)).fetchone()
    inventory = None
    if inv:
        inventory = {**dict(inv), "link": _link(ASSUMED, "생산량 − SBL 출하 합 추정(A-5)") if inv["measured_qty"] is None
                     else _link(ADDED, f"실측 회신({inv['measured_by']})")}

    tasks = [dict(t) for t in conn.execute(
        """SELECT * FROM task WHERE related_id IN (?, ?) OR related_id LIKE ? OR detail_json LIKE ?
           ORDER BY id""", (batch_id, f"INV-{batch_id}", f"{batch_id}|%", f'%"{batch_id}"%'))]
    subs = [dict(s) for s in conn.execute("SELECT * FROM submission ORDER BY id")
            if batch_id in json.loads(s["batch_ids"] or "[]")]
    for s in subs:
        d = conn.execute("SELECT * FROM decision WHERE submission_id=? AND batch_id=?", (s["id"], batch_id)).fetchone()
        s["decision"] = dict(d) if d else None

    return {
        "batch": dict(b), "state": dict(state) if state else None, "a": dict(a) if a else None,
        "claims": claims, "lot": dict(lot) if lot else None, "lot_state": dict(lot_state) if lot_state else None,
        "lot_link": _link(ORIGIN, "prod_batch_log_cv.input_lot_ids → material_lot_master_cv.lot_id"),
        "same_lot": same_lot, "shipments": ships, "customers": customers_list, "inventory": inventory,
        "tasks": tasks, "submissions": subs,
        "counts": {
            "claims_src": sum(1 for c in claims if c["link"]["kind"] == ORIGIN),
            "claims_added": sum(1 for c in claims if c["link"]["kind"] == ADDED),
            "ships_src": sum(1 for s in ships if s["source"] == "SBL"),
            "ships_added": sum(1 for s in ships if s["source"] != "SBL"),
            "customers_unlinked": sum(1 for c in customers_list if c["link"]["kind"] == ASSUMED),
        },
    }
