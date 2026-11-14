"""판정 · 상신 · 배치별 의결 · 의결 후속 처리 (DESIGN.md §1~§5)."""
import json

from . import ai, bizcal, resupply, rules
from .config import AS_OF, RECALL_THRESHOLD_PCT
from .core import (AUTO, HOLD, OPEN_TASK_STATES, SYSTEM_ACTOR, TransitionError, cancel_task, finish_task,
                   hold_task, marks, new_task, open_task, resume_task, set_state, update_task)
from .db import current_business_date, now, record, set_business_date

__all__ = ["TransitionError"]


# ---------- 판정 ----------

def refresh_assessment(conn) -> dict[str, dict]:
    """유효 클레임(원본+정정)으로 전 배치 불량률을 다시 계산해 assessment 테이블에 쓴다."""
    results = rules.assess_batches(conn)
    ts = now()
    conn.execute("DELETE FROM assessment")
    conn.executemany(
        "INSERT INTO assessment VALUES (:batch_id,:confirmed_claims,:confirmed_qty,:produced_qty,:shipped_qty,"
        ":rate_prod,:rate_ship,:recall_prod,:recall_ship,:is_target,:warning,:computed_at)",
        [{**r, "computed_at": ts} for r in results],
    )
    return {r["batch_id"]: r for r in results}


def run_assessment(conn) -> dict:
    """R1 판정 → 회수대상 전이, R2·R6 → 조사대상 플래그(상태는 바꾸지 않음)."""
    results = refresh_assessment(conn)
    targets = [r for r in results.values() if r["is_target"]]
    newly = []
    for r in targets:
        state = conn.execute("SELECT state FROM batch_state WHERE batch_id = ?", (r["batch_id"],)).fetchone()["state"]
        if state == "정상":
            set_state(conn, "batch", r["batch_id"], "회수대상", *SYSTEM_ACTOR,
                      f"R1 확정 불량률 ≥ {RECALL_THRESHOLD_PCT:g}% (계산 정의서 1.9, 품질 정책)",
                      evidence(r))
            newly.append(r["batch_id"])

    flags = update_flags(conn, {r["batch_id"] for r in targets})
    conn.commit()
    return {"targets": [r["batch_id"] for r in targets], "newly_targeted": newly,
            "flagged": sum(1 for v in flags.values() if v)}


def update_flags(conn, target_ids: set[str]) -> dict:
    """R2·R6 조사대상 플래그를 다시 계산한다(배치 상태는 바꾸지 않음)."""
    flags = rules.investigation_flags(conn, target_ids)
    for row in conn.execute("SELECT batch_id, investigate_flag, investigate_reason FROM batch_state").fetchall():
        reasons = "; ".join(flags.get(row["batch_id"], []))
        on = 1 if reasons else 0
        if on != row["investigate_flag"] or reasons != row["investigate_reason"]:
            conn.execute("UPDATE batch_state SET investigate_flag = ?, investigate_reason = ? WHERE batch_id = ?",
                         (on, reasons, row["batch_id"]))
            record(conn, "batch_flag", row["batch_id"], "조사대상" if row["investigate_flag"] else "-",
                   "조사대상" if on else "-", *SYSTEM_ACTOR,
                   "R2/R6 조사대상 플래그 갱신 (배치 상태는 변경하지 않음, 규칙 우선순위 R1 > R2·R6)",
                   {"reasons": flags.get(row["batch_id"], [])})
    return flags


def evidence(r: dict) -> dict:
    return {
        "confirmed_claims": r["confirmed_claims"], "confirmed_qty": r["confirmed_qty"],
        "produced_qty": r["produced_qty"], "shipped_qty": r["shipped_qty"],
        "rate_prod_pct": _r(r["rate_prod"]), "rate_ship_pct": _r(r["rate_ship"]),
        "recall_by_prod": bool(r["recall_prod"]), "recall_by_ship": bool(r["recall_ship"]),
        "threshold_pct": RECALL_THRESHOLD_PCT, "warning": r["warning"],
        "rule": "회수대상 = 확정 AND (생산량 기준 OR 출하량 기준 ≥ 10%)  [A-2]",
    }


def _r(v):
    return None if v is None else round(v, 2)


def build_facts(conn, batch_ids: list[str]) -> dict:
    """AI·위원회가 보는 사실 묶음. 모든 수치는 규칙 엔진 계산값."""
    targets = []
    for r in conn.execute(
        f"""SELECT a.*, b.product_id, b.production_line, b.production_date, b.input_lot_ids lot_id, b.qc_result
            FROM assessment a JOIN src_batches b USING (batch_id) WHERE a.batch_id IN ({marks(batch_ids)})
            ORDER BY a.batch_id""", batch_ids):
        d = dict(r)
        d["rate_prod"], d["rate_ship"] = _r(d["rate_prod"]), _r(d["rate_ship"])
        targets.append(d)
    lots = sorted({t["lot_id"] for t in targets})
    facts = {
        "as_of": AS_OF,
        "rule": "회수대상 = 규격이탈 확정 클레임 기준 배치 불량률 10% 이상 (생산량·출하량 분모 중 하나라도) — 실행은 품질위원회 의결",
        "targets": targets,
        "lots": lots,
        "lot_info": [dict(r) for r in conn.execute(f"SELECT * FROM src_lots WHERE lot_id IN ({marks(lots)})", lots)],
        # 원인 조사 단서(날짜 비교만) — 원인 판정이 아님. A05 알려진 한계 가능성 포함, 확인 필요.
        "lot_clues": [{"lot_id": c["lot_id"], "status": c["label"], "clues": c["clues"]}
                      for c in (rules.lot_clues(conn, lot) for lot in lots) if c],
        "same_lot_batches": [r["batch_id"] for r in conn.execute(
            f"SELECT batch_id FROM src_batches WHERE input_lot_ids IN ({marks(lots)})"
            f" AND batch_id NOT IN ({marks(batch_ids)}) ORDER BY batch_id", lots + batch_ids)],
        "impact": rules.impact(conn, batch_ids),
        "resupply": rules.resupply_check(conn, batch_ids),
        "corrections": [dict(r) for r in conn.execute(
            f"SELECT * FROM claim_correction WHERE batch_id IN ({marks(batch_ids)}) ORDER BY id", batch_ids)],
        "gaps": [
            "SBL(품질 출하)과 SH(매출 출하)는 연결 키가 없어 SBL 밖 출하는 배치를 확정할 수 없음",
            "사내 잔여재고는 생산량 − SBL 출하 합 추정치(실재고 자료 없음, 가정 A-5)",
            "로트 투입량이 입고량을 초과·투입일이 입고일보다 빠른 기록 존재(A05 알려진 한계)",
        ],
        "policy": {"R1": "회수 대상 = 규격이탈 확정 건, 배치 불량률 10% 이상(품질 정책)",
                   "R2": "미확정(보류) 건은 조사·소명 대상, 즉시 회수 대상 아님(품질 정책)",
                   "R3": "회수 실행 = 품질위원회 의결(승인권한 규정)",
                   "R4": "요건 충족 시 원인 규명을 기다리지 않음, 원인 조사는 별도 트랙(품질 정책)",
                   "R5": "언론 보도는 회수 근거 아님(품질 정책)",
                   "R6": "같은 원료 로트를 쓴 다른 배치는 회수가 아니라 조사대상(가정 A-3)"},
    }
    facts["request"] = request_line(facts)
    return facts


def request_line(facts: dict, kind: str = "회수상신") -> dict:
    """상신 화면 맨 위 '요청 사항' 한 줄 — 무엇을 승인해 달라는지 · 영향 · 승인 시 공급 판정(모두 계산값)."""
    ids = [t["batch_id"] for t in facts["targets"]]
    imp = facts["impact"]
    n_cust, qty = len(imp["customers"]), sum(c["qty"] for c in imp["customers"])
    inv = sum(i["est_qty"] for i in imp["inventory"])
    supply = facts["resupply"]["overall"]
    if kind == "재상신":
        text = f"{'·'.join(ids)} {len(ids)}개 배치 회수 해제 승인 요청 — 정정 후 확정 불량률 두 분모 모두 10% 미만 · 고객 {n_cust}곳 {qty:,}EA 회수요청 철회 대상"
    else:
        text = (f"{'·'.join(ids)} {len(ids)}개 배치 회수 실행 승인 요청 — 영향 고객 {n_cust}곳·{qty:,}EA, "
                f"사내재고 추정 {inv:,}EA 출하보류 · 승인 시 공급 판정 {supply}")
    return {"headline": text, "batch_ids": ids, "batch_count": len(ids), "customer_count": n_cust,
            "customer_qty": qty, "inventory_est_qty": inv, "supply_result": supply}


# ---------- 상신 ----------

def submission_business_date(conn, sid: int) -> str | None:
    r = conn.execute("SELECT business_date FROM history WHERE entity_type='submission' AND entity_id=? ORDER BY id LIMIT 1",
                     (str(sid),)).fetchone()
    return r[0] if r else None


