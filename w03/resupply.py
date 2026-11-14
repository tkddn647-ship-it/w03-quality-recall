"""재공급 계획 (DESIGN.md §2 R8, §6).

R8 생산 판정이 '대안필요'(필요량 ÷ 여유량 > 1)이면 대안을 **순차 적용**한다.
  (b) 고객 보유량 회신으로 필요량 축소 → 남은 부족분만 (a) 기존 주문 납기 연기 → 그래도 남으면 (c) 증설 검토.
(a)는 생산능력을 만들지 않고 순서를 바꾸는 것이다 — 연기 물량은 이후 기간 여유량을 다시 잠식한다(A-17).
"""
import json
import math
import statistics
from datetime import date, timedelta

from . import rules
from .config import AS_OF, RESUPPLY_LINE
from .core import (AUTO, HOLD, OPEN_TASK_STATES, TransitionError, cancel_task, finish_task, hold_task, marks,
                   new_task, open_task, replace_task, resume_task, set_state, update_task)
from .db import current_business_date, now, record

S_DONE, S_CONFIRMED, S_EXHAUSTED = "판정완료", "대안확정", "대안소진"
S_B = "(b) 회신대기"
S_A_APPROVAL, S_A_CONSENT = "(a) 승인대기", "(a) 고객동의대기"
S_C_SELECT, S_C_PROC, S_C_APPROVAL = "(c) 선택대기", "(c) 조달확인대기", "(c) 승인대기"
STAGE_OF = {S_B: "b", S_A_APPROVAL: "a", S_A_CONSENT: "a", S_C_SELECT: "c", S_C_PROC: "c", S_C_APPROVAL: "c"}

RESUPPLY_TASKS = ("재공급 요청", "재공급 생산", "재공급 원료 확보")
STAGE_TASKS = ("보유량 회신 수집", "영업 승인", "납기 연기 동의 요청", "조달 확인", "투자 승인")
STAGES = {
    "b": "고객 보유량 회신으로 필요량 축소",
    "a": "기존 PL-02 주문 납기 연기(남은 부족분만)",
    "c": "증설안 검토(증설안 원본, 그래도 남는 부족분)",
}
A17 = "연기는 생산능력을 만들지 않고 순서를 바꾸는 것 — 연기 물량은 이후 기간 여유량을 다시 잠식한다(가정 A-17)"


def active_batches(conn, sid) -> list[str]:
    """이 상신에서 승인됐고 지금도 회수의결인 배치(재검토 중인 배치는 빠진다)."""
    return [r[0] for r in conn.execute(
        """SELECT d.batch_id FROM decision d JOIN batch_state s USING (batch_id)
           WHERE d.submission_id=? AND d.result='승인' AND s.state='회수의결' ORDER BY d.batch_id""", (sid,))]


def plan_row(conn, sid):
    return conn.execute("SELECT * FROM resupply_plan WHERE submission_id=?", (sid,)).fetchone()


def _detail(plan) -> dict:
    return json.loads(plan["detail_json"]) if plan and plan["detail_json"] else {}


def _save_detail(conn, sid, detail):
    conn.execute("UPDATE resupply_plan SET detail_json=?, updated_at=? WHERE submission_id=?",
                 (json.dumps(detail, ensure_ascii=False), now(), sid))


def _move(conn, sid, to, actor_type, actor, reason, evidence=None, stage=None):
    set_state(conn, "plan", sid, to, actor_type, actor, reason, evidence)
    conn.execute("UPDATE resupply_plan SET alternative=? WHERE submission_id=?", (stage or STAGE_OF.get(to), sid))


def _tasks_of(conn, sid, types):
    return conn.execute(
        f"SELECT * FROM task WHERE submission_id=? AND type IN ({marks(types)}) AND state IN ({marks(OPEN_TASK_STATES)})",
        (sid, *types, *OPEN_TASK_STATES)).fetchall()


def shortfall(rs) -> int:
    p = rs["production"]
    return max(p["need_qty"] - p["spare_qty"], 0)


# ---------- R8 재판정 → 계획·Task 동기화 ----------

