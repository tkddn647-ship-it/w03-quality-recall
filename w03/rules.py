"""판단 규칙 (DESIGN.md §2). 계산만 하고 상태는 바꾸지 않는다 — 상태 변경은 workflow.py."""
import statistics

from .config import AS_OF, RECALL_THRESHOLD_PCT, RESUPPLY_LINE, RESUPPLY_MATERIAL

OPEN_CLAIM_STATUSES = ("접수", "조사중")
LEVEL = {"가능": 0, "지연": 1, "대안필요": 2}


def assess_batches(conn) -> list[dict]:
    """R1: 배치별 확정 불량률(분모: 생산량·출하량)과 회수대상 여부."""
    rows = conn.execute(
        """
        SELECT b.batch_id, b.product_id, b.production_line, b.production_date, b.input_lot_ids AS lot_id,
               b.qc_result, CAST(b.produced_qty AS INTEGER) AS produced_qty,
               COALESCE(c.n, 0) AS confirmed_claims, COALESCE(c.qty, 0) AS confirmed_qty,
               s.qty AS shipped_qty
        FROM src_batches b
        LEFT JOIN (SELECT batch_id, COUNT(*) n, SUM(CAST(qty_affected AS INTEGER)) qty
                   FROM claims_effective WHERE defect_confirmed = 'True' GROUP BY batch_id) c USING (batch_id)
        LEFT JOIN (SELECT batch_id, SUM(CAST(shipped_qty AS INTEGER)) qty
                   FROM src_sbl GROUP BY batch_id) s USING (batch_id)
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["rate_prod"] = d["confirmed_qty"] / d["produced_qty"] * 100 if d["produced_qty"] else None
        d["rate_ship"] = d["confirmed_qty"] / d["shipped_qty"] * 100 if d["shipped_qty"] else None
        has_conf = d["confirmed_qty"] > 0
        d["recall_prod"] = has_conf and d["rate_prod"] is not None and d["rate_prod"] >= RECALL_THRESHOLD_PCT
        d["recall_ship"] = has_conf and d["rate_ship"] is not None and d["rate_ship"] >= RECALL_THRESHOLD_PCT
        # A-2: 어느 한쪽이라도 기준 이상이면 회수대상, 갈리면 경고
        d["is_target"] = d["recall_prod"] or d["recall_ship"]
        d["warning"] = ""
        if d["is_target"] and d["recall_prod"] != d["recall_ship"]:
            d["warning"] = "분모 차이: 생산량·출하량 기준 판정이 다름"
        if d["shipped_qty"] is None and has_conf:
            d["warning"] = "출하량 분모 계산 불가(SBL 연결 없음)"
        out.append(d)
    return out


def investigation_flags(conn, target_ids: set[str]) -> dict[str, list[str]]:
    """R2(미확정 클레임 진행·qc 보류)와 R6(회수대상과 같은 로트) → 배치별 조사대상 사유."""
    flags: dict[str, list[str]] = {}
    for r in conn.execute(
        f"SELECT batch_id, COUNT(*) n FROM claims_effective WHERE defect_confirmed != 'True'"
        f" AND status IN ({','.join('?' * len(OPEN_CLAIM_STATUSES))}) GROUP BY batch_id",
        OPEN_CLAIM_STATUSES,
    ):
        flags.setdefault(r["batch_id"], []).append(f"R2 미확정 클레임 진행 {r['n']}건")
    for r in conn.execute("SELECT batch_id FROM src_batches WHERE qc_result = '보류'"):
        flags.setdefault(r["batch_id"], []).append("R2 원본 QC 결과 보류")
    if target_ids:
        marks = ",".join("?" * len(target_ids))
        target_lots = [r[0] for r in conn.execute(
            f"SELECT DISTINCT input_lot_ids FROM src_batches WHERE batch_id IN ({marks})", list(target_ids))]
        for lot in target_lots:
            for r in conn.execute("SELECT batch_id FROM src_batches WHERE input_lot_ids = ?", (lot,)):
                if r["batch_id"] not in target_ids:
                    flags.setdefault(r["batch_id"], []).append(f"R6 회수대상과 같은 로트 {lot}")
    return flags


def trace_stock(conn, mode: str = "actual") -> dict:
    """같은 원료 로트 역추적 재고(kind='trace') 중 재공급에 쓸 수 있는 수량.
    actual: 추가검사 합격(출하가능)분만 / no_trace: 확장이 없다면 — 검사 없이 정상 재고로 취급했을 경우."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM inventory_hold WHERE kind='trace' ORDER BY batch_id")]
    qty = lambda r: r["measured_qty"] if r["measured_qty"] is not None else r["est_qty"]
    usable = rows if mode == "no_trace" else [r for r in rows if r["state"] == "출하가능"]
    return {"qty": sum(qty(r) for r in usable), "rows": rows,
            "blocked": sum(qty(r) for r in rows if r["state"] in ("추가검사필요", "검사불합격")) if mode == "actual" else 0}