def decision_deadline(conn, sub) -> dict:
    """위원회 의결 처리 목표(가정 A-32: 상신 업무일 + 1영업일)와 현재 업무일 기준 지연 영업일."""
    submitted = submission_business_date(conn, sub["id"]) or current_business_date(conn)
    due = bizcal.add_business_days(submitted, bizcal.DECISION_DUE_BDAYS)
    today = current_business_date(conn)
    late = max(bizcal.business_days_between(due, today), 0)
    return {"submitted": submitted, "due": due, "late": late, "left": bizcal.business_days_between(today, due)}


def pending_decision_batches(conn, sub) -> list[str]:
    """상신 중 아직 의결되지 않았고 의결 대상 상태(회수대상/재검토)인 배치."""
    decided = {r[0] for r in conn.execute("SELECT batch_id FROM decision WHERE submission_id=?", (sub["id"],))}
    want = "재검토" if sub["kind"] == "재상신" else "회수대상"
    return [b for b in json.loads(sub["batch_ids"]) if b not in decided and
            conn.execute("SELECT state FROM batch_state WHERE batch_id=?", (b,)).fetchone()[0] == want]


def _open_submission_batches(conn) -> set[str]:
    ids = set()
    for r in conn.execute("SELECT id, batch_ids FROM submission WHERE status = '상신'"):
        decided = {d[0] for d in conn.execute("SELECT batch_id FROM decision WHERE submission_id=?", (r["id"],))}
        ids |= set(json.loads(r["batch_ids"])) - decided
    return ids


def create_submission(conn, use_ai: bool = True) -> int | None:
    """회수대상 중 아직 상신되지 않은 배치를 묶어 위원회에 상신한다(AI 요약 시도)."""
    pending = [r["batch_id"] for r in conn.execute(
        "SELECT batch_id FROM batch_state WHERE state = '회수대상' ORDER BY batch_id")]
    open_ids = _open_submission_batches(conn)
    batch_ids = [b for b in pending if b not in open_ids]
    if not batch_ids:
        return None
    facts = build_facts(conn, batch_ids)
    if use_ai:
        summary, source, status, info = ai.committee_summary(facts)
    else:
        summary, source, status, info = None, None, ai.STATUS_FAIL, {"reason": "시연 프리셋 — AI 호출 생략(비용 없음)"}
    cur = conn.execute(
        "INSERT INTO submission (kind, created_at, batch_ids, facts_json, ai_json, ai_source, ai_status, ai_check)"
        " VALUES ('회수상신',?,?,?,?,?,?,?)",
        (now(), json.dumps(batch_ids), json.dumps(facts, ensure_ascii=False),
         json.dumps(summary, ensure_ascii=False) if summary else None, source, status,
         json.dumps(info, ensure_ascii=False)),
    )
    sid = cur.lastrowid
    record(conn, "submission", str(sid), None, "상신", *SYSTEM_ACTOR,
           "회수대상 배치 품질위원회 상신 (R3: 실행은 위원회 의결)", {"batch_ids": batch_ids})
    if source in ai.AI_SOURCES:
        record(conn, "submission_summary", str(sid), None, status, "AI", f"{ai.label(source)} {info.get('model', '')}",
               "상신 요약 생성 · 수치·표현 대조 " + ("통과" if status == ai.STATUS_OK else f"{status} {len(info['problems'])}건")
               + (f" · 표현 경고로 자동 재생성 {info['wording_retries']}회" if info.get("wording_retries") else "")
               + (f" · 503 자동 재시도 {info['retries']}회" if info.get("retries") else ""),
               info)
    else:
        record(conn, "submission_summary", str(sid), None, status, "시스템", "AI 요약기",
               f"AI 요약 없음 → 수동 작성 필요: {info['reason']}", info)
    conn.commit()
    return sid


def create_resubmission(conn) -> int | None:
    """정정 후 기준 미달로 재검토가 된 배치를 위원회에 재상신한다(회수 해제 여부). 요약은 규칙 기반."""
    pending = [r["batch_id"] for r in conn.execute(
        "SELECT batch_id FROM batch_state WHERE state = '재검토' ORDER BY batch_id")]
    open_ids = _open_submission_batches(conn)
    batch_ids = [b for b in pending if b not in open_ids]
    if not batch_ids:
        return None
    from . import corrections
    facts = build_facts(conn, batch_ids)
    facts["request"] = request_line(facts, "재상신")
    froms = {r["batch_id"]: r["review_from"] for r in conn.execute(
        f"SELECT batch_id, review_from FROM batch_state WHERE batch_id IN ({marks(batch_ids)})", batch_ids)}
    pct = lambda v: "계산 불가" if v is None else f"{v:.2f}%"
    summary = {
        "headline": f"정정 후 기준 미달 {len(batch_ids)}개 배치 회수 해제 여부 재상신 ({', '.join(batch_ids)})",
        "summary": " ".join(
            f"{t['batch_id']}(재검토 전 {froms[t['batch_id']]}): 정정 후 확정 {t['confirmed_qty']:,}EA, "
            f"불량률 {pct(t['rate_prod'])}(생산) / {pct(t['rate_ship'])}(출하) — 두 분모 모두 10% 미만."
            for t in facts["targets"]),
        "actions": [{"dept": "품질위원회", "action": "해제 승인 → 정상 복귀·회수요청 철회·출하보류 해제·후속 Task 취소 / "
                                                "해제 거절 → 회수 유지·보류된 Task 재개"}],
        "open_questions": [f"정정 #{c['id']} {c['claim_id']} {corrections._shown(c['field'], c['previous_value'])} → "
                           f"{corrections._shown(c['field'], c['corrected_value'])} (근거: {c['evidence']})"
                           for c in facts["corrections"]],
    }
    cur = conn.execute(
        "INSERT INTO submission (kind, created_at, batch_ids, facts_json, ai_json, ai_source, ai_status, ai_check)"
        " VALUES ('재상신',?,?,?,?,'rule',?,?)",
        (now(), json.dumps(batch_ids), json.dumps(facts, ensure_ascii=False), json.dumps(summary, ensure_ascii=False),
         ai.STATUS_RULE, json.dumps({"reason": "재상신 요약은 규칙 기반(가정 A-15)"}, ensure_ascii=False)))
    sid = cur.lastrowid
    record(conn, "submission", str(sid), None, "상신", *SYSTEM_ACTOR, "정정 후 기준 미달 배치 재상신(회수 해제 여부)",
           {"batch_ids": batch_ids})
    conn.commit()
    return sid


def save_manual_summary(conn, sid: int, author: str, headline: str, summary: str,
                        actions_text: str, questions_text: str) -> None:
    sub = conn.execute("SELECT ai_status FROM submission WHERE id = ?", (sid,)).fetchone()
    if sub is None:
        raise TransitionError(f"상신 {sid} 없음")
    if sub["ai_status"] not in (ai.STATUS_FAIL, ai.STATUS_MANUAL):
        raise TransitionError("AI 요약이 있는 상신은 수동 작성 대상이 아님")
    if not author.strip() or not summary.strip():
        raise TransitionError("작성자와 요약 본문은 필수")
    actions = []
    for line in actions_text.splitlines():
        if line.strip():
            dept, _, act = line.partition(":")
            actions.append({"dept": dept.strip() if act else "-", "action": (act or dept).strip()})
    out = {"headline": headline.strip(), "summary": summary.strip(), "actions": actions,
           "open_questions": [q.strip() for q in questions_text.splitlines() if q.strip()]}
    check = json.loads(conn.execute("SELECT ai_check FROM submission WHERE id=?", (sid,)).fetchone()[0] or "{}")
    check.pop("stale", None)          # 사람이 현재 계산값 초안으로 새로 쓴 요약 → 갱신 필요 표시 해제
    conn.execute("UPDATE submission SET ai_json = ?, ai_source = 'manual', ai_status = ?, ai_check = ? WHERE id = ?",
                 (json.dumps(out, ensure_ascii=False), ai.STATUS_MANUAL, json.dumps(check, ensure_ascii=False), sid))
    record(conn, "submission_summary", str(sid), sub["ai_status"], ai.STATUS_MANUAL, "사람", author.strip(),
           "상신 요약 수동 작성", {"headline": out["headline"]})
    conn.commit()


def _numbers(targets) -> dict:
    return {t["batch_id"]: {"confirmed_qty": t["confirmed_qty"], "rate_prod": t["rate_prod"], "rate_ship": t["rate_ship"]}
            for t in targets}