def refresh(conn, sid) -> None:
    """승인 배치·고객 보유량 회신·정정이 바뀔 때마다 호출: R8 재판정 → 계획 상태 → 재공급 Task 갱신."""
    active = active_batches(conn, sid)
    plan = plan_row(conn, sid)
    if not active:
        for t in _tasks_of(conn, sid, RESUPPLY_TASKS + STAGE_TASKS):
            hold_task(conn, t["id"], "회수의결 배치 없음 — 재검토 결과 대기")
        if plan and plan["state"] != HOLD:
            d = _detail(plan)
            d["prev_state"] = plan["state"]
            _save_detail(conn, sid, d)
            set_state(conn, "plan", sid, HOLD, *AUTO, "회수의결 배치 없음 — 재검토 결과 대기")
        return
    for t in _tasks_of(conn, sid, RESUPPLY_TASKS + STAGE_TASKS):
        resume_task(conn, t["id"], f"회수의결 배치 {', '.join(active)} 기준으로 재개")

    rs = rules.resupply_check(conn, active)
    _sync_plan(conn, sid, rs, active)
    plan = plan_row(conn, sid)
    p, m = rs["production"], rs["material"]
    need = f"{p['need_qty']:,}EA ({p['need_basis']})"
    note = _plan_note(plan)
    replace_task(conn, sid, "재공급 요청", "영업", "submission", str(sid),
                 f"[{rs['overall']}] 회수 고객 재공급 요청 — {need} · {', '.join(active)}" + note,
                 {"overall": rs["overall"], "batches": active, "need_qty": p["need_qty"], "upper_qty": p["upper_qty"],
                  "need_basis": p["need_basis"], "plan": f"/plans/{sid}",
                  "next": {"가능": "고객별 재공급 일정 회신",
                           "지연": "고객에 재공급 지연 가능성 사전 안내 · 조달 입고 회신 대기",
                           "대안필요": f"재공급 계획 #{sid}: (b) → (a) → (c) 순차 적용"}[rs["overall"]]})
    replace_task(conn, sid, "재공급 생산", "생산", "line", p["line_id"],
                 f"[{p['result']}] {p['line_id']} 재생산 {need} — 여유 {p['spare_qty']:,}EA, 필요÷여유 {p['periods_needed']}" + note,
                 {**p, "next": {"가능": "재생산 일정 확정",
                                "대안필요": f"필요÷여유 {p['periods_needed']} > 1 — 재공급 계획의 단계별 결과에 따라 일정 확정"
                                }[p["result"]]})
    replace_task(conn, sid, "재공급 원료 확보", "조달", "material", m["material_id"],
                 f"[{m['result']}] {m['material_id']} ({m['supplier_id']}{' 독점' if m['is_exclusive'] else ''}) 확보"
                 + (f" — 참고 필요량 약 {m['est_need_kg']:,}kg" if m["est_need_kg"] is not None else ""),
                 {**m, "next": {"가능": f"사용 가능 로트 배정: {', '.join(m['usable_lots'])}",
                                "지연": "입고예정 PO 입고일·수량 확인: " + ", ".join(x["po_id"] for x in m["pending_pos"]),
                                "대안필요": "V-205 독점 — 대체 원료 검토(W04) 착수"}[m["result"]]})


def _plan_note(plan) -> str:
    if plan is None or plan["state"] == S_DONE:
        return ""
    d = _detail(plan)
    if plan["state"] == S_CONFIRMED:
        c = d.get("confirmed", {})
        if c.get("stage") == "b":
            return " · 대안(b) 확정: 보유량 회신으로 부족분 해소"
        if c.get("stage") == "a":
            return (f" · 대안(a) 확정: 납기 연기 {c['qty']:,}EA로 순서 조정 "
                    f"(A-17: 이후 여유 약 {c['periods']}기간 잠식)")
        if c.get("stage") == "c":
            return f" · 대안(c) 승인: {c['option_id']}" + (f" + 연기 {c['a_qty']:,}EA" if c.get("a_qty") else "")
    return f" · 재공급 계획: {plan['state']}"