def resupply_check(conn, batch_ids: list[str], stock_mode: str = "actual") -> dict:
    """R8: 재공급 가능 여부를 생산(L-02 여유)·조달(M-B 로트) 두 갈래로 판정한다.
    필요량에서 재공급에 쓸 수 있는 완제품 재고(같은 로트 역추적 재고 중 추가검사 합격분, 가정 A-22)를 뺀다."""
    marks = ",".join("?" * len(batch_ids))
    # 필요 수량: 고객 보유량 회신(customer_holding)이 있으면 그 값, 없으면 출하분 전량 = 상한값(가정 A-8)
    pairs = conn.execute(
        f"""SELECT s.batch_id, s.customer_id, SUM(s.shipped_qty) shipped, h.held_qty
            FROM shipment_state s LEFT JOIN customer_holding h USING (batch_id, customer_id)
            WHERE s.batch_id IN ({marks}) GROUP BY s.batch_id, s.customer_id""", batch_ids).fetchall()
    upper_qty = sum(p["shipped"] for p in pairs)
    gross_need = sum(p["shipped"] if p["held_qty"] is None else p["held_qty"] for p in pairs)
    stock = trace_stock(conn, stock_mode)
    need_qty = max(gross_need - stock["qty"], 0)
    replied = sum(1 for p in pairs if p["held_qty"] is not None)
    if replied == 0:
        need_basis = "상한값(가정 A-8: 출하분 전량 재공급)"
    elif replied < len(pairs):
        need_basis = f"일부 회신 반영({replied}/{len(pairs)}) · 미회신분은 상한값(A-8)"
    else:
        need_basis = f"고객 보유량 회신 반영({replied}/{len(pairs)})"
    if stock["qty"]:
        need_basis += f" − 재공급 가능 재고 {stock['qty']:,}" + (" (확장 없음 가정)" if stock_mode == "no_trace" else "(추가검사 합격분)")

    # (a) 생산: 여유 = capacity_qty × (1 − utilization_pct). 원본에 기간 단위가 없다(A-6).
    #     판정은 '필요량 ÷ 1기간 여유량'으로 하므로 단위와 무관: ≤ 1 가능, > 1(또는 여유 없음) 대안필요.
    cap = conn.execute("SELECT * FROM src_capacity WHERE line_id = ?", (RESUPPLY_LINE,)).fetchone()
    capacity, util = float(cap["capacity_qty"]), float(cap["utilization_pct"])
    spare = round(capacity * (1 - util / 100))
    ratio = round(need_qty / spare, 2) if spare > 0 else None
    prod_result = "가능" if ratio is not None and ratio <= 1 else "대안필요"
    actual_2025 = conn.execute(
        "SELECT SUM(CAST(produced_qty AS INTEGER)) FROM src_batches WHERE product_id = ? AND production_date LIKE '2025-%'",
        (cap["product_id"],)).fetchone()[0] or 0
    production = {
        "result": prod_result, "line_id": RESUPPLY_LINE, "plant_id": cap["plant_id"],
        "capacity_qty": int(capacity), "utilization_pct": util, "spare_qty": spare,
        "need_qty": need_qty, "upper_qty": upper_qty, "need_basis": need_basis,
        "gross_need_qty": gross_need, "stock_offset_qty": stock["qty"], "stock_blocked_qty": stock["blocked"],
        "shortfall_qty": max(need_qty - spare, 0),
        "periods_needed": ratio,
        "actual_2025_qty": actual_2025,
        "capacity_period": f"단위 미확정(실생산 2025년 {actual_2025 / 10000:,.0f}만EA와 불일치) — 판정은 단위와 무관 (가정 A-6)",
        "rule": "필요량 ÷ 여유량 ≤ 1 → 가능, > 1 → 대안필요",
        "source": "strat_capacity_cv (기준일 " + cap["snapshot_date"] + ")",
    }

    # (b) 조달: 사용 가능 로트 → 입고예정 PO → 대안필요
    bad_lots = [r[0] for r in conn.execute(
        f"SELECT DISTINCT input_lot_ids FROM src_batches WHERE batch_id IN ({marks})", batch_ids)]
    stopped = [r[0] for r in conn.execute("SELECT lot_id FROM lot_status WHERE state='사용정지'")]
    if stock_mode != "no_trace":
        bad_lots = sorted(set(bad_lots) | set(stopped))
    used = {r[0] for r in conn.execute("SELECT DISTINCT lot_id FROM src_consumption")}
    lots = [dict(r) for r in conn.execute(
        "SELECT lot_id, supplier_id, receipt_date, inspection_result, received_qty FROM src_lots"
        " WHERE material_id = ? ORDER BY receipt_date", (RESUPPLY_MATERIAL,))]
    usable = [l for l in lots if l["inspection_result"] == "규격내" and l["lot_id"] not in bad_lots
              and l["lot_id"] not in used]
    last_receipt = max(l["receipt_date"] for l in lots)
    pending = [dict(r) for r in conn.execute(
        "SELECT po_id, supplier_id, purchase_date, qty FROM src_purchases"
        " WHERE material_id = ? AND purchase_date > ? AND purchase_date <= ? ORDER BY purchase_date",
        (RESUPPLY_MATERIAL, last_receipt, AS_OF))]
    mat = conn.execute("SELECT * FROM src_materials WHERE material_id = ?", (RESUPPLY_MATERIAL,)).fetchone()
    if usable:
        mat_result = "가능"
    elif pending:
        mat_result = "지연"
    else:
        mat_result = "대안필요"
    # M-B 필요량 참고치: PL-02 배치 투입비(kg/EA) 중앙값 × 필요 수량 (A05: 투입 이력 신뢰도 제한)
    ratios = [float(r["consumed_qty"]) / float(r["produced_qty"]) for r in conn.execute(
        "SELECT c.consumed_qty, b.produced_qty FROM src_consumption c JOIN src_batches b USING (batch_id)"
        " WHERE c.material_id = ?", (RESUPPLY_MATERIAL,))]
    material = {
        "result": mat_result, "material_id": RESUPPLY_MATERIAL, "supplier_id": mat["supplier_id"],
        "is_exclusive": mat["is_exclusive"] == "True", "contract_type": mat["contract_type"],
        "excluded_lots": bad_lots, "stopped_lots": stopped if stock_mode != "no_trace" else [],
        "usable_lots": [l["lot_id"] for l in usable],
        "last_lot_receipt": last_receipt, "pending_pos": pending,
        "est_need_kg": round(statistics.median(ratios) * need_qty, 1) if ratios else None,
        "note": "투입량>입고량 한계(A05)로 로트 잔량은 산출 불가 — 투입 기록 없는 규격내 로트만 '사용 가능'으로 본다",
        "source": "material_lot_master_cv, procurement_material_purchase_cv, m_material_cv",
    }
    overall = max(prod_result, mat_result, key=LEVEL.get)
    return {"overall": overall, "production": production, "material": material}