def refresh_open_submissions(conn, batch_id: str, cause: str) -> list[int]:
    """의결 전 회수상신에 든 배치의 수치가 바뀌면(클레임 접수·정정·검사 불합격) 판정 근거(facts)를 다시 계산해 둔다.
    위원회는 최신 사실로 의결한다. 요약은 다시 만들지 않고 '수치 변경으로 요약 갱신 필요'만 표시한다(AI 재호출은 사람이 정함)."""
    refreshed = []
    for sub in conn.execute("SELECT * FROM submission WHERE status='상신' AND kind='회수상신' ORDER BY id").fetchall():
        ids = json.loads(sub["batch_ids"])
        if batch_id not in ids or not pending_decision_batches(conn, sub):
            continue
        old = json.loads(sub["facts_json"])
        new = build_facts(conn, ids)
        if _numbers(old["targets"]) == _numbers(new["targets"]):
            continue
        check = json.loads(sub["ai_check"] or "{}")
        if sub["ai_status"] in (ai.STATUS_OK, ai.STATUS_WARN, ai.STATUS_WORDING, ai.STATUS_MANUAL):
            stale = check.setdefault("stale", {"basis": _numbers(old["targets"]), "since": current_business_date(conn),
                                               "causes": []})
            stale["causes"].append(cause)
        conn.execute("UPDATE submission SET facts_json=?, ai_check=? WHERE id=?",
                     (json.dumps(new, ensure_ascii=False), json.dumps(check, ensure_ascii=False), sub["id"]))
        record(conn, "submission", str(sub["id"]), "상신", "상신", *SYSTEM_ACTOR,
               f"{cause} → 의결 전 상신 #{sub['id']} 판정 근거 재계산"
               + (" · 수치 변경으로 요약 갱신 필요" if "stale" in check else ""),
               {"batch_id": batch_id, "before": _numbers(old["targets"]).get(batch_id),
                "after": _numbers(new["targets"]).get(batch_id)})
        refreshed.append(sub["id"])
    return refreshed


def regenerate_summary(conn, sid: int, actor: str) -> int:
    """'요약 다시 만들기' — 사람이 눌렀을 때만 현재 계산값으로 AI 요약을 다시 만든다. 실패하면 수동 작성 경로."""
    sub = conn.execute("SELECT * FROM submission WHERE id=?", (sid,)).fetchone()
    if sub is None or sub["kind"] != "회수상신" or sub["status"] != "상신" or not pending_decision_batches(conn, sub):
        raise TransitionError("의결 전 회수상신만 요약을 다시 만들 수 있음")
    actor = (actor or "").strip()
    if not actor:
        raise TransitionError("요청자는 필수")
    facts = build_facts(conn, json.loads(sub["batch_ids"]))
    summary, source, status, info = ai.committee_summary(facts)
    conn.execute("UPDATE submission SET facts_json=?, ai_json=?, ai_source=?, ai_status=?, ai_check=? WHERE id=?",
                 (json.dumps(facts, ensure_ascii=False), json.dumps(summary, ensure_ascii=False) if summary else None,
                  source, status, json.dumps(info, ensure_ascii=False), sid))
    if source in ai.AI_SOURCES:
        record(conn, "submission_summary", str(sid), sub["ai_status"], status, "AI", f"{ai.label(source)} {info.get('model', '')}",
               f"요약 다시 만들기(요청 {actor}) · 현재 계산값 기준 · 수치·표현 대조 "
               + ("통과" if status == ai.STATUS_OK else f"{status} {len(info['problems'])}건")
               + (f" · 표현 경고로 자동 재생성 {info['wording_retries']}회" if info.get("wording_retries") else ""), info)
    else:
        record(conn, "submission_summary", str(sid), sub["ai_status"], status, "사람", actor,
               f"요약 다시 만들기 실패 → 수동 작성 필요: {info.get('reason')}", info)
    conn.commit()
    return sid


# ---------- 품질위원회 배치별 의결 (모의) ----------

def decide_batch(conn, sid: int, batch_id: str, approve: bool, decider: str, reason: str) -> None:
    sub = conn.execute("SELECT * FROM submission WHERE id = ?", (sid,)).fetchone()
    if sub is None or sub["status"] != "상신":
        raise TransitionError(f"상신 {sid}은(는) 의결할 수 있는 상태가 아님")
    if sub["ai_status"] not in ai.DECIDABLE:
        raise TransitionError("상신 요약이 아직 없음 — 수동 작성 후 의결")
    if batch_id not in json.loads(sub["batch_ids"]):
        raise TransitionError(f"{batch_id}는 상신 {sid}의 대상이 아님")
    if conn.execute("SELECT 1 FROM decision WHERE submission_id=? AND batch_id=?", (sid, batch_id)).fetchone():
        raise TransitionError(f"{batch_id}는 이미 의결됨")
    decider, reason = decider.strip(), reason.strip()
    if not decider or not reason:
        raise TransitionError("결정자와 사유는 필수")
    state_row = conn.execute("SELECT state, review_from FROM batch_state WHERE batch_id=?", (batch_id,)).fetchone()
    expected = "재검토" if sub["kind"] == "재상신" else "회수대상"
    if state_row["state"] != expected:
        raise TransitionError(f"{batch_id}는 현재 {state_row['state']} — {sub['kind']} 의결 대상 상태({expected})가 아님")

    who = ("사람", decider)
    if sub["kind"] == "재상신":
        result = "해제 승인" if approve else "해제 거절"
        conn.execute("INSERT INTO decision VALUES (?,?,?,?,?,?,?)",
                     (sid, batch_id, result, decider, reason, now(), current_business_date(conn)))
        if approve:
            set_state(conn, "batch", batch_id, "정상", *who, f"재상신 해제 승인(모의): {reason}", {"submission": sid})
            cancel_batch_followups(conn, batch_id, f"재상신 #{sid} 해제 승인 — {batch_id} 회수 해제")
        else:
            back = state_row["review_from"] or "회수대상"
            set_state(conn, "batch", batch_id, back, *who, f"재상신 해제 거절(모의, 회수 유지): {reason}", {"submission": sid})
            resume_batch_followups(conn, batch_id, f"재상신 #{sid} 해제 거절 — {batch_id} 회수 유지")
    else:
        result = "승인" if approve else "거절"
        conn.execute("INSERT INTO decision VALUES (?,?,?,?,?,?,?)",
                     (sid, batch_id, result, decider, reason, now(), current_business_date(conn)))
        facts = json.loads(sub["facts_json"])
        if approve:
            set_state(conn, "batch", batch_id, "회수의결", *who, f"품질위원회 회수 의결(모의): {reason}",
                      {"submission": sid})
            _after_approval(conn, sid, batch_id, facts)
        else:
            set_state(conn, "batch", batch_id, "보류", *who, f"품질위원회 거절(모의): {reason}", {"submission": sid})
            new_task(conn, sid, "재검토", f"{batch_id} 거절 사유 확인·보완 후 재상신 — 사유: {reason}", "품질",
                     "batch", batch_id, {"reason": reason, "decider": decider,
                                         "next": "거절 사유에 해당하는 근거 보완 → 재상신 또는 판정 재검토"})
            # 회수와 원인 조사는 별도 트랙(R4) — 거절은 조사 중단이 아니다.
            _cause_task(conn, sid, batch_id, facts, "거절")
    _close_if_complete(conn, sid)
    conn.commit()


def _close_if_complete(conn, sid):
    sub = conn.execute("SELECT * FROM submission WHERE id=?", (sid,)).fetchone()
    decided = {r[0] for r in conn.execute("SELECT batch_id FROM decision WHERE submission_id=?", (sid,))}
    if sub["status"] == "상신" and decided >= set(json.loads(sub["batch_ids"])):
        conn.execute("UPDATE submission SET status = '의결완료' WHERE id = ?", (sid,))
        counts = dict(conn.execute("SELECT result, COUNT(*) FROM decision WHERE submission_id=? GROUP BY result",
                                   (sid,)).fetchall())
        record(conn, "submission", str(sid), "상신", "의결완료", "시스템", "워크플로", "모든 대상 배치 의결(또는 이관) 완료", counts)


def _notice_title(d) -> str:
    live = [p for p in d["parts"] if not p.get("held") and not p.get("removed")]
    held = [p for p in d["parts"] if p.get("held") and not p.get("removed")]
    head = f"{d['customer_id']} {d['customer_name']} 회수요청 통지 — "
    held_txt = f"{', '.join(p['batch_id'] for p in held)} {sum(p['qty'] for p in held):,}EA 재검토 대기"
    if not live and held:
        return head + f"⚠ 전량 보류({held_txt})"
    if not live:
        return head + f"회수 해제({', '.join(p['batch_id'] for p in d['parts'])})"
    base = head + f"{sum(p['qty'] for p in live):,}EA ({', '.join(p['batch_id'] for p in live)})"
    return base + (f" · ⚠ {held_txt}분 보류" if held else "")


def _cause_title(d) -> str:
    live = [p for p in d["parts"] if not p.get("removed")]
    approved = [p["batch_id"] for p in live if p.get("decision", "승인") == "승인"]
    rejected = [p["batch_id"] for p in live if p.get("decision") == "거절"]
    who = " · ".join(x for x in (f"회수의결 {', '.join(approved)}" if approved else "",
                                 f"위원회 거절 {', '.join(rejected)}(조사는 계속)" if rejected else "") if x)
    return (f"로트 {d['lot']} 원인 조사 (회수와 별도 트랙, R4) — {who} · "
            f"같은 로트 {', '.join(d['same_lot_batches']) or '없음'}")