def _sync_plan(conn, sid, rs, active):
    row = plan_row(conn, sid)
    needs_alt = rs["production"]["result"] == "대안필요"
    ratio = rs["production"]["periods_needed"]
    if row is None:
        state = S_B if needs_alt else S_DONE
        detail = {"resupply": rs, "active_batches": active}
        conn.execute("INSERT INTO resupply_plan (submission_id, state, overall, alternative, detail_json, updated_at)"
                     " VALUES (?,?,?,?,?,?)", (sid, state, rs["overall"], STAGE_OF.get(state),
                                               json.dumps(detail, ensure_ascii=False), now()))
        record(conn, "plan", str(sid), None, state, *AUTO,
               f"R8 판정 {rs['overall']} — 필요÷여유 {ratio}"
               + (" > 1 → 대안 순차 적용 시작: (b) 고객 보유량 회신" if needs_alt else " ≤ 1 → 대안 불필요"),
               {"production": rs["production"]})
        if needs_alt:
            _start_b(conn, sid, active)
        return
    d = _detail(row)
    d["resupply"], d["active_batches"] = rs, active
    _save_detail(conn, sid, d)
    conn.execute("UPDATE resupply_plan SET overall=? WHERE submission_id=?", (rs["overall"], sid))
    state = row["state"]
    if state == HOLD:
        back = d.get("prev_state") if needs_alt else S_DONE
        _move(conn, sid, back or S_B, *AUTO, f"회수의결 배치 복귀 → 재판정(필요÷여유 {ratio})")
        state = back or S_B
    if not needs_alt and state not in (S_DONE, S_CONFIRMED):
        _cancel_stage_tasks(conn, sid, f"필요÷여유 {ratio} ≤ 1 — 부족분 해소")
        if state == S_B:
            d["confirmed"] = {"stage": "b", "need_qty": rs["production"]["need_qty"]}
            _save_detail(conn, sid, d)
            _move(conn, sid, S_CONFIRMED, *AUTO, f"(b) 보유량 회신으로 부족분 해소 — 필요÷여유 {ratio} ≤ 1", stage="b")
        else:
            _move(conn, sid, S_DONE, *AUTO, f"재판정: 필요÷여유 {ratio} ≤ 1 → 대안 불필요")
    elif needs_alt and state == S_DONE:
        _move(conn, sid, S_B, *AUTO, f"재판정: 필요÷여유 {ratio} > 1 → 대안 순차 적용 시작")
        _start_b(conn, sid, active)
    elif state == S_B:
        _start_b(conn, sid, active)          # 승인 배치·회신이 바뀌면 회신 수집 Task 범위를 갱신
    elif state == S_A_CONSENT:
        _reselect(conn, sid)


def _cancel_stage_tasks(conn, sid, reason):
    for t in _tasks_of(conn, sid, STAGE_TASKS):
        cancel_task(conn, t["id"], reason)
    conn.execute("UPDATE deferral_request SET state='제외' WHERE submission_id=? AND state='요청'", (sid,))


def _start_b(conn, sid, active):
    """(b) 시작: 보유량 회신 수집 Task + (있으면) 추정 납기 경과 주문 상태 확인 Task."""
    b = holdings_status(conn, active)
    p = rules.resupply_check(conn, active)["production"]
    replied = sum(r["held_qty"] for r in b["replied"])
    stock = f" − 재공급 가능 재고 {p['stock_offset_qty']:,}" if p["stock_offset_qty"] else ""
    replace_task(conn, sid, "보유량 회신 수집", "영업", "plan", str(sid),
                 f"(b) 고객 보유량 회신 수집 — 필요량 {p['need_qty']:,}EA = 회신분 {replied:,} + 미회신분 상한 "
                 f"{b['unreplied_qty']:,}(A-8){stock} · 미회신 {b['unreplied']}/{b['pairs']}건 · 고객 {b['customers']}곳",
                 {"pairs": b["unreplied_list"], "need_qty": p["need_qty"], "replied_qty": replied,
                  "unreplied_upper_qty": b["unreplied_qty"], "stock_offset_qty": p["stock_offset_qty"],
                  "next": "고객별 보유량 회신 입력 → 회신분은 회신 수량, 미회신분은 출하 수량(상한)으로 필요량 계산 · 마감 시 남은 부족분만 (a)로"})
    pool, overdue = deferral_pool(conn)
    if overdue and open_task(conn, sid, "주문 상태 확인 요청", str(sid)) is None:
        new_task(conn, sid, "주문 상태 확인 요청",
                 f"미출하 주문 상태 확인 요청 — 추정 납기 경과 {len(overdue)}건 {sum(o['qty'] for o in overdue):,}EA "
                 "(데이터 확인 필요 · 연기 후보 제외, A-12)", "영업", "plan", str(sid),
                 {"orders": overdue, "next": "주문별 상태(취소·출하 누락·보류)를 확인해 회신"})


# ---------- 단계 계산 ----------

def lead_time_stats(conn) -> dict[str, dict]:
    """영업팀별 주문→출하 소요일 중앙값 (sales_orders_cv × sales_shipments_cv, 가정 A-11: 원본 도출)."""
    lags: dict[str, list[int]] = {}
    for r in conn.execute(
            "SELECT o.sales_team, o.order_date, s.ship_date FROM src_orders o JOIN src_sales_shipments s USING (order_id)"):
        lags.setdefault(r["sales_team"], []).append(
            (date.fromisoformat(r["ship_date"]) - date.fromisoformat(r["order_date"])).days)
    return {team: {"median_days": statistics.median(v), "n": len(v), "min": min(v), "max": max(v)}
            for team, v in sorted(lags.items())}


