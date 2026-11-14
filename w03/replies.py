"""AI ② 회신 구조화 (DESIGN.md §3, §7).

자유 형식 회신 접수 → AI가 Task 종류별 스키마로 구조화 → 규칙 엔진 검증
→ 통과 시에만 사람이 [반영] → 상태 갱신. 실패하면 '사람 확인 필요'(사유 표시) → 사람이 구조화 폼을 채운다.
원문·AI 결과·검증 결과·반영자를 모두 이력에 남긴다.
"""
import json
import re

from . import ai, resupply, workflow
from .core import TransitionError, finish_task
from .db import current_business_date, now, record

PASS, REVIEW, APPLIED = "검증 통과", "사람 확인 필요", "반영"

SPECS = {
    "고객 통지": {
        "label": "고객 회수·보유량 회신",
        "fields": {"customer_id": "str", "batch_id": "str", "held_qty": "int", "recall_agree": "bool", "pickup_date": "str"},
        "labels": {"customer_id": "고객", "batch_id": "배치", "held_qty": "보유수량", "recall_agree": "회수동의",
                   "pickup_date": "수거가능일"},
        "required": ["customer_id", "batch_id", "held_qty", "recall_agree"],
        "example": "C-101입니다. B-02-2602-01 배치 재고 12,000개 보유 중이고 회수 동의합니다. 다음 주 화요일 수거 가능.",
    },
    "출하 경로 확인 요청": {
        "label": "출하 경로 확인 회신",
        "fields": {"customer_id": "str", "batch_id": "str", "shipped": "bool", "qty": "int", "note": "str"},
        "labels": {"customer_id": "고객", "batch_id": "배치", "shipped": "출하여부", "qty": "출하수량", "note": "비고"},
        "required": ["customer_id", "batch_id", "shipped"],
        "example": "C-105 확인 결과 해당 배치 3,000개가 대리점 경유로 출하된 것이 맞습니다.",
    },
    "조달 확인": {
        "label": "증설안 선행 조건 조달 회신",
        "fields": {"option_id": "str", "feasible": "bool", "reason": "str"},
        "labels": {"option_id": "증설안", "feasible": "가능여부", "reason": "사유"},
        "required": ["option_id", "feasible"],
        "example": "OPT-02 관련 V-205와 재계약 협의 완료, 처방 재검증 6주 내 가능합니다.",
    },
    "추가검사": {
        "label": "추가검사 회신",
        "fields": {"batch_id": "str", "result": ["합격", "불합격"], "inspected_qty": "int", "defect_qty": "int"},
        "labels": {"batch_id": "배치", "result": "판정", "inspected_qty": "검사수량", "defect_qty": "불량수량"},
        "required": ["batch_id", "result", "inspected_qty"],
        "example": "B-02-2602-03 잔여 10,483개 전수 검사 결과 규격 이상 없음, 합격입니다.",
    },
    "납기 연기 동의 요청": {
        "label": "납기 연기 동의 회신",
        "fields": {"customer_id": "str", "agree": "bool", "reason": "str"},
        "labels": {"customer_id": "고객", "agree": "동의여부", "reason": "사유"},
        "required": ["customer_id", "agree"],
        "example": "C-102입니다. 9월 판촉 전까지만 들어오면 되니 연기 동의합니다.",
    },
    "사내재고 격리": {
        "label": "사내재고 격리 회신",
        "fields": {"batch_id": "str", "qty": "int"},
        "labels": {"batch_id": "배치", "qty": "격리수량"},
        "required": ["batch_id", "qty"],
        "example": "B-02-2602-01 창고 재고 4,700개 격리 완료했습니다.",
    },
}


