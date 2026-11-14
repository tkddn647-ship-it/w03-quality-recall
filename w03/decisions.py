"""결정 대기: 사람이 지금 판단해야 하는 것만 모아 급한 순서로 보여 준다(대시보드 최상단).

급한 순서 = ⓪ 위원회 의결 지연(A-32, 맨 위) · 처리 목표일이 이미 지난 작업(지연) → ① 회수 실행이 멈춘 것(상신·요약·위원회 의결) →
② 오늘·다음 영업일 기한 작업, 추정 납기가 지난 주문 → ③ 사람 확인이 필요한 회신 → ④ 재공급 대안 결정.
같은 순위 안에서는 영향 수량이 큰 것부터. 처리 목표일은 가정 A-28(A05 업무 달력 영업일)이다.
"""
import json

from . import ai, bizcal, resupply
from .db import current_business_date
from .workflow import decision_deadline, pending_decision_batches

URGENCY = {0: "지연", 1: "지금", 2: "기한", 3: "확인", 4: "계획"}
URGENCY_HELP = {
    0: "처리 목표일이 이미 지남(위원회 의결 지연이 맨 위)",
    1: "회수 실행이 멈춰 있음",
    2: "처리 목표일이 오늘·다음 영업일",
    3: "회신이 반영되지 않아 계획이 상한값으로 남아 있음",
    4: "재공급 계획의 다음 단계를 사람이 정해야 함",
}

def _impact(conn, batch_ids):
    m = ",".join("?" * len(batch_ids))
    r = conn.execute(f"SELECT COUNT(DISTINCT customer_id) n, COALESCE(SUM(shipped_qty),0) q FROM shipment_state"
                     f" WHERE batch_id IN ({m})", batch_ids).fetchone()
    return r["n"], r["q"]