def deferral_pool(conn) -> tuple[list[dict], list[dict]]:
    """출하 기록 없는 PL-02 주문 → (연기 후보 풀, 추정 납기 경과 '데이터 확인 필요' 목록)."""
    product = conn.execute("SELECT product_id FROM src_capacity WHERE line_id=?", (RESUPPLY_LINE,)).fetchone()[0]
    stats = lead_time_stats(conn)
    rows = []
    for r in conn.execute(
            """SELECT order_id, order_date, customer_id, sales_team, CAST(order_qty AS INTEGER) qty,
                      CAST(order_amount_krw AS INTEGER) amount
               FROM src_orders WHERE product_id=? AND order_date <= ?
               AND order_id NOT IN (SELECT order_id FROM src_sales_shipments)""", (product, AS_OF)):
        o = dict(r)
        o["est_due"] = (date.fromisoformat(o["order_date"]) + timedelta(days=stats[o["sales_team"]]["median_days"])).isoformat()
        rows.append(o)
    rows.sort(key=lambda o: (o["est_due"], o["order_id"]), reverse=True)
    pool = [o for o in rows if o["est_due"] >= AS_OF]
    overdue = [o for o in rows if o["est_due"] < AS_OF]
    return pool, overdue


def select_deferrals(conn, target: int, excluded_customers=()) -> dict:
    """대안 (a): 추정 납기가 늦은 주문부터 target(남은 부족분)을 채울 때까지 선정. 경과 주문은 제외(A-12)."""
    pool, overdue = deferral_pool(conn)
    names = {r["customer_id"]: r["customer_name"] for r in conn.execute("SELECT * FROM src_customers")}
    eligible = [o for o in pool if o["customer_id"] not in excluded_customers]
    selected, total = [], 0
    for o in eligible:
        if total >= target:
            break
        selected.append(dict(o))
        total += o["qty"]
    by_cust: dict[str, dict] = {}
    for rank, o in enumerate(selected, 1):
        o["rank"] = rank
        c = by_cust.setdefault(o["customer_id"], {"customer_id": o["customer_id"],
                                                  "customer_name": names.get(o["customer_id"], ""),
                                                  "orders": 0, "qty": 0, "amount": 0, "order_ids": [],
                                                  "first_rank": rank, "dues": []})
        c["orders"] += 1
        c["qty"] += o["qty"]
        c["amount"] += o["amount"]
        c["order_ids"].append(o["order_id"])
        c["dues"].append(o["est_due"])
    for c in by_cust.values():
        dues = c.pop("dues")
        c["est_due_range"] = [min(dues), max(dues)]
    return {
        "basis": "가정 A-11(원본 도출): 추정 납기 = 주문일 + 영업팀별 주문→출하 소요일 중앙값",
        "rule": "추정 납기가 늦은 주문부터(기존 약속 중 덜 급한 것부터) 남은 부족분을 채울 때까지 연기 후보로 선정",
        "lead_stats": lead_time_stats(conn), "target": target,
        "pool_orders": len(pool), "pool_qty": sum(o["qty"] for o in pool),
        "data_check": overdue, "data_check_qty": sum(o["qty"] for o in overdue),
        "excluded_customers": list(excluded_customers),
        "selected": selected, "selected_qty": total, "selected_amount": sum(o["amount"] for o in selected),
        "covered": total >= target,
        "customers": sorted(by_cust.values(), key=lambda c: c["first_rank"]),
    }


def holdings_status(conn, batch_ids) -> dict:
    rows = conn.execute(
        f"""SELECT s.batch_id, s.customer_id, SUM(s.shipped_qty) shipped, h.held_qty
            FROM shipment_state s LEFT JOIN customer_holding h USING (batch_id, customer_id)
            WHERE s.batch_id IN ({marks(batch_ids)}) GROUP BY s.batch_id, s.customer_id
            ORDER BY s.customer_id, s.batch_id""", batch_ids).fetchall() if batch_ids else []
    unreplied = [dict(r) for r in rows if r["held_qty"] is None]
    return {"pairs": len(rows), "customers": len({r["customer_id"] for r in rows}),
            "unreplied": len(unreplied), "unreplied_qty": sum(r["shipped"] for r in unreplied),
            "unreplied_list": unreplied, "all": [dict(r) for r in rows],
            "replied": [dict(r) for r in rows if r["held_qty"] is not None]}


def _agreed(conn, sid) -> dict:
    rows = conn.execute("SELECT * FROM deferral_request WHERE submission_id=? AND state='동의'", (sid,)).fetchall()
    return {"qty": sum(r["qty"] for r in rows), "amount": sum(r["amount"] for r in rows), "customers": len(rows),
            "orders": [o for r in rows for o in json.loads(r["order_ids"])]}


def _requires_check(note: str) -> list[str]:
    """증설안 메모(원본 strat_capex_plan_cv.note)의 선행 조건. OPT-01은 '재계약·처방 재검증 불필요',
    OPT-02는 '원료(M-B) 재계약·처방 재검증 필요'로 적혀 있다."""
    if "불필요" in note or not ("재계약" in note or "재검증" in note):
        return []
    return ["원료(M-B) 재계약", "처방 재검증"]