def impact(conn, batch_ids: list[str]) -> dict:
    """회수대상 배치의 영향 범위: 고객 인도분, 사내 잔여재고(추정), 클레임 고객과의 대조."""
    marks = ",".join("?" * len(batch_ids))
    by_customer = [dict(r) for r in conn.execute(
        f"""SELECT s.customer_id, c.customer_name, c.cost_linked_clause,
                   COUNT(*) shipments, SUM(s.shipped_qty) qty, MIN(s.ship_date) first_ship, MAX(s.ship_date) last_ship,
                   GROUP_CONCAT(DISTINCT s.batch_id) batches
            FROM shipment_state s LEFT JOIN src_customers c USING (customer_id)
            WHERE s.batch_id IN ({marks}) GROUP BY s.customer_id ORDER BY qty DESC""", batch_ids)]
    claim_customers = [dict(r) for r in conn.execute(
        f"""SELECT customer_id, COUNT(*) claims, SUM(CAST(qty_affected AS INTEGER)) qty
            FROM claims_effective WHERE batch_id IN ({marks}) AND defect_confirmed='True' AND origin='원본'
            GROUP BY customer_id ORDER BY customer_id""", batch_ids)]
    shipped_to = {r["customer_id"] for r in by_customer}
    gaps = [c for c in claim_customers if c["customer_id"] not in shipped_to]
    inventory = [dict(r) for r in conn.execute(
        f"""SELECT b.batch_id, CAST(b.produced_qty AS INTEGER) produced_qty,
                   COALESCE(SUM(s.shipped_qty),0) shipped_qty,
                   CAST(b.produced_qty AS INTEGER) - COALESCE(SUM(s.shipped_qty),0) est_qty
            FROM src_batches b LEFT JOIN shipment_state s USING (batch_id)
            WHERE b.batch_id IN ({marks}) GROUP BY b.batch_id""", batch_ids)]
    return {"customers": by_customer, "claim_customers": claim_customers,
            "claim_customers_without_sbl": gaps, "inventory": inventory}