def _upsert_part_task(conn, sid, type_, dept, related_type, related_id, part, base_detail, title_fn):
    row = open_task(conn, sid, type_, related_id)
    if row is None:
        detail = {**base_detail, "parts": [part]}
        new_task(conn, sid, type_, title_fn(detail), dept, related_type, related_id, detail)
        return
    detail = json.loads(row["detail_json"])
    detail["parts"].append(part)
    update_task(conn, row, title_fn(detail), detail, "내용 갱신")


def _after_approval(conn, sid, batch_id, facts):
    # 고객 인도분(SBL) → 회수요청
    for s in conn.execute("SELECT * FROM shipment_state WHERE batch_id = ? ORDER BY shipment_id", (batch_id,)).fetchall():
        set_state(conn, "shipment", s["shipment_id"], "회수요청", *AUTO,
                  f"배치 {batch_id} 회수의결 → 고객 인도분 회수요청", {"submission": sid})

    # 사내 잔여재고(추정, A-5) → 출하보류 + 물류 격리 Task
    inv = _inventory_estimate(conn, batch_id)
    hid = f"INV-{batch_id}"
    basis = f"가정 A-5: 생산량 {inv['produced_qty']:,} − SBL 출하 합 {inv['shipped_qty']:,} = {inv['est_qty']:,} (추정)"
    existing = conn.execute("SELECT * FROM inventory_hold WHERE hold_id=?", (hid,)).fetchone()
    if existing is None:
        conn.execute("INSERT INTO inventory_hold (hold_id, batch_id, est_qty, basis, kind, submission_id)"
                     " VALUES (?,?,?,?,'recall',?)", (hid, batch_id, inv["est_qty"], basis, sid))
        record(conn, "inventory", hid, None, "정상", *AUTO, "사내 잔여재고 추정 레코드 생성 (가정 A-5)", inv)
    else:   # 같은 로트 역추적으로 먼저 잡혀 있던 재고가 이번에 회수의결 → recall로 전환
        conn.execute("UPDATE inventory_hold SET kind='recall', submission_id=? WHERE hold_id=?", (sid, hid))
        t = open_task(conn, existing["submission_id"], "추가검사", hid)
        if t:
            cancel_task(conn, t["id"], f"{batch_id} 회수의결 — 추가검사 대신 회수 절차")
    set_state(conn, "inventory", hid, "출하보류", *AUTO, f"배치 {batch_id} 회수의결 → 사내 잔여재고 출하보류 ({basis})",
              {"submission": sid})
    new_task(conn, sid, "사내재고 격리", f"{batch_id} 사내 잔여재고 격리 — 추정 {inv['est_qty']:,}EA (가정 A-5) · 실측 수량 회신 필요",
             "물류", "inventory", hid, {**inv, "basis": basis,
                                        "next": "창고에서 해당 배치 재고를 격리하고 실제 격리 수량을 회신"})

    # 고객 통지 Task — 고객별, 같은 상신에서 열린 Task가 있으면 배치를 덧붙인다
    for row in conn.execute(
            """SELECT s.customer_id, c.customer_name, COUNT(*) n, SUM(s.shipped_qty) qty
               FROM shipment_state s LEFT JOIN src_customers c USING (customer_id)
               WHERE s.batch_id = ? GROUP BY s.customer_id ORDER BY s.customer_id""", (batch_id,)).fetchall():
        _upsert_part_task(conn, sid, "고객 통지", "영업", "customer", row["customer_id"],
                          {"batch_id": batch_id, "shipments": row["n"], "qty": row["qty"]},
                          {"customer_id": row["customer_id"], "customer_name": row["customer_name"],
                           "next": "고객에 회수요청 통지 후 회신(회수 수량·일정·보유량) 접수"}, _notice_title)

    _cause_task(conn, sid, batch_id, facts, "승인")
    lot = next(t["lot_id"] for t in facts["targets"] if t["batch_id"] == batch_id)
    _trace_lot(conn, sid, batch_id, lot)               # 필수 확장: 원료 로트 역추적
    _flag_unlinked_customers(conn, sid, batch_id)      # 정보 부족: 출하 연결 미확인 고객
    resupply.refresh(conn, sid)


def _cause_task(conn, sid, batch_id, facts, decision):
    """원인 조사 Task — 회수와 별도 트랙(R4). 로트 단위로 묶고, 위원회 결과(승인·거절)와 무관하게 만든다."""
    lot = next(t["lot_id"] for t in facts["targets"] if t["batch_id"] == batch_id)
    clue = rules.lot_clues(conn, lot)
    _upsert_part_task(conn, sid, "원인 조사", "품질", "lot", lot, {"batch_id": batch_id, "decision": decision},
                      {"lot": lot, "same_lot_batches": facts["same_lot_batches"], "lot_info": facts["lot_info"],
                       "clues": clue["clues"] if clue else [], "clue_limits": clue["limits"] if clue else [],
                       "clue_label": rules.CLUE_LABEL,
                       "next": "단서 확인(아래) → 입고검사 보류 사유·공급처(V-205) 시험성적 확인, 실제 투입 순서(생산·자재 기록) 확인, "
                               "같은 로트 배치 재검사"}, _cause_title)


def _inventory_estimate(conn, batch_id) -> dict:
    r = conn.execute(
        """SELECT b.batch_id, CAST(b.produced_qty AS INTEGER) produced_qty,
                  COALESCE((SELECT SUM(CAST(shipped_qty AS INTEGER)) FROM src_sbl s WHERE s.batch_id=b.batch_id),0) shipped_qty
           FROM src_batches b WHERE b.batch_id=?""", (batch_id,)).fetchone()
    d = dict(r)
    d["est_qty"] = d["produced_qty"] - d["shipped_qty"]
    return d


# ---------- 필수 확장: 원료 로트 역추적 ----------