def capex_options(conn, target: int, detail: dict, spare: int) -> list[dict]:
    checks, rejected = detail.get("c_checks", {}), detail.get("c_rejected", [])
    out = []
    for r in conn.execute("SELECT * FROM src_capex ORDER BY option_id"):
        added = int(r["added_capacity_qty"])
        cond = _requires_check(r["note"])
        cap_ok = added >= target
        chk = checks.get(r["option_id"])
        if r["option_id"] in rejected:
            status = "투자 거절"
        elif not cap_ok:
            status = "미달(용량 부족)"
        elif cond and chk is None:
            status = "조건부 충족(조달 확인 전)"
        elif cond and chk == "불가":
            status = "미달(조달 불가)"
        elif cond and chk == "가능":
            status = "충족(조달 가능 회신)"
        else:
            status = "충족"
        out.append({"option_id": r["option_id"], "option_type": r["option_type"], "target_line": r["target_line"],
                    "capex_krw": int(r["capex_krw"]), "lead_time_weeks": int(r["lead_time_weeks"]),
                    "added_capacity_qty": added, "note": r["note"], "conditions": cond, "check": chk,
                    "status": status, "selectable": status in ("충족", "조건부 충족(조달 확인 전)", "충족(조달 가능 회신)"),
                    "remaining_after": max(target - added, 0)})
    return out


def compare(conn, sid) -> dict | None:
    """재공급 계획 화면용: (b) → (a) → (c) 순차 적용 시 단계별 남는 부족분·영향 고객·지연 금액."""
    plan = plan_row(conn, sid)
    if plan is None:
        return None
    d = _detail(plan)
    rs = d["resupply"]
    p = rs["production"]
    spare, need, upper = p["spare_qty"], p["need_qty"], p["upper_qty"]
    active = d["active_batches"]
    state = plan["state"]
    b = holdings_status(conn, active)
    s0 = max(upper - spare, 0)
    after_b = max(need - spare, 0)

    excluded = [r[0] for r in conn.execute(
        "SELECT customer_id FROM deferral_request WHERE submission_id=? AND state='거절'", (sid,))]
    a = select_deferrals(conn, after_b, excluded)
    agreed = _agreed(conn, sid)
    a_closed = state in (S_C_SELECT, S_C_PROC, S_C_APPROVAL, S_EXHAUSTED) or (
        state == S_CONFIRMED and d.get("confirmed", {}).get("stage") in ("a", "c"))
    a_qty = agreed["qty"] if a_closed else a["selected_qty"]
    after_a = max(after_b - a_qty, 0)
    a_warning = {"qty": a_qty, "periods": round(a_qty / spare, 2) if spare else None,
                 "text": f"{A17}: 연기 {a_qty:,}EA ÷ 기간당 여유 {spare:,}EA ≈ {round(a_qty / spare, 2) if spare else '-'}기간 동안 "
                         "이후 여유량이 연기 물량 생산에 쓰인다"}
    options = capex_options(conn, after_a, d, spare)
    return {
        "plan": dict(plan), "detail": d, "resupply": rs, "active_batches": active, "stage": STAGE_OF.get(state),
        "note": _plan_note(plan).lstrip(" ·"),
        "start": {"upper": upper, "spare": spare, "shortfall": s0},
        "b": {**b, "reduction": upper - need, "after": after_b, "delay_amount": 0,
              "approval": "고객 회신(승인 아님) — 회신 전 미회신분은 상한값(A-8)"},
        "a": {**a, "agreed": agreed, "closed": a_closed, "applied_qty": a_qty, "after": after_a,
              "projected": not a_closed, "warning": a_warning,
              "approval": "영업 승인(영업 임원, 가정 A-10) + 고객별 동의"},
        "c": {"target": after_a, "options": options,
              "best_after": min((o["remaining_after"] for o in options if o["selectable"]), default=after_a),
              "approval": "투자 승인 = CEO/이사회 (승인권한 규정) · 선행 조건이 있으면 조달 확인 먼저"},
    }


# ---------- 단계 진행 ----------