def context(conn, task) -> dict:
    """검증 기준: Task가 기대하는 고객·배치·수량 한도."""
    d = json.loads(task["detail_json"])
    t = task["type"]
    if t == "고객 통지":
        live = [p["batch_id"] for p in d["parts"] if not p.get("held") and not p.get("removed")]
        shipped = {b: conn.execute("SELECT COALESCE(SUM(shipped_qty),0) FROM shipment_state WHERE batch_id=? AND customer_id=?",
                                   (b, d["customer_id"])).fetchone()[0] for b in live}
        return {"customer_id": d["customer_id"], "batches": live, "shipped_by_batch": shipped}
    if t == "출하 경로 확인 요청":
        return {"customer_id": d["customer_id"], "batches": [d["batch_id"]],
                "max_qty": workflow.unlinked_limit(conn, d["batch_id"], d["max_qty"])}
    if t == "조달 확인":
        return {"option_id": task["related_id"]}
    if t == "추가검사":
        return {"batches": [d["batch_id"]], "produced_qty": d["produced_qty"], "est_qty": d["est_qty"]}
    if t == "납기 연기 동의 요청":
        return {"customer_id": task["related_id"]}
    if t == "사내재고 격리":
        inv = conn.execute("SELECT * FROM inventory_hold WHERE hold_id=?", (task["related_id"],)).fetchone()
        produced = int(conn.execute("SELECT produced_qty FROM src_batches WHERE batch_id=?", (inv["batch_id"],)).fetchone()[0])
        return {"batches": [inv["batch_id"]], "produced_qty": produced, "est_qty": inv["est_qty"]}
    raise TransitionError(f"{t} Task는 회신 구조화 대상이 아님")


def _digits(raw: str) -> set[int]:
    return {int(x.replace(",", "")) for x in re.findall(r"\d[\d,]*", raw or "")}


def validate(task_type: str, data: dict, ctx: dict, raw: str | None, source: str) -> dict:
    """규칙 엔진 검증. 문제가 하나라도 있으면 '사람 확인 필요'."""
    spec = SPECS[task_type]
    data = {k: data.get(k) for k in spec["fields"]}
    lab = spec["labels"]
    problems, notes = [], []

    if "batches" in ctx and "batch_id" in spec["fields"]:
        if data["batch_id"] is None and len(ctx["batches"]) == 1:
            data["batch_id"] = ctx["batches"][0]
            notes.append(f"배치 미기재 → Task 대상 배치가 하나뿐이라 {data['batch_id']}로 지정(가정 A-24)")
        elif data["batch_id"] is None and len(ctx["batches"]) > 1:
            problems.append(f"배치 특정 불가: Task 대상 배치가 {len(ctx['batches'])}개({', '.join(ctx['batches'])})")
        elif data["batch_id"] not in ctx["batches"]:
            problems.append(f"배치 불일치: 회신 {data['batch_id']} ≠ Task 대상 {', '.join(ctx['batches']) or '없음'}")
    for f in spec["required"]:
        if data[f] is None and not (f == "batch_id" and any("배치" in p for p in problems)):
            problems.append(f"필수 항목 누락: {lab[f]}")
    for key in ("customer_id", "option_id"):
        if key in ctx and data.get(key) is not None and data[key] != ctx[key]:
            problems.append(f"{lab[key]} 불일치: 회신 {data[key]} ≠ Task {ctx[key]}")

    if task_type == "고객 통지" and data["held_qty"] is not None and data["batch_id"] in ctx.get("shipped_by_batch", {}):
        lim = ctx["shipped_by_batch"][data["batch_id"]]
        if not 0 <= data["held_qty"] <= lim:
            problems.append(f"수량 초과: 보유수량 {data['held_qty']:,} > {data['batch_id']} 출하량 {lim:,}")
    if task_type == "출하 경로 확인 요청" and data["shipped"]:
        if data["qty"] is None:
            problems.append("필수 항목 누락: 출하수량(출하 확인 시)")
        elif not 0 < data["qty"] <= ctx["max_qty"]:
            problems.append(f"수량 초과: 출하수량 {data['qty']:,} > 생산량−SBL 출하 합 잔여 {ctx['max_qty']:,}")
    if task_type == "추가검사":
        if data["inspected_qty"] is not None and not 0 < data["inspected_qty"] <= ctx["produced_qty"]:
            problems.append(f"수량 초과: 검사수량 {data['inspected_qty']:,} > 생산량 {ctx['produced_qty']:,}")
        if data["result"] == "불합격" and (data["defect_qty"] is None or not 0 < data["defect_qty"] <= (data["inspected_qty"] or 0)):
            problems.append("불합격이면 불량수량(1 이상, 검사수량 이하)이 필요")
        if data["result"] == "합격" and data["defect_qty"]:
            problems.append("합격인데 불량수량이 있음 — 판정 확인 필요")
    if task_type == "사내재고 격리" and data["qty"] is not None and not 0 <= data["qty"] <= ctx["produced_qty"]:
        problems.append(f"수량 초과: 격리수량 {data['qty']:,} > 생산량 {ctx['produced_qty']:,}")

    if source in ai.AI_SOURCES:   # AI가 원문에 없는 숫자를 만들어 냈는지
        nums = _digits(raw)
        for f, kind in spec["fields"].items():
            if kind == "int" and data[f] is not None and data[f] not in nums:
                problems.append(f"원문에 없는 수량: {lab[f]} {data[f]:,} (AI 추정 가능성)")
    return {"ok": not problems, "problems": problems, "notes": notes, "data": data}