def _trace_lot(conn, sid, batch_id, lot):
    """회수의결 배치의 원료 로트 → 사용정지, 같은 로트를 쓴 정상 배치의 사내 잔여재고 → 출하 전 추가검사 필요."""
    row = conn.execute("SELECT state FROM lot_status WHERE lot_id=?", (lot,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO lot_status VALUES (?, '사용가능', '', ?, ?)", (lot, sid, now()))
        state = "사용가능"
    else:
        state = row["state"]
    if state != "사용정지":
        set_state(conn, "lot", lot, "사용정지", *AUTO,
                  f"회수의결 배치 {batch_id}의 투입 로트 → 사용 정지(재공급 조달 판정에서 제외)", {"submission": sid})
        conn.execute("UPDATE lot_status SET reason=?, updated_at=? WHERE lot_id=?", (f"{batch_id} 회수의결", now(), lot))
    for (b,) in conn.execute("SELECT batch_id FROM src_batches WHERE input_lot_ids=? ORDER BY batch_id", (lot,)).fetchall():
        bstate = conn.execute("SELECT state FROM batch_state WHERE batch_id=?", (b,)).fetchone()["state"]
        hid = f"INV-{b}"
        if bstate != "정상" or conn.execute("SELECT 1 FROM inventory_hold WHERE hold_id=?", (hid,)).fetchone():
            continue
        inv = _inventory_estimate(conn, b)
        basis = f"가정 A-5: 생산량 {inv['produced_qty']:,} − SBL 출하 합 {inv['shipped_qty']:,} = {inv['est_qty']:,} (추정)"
        conn.execute("INSERT INTO inventory_hold (hold_id, batch_id, est_qty, basis, kind, submission_id)"
                     " VALUES (?,?,?,?,'trace',?)", (hid, b, inv["est_qty"], basis, sid))
        record(conn, "inventory", hid, None, "정상", *AUTO, f"같은 로트({lot}) 역추적 재고 레코드 생성 (가정 A-5)", inv)
        set_state(conn, "inventory", hid, "추가검사필요", *AUTO,
                  f"원료 로트 {lot} 사용정지 → {b} 사내 잔여재고 출하 전 추가검사 필요 (확장: 로트 역추적)", {"submission": sid})
        new_task(conn, sid, "추가검사", f"{b} 사내 잔여재고 출하 전 추가검사 — 추정 {inv['est_qty']:,}EA (같은 로트 {lot})",
                 "품질", "inventory", hid,
                 {"batch_id": b, "lot": lot, "est_qty": inv["est_qty"], "produced_qty": inv["produced_qty"],
                  "next": "검사 회신(합격/불합격·검사 수량·불량 수량) — 합격 → 출하 가능, 불합격 → 클레임 등록·R1 재판정"})


def record_inspection(conn, task_id: int, passed: bool, inspected_qty: int, defect_qty, inspector: str,
                      note: str = "") -> dict:
    """추가검사 회신(모의): 합격 → 출하가능 / 불합격 → 검사불합격 + 확정 클레임 등록 → R1 재판정."""
    task = conn.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
    if task is None or task["type"] != "추가검사" or task["state"] not in ("대기", "진행"):
        raise TransitionError("회신할 수 있는 추가검사 Task가 아님")
    inspector = inspector.strip()
    if not inspector:
        raise TransitionError("검사자는 필수")
    inv = conn.execute("SELECT * FROM inventory_hold WHERE hold_id=?", (task["related_id"],)).fetchone()
    d = json.loads(task["detail_json"])
    if not 0 < inspected_qty <= d["produced_qty"]:
        raise TransitionError(f"검사 수량은 1 이상 생산량({d['produced_qty']:,}) 이하")
    if not passed and (defect_qty is None or not 0 < defect_qty <= inspected_qty):
        raise TransitionError("불합격이면 불량 수량(1 이상, 검사 수량 이하)이 필요")
    conn.execute("UPDATE inventory_hold SET measured_qty=?, qty_basis='실측', measured_by=?, measured_at=?,"
                 " measured_business_date=?, measured_source=? WHERE hold_id=?",
                 (inspected_qty, inspector, now(), current_business_date(conn),
                  f"추가검사 Task #{task_id} 회신({inspection_source(inspector)})", inv["hold_id"]))
    record(conn, "inventory_qty", inv["hold_id"], f"추정 {inv['est_qty']:,}", f"실측 {inspected_qty:,}", "사람", inspector,
           "추가검사 수량 회신(모의)", {"est_qty": inv["est_qty"], "inspected_qty": inspected_qty})
    out = {"batch_id": inv["batch_id"], "passed": passed}
    if passed:
        set_state(conn, "inventory", inv["hold_id"], "출하가능", "사람", inspector,
                  f"추가검사 합격 {inspected_qty:,}EA → 출하 가능 복귀" + (f" · {note}" if note else ""))
        finish_task(conn, task_id, inspector, f"추가검사 합격 · {inspected_qty:,}EA")
    else:
        set_state(conn, "inventory", inv["hold_id"], "검사불합격", "사람", inspector,
                  f"추가검사 불합격 — 불량 {defect_qty:,}/{inspected_qty:,}EA · 출하 불가" + (f" · {note}" if note else ""))
        n = conn.execute("SELECT COUNT(*) FROM claim_addition").fetchone()[0] + 1
        cid = f"QC-X{n:04d}"
        b = conn.execute("SELECT * FROM src_batches WHERE batch_id=?", (inv["batch_id"],)).fetchone()
        today = current_business_date(conn)
        conn.execute("INSERT INTO claim_addition VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (cid, today, "사내(추가검사)", b["product_id"], b["batch_id"], b["input_lot_ids"], "규격",
                      "True", str(defect_qty), "확정", "추가검사", now(), today,
                      f"추가검사 Task #{task_id} 회신({inspection_source(inspector)})",
                      f"추가검사 불합격 — 불량 {defect_qty:,}/{inspected_qty:,}EA를 확정 클레임으로 등록(A-23)"))
        record(conn, "claim", cid, None, f"확정 · 수량 {defect_qty}", "사람", inspector,
               f"추가검사 불합격 → {b['batch_id']} 확정 클레임 등록(원본 불변, claim_addition)",
               {"inspected_qty": inspected_qty, "defect_qty": defect_qty})
        finish_task(conn, task_id, inspector, f"추가검사 불합격 · 불량 {defect_qty:,}/{inspected_qty:,}EA → 클레임 {cid}")
        from . import corrections
        out["revalidation"] = corrections.revalidate(conn, b["batch_id"], f"추가검사 불합격 클레임 {cid}")
        out["claim_id"] = cid
        out["alert"] = _sample_alert(conn, inv, b, inspected_qty, defect_qty, inspector)
    resupply.refresh(conn, inv["submission_id"])
    conn.commit()
    return out


def inspection_source(actor: str | None) -> str:
    """검사 회신 수치의 출처 — 원본에 검사 데이터가 없으므로 모두 모의 데이터다."""
    return "프리셋 모의 데이터" if actor and "시연 프리셋" in actor else "모의 회신(화면 입력)"


def _sample_alert(conn, inv, b, inspected_qty: int, defect_qty: int, inspector: str) -> dict:
    """추가검사 불합격 후속(가정 A-27). R1 판정은 배치 분모로 그대로 두고, 표본 결과는 경보로만 쓴다.
    ① 불합격 재고 격리 유지 + 폐기 검토 Task ② 표본 불량률 경보 + 품질위원회 보고 Task ③ 기출하 고객 클레임 모니터링 강화."""
    bid, sid, src = b["batch_id"], inv["submission_id"], inspection_source(inspector)
    rate = round(defect_qty / inspected_qty * 100, 2)
    a = conn.execute("SELECT rate_prod, rate_ship FROM assessment WHERE batch_id=?", (bid,)).fetchone()
    r1 = f"R1 {a['rate_prod']:.2f}% / {a['rate_ship']:.2f}%" if a and a["rate_prod"] is not None else "R1 계산 불가"
    dispose = new_task(conn, sid, "폐기 검토", f"{bid} 검사불합격 재고 {inspected_qty:,}EA 폐기 검토 — 격리 유지(출하 불가)",
                       "품질", "inventory", inv["hold_id"],
                       {"batch_id": bid, "qty": inspected_qty, "defect_qty": defect_qty, "data_source": src,
                        "next": "폐기·재작업 여부 결정 — 결정 전까지 격리 유지"})
    customers = conn.execute(
        "SELECT customer_id, SUM(shipped_qty) qty FROM shipment_state WHERE batch_id=? GROUP BY customer_id"
        " ORDER BY customer_id", (bid,)).fetchall()
    report = new_task(conn, sid, "품질위원회 보고", f"{bid} 표본 불량률 경보 보고 — 불량 {defect_qty:,}/{inspected_qty:,}EA = {rate:.2f}% "
                      f"(배치 {r1}, 회수 의결 아님)", "품질위원회", "batch", bid,
                      {"batch_id": bid, "rate_pct": rate, "defect_qty": defect_qty, "inspected_qty": inspected_qty,
                       "data_source": src, "watch_customers": [c["customer_id"] for c in customers],
                       "next": "보고 확인 — 회수 판정은 R1(배치 분모) 기준 유지, 필요 시 추가 조사 지시"})
    conn.execute("INSERT OR REPLACE INTO sample_alert VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (bid, b["input_lot_ids"], inspected_qty, defect_qty, rate, src, report, dispose, sid, now(),
                  current_business_date(conn), f"{inv['hold_id']} 추가검사 회신({src})"))
    record(conn, "batch_flag", bid, None, "표본 불량률 경보", *AUTO,
           f"추가검사 표본 불량 {defect_qty:,}/{inspected_qty:,}EA = {rate:.2f}% ({src}) — {r1}는 정책대로 유지, "
           f"품질위원회 보고 Task #{report}", {"rate_pct": rate, "data_source": src, "격리 재고": inspected_qty})
    for c in customers:
        conn.execute("INSERT OR REPLACE INTO claim_watch VALUES (?,?,?,?,?,?)",
                     (bid, c["customer_id"], c["qty"], f"표본 불량률 경보 {rate:.2f}%", now(), current_business_date(conn)))
        record(conn, "claim_watch", f"{bid}/{c['customer_id']}", None, "클레임 모니터링 강화", *AUTO,
               f"{bid} 표본 불량률 경보 — 기출하 {c['qty']:,}EA 고객 클레임 모니터링 강화(회수요청 아님)")
    return {"rate_pct": rate, "report_task_id": report, "dispose_task_id": dispose,
            "watch_customers": [c["customer_id"] for c in customers]}


# ---------- 정보 부족: 출하 연결 미확인 고객 ----------

def _flag_unlinked_customers(conn, sid, batch_id):
    """확정 클레임 고객 중 이 배치의 SBL 출하 기록이 없는 고객 → 회수요청 보류 + 출하 경로 확인 요청."""
    rows = conn.execute(
        """SELECT customer_id, COUNT(*) n, SUM(CAST(qty_affected AS INTEGER)) qty FROM claims_effective
           WHERE batch_id=? AND defect_confirmed='True' AND origin='원본' AND customer_id NOT IN
                 (SELECT customer_id FROM shipment_state WHERE batch_id=?)
           GROUP BY customer_id ORDER BY customer_id""", (batch_id, batch_id)).fetchall()
    rem = _inventory_estimate(conn, batch_id)["est_qty"]
    for r in rows:
        if conn.execute("SELECT 1 FROM unlinked_customer WHERE batch_id=? AND customer_id=?",
                        (batch_id, r["customer_id"])).fetchone():
            continue
        tid = new_task(conn, sid, "출하 경로 확인 요청",
                       f"{r['customer_id']} 출하 경로 확인 — {batch_id} 확정 클레임 {r['n']}건 {r['qty']:,}EA, SBL 출하 기록 없음",
                       "영업", "unlinked", f"{batch_id}|{r['customer_id']}",
                       {"batch_id": batch_id, "customer_id": r["customer_id"], "claims": r["n"], "claim_qty": r["qty"],
                        "max_qty": rem,
                        "next": f"출하 확인(수량, 최대 {rem:,}EA = 생산량−SBL 출하 합) → 회수요청 추가 / 출하 없음 → 클레임 배치 재확인"})
        conn.execute("INSERT INTO unlinked_customer (batch_id, customer_id, claims, claim_qty, task_id, submission_id)"
                     " VALUES (?,?,?,?,?,?)", (batch_id, r["customer_id"], r["n"], r["qty"], tid, sid))
        record(conn, "unlinked", f"{batch_id}/{r['customer_id']}", None, "출하 연결 미확인", *AUTO,
               "확정 클레임은 있으나 SBL 출하 기록 없음 → 회수요청 보류, 영업 출하 경로 확인 요청",
               {"claims": r["n"], "claim_qty": r["qty"]})