def close_b(conn, sid, actor) -> None:
    """(b) 회신 마감 → 남은 부족분이 있으면 (a)로. 미회신분은 상한값(A-8)으로 남는다."""
    plan = plan_row(conn, sid)
    if plan is None or plan["state"] != S_B:
        raise TransitionError("(b) 회신대기 상태에서만 마감할 수 있음")
    actor = actor.strip()
    if not actor:
        raise TransitionError("마감자는 필수")
    comp = compare(conn, sid)
    t = open_task(conn, sid, "보유량 회신 수집", str(sid))
    if t:
        finish_task(conn, t["id"], actor, f"(b) 회신 마감 — 회신 {comp['b']['pairs'] - comp['b']['unreplied']}/{comp['b']['pairs']}")
    after_b = comp["b"]["after"]
    if comp["a"]["selected"]:
        a = comp["a"]
        new_task(conn, sid, "영업 승인",
                 f"(a) 납기 연기안 승인 요청 — 남은 부족분 {after_b:,}EA · 후보 {len(a['selected'])}건 {a['selected_qty']:,}EA · "
                 f"고객 {len(a['customers'])}곳 · {a['selected_amount'] / 1e8:,.1f}억원 (영업 임원, A-10)",
                 "영업", "plan", str(sid),
                 {"target": after_b, "customers": [c["customer_id"] for c in a["customers"]],
                  "warning": comp["a"]["warning"]["text"], "next": "영업 임원 승인/거절 회신(모의) · " + A17})
        _move(conn, sid, S_A_APPROVAL, "사람", actor, f"(b) 마감: 축소 {comp['b']['reduction']:,}EA → 남은 부족분 {after_b:,}EA → (a) 영업 승인 요청",
              {"after_b": after_b})
    else:
        _move(conn, sid, S_C_SELECT, "사람", actor, f"(b) 마감: 남은 부족분 {after_b:,}EA, 연기 후보 없음 → (c)")
    conn.commit()


def reply_approval(conn, task_id, approve: bool, actor: str, reason: str) -> None:
    task = conn.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
    if task is None or task["type"] not in ("영업 승인", "투자 승인") or task["state"] not in ("대기", "진행"):
        raise TransitionError("회신할 수 있는 승인 Task가 아님")
    actor, reason = actor.strip(), reason.strip()
    if not actor or not reason:
        raise TransitionError("회신자와 사유는 필수")
    sid = task["submission_id"]
    plan = plan_row(conn, sid)
    expected = S_A_APPROVAL if task["type"] == "영업 승인" else S_C_APPROVAL
    if plan["state"] != expected:
        raise TransitionError(f"{expected} 상태가 아님(현재 {plan['state']})")
    finish_task(conn, task_id, actor, f"{'승인' if approve else '거절'}(모의 회신): {reason}")
    if task["type"] == "영업 승인":
        if approve:
            _move(conn, sid, S_A_CONSENT, "사람", actor, f"영업 승인: {reason} → 고객별 납기 연기 동의 요청")
            _reselect(conn, sid)
        else:
            comp = compare(conn, sid)
            _move(conn, sid, S_C_SELECT, "사람", actor, f"영업 거절: {reason} → 남은 부족분 {comp['b']['after']:,}EA 전량 (c)로")
    else:
        d = _detail(plan)
        opt = d.get("c_chosen")
        if approve:
            agreed = _agreed(conn, sid)
            d["confirmed"] = {"stage": "c", "option_id": opt, "a_qty": agreed["qty"]}
            _save_detail(conn, sid, d)
            _move(conn, sid, S_CONFIRMED, "사람", actor, f"투자 승인({opt}): {reason}", stage="c")
        else:
            d.setdefault("c_rejected", []).append(opt)
            _save_detail(conn, sid, d)
            _after_c_failure(conn, sid, actor, f"{opt} 투자 거절: {reason}")
    refresh(conn, sid)
    conn.commit()


def _after_c_failure(conn, sid, actor, reason):
    comp = compare(conn, sid)
    if any(o["selectable"] for o in comp["c"]["options"]):
        _move(conn, sid, S_C_SELECT, "사람", actor, f"{reason} → 다른 증설안 검토")
    else:
        _move(conn, sid, S_EXHAUSTED, "사람", actor,
              f"{reason} → 선택 가능한 증설안 없음 · 남은 부족분 {comp['c']['target']:,}EA — 경영 판단 필요")