def build(conn) -> list[dict]:
    items = []

    def add(rank, kind, title, why, href, qty=0, owner="품질", pin=False):
        items.append({"rank": rank, "urgency": URGENCY[rank], "kind": kind, "title": title, "why": why,
                      "href": href, "qty": qty, "owner": owner, "pin": pin})

    # ① 회수 실행이 멈춘 것: 상신 요약 작성 · 위원회 의결 — 의결 처리 목표(A-32, 상신 후 1영업일)가 지나면 ⓪ 의결 지연
    open_ids = set()
    for s in conn.execute("SELECT * FROM submission WHERE status='상신' ORDER BY id"):
        ids = json.loads(s["batch_ids"])
        decided = {r[0] for r in conn.execute("SELECT batch_id FROM decision WHERE submission_id=?", (s["id"],))}
        pending = pending_decision_batches(conn, s)
        open_ids |= set(ids) - decided
        if not pending:
            continue
        n_cust, qty = _impact(conn, pending)
        href = f"/submissions/{s['id']}"
        dl = decision_deadline(conn, s)
        due = bizcal.label(dl["due"])
        no_summary = s["ai_status"] not in ai.DECIDABLE
        stale = " · 수치 변경으로 요약 갱신 필요(판정 근거는 최신값)" if json.loads(s["ai_check"] or "{}").get("stale") else ""
        if dl["late"]:
            reason = json.loads(s["ai_check"] or "{}").get("reason", "요약 없음")
            add(0, "의결 지연", f"{s['kind']} #{s['id']} 의결 지연 {dl['late']}영업일 — {', '.join(pending)}",
                f"처리 목표 {due}(상신 {bizcal.label(dl['submitted'])} + {bizcal.DECISION_DUE_BDAYS}영업일, A-32)에서 "
                f"{dl['late']}영업일 지남 — " + (f"요약 없음(AI 요약 실패: {reason}) → 요약 작성부터 · " if no_summary else "")
                + ("회수 해제 여부가 정해지지 않음" if s["kind"] == "재상신"
                   else f"고객 {n_cust}곳 출하분 {qty:,}EA의 회수요청·사내재고 출하보류가 시작되지 않음"),
                href + ("" if no_summary else "#decide"), qty, "품질위원회", pin=True)
            continue
        when = f"의결 처리 목표 {'오늘 ' if dl['left'] == 0 else ''}{due}(상신 후 {bizcal.DECISION_DUE_BDAYS}영업일, A-32)"
        if no_summary:
            reason = json.loads(s["ai_check"] or "{}").get("reason", "요약 없음")
            add(1, "상신 요약 작성", f"{s['kind']} #{s['id']} 요약을 사람이 작성해야 함",
                f"AI 요약 실패({reason}) — 요약이 없으면 위원회가 의결할 수 없음 · {when}", href, qty)
        elif s["kind"] == "재상신":
            add(1, "위원회 의결", f"재상신 #{s['id']} 회수 해제 여부 의결 — {', '.join(pending)}",
                f"정정 후 기준 미달 — 의결 전까지 해당 배치의 고객 통지·재고 격리 Task가 보류(재검토 대기) · {when}",
                href + "#decide", qty, "품질위원회")
        else:
            add(1, "위원회 의결", f"회수상신 #{s['id']} 의결 — {', '.join(pending)}",
                f"의결 전이라 고객 {n_cust}곳 출하분 {qty:,}EA의 회수요청·사내재고 출하보류가 시작되지 않음 · {when}{stale}",
                href + "#decide", qty, "품질위원회")

    for state, kind, why in (
            ("회수대상", "회수대상 상신", "회수 기준(R1) 충족 — 상신해야 위원회가 의결할 수 있음"),
            ("재검토", "재상신", "정정 후 기준 미달 — 재상신해 회수 해제 여부를 의결받아야 함")):
        rows = [r for r in conn.execute(
            "SELECT s.batch_id, a.rate_prod, a.rate_ship FROM batch_state s LEFT JOIN assessment a USING (batch_id)"
            " WHERE s.state=? ORDER BY s.batch_id", (state,)) if r["batch_id"] not in open_ids]
        if rows:
            ids = [r["batch_id"] for r in rows]
            rates = ", ".join(f"{r['batch_id']} {r['rate_prod']:.2f}%/{r['rate_ship']:.2f}%" for r in rows
                              if r["rate_prod"] is not None)
            n_cust, qty = _impact(conn, ids)
            add(1, kind, f"{kind} {len(ids)}건 — {', '.join(ids)}", f"{why} ({rates}; 고객 {n_cust}곳 {qty:,}EA)",
                "/#submit", qty)

    # ⓪ 지연 · ② 기한: 처리 목표일(A-28, 영업일)과 현재 업무일 비교. 추정 납기가 이미 지난 주문(A-11·A-12)도 ②.
    today = current_business_date(conn)
    groups: dict[tuple, dict] = {}
    for t in conn.execute("SELECT * FROM task WHERE state IN ('대기', '진행') AND due_date IS NOT NULL ORDER BY due_date, id"):
        left = bizcal.business_days_between(today, t["due_date"])
        detail = json.loads(t["detail_json"] or "{}")
        overdue_orders = len(detail.get("orders", [])) if t["type"] == "주문 상태 확인 요청" else 0
        if left < 0:
            key = ("지연", t["type"])
        elif left <= 1 or overdue_orders:
            key = ("기한", t["type"])
        else:
            continue
        g = groups.setdefault(key, {"dept": t["dept"], "ids": [], "due": t["due_date"], "left": left,
                                    "why": bizcal.due_rule(t["type"])[1], "orders": overdue_orders})
        g["ids"].append(t["id"])
    for (kind, ttype), g in sorted(groups.items(), key=lambda kv: (kv[1]["left"], kv[0][1])):
        due = bizcal.label(g["due"])
        if kind == "지연":
            add(0, "지연 작업", f"[{g['dept']}] {ttype} {len(g['ids'])}건 — 기한 지남",
                f"처리 목표 {due}에서 {-g['left']}영업일 지남(A-28) — {g['why']}", f"/#task-{g['ids'][0]}", owner=g["dept"])
            continue
        if g["left"] <= 1:
            when = f"처리 목표 {'오늘' if g['left'] == 0 else '다음 영업일'} {due}(A-28)"
        else:
            when = f"처리 목표 {due}(A-28)"
        if g["orders"]:
            when = f"추정 납기 이미 지난 주문 {g['orders']}건(A-11·A-12) · {when}"
        add(2, "기한 임박 작업", f"[{g['dept']}] {ttype} {len(g['ids'])}건", f"{when} — {g['why']}",
            f"/#task-{g['ids'][0]}", owner=g["dept"])

    # ③ 사람 확인이 필요한 회신
    for r in conn.execute("SELECT * FROM reply_intake WHERE status IN ('사람 확인 필요', '검증 통과') ORDER BY id"):
        v = json.loads(r["validation_json"] or "{}")
        task = conn.execute("SELECT title FROM task WHERE id=?", (r["task_id"],)).fetchone()
        who = task["title"].split(" — ")[0] if task else f"Task #{r['task_id']}"
        if r["status"] == "검증 통과":
            add(3, "회신 반영", f"회신 #{r['id']} {r['task_type']} 반영 — {who}",
                "검증 통과 — 사람이 [반영]해야 재공급 필요량·상태에 들어감", f"/intakes/{r['id']}")
        else:
            first = (v.get("problems") or ["사유 미상"])[0]
            add(3, "회신 확인", f"회신 #{r['id']} {r['task_type']} 사람 확인 — {who}",
                f"{first} — 확인·반영 전까지 계획은 상한값(A-8) 기준", f"/intakes/{r['id']}")

    # ④ 재공급 대안
    for p in conn.execute("SELECT * FROM resupply_plan ORDER BY submission_id"):
        sid, state = p["submission_id"], p["state"]
        href = f"/plans/{sid}"
        if state == resupply.S_EXHAUSTED:
            add(2, "재공급 대안", f"재공급 계획 #{sid} 대안 소진", "모든 대안을 적용해도 부족분이 남음 — 수량 축소 협의·일정 재약속 등 경영 판단 필요",
                href, owner="경영")
        elif state == resupply.S_C_SELECT:
            c = resupply.compare(conn, sid)
            target = c["c"]["target"] if c else 0
            add(4, "재공급 대안 선택", f"재공급 계획 #{sid} 증설안 선택",
                f"(b)·(a) 적용 후에도 {target:,}EA 부족 — 증설안(c)을 골라야 투자 승인 요청으로 넘어감", href, target)
        elif state == resupply.S_B:
            c = resupply.compare(conn, sid)
            if c:
                b = c["b"]
                add(4, "재공급 대안 선택", f"재공급 계획 #{sid} 보유량 회신 마감 판단",
                    f"고객 보유량 회신 {b['pairs'] - b['unreplied']}/{b['pairs']} — 마감해야 남은 부족분 "
                    f"{b['after']:,}EA로 납기 연기(a) 단계를 시작", href, b["after"], "영업")

    items.sort(key=lambda i: (i["rank"], not i["pin"], -i["qty"]))   # 의결 지연은 지연 작업보다도 위
    for n, i in enumerate(items, 1):
        i["no"] = n
    return items