def record_unlinked_reply(conn, task_id: int, shipped: bool, qty, actor: str, note: str = "") -> None:
    task = conn.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
    if task is None or task["type"] != "출하 경로 확인 요청" or task["state"] not in ("대기", "진행"):
        raise TransitionError("회신할 수 있는 출하 경로 확인 Task가 아님")
    actor = actor.strip()
    if not actor:
        raise TransitionError("회신자는 필수")
    d = json.loads(task["detail_json"])
    batch_id, cid, sid = d["batch_id"], d["customer_id"], task["submission_id"]
    key = f"{batch_id}/{cid}"
    if shipped:
        limit = unlinked_limit(conn, batch_id, d["max_qty"])
        if qty is None or not 0 < qty <= limit:
            raise TransitionError(f"출하 수량은 1 이상 {limit:,}EA(생산량−SBL 출하 합−기확인분) 이하")
        shp = f"CNF-{batch_id}-{cid}"
        conn.execute("INSERT INTO shipment_state (shipment_id, batch_id, customer_id, ship_date, shipped_qty, source,"
                     " business_date, origin_ref, reason) VALUES (?,?,?,NULL,?,'확인 회신',?,?,?)",
                     (shp, batch_id, cid, qty, current_business_date(conn), f"출하 경로 확인 요청 Task #{task_id} 회신(모의)",
                      "확정 클레임은 있으나 SBL 출하 기록이 없던 고객의 출하 확인(A-21)" + (f" · {note}" if note else "")))
        record(conn, "shipment", shp, None, "정상", "사람", actor,
               f"출하 경로 확인 회신(모의): {cid}에 {qty:,}EA 출하 확인" + (f" · {note}" if note else ""),
               {"source": "확인 회신", "qty": qty})
        bstate = conn.execute("SELECT state FROM batch_state WHERE batch_id=?", (batch_id,)).fetchone()["state"]
        if bstate == "회수의결":
            set_state(conn, "shipment", shp, "회수요청", *AUTO, f"출하 확인 → {batch_id} 회수요청 대상 추가")
            name = conn.execute("SELECT customer_name FROM src_customers WHERE customer_id=?", (cid,)).fetchone()
            _upsert_part_task(conn, sid, "고객 통지", "영업", "customer", cid,
                              {"batch_id": batch_id, "shipments": 1, "qty": qty},
                              {"customer_id": cid, "customer_name": name[0] if name else "",
                               "next": "고객에 회수요청 통지 후 회신(회수 수량·일정·보유량) 접수"}, _notice_title)
        inv = conn.execute("SELECT * FROM inventory_hold WHERE hold_id=?", (f"INV-{batch_id}",)).fetchone()
        if inv:   # 출하 확인분만큼 사내 잔여재고 추정치를 줄인다(A-5 보정)
            conn.execute("UPDATE inventory_hold SET est_qty=?, basis=? WHERE hold_id=?",
                         (inv["est_qty"] - qty, inv["basis"] + f" − 확인 출하 {qty:,}({cid})", inv["hold_id"]))
            record(conn, "inventory_qty", inv["hold_id"], f"추정 {inv['est_qty']:,}", f"추정 {inv['est_qty'] - qty:,}",
                   *AUTO, f"{cid} 출하 확인분만큼 사내 잔여재고 추정치 보정(A-5)")
        conn.execute("UPDATE unlinked_customer SET state='출하 확인', confirmed_qty=? WHERE batch_id=? AND customer_id=?",
                     (qty, batch_id, cid))
        record(conn, "unlinked", key, "출하 연결 미확인", "출하 확인", "사람", actor,
               f"출하 {qty:,}EA 확인 → 회수요청 대상 추가·재공급 필요량 재계산")
        finish_task(conn, task_id, actor, f"출하 확인 {qty:,}EA")
    else:
        conn.execute("UPDATE unlinked_customer SET state='출하 없음' WHERE batch_id=? AND customer_id=?", (batch_id, cid))
        record(conn, "unlinked", key, "출하 연결 미확인", "출하 없음", "사람", actor,
               f"{cid}: {batch_id} 출하 없음(타 배치·유통 경유 가능)" + (f" · {note}" if note else ""))
        finish_task(conn, task_id, actor, "출하 없음 회신")
        new_task(conn, sid, "클레임 배치 재확인",
                 f"{cid} 클레임 {d['claims']}건 {d['claim_qty']:,}EA의 배치 귀속 재확인 ({batch_id} 출하 없음)",
                 "품질", "unlinked", f"{batch_id}|{cid}",
                 {**d, "next": "실제 구매 경로·로트 확인 → 배치가 다르면 검사 정정 또는 해당 배치로 클레임 재등록"})
    resupply.refresh(conn, sid)
    conn.commit()


def unlinked_limit(conn, batch_id, max_qty) -> int:
    already = conn.execute("SELECT COALESCE(SUM(shipped_qty),0) FROM shipment_state WHERE batch_id=? AND source='확인 회신'",
                           (batch_id,)).fetchone()[0]
    return max_qty - already


# ---------- 재검토에 따른 후속 Task 보류·재개·취소 ----------

def _approved_submissions(conn, batch_id) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT submission_id FROM decision WHERE batch_id=? AND result='승인'", (batch_id,))]


def _unlinked_tasks(conn, sid, batch_id):
    return conn.execute(
        f"SELECT * FROM task WHERE submission_id=? AND type='출하 경로 확인 요청' AND related_id LIKE ? "
        f"AND state IN ({marks(OPEN_TASK_STATES)})", (sid, f"{batch_id}|%", *OPEN_TASK_STATES)).fetchall()


def _notice_tasks(conn, sid, batch_id):
    out = []
    for t in conn.execute(
            f"SELECT * FROM task WHERE submission_id=? AND type='고객 통지' AND state IN ({marks(OPEN_TASK_STATES)})",
            (sid, *OPEN_TASK_STATES)).fetchall():
        d = json.loads(t["detail_json"])
        if any(p["batch_id"] == batch_id for p in d["parts"]):
            out.append((t, d))
    return out


def hold_batch_followups(conn, batch_id, reason):
    """회수의결 배치가 재검토로 가면: 그 배치의 격리·고객 통지·재공급 Task를 보류(재검토 대기)로."""
    for sid in _approved_submissions(conn, batch_id):
        t = open_task(conn, sid, "사내재고 격리", f"INV-{batch_id}")
        if t:
            hold_task(conn, t["id"], reason)
        for t in _unlinked_tasks(conn, sid, batch_id):
            hold_task(conn, t["id"], reason)
        for t, d in _notice_tasks(conn, sid, batch_id):
            for p in d["parts"]:
                if p["batch_id"] == batch_id:
                    p["held"] = True
            if all(p.get("held") or p.get("removed") for p in d["parts"]):
                update_task(conn, t, _notice_title(d), d, "재검토 대기 표시")
                hold_task(conn, t["id"], reason)
            else:
                update_task(conn, t, _notice_title(d), d, f"{batch_id} 재검토 대기분 보류(다른 배치 통지는 계속)")
        resupply.refresh(conn, sid)


def resume_batch_followups(conn, batch_id, reason):
    for sid in _approved_submissions(conn, batch_id):
        t = open_task(conn, sid, "사내재고 격리", f"INV-{batch_id}")
        if t:
            resume_task(conn, t["id"], reason)
        for t in _unlinked_tasks(conn, sid, batch_id):
            resume_task(conn, t["id"], reason)
        for t, d in _notice_tasks(conn, sid, batch_id):
            for p in d["parts"]:
                if p["batch_id"] == batch_id:
                    p.pop("held", None)
            update_task(conn, t, _notice_title(d), d, f"{batch_id} 회수 유지 — 통지 재개")
            resume_task(conn, t["id"], reason)
        resupply.refresh(conn, sid)