def intake(conn, task_id: int, raw_text: str, submitted_by: str) -> int:
    task = conn.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
    if task is None or task["type"] not in SPECS:
        raise TransitionError("회신 구조화 대상 Task가 아님")
    if task["state"] not in ("대기", "진행"):
        raise TransitionError(f"Task가 {task['state']} 상태 — 회신을 받을 수 없음")
    raw_text, submitted_by = raw_text.strip(), submitted_by.strip()
    if not raw_text or not submitted_by:
        raise TransitionError("회신 원문과 회신자는 필수")
    spec = SPECS[task["type"]]
    ctx = context(conn, task)
    data, info = ai.structure_reply(task["type"], spec["fields"], ctx, raw_text)
    if data is None:
        source, v, status = None, {"ok": False, "problems": [f"AI 구조화 실패: {info['reason']} — 구조화 폼을 직접 채워 주세요"],
                                   "notes": [], "data": {k: None for k in spec["fields"]}}, REVIEW
    else:
        source = info["provider"]
        v = validate(task["type"], data, ctx, raw_text, source)
        status = PASS if v["ok"] else REVIEW
    cur = conn.execute(
        "INSERT INTO reply_intake (task_id, task_type, raw_text, submitted_by, created_at, structured_json, source,"
        " ai_info, validation_json, status, business_date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, task["type"], raw_text, submitted_by, now(), json.dumps(data, ensure_ascii=False) if data else None,
         source, json.dumps(info, ensure_ascii=False), json.dumps(v, ensure_ascii=False), status,
         current_business_date(conn)))
    iid = str(cur.lastrowid)
    record(conn, "reply", iid, None, "접수", "사람", submitted_by, f"Task #{task_id}({task['type']}) 자유 형식 회신 접수(모의)",
           {"raw_text": raw_text})
    if data is None:
        record(conn, "reply", iid, "접수", "구조화 실패", "AI", "회신 구조화기", info["reason"], info)
    else:
        record(conn, "reply", iid, "접수", "AI 구조화", "AI", f"{ai.label(source)} {info.get('model', '')}",
               "Task 종류별 스키마로 구조화" + (f" · 503 자동 재시도 {info['retries']}회" if info.get("retries") else ""),
               {"structured": data})
    record(conn, "reply", iid, "AI 구조화" if data else "구조화 실패", status, "시스템", "규칙엔진",
           "검증 통과 — 사람 반영 대기" if v["ok"] else "검증 실패: " + "; ".join(v["problems"]), v)
    conn.commit()
    return int(iid)


_BOOL = {"true": True, "false": False, "예": True, "아니오": False, "": None}