def choose_c(conn, sid, option_id, actor) -> None:
    plan = plan_row(conn, sid)
    if plan is None or plan["state"] != S_C_SELECT:
        raise TransitionError("(c) 선택대기 상태에서만 증설안을 고를 수 있음")
    actor = actor.strip()
    if not actor:
        raise TransitionError("선택자는 필수")
    comp = compare(conn, sid)
    opt = next((o for o in comp["c"]["options"] if o["option_id"] == option_id), None)
    if opt is None or not opt["selectable"]:
        raise TransitionError(f"{option_id}는 선택할 수 없음({opt['status'] if opt else '없음'})")
    d = _detail(plan)
    d["c_chosen"] = option_id
    _save_detail(conn, sid, d)
    target = comp["c"]["target"]
    if opt["conditions"] and opt["check"] != "가능":
        new_task(conn, sid, "조달 확인",
                 f"(c) {option_id} 선행 조건 확인 — {' · '.join(opt['conditions'])} 가능 여부 (조달 회신 전 '충족' 확정 안 함)",
                 "조달", "capex", option_id, {**opt, "target": target, "next": "V-205 재계약·처방 재검증 가능 여부 회신(모의)"})
        _move(conn, sid, S_C_PROC, "사람", actor, f"{option_id} 선택 — 조건부 충족 → 조달 확인 요청")
    else:
        _request_investment(conn, sid, opt, target)
        _move(conn, sid, S_C_APPROVAL, "사람", actor, f"{option_id} 선택 → 투자 승인 요청")
    conn.commit()


def _request_investment(conn, sid, opt, target):
    new_task(conn, sid, "투자 승인",
             f"(c) {opt['option_id']} {opt['option_type']} 투자 승인 요청 — {opt['capex_krw'] / 1e8:,.0f}억원 · "
             f"리드타임 {opt['lead_time_weeks']}주 · +{opt['added_capacity_qty']:,}EA · 남은 부족분 {target:,}EA (CEO/이사회)",
             "CEO/이사회", "capex", opt["option_id"], {**opt, "target": target, "next": "투자 승인/거절 회신(모의)"})


def reply_procurement(conn, task_id, feasible: bool, actor: str, reason: str) -> None:
    task = conn.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
    if task is None or task["type"] != "조달 확인" or task["state"] not in ("대기", "진행"):
        raise TransitionError("회신할 수 있는 조달 확인 Task가 아님")
    actor, reason = actor.strip(), reason.strip()
    if not actor or not reason:
        raise TransitionError("회신자와 사유는 필수")
    sid, option_id = task["submission_id"], task["related_id"]
    plan = plan_row(conn, sid)
    if plan["state"] != S_C_PROC:
        raise TransitionError("(c) 조달확인대기 상태가 아님")
    result = "가능" if feasible else "불가"
    finish_task(conn, task_id, actor, f"조달 확인 {result}(모의 회신): {reason}")
    d = _detail(plan)
    d.setdefault("c_checks", {})[option_id] = result
    _save_detail(conn, sid, d)
    record(conn, "capex_check", option_id, "조건부 충족", "충족(조달 가능 회신)" if feasible else "미달(조달 불가)",
           "사람", actor, f"{option_id} 선행 조건(M-B 재계약·처방 재검증) 조달 회신: {result} — {reason}")
    if feasible:
        comp = compare(conn, sid)
        opt = next(o for o in comp["c"]["options"] if o["option_id"] == option_id)
        _request_investment(conn, sid, opt, comp["c"]["target"])
        _move(conn, sid, S_C_APPROVAL, "사람", actor, f"조달 가능 회신 → {option_id} 충족 → 투자 승인 요청")
    else:
        _after_c_failure(conn, sid, actor, f"조달 불가 회신({option_id})")
    conn.commit()


def reply_customer(conn, task_id, agree: bool, actor: str, reason: str) -> None:
    task = conn.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
    if task is None or task["type"] != "납기 연기 동의 요청" or task["state"] not in ("대기", "진행"):
        raise TransitionError("회신할 수 있는 동의 요청 Task가 아님")
    actor, reason = actor.strip(), reason.strip()
    if not actor or not reason:
        raise TransitionError("회신자와 사유는 필수")
    sid, cid = task["submission_id"], task["related_id"]
    if plan_row(conn, sid)["state"] != S_A_CONSENT:
        raise TransitionError("(a) 고객동의대기 상태가 아님")
    result = "동의" if agree else "거절"
    finish_task(conn, task_id, actor, f"고객 {result}(모의 회신): {reason}")
    if not agree:      # 재선정 후 순번이 다시 1부터 매겨지므로, 거절한 요청의 예전 순번과 헷갈리지 않게 표시
        conn.execute("UPDATE task SET title = title || ' → 거절(후보 제외, 이후 순번은 재선정 기준)' WHERE id=?", (task_id,))
    conn.execute("UPDATE deferral_request SET state=?, reply_reason=?, replied_business_date=?"
                 " WHERE submission_id=? AND customer_id=?", (result, reason, current_business_date(conn), sid, cid))
    record(conn, "deferral", f"{sid}/{cid}", "요청", result, "사람", actor, f"납기 연기 {result}(모의 고객 회신): {reason}")
    _reselect(conn, sid, cause=None if agree else f"{cid} 거절 → 해당 고객 주문 제외 후 다음 순번 재선정")
    refresh(conn, sid)
    conn.commit()