def cancel_batch_followups(conn, batch_id, reason):
    """재상신 해제 승인: 회수요청 철회·출하보류 해제·해당 Task 취소(다른 배치분은 유지)."""
    for s in conn.execute("SELECT shipment_id, state FROM shipment_state WHERE batch_id=?", (batch_id,)).fetchall():
        if s["state"] == "회수요청":
            set_state(conn, "shipment", s["shipment_id"], "요청철회", *AUTO, reason)
    inv = conn.execute("SELECT state FROM inventory_hold WHERE hold_id=?", (f"INV-{batch_id}",)).fetchone()
    if inv and inv["state"] == "출하보류":
        set_state(conn, "inventory", f"INV-{batch_id}", "보류해제", *AUTO, reason)
    for sid in _approved_submissions(conn, batch_id):
        t = open_task(conn, sid, "사내재고 격리", f"INV-{batch_id}")
        if t:
            cancel_task(conn, t["id"], reason)
        for t in _unlinked_tasks(conn, sid, batch_id):
            cancel_task(conn, t["id"], reason)
        for t, d in _notice_tasks(conn, sid, batch_id):
            for p in d["parts"]:
                if p["batch_id"] == batch_id:
                    p["removed"] = True
                    p.pop("held", None)
            if all(p.get("removed") for p in d["parts"]):
                update_task(conn, t, _notice_title(d), d, "회수 해제")
                cancel_task(conn, t["id"], reason)
            else:
                update_task(conn, t, _notice_title(d), d, f"{batch_id} 회수 해제 — 통지 범위에서 제외")
                resume_task(conn, t["id"], "다른 배치 통지는 계속")
        # 이미 완료된 고객 통지가 있으면 철회 안내 Task
        for t in conn.execute("SELECT * FROM task WHERE submission_id=? AND type='고객 통지' AND state='완료'",
                              (sid,)).fetchall():
            d = json.loads(t["detail_json"])
            if any(p["batch_id"] == batch_id for p in d["parts"]):
                new_task(conn, sid, "회수요청 철회 안내", f"{d['customer_id']} {d['customer_name']} — {batch_id} 회수요청 철회 안내",
                         "영업", "customer", d["customer_id"], {"batch_id": batch_id, "next": "고객에 회수요청 철회 안내"})
        resupply.refresh(conn, sid)


# ---------- 회신 ----------

def record_quarantine(conn, task_id: int, qty: int, actor: str, note: str = "") -> None:
    """물류 회신(모의): 실제 격리 수량으로 사내 잔여재고를 추정치 → 실측치로 바꾸고 Task를 완료한다."""
    task = conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
    if task is None or task["type"] != "사내재고 격리":
        raise TransitionError(f"task {task_id}는 사내재고 격리 Task가 아님")
    if task["state"] not in ("대기", "진행"):
        raise TransitionError(f"task {task_id}는 {task['state']} — 회신할 수 없음")
    actor = actor.strip()
    if not actor:
        raise TransitionError("회신자는 필수")
    inv = conn.execute("SELECT * FROM inventory_hold WHERE hold_id = ?", (task["related_id"],)).fetchone()
    produced = int(conn.execute("SELECT produced_qty FROM src_batches WHERE batch_id = ?",
                                (inv["batch_id"],)).fetchone()[0])
    if not 0 <= qty <= produced:
        raise TransitionError(f"격리 수량은 0 이상 생산량({produced:,}) 이하여야 함")
    before = (f"실측 {inv['measured_qty']:,}" if inv["qty_basis"] == "실측" else f"추정 {inv['est_qty']:,}")
    conn.execute("UPDATE inventory_hold SET measured_qty=?, qty_basis='실측', measured_by=?, measured_at=?,"
                 " measured_business_date=?, measured_source=? WHERE hold_id=?",
                 (qty, actor, now(), current_business_date(conn), f"사내재고 격리 Task #{task_id} 회신(모의)", inv["hold_id"]))
    record(conn, "inventory_qty", inv["hold_id"], before, f"실측 {qty:,}", "사람", actor,
           f"물류 격리 수량 회신(모의) — 추정 대비 {qty - inv['est_qty']:+,}EA" + (f" · {note.strip()}" if note.strip() else ""),
           {"est_qty": inv["est_qty"], "measured_qty": qty, "diff": qty - inv["est_qty"], "est_basis": inv["basis"]})
    finish_task(conn, task_id, actor, f"격리 완료 · 실측 {qty:,}EA 회신")
    conn.commit()


def record_customer_holding(conn, batch_id: str, customer_id: str, held_qty: int, actor: str,
                            source: str = "모의 회신") -> None:
    """고객 보유량 회신을 기록하고 재공급 판정(R8)·계획을 다시 계산한다.
    AI ②(회신 구조화)가 검증을 통과한 회신을 이 함수로 넘길 자리."""
    shipped = conn.execute("SELECT SUM(shipped_qty) FROM shipment_state WHERE batch_id=? AND customer_id=?",
                           (batch_id, customer_id)).fetchone()[0]
    if shipped is None:
        raise TransitionError(f"{batch_id} → {customer_id} 고객 인도분 없음")
    if not 0 <= held_qty <= shipped:
        raise TransitionError(f"보유량은 0 이상 인도 수량({shipped:,}) 이하여야 함")
    prev = conn.execute("SELECT held_qty FROM customer_holding WHERE batch_id=? AND customer_id=?",
                        (batch_id, customer_id)).fetchone()
    conn.execute("INSERT OR REPLACE INTO customer_holding VALUES (?,?,?,?,?,?,?)",
                 (batch_id, customer_id, held_qty, now(), actor, source, current_business_date(conn)))
    record(conn, "customer_holding", f"{batch_id}/{customer_id}",
           f"보유 {prev[0]:,}" if prev else f"상한 {shipped:,}", f"보유 {held_qty:,}", "사람", actor,
           f"고객 보유량 회신({source}) → 재공급 필요 수량 재계산", {"shipped": shipped, "held": held_qty})
    for sid in _approved_submissions(conn, batch_id):
        resupply.refresh(conn, sid)
    conn.commit()


NON_ADVANCE = {"사내재고 격리": "실제 격리 수량 회신", "추가검사": "검사 회신", "출하 경로 확인 요청": "출하 확인/없음 회신", "영업 승인": "승인/거절 회신", "투자 승인": "승인/거절 회신",
               "납기 연기 동의 요청": "고객 동의/거절 회신", "조달 확인": "가능/불가 회신",
               "보유량 회신 수집": "(b) 회신 마감"}