CLUE_LABEL = "단서(데이터 한계 가능성 포함, 확인 필요)"


def lot_clues(conn, lot_id: str) -> dict | None:
    """원료 로트 투입 시점 단서 — 원인 조사의 출발점이지 원인 판정이 아니다.
    투입일을 로트 입고일·입고검사일·검사 결과와 비교한다. A05 알려진 한계(투입일 < 입고일, 투입량 > 입고량)가 있어 확인이 필요하다."""
    lot = conn.execute("SELECT * FROM src_lots WHERE lot_id=?", (lot_id,)).fetchone()
    if lot is None:
        return None
    receipt, insp, result = lot["receipt_date"], lot["inspection_date"], lot["inspection_result"]
    uses, clues = [], []
    for u in conn.execute(
            """SELECT c.batch_id, c.consumption_date, CAST(c.consumed_qty AS REAL) kg, s.state
               FROM src_consumption c JOIN batch_state s USING (batch_id) WHERE c.lot_id=? ORDER BY c.consumption_date""",
            (lot_id,)):
        d = u["consumption_date"]
        if d < receipt:
            timing = "입고 전 투입"
            clues.append(f"{u['batch_id']} 투입일 {d}이 로트 입고일 {receipt}·입고검사일 {insp}보다 빠름 — 입고검사 결과 전 투입 가능성"
                         f" (A05 알려진 한계 '투입일 < 입고일'일 수 있음)")
        elif d < insp:
            timing = "입고검사 전 투입"
            clues.append(f"{u['batch_id']} 투입일 {d} — 입고검사일 {insp} 전 투입 가능성")
        elif result != "규격내":
            timing = f"입고검사 '{result}' 이후 투입"
            clues.append(f"{u['batch_id']} 투입일 {d} — 입고검사 '{result}'({insp}) 상태에서 투입")
        else:
            timing = "입고검사 후 투입"
        uses.append({**dict(u), "timing": timing})
    received, consumed = float(lot["received_qty"]), round(sum(u["kg"] for u in uses), 2)
    limits = ["투입일이 입고일보다 빠른 기록은 A05 알려진 자료 한계로 공개된 패턴 — 실제 투입 순서는 생산·자재 기록으로 확인 필요"]
    if consumed > received:
        limits.append(f"이 로트의 누적 투입 {consumed:,}kg이 입고량 {received:,}kg보다 많음(A05 알려진 한계) — 로트 기록만으로 투입 경로를 확정할 수 없음")
    return {"lot_id": lot_id, "receipt_date": receipt, "inspection_date": insp, "inspection_result": result,
            "received_kg": received, "consumed_kg": consumed, "uses": uses, "clues": clues, "limits": limits,
            "label": CLUE_LABEL}