def _request_title(c) -> str:
    return (f"{c['customer_id']} {c['customer_name']} 납기 연기 동의 요청 — {c['orders']}건 {c['qty']:,}EA · "
            f"{c['amount'] / 1e8:,.1f}억원 · 연기순번 {c['first_rank']} (모의 회신)")


def _reselect(conn, sid, cause=None) -> None:
    """남은 부족분·거절 고객을 반영해 연기 후보를 다시 고르고, 새로 들어온 고객에게 동의 요청을 만든다.
    모든 요청이 회신되면: 동의 수량 ≥ 부족분 → 대안확정, 아니면 남은 부족분을 (c)로 넘긴다."""
    plan = plan_row(conn, sid)
    if plan["state"] != S_A_CONSENT:
        return
    comp = compare(conn, sid)
    a = comp["a"]
    reqs = {r["customer_id"]: r for r in conn.execute("SELECT * FROM deferral_request WHERE submission_id=?", (sid,))}
    if cause:
        record(conn, "plan", str(sid), plan["state"], plan["state"], *AUTO, cause,
               {"selected_customers": [c["customer_id"] for c in a["customers"]], "selected_qty": a["selected_qty"],
                "covered": a["covered"]})
    selected = {c["customer_id"]: c for c in a["customers"]}
    for cid, c in selected.items():
        r = reqs.get(cid)
        if r is None or r["state"] == "제외":
            tid = new_task(conn, sid, "납기 연기 동의 요청", _request_title(c), "영업", "customer", cid,
                           {"order_ids": c["order_ids"], "qty": c["qty"], "amount": c["amount"],
                            "next": "고객에 납기 연기 동의 요청 → 동의/거절 회신 입력"})
            conn.execute("INSERT OR REPLACE INTO deferral_request VALUES (?,?,?,?,?,'요청',?,NULL,NULL)",
                         (sid, cid, json.dumps(c["order_ids"]), c["qty"], c["amount"], tid))
            record(conn, "deferral", f"{sid}/{cid}", None, "요청", *AUTO,
                   f"연기 후보 선정(순번 {c['first_rank']}) → 동의 요청", {"order_ids": c["order_ids"], "qty": c["qty"]})
        elif r["state"] in ("요청", "동의") and json.loads(r["order_ids"]) != c["order_ids"]:
            conn.execute("UPDATE deferral_request SET order_ids=?, qty=?, amount=? WHERE submission_id=? AND customer_id=?",
                         (json.dumps(c["order_ids"]), c["qty"], c["amount"], sid, cid))
            record(conn, "deferral", f"{sid}/{cid}", r["state"], r["state"], *AUTO,
                   f"재선정으로 연기 주문 변경 → {len(c['order_ids'])}건 {c['qty']:,}EA")
            t = conn.execute("SELECT * FROM task WHERE id=?", (r["task_id"],)).fetchone()
            if t and t["state"] in ("대기", "진행"):
                d = json.loads(t["detail_json"])
                d.update(order_ids=c["order_ids"], qty=c["qty"], amount=c["amount"])
                update_task(conn, t, _request_title(c), d, "재선정 반영")
    for cid, r in reqs.items():
        if cid not in selected and r["state"] == "요청":
            conn.execute("UPDATE deferral_request SET state='제외' WHERE submission_id=? AND customer_id=?", (sid, cid))
            cancel_task(conn, r["task_id"], "부족분 감소로 연기 후보에서 제외")
            record(conn, "deferral", f"{sid}/{cid}", "요청", "제외", *AUTO, "부족분 감소로 연기 후보에서 제외")

    pending = conn.execute("SELECT COUNT(*) FROM deferral_request WHERE submission_id=? AND state='요청'",
                           (sid,)).fetchone()[0]
    if pending:
        return
    agreed = _agreed(conn, sid)
    target = comp["b"]["after"]
    spare = comp["start"]["spare"]
    d = _detail(plan_row(conn, sid))
    if agreed["qty"] >= target:
        d["confirmed"] = {"stage": "a", "qty": agreed["qty"], "orders": agreed["orders"],
                          "periods": round(agreed["qty"] / spare, 2) if spare else None}
        _save_detail(conn, sid, d)
        _move(conn, sid, S_CONFIRMED, *AUTO,
              f"(a) 동의 {agreed['customers']}곳 {agreed['qty']:,}EA ≥ 남은 부족분 {target:,}EA → 납기 연기 확정 · {A17}",
              {"agreed": agreed}, stage="a")
    else:
        _move(conn, sid, S_C_SELECT, *AUTO,
              f"(a) 후보 소진 — 동의 {agreed['qty']:,}EA, 남은 부족분 {target - agreed['qty']:,}EA → (c) 증설 검토",
              {"agreed": agreed})