def advance_task(conn, task_id: int, actor: str) -> None:
    row = conn.execute("SELECT state, type FROM task WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise TransitionError(f"task {task_id} 없음")
    nxt = {"대기": "진행", "진행": "완료"}.get(row["state"])
    if nxt is None:
        raise TransitionError(f"task {task_id}는 {row['state']} — 진행할 수 없음")
    if row["type"] in NON_ADVANCE and nxt == "완료":
        raise TransitionError(f"{row['type']} Task는 {NON_ADVANCE[row['type']]}으로만 완료할 수 있음")
    set_state(conn, "task", task_id, nxt, "사람", actor.strip() or "담당자", f"Task {row['state']} → {nxt}")
    conn.commit()


# ---------- 업무일 (A05 업무 달력, 가정 A-29) ----------

def advance_business_day(conn, actor: str) -> dict:
    """업무일을 다음 영업일로 넘긴다(주말 건너뜀). 처리 목표일(A-28)이 지난 열린 작업은 '지연'으로 표시하고 이력에 남긴다."""
    actor = (actor or "").strip() or "운영 담당"
    before = current_business_date(conn)
    after = bizcal.next_business_day(before)
    set_business_date(conn, after)
    record(conn, "business_date", "업무일", before, after, "사람", actor,
           f"업무일 {bizcal.label(before)} → {bizcal.label(after)} ({bizcal.BASIS}, 주말 건너뜀)")
    delayed = []
    for t in conn.execute(
            "SELECT * FROM task WHERE state IN ('대기', '진행') AND delayed_at IS NULL AND due_date < ? ORDER BY due_date, id",
            (after,)).fetchall():
        late = bizcal.business_days_between(t["due_date"], after)
        conn.execute("UPDATE task SET delayed_at=? WHERE id=?", (after, t["id"]))
        record(conn, "task", str(t["id"]), t["state"], "지연", *SYSTEM_ACTOR,
               f"[{t['dept']}] {t['type']} 처리 목표 {bizcal.label(t['due_date'])} 경과({late}영업일) → 지연 "
               f"(상태 {t['state']} 유지, 가정 A-28)", {"due_date": t["due_date"], "business_date": after})
        delayed.append(t["id"])
    late_subs = []
    for sub in conn.execute("SELECT * FROM submission WHERE status='상신' ORDER BY id").fetchall():
        pending = pending_decision_batches(conn, sub)
        dl = decision_deadline(conn, sub)
        if pending and dl["late"] and dl["due"] >= before:          # 이번에 처리 목표를 처음 넘김
            record(conn, "submission", str(sub["id"]), "상신", "의결 지연", *SYSTEM_ACTOR,
                   f"{sub['kind']} #{sub['id']} 위원회 의결 처리 목표 {bizcal.label(dl['due'])} 경과({dl['late']}영업일) → 의결 지연 "
                   f"({', '.join(pending)}, 가정 A-32: 상신 후 {bizcal.DECISION_DUE_BDAYS}영업일)",
                   {"due_date": dl["due"], "business_date": after, "batch_ids": pending})
            late_subs.append(sub["id"])
    conn.commit()
    return {"before": before, "after": after, "delayed": delayed, "late_submissions": late_subs}


# ---------- 시작 상태 자동 생성 ----------

def seed_submission(conn) -> int | None:
    """앱이 빈 DB로 시작했을 때 시작 상태의 회수상신을 만든다(판정은 먼저 끝난 상태). 이미 상신이 있으면 만들지 않는다."""
    if conn.execute("SELECT COUNT(*) FROM submission").fetchone()[0]:
        return None
    sid = create_submission(conn)
    if sid:
        record(conn, "system", "시작 상태", None, f"회수상신 #{sid}", "시스템", "앱 시작",
               "빈 DB로 시작 → 시작 상태 자동 생성(업무일 초기화 · 판정 · 상신, AI 요약 실패 시 수동 작성 경로)")
        conn.commit()
    return sid


# ---------- 회수의결 이후 클레임 · 외부 정보 ----------

def claim_on_recalled_batch(conn, batch_id: str, customer_id: str, claim_id: str, qty: int, confirmed: bool) -> int:
    """이미 회수의결된 배치의 새 클레임: 상신은 새로 만들지 않고 '이미 회수 진행 중'으로 남긴 뒤,
    기존 회수 범위에 영향이 있는지(새 고객인지)만 확인 작업으로 만든다."""
    sid = conn.execute("SELECT submission_id FROM decision WHERE batch_id=? AND result='승인' ORDER BY submission_id DESC",
                       (batch_id,)).fetchone()
    sid = sid[0] if sid else None
    ships = conn.execute("SELECT state, COUNT(*) n, SUM(shipped_qty) q FROM shipment_state WHERE batch_id=? AND customer_id=?"
                         " GROUP BY state", (batch_id, customer_id)).fetchall()
    in_scope = bool(ships)
    scope = (", ".join(f"{r['state']} {r['n']}건 {r['q']:,}EA" for r in ships) if in_scope
             else "이 배치의 SBL 출하 기록 없음")
    record(conn, "batch", batch_id, "회수의결", "회수의결", *SYSTEM_ACTOR,
           f"이미 회수 진행 중 — 클레임 {claim_id}({customer_id}, {'확정' if confirmed else '미확정'} {qty:,}EA) 접수, 새 상신을 만들지 않음"
           f" · 회수 범위 {'안 고객' if in_scope else '밖 고객(새 고객)'}: {scope}", {"claim_id": claim_id, "submission": sid})
    if in_scope:
        title = (f"{batch_id} 회수 범위 확인 — {customer_id} 클레임 {claim_id} {qty:,}EA · 기존 회수 범위 안({scope}) "
                 "→ 회수 수량·보유량 회신에 반영할지 확인")
        nxt = "기존 회수요청과 같은 고객 — 범위 변경 없음 확인, 회수 수량·보유량 회신에 반영"
    else:
        title = (f"{batch_id} 회수 범위 확인 — {customer_id} 클레임 {claim_id} {qty:,}EA · 새 고객(SBL 출하 기록 없음) "
                 "→ 출하 경로 확인 후 회수 범위 추가 여부 결정")
        nxt = "영업에 출하 경로 확인 → 출하가 확인되면 회수요청 대상 추가(A-21), 없으면 클레임 배치 재확인"
    return new_task(conn, sid, "회수 범위 확인", title, "품질", "batch", batch_id,
                    {"batch_id": batch_id, "customer_id": customer_id, "claim_id": claim_id, "qty": qty,
                     "in_scope": in_scope, "scope": scope, "next": nxt})


EXTERNAL_MONITOR = "외부 정보 모니터링"
EXTERNAL_CHECK = "보도 지목 배치 사실 확인"


def register_external_info(conn, source: str, published_date: str, summary: str, product_id: str, batch_ids: list[str],
                           actor: str, applied_date: str = "") -> dict:
    """외부 정보(언론 보도) 접수 → 판단 재검토. 보도는 회수 근거가 아니므로(R5) 판정은 확정 클레임 기준 그대로 두고 그 근거를 이력에 남긴다.
    특정 배치를 지목하지 않으면 품질팀 모니터링 작업만, 지목하면 그 배치의 사실 확인 작업을 만든다."""
    source, summary, actor, product_id = source.strip(), summary.strip(), actor.strip(), (product_id or "").strip()
    batch_ids = [b.strip() for b in batch_ids if b and b.strip()]
    if not source or not summary or not actor:
        raise TransitionError("출처·내용 요약·접수자는 필수")
    today = current_business_date(conn)
    applied_date = (applied_date or "").strip() or today
    for name, v in (("보도일", published_date), ("적용일", applied_date)):
        try:
            bizcal.label(v)
        except ValueError:
            raise TransitionError(f"{name}은 YYYY-MM-DD 날짜")
    if not published_date <= applied_date <= today:
        raise TransitionError(f"보도일 ≤ 적용일 ≤ 현재 업무일({today})이어야 함")
    if product_id and conn.execute("SELECT 1 FROM src_products WHERE product_id=?", (product_id,)).fetchone() is None:
        raise TransitionError(f"제품 {product_id} 없음")
    for b in batch_ids:
        if conn.execute("SELECT 1 FROM src_batches WHERE batch_id=?", (b,)).fetchone() is None:
            raise TransitionError(f"배치 {b} 없음")
    n = conn.execute("SELECT COUNT(*) FROM external_info").fetchone()[0] + 1
    eid = f"EXT-{n:04d}"
    named = "배치 " + ", ".join(batch_ids) if batch_ids else (f"제품 {product_id}(배치 지목 없음)" if product_id else "제품·배치 지목 없음")
    conn.execute("INSERT INTO external_info VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (eid, source, published_date, applied_date, summary, product_id or None, json.dumps(batch_ids),
                  actor, now(), today, f"외부 정보 접수 화면 — 출처 {source}",
                  f"언론 보도 접수(미확정 정보) — {named}"))
    record(conn, "external_info", eid, None, "접수", "사람", actor,
           f"외부 정보 접수(모의) — {source} · 보도 {published_date} · 적용 {applied_date} · {named}: {summary[:80]}",
           {"batch_ids": batch_ids, "product_id": product_id})

    # 판단 재검토: 확정 클레임 기준 불량률을 다시 계산해도 보도는 입력이 아니므로 바뀌지 않는다(R5).
    results = refresh_assessment(conn)
    reviewed = []
    watch = {r[0] for r in conn.execute(
        "SELECT batch_id FROM batch_state WHERE state IN ('회수대상', '회수의결', '보류', '재검토')")} | set(batch_ids)
    for b in sorted(watch):
        r = results.get(b)
        if r is None:
            continue
        state = conn.execute("SELECT state FROM batch_state WHERE batch_id=?", (b,)).fetchone()[0]
        nums = (f"확정 불량률 {r['rate_prod']:.2f}% / {r['rate_ship']:.2f}%" if r["rate_prod"] is not None else "확정 불량 없음")
        verdict = "회수 기준 충족 — 판정 유지" if r["is_target"] else "회수 기준 미달 — 보도만으로 회수대상이 되지 않음"
        record(conn, "batch", b, state, state, *SYSTEM_ACTOR,
               f"외부 정보 {eid} 접수 → 판단 재검토: {nums}, {verdict} (R5: 언론 보도는 회수 근거 아님, 확정 클레임 기준)",
               {"external_info": eid, **evidence(r)})
        reviewed.append({"batch_id": b, "state": state, "is_target": bool(r["is_target"])})

    tasks = []
    if batch_ids:
        for b in batch_ids:
            tasks.append(new_task(conn, None, EXTERNAL_CHECK,
                                  f"{b} 보도 지목 사실 확인 — {source}({published_date}) · 판정은 확정 클레임 기준 유지(R5)", "품질",
                                  "batch", b, {"external_info": eid, "batch_id": b,
                                               "next": "해당 배치 클레임·검사 기록 확인 — 확정 불량이 새로 나오면 클레임 접수로 반영"}))
    else:
        tasks.append(new_task(conn, None, EXTERNAL_MONITOR,
                              f"외부 정보 모니터링 — {source}({published_date}) · {named} · 판정 변경 없음(R5)", "품질",
                              "external_info", eid, {"external_info": eid, "product_id": product_id,
                                                     "next": "고객 클레임 추이·추가 보도 모니터링 — 확정 불량이 접수되면 클레임 접수로 반영"}))
    conn.commit()
    return {"external_id": eid, "reviewed": reviewed, "tasks": tasks, "named": batch_ids}