def manual_structure(conn, intake_id: int, form: dict, actor: str) -> None:
    """AI 실패·검증 실패 시 사람이 구조화 폼을 직접 채운다(다시 검증)."""
    row = conn.execute("SELECT * FROM reply_intake WHERE id=?", (intake_id,)).fetchone()
    if row is None or row["status"] == APPLIED:
        raise TransitionError("수정할 수 있는 회신이 아님")
    actor = actor.strip()
    if not actor:
        raise TransitionError("작성자는 필수")
    spec = SPECS[row["task_type"]]
    data = {}
    for f, kind in spec["fields"].items():
        v = (form.get(f) or "").strip()
        if kind == "int":
            if v and not v.replace(",", "").isdigit():
                raise TransitionError(f"{spec['labels'][f]}은(는) 0 이상의 정수")
            data[f] = int(v.replace(",", "")) if v else None
        elif kind == "bool":
            data[f] = _BOOL.get(v.lower(), None)
        else:
            data[f] = v or None
    task = conn.execute("SELECT * FROM task WHERE id=?", (row["task_id"],)).fetchone()
    v = validate(row["task_type"], data, context(conn, task), row["raw_text"], "manual")
    status = PASS if v["ok"] else REVIEW
    conn.execute("UPDATE reply_intake SET structured_json=?, source='manual', validation_json=?, status=? WHERE id=?",
                 (json.dumps(data, ensure_ascii=False), json.dumps(v, ensure_ascii=False), status, intake_id))
    record(conn, "reply", str(intake_id), row["status"], status, "사람", actor,
           "사람이 구조화 폼 작성 → " + ("검증 통과" if v["ok"] else "검증 실패: " + "; ".join(v["problems"])), v)
    conn.commit()


def apply(conn, intake_id: int, actor: str) -> None:
    """검증 통과 회신만 사람이 [반영] → 해당 Task의 회신 처리 함수로 상태 갱신."""
    row = conn.execute("SELECT * FROM reply_intake WHERE id=?", (intake_id,)).fetchone()
    if row is None or row["status"] != PASS:
        raise TransitionError("검증 통과 상태의 회신만 반영할 수 있음")
    actor = actor.strip()
    if not actor:
        raise TransitionError("반영자는 필수")
    d = json.loads(row["validation_json"])["data"]
    task = conn.execute("SELECT * FROM task WHERE id=?", (row["task_id"],)).fetchone()
    t, tid, why = row["task_type"], row["task_id"], f"회신 #{intake_id} 반영"
    if t == "고객 통지":
        workflow.record_customer_holding(conn, d["batch_id"], d["customer_id"], d["held_qty"], actor, why)
        task = conn.execute("SELECT * FROM task WHERE id=?", (tid,)).fetchone()
        td = json.loads(task["detail_json"])
        live = [p["batch_id"] for p in td["parts"] if not p.get("held") and not p.get("removed")]
        replied = {r[0] for r in conn.execute("SELECT batch_id FROM customer_holding WHERE customer_id=?", (d["customer_id"],))}
        if task["state"] in ("대기", "진행") and set(live) <= replied:
            finish_task(conn, tid, actor, f"고객 회신 반영 완료(회수동의 {d['recall_agree']}, 수거 {d['pickup_date'] or '-'})")
    elif t == "출하 경로 확인 요청":
        workflow.record_unlinked_reply(conn, tid, bool(d["shipped"]), d["qty"], actor, d.get("note") or why)
    elif t == "조달 확인":
        resupply.reply_procurement(conn, tid, bool(d["feasible"]), actor, d.get("reason") or why)
    elif t == "추가검사":
        workflow.record_inspection(conn, tid, d["result"] == "합격", d["inspected_qty"], d["defect_qty"], actor, why)
    elif t == "납기 연기 동의 요청":
        resupply.reply_customer(conn, tid, bool(d["agree"]), actor, d.get("reason") or why)
    elif t == "사내재고 격리":
        workflow.record_quarantine(conn, tid, d["qty"], actor, why)
    conn.execute("UPDATE reply_intake SET status=?, applied_by=?, applied_at=? WHERE id=?", (APPLIED, actor, now(), intake_id))
    record(conn, "reply", str(intake_id), PASS, APPLIED, "사람", actor, f"검증 통과 회신을 반영 → Task #{tid}({t}) 처리", d)
    conn.commit()
