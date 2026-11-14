"""W03 품질 이슈·공급 복구 운영 시스템 — FastAPI 진입점 (uvicorn main:app)."""
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from w03 import (ai, bizcal, corrections, db, decisions, effects, glossary, lots, presets, provenance, replies, resupply,
                 rules, trace, workflow)
from w03.config import AS_OF, DB_PATH
from w03.core import HOLD, TransitionError

templates = Jinja2Templates(directory=Path(__file__).parent / "w03" / "templates")
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else None
templates.env.filters["num"] = lambda v: "-" if v is None else f"{v:,}"
templates.env.filters["pct"] = lambda v: "계산 불가" if v is None else f"{v:.2f}%"
templates.env.filters["eok"] = lambda v: "-" if v is None else f"{v / 1e8:,.1f}억원"
templates.env.filters["tone"] = lambda s: {
    ai.STATUS_OK: "ok", ai.STATUS_MANUAL: "ok", ai.STATUS_RULE: "ok", ai.STATUS_WARN: "warn", ai.STATUS_WORDING: "warn", ai.STATUS_FAIL: "bad",
    "승인": "ok", "거절": "bad", "의결완료": "ok", "상신": "warn", "해제 승인": "ok", "해제 거절": "bad",
    "재검토 이관": "warn", HOLD: "warn", "취소": "", "동의": "ok", "요청": "warn", "제외": "",
    "판정완료": "ok", "대안확정": "ok", "대안소진": "bad", "(b) 회신대기": "warn", "(a) 승인대기": "warn",
    "(a) 고객동의대기": "warn", "(c) 선택대기": "warn", "(c) 조달확인대기": "warn", "(c) 승인대기": "warn",
    "충족": "ok", "충족(조달 가능 회신)": "ok", "조건부 충족(조달 확인 전)": "warn", "미달(조달 불가)": "bad",
    "미달(용량 부족)": "bad", "투자 거절": "bad", "검증 통과": "ok", "사람 확인 필요": "bad", "반영": "ok",
    "사용정지": "bad", "추가검사필요": "warn", "출하가능": "ok", "검사불합격": "bad",
    "출하 연결 미확인": "warn", "출하 확인": "ok", "출하 없음": "bad", "표본 불량률 경보": "bad",
    "클레임 모니터링 강화": "warn"}.get(s, "")   # 모르는 값은 s-<상태> 색을 쓴다
templates.env.globals["AI_SOURCES"] = ai.AI_SOURCES
templates.env.tests["wording"] = lambda p: str(p).startswith(ai.WORDING_PREFIX)   # 표현 경고(R3)와 수치 불일치 구분


def _business_date_label() -> str:
    """화면 상단 업무일(A05 업무 달력, 하루씩 넘긴다)."""
    conn = db.connect()
    try:
        return bizcal.label(db.current_business_date(conn))
    finally:
        conn.close()


templates.env.globals["business_date_label"] = _business_date_label
templates.env.filters["daylabel"] = lambda s: bizcal.label(s) if s else "-"
templates.env.globals["GLOSSARY_TIPS"] = {c: glossary.TOOLTIPS[c] for c in glossary.AUTO_CODES}
templates.env.filters["ailabel"] = ai.label
templates.env.filters["terms"] = glossary.terms        # 코드명 → 한글 업무 용어(원래 이름은 툴팁)
templates.env.filters["plain"] = glossary.plain
templates.env.filters["entity"] = glossary.entity      # 처리 이력 대상 종류 → 한글
templates.env.filters["yn"] = glossary.yes_no            # True/False → 예/아니오
templates.env.filters["conf"] = lambda v: glossary.yes_no(v, "확정", "미확정")
templates.env.filters["prettyjson"] = lambda s: json.dumps(json.loads(s), ensure_ascii=False, indent=1) if s else ""


log = logging.getLogger("w03")
# 시작 상태 자동 생성 진행 상황(화면 안내용). 테스트는 W03_AUTOSEED=0 으로 끈다.
SEED = {"state": "idle", "detail": ""}


def _seed_submission():
    """시작 상태의 회수상신 #1 — AI 요약 호출이 오래 걸려도 앱 시작·상태 확인(healthz)을 막지 않도록 백그라운드에서 만든다.
    AI가 실패하면 상신은 '요약 실패 - 수동 작성'으로 남고 사유가 표시된다."""
    conn = db.connect()
    try:
        SEED.update(state="running", detail="회수상신 #1 AI 요약 생성 중")
        sid = workflow.seed_submission(conn)
        row = conn.execute("SELECT ai_status FROM submission WHERE id=?", (sid,)).fetchone() if sid else None
        SEED.update(state="done", detail=f"회수상신 #{sid} · 요약 {row['ai_status']}" if row else "상신 대상 없음")
    except Exception as e:      # 시작 상태 생성 실패가 앱을 멈추지 않게 한다(화면에서 '판정 실행'·'상신'으로 이어갈 수 있음)
        log.exception("시작 상태 자동 생성 실패")
        SEED.update(state="failed", detail=f"자동 상신 실패: {type(e).__name__}")
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_app):
    conn = db.connect()
    try:
        fresh = db.needs_reset(conn)
        if fresh:                    # 새 DB(재시작으로 비었음)이거나 스키마 버전이 바뀐 경우
            db.reset(conn)
        if fresh and os.environ.get("W03_AUTOSEED", "1") != "0":
            # 시작 상태: 업무일 07-01(초기화가 설정) → 판정 완료 → 회수상신 #1(백그라운드, AI 요약)
            workflow.run_assessment(conn)
            SEED.update(state="running", detail="회수상신 #1 AI 요약 생성 중")
            threading.Thread(target=_seed_submission, name="w03-seed", daemon=True).start()
    finally:
        conn.close()
    yield


templates.env.globals["REPLY_SPECS"] = replies.SPECS

app = FastAPI(title="W03 품질 이슈·공급 복구", lifespan=lifespan)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    """화면(브라우저)에서 보낸 요청의 입력 오류는 JSON 대신 안내 화면으로 보여 준다. API·테스트 요청은 JSON 그대로."""
    if "text/html" in request.headers.get("accept", "") and not request.url.path.startswith("/api/"):
        return templates.TemplateResponse(request, "error.html", {"code": exc.status_code, "detail": exc.detail},
                                          status_code=exc.status_code)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


def _conn():
    return db.connect()


def ai_configured() -> bool:
    """키 값은 노출하지 않고 현재 공급자(AI_PROVIDER)의 키 설정 여부만 본다."""
    return ai.configured()


def _back(url="/"):
    return RedirectResponse(url if url.startswith("/") else "/", status_code=303)


def _act(fn, redirect):
    """상태 변경 요청 공통 처리: 허용되지 않은 전이·입력 오류는 409."""
    conn = _conn()
    try:
        out = fn(conn)
    except TransitionError as e:
        raise HTTPException(409, str(e))
    finally:
        conn.close()
    return _back(redirect(out) if callable(redirect) else redirect)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


# ---------- 화면 ----------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = _conn()
    try:
        batches = conn.execute(
            """SELECT s.*, a.confirmed_claims, a.confirmed_qty, a.produced_qty, a.shipped_qty, a.rate_prod, a.rate_ship,
                      a.recall_prod, a.recall_ship, a.warning, b.product_id, b.input_lot_ids lot_id, b.qc_result,
                      (SELECT COUNT(*) FROM claim_correction k WHERE k.batch_id = s.batch_id) corrections
               FROM batch_state s JOIN src_batches b USING (batch_id) LEFT JOIN assessment a USING (batch_id)
               WHERE s.state != '정상' OR a.confirmed_qty > 0 OR s.investigate_reason LIKE '%R6%'
               ORDER BY (s.state != '정상') DESC, a.rate_prod DESC""").fetchall()
        open_ids = workflow._open_submission_batches(conn)
        ctx = {
            "as_of": AS_OF,
            "business_date": db.current_business_date(conn),
            "assessed": conn.execute("SELECT COUNT(*) FROM assessment").fetchone()[0],
            "flagged": conn.execute("SELECT COUNT(*) FROM batch_state WHERE investigate_flag=1").fetchone()[0],
            "batches": batches,
            "state_counts": {
                name: conn.execute(f"SELECT state, COUNT(*) n, {cust} c FROM {table} GROUP BY state").fetchall()
                for name, table, cust in (("배치", "batch_state", "NULL"),
                                          ("고객 인도분", "shipment_state", "COUNT(DISTINCT customer_id)"),
                                          ("사내 잔여재고", "inventory_hold", "NULL"), ("Task", "task", "NULL"))},
            "submissions": conn.execute("SELECT * FROM submission ORDER BY id DESC").fetchall(),
            "sub_dates": {int(r[0]): r[1] for r in conn.execute(
                "SELECT entity_id, MIN(business_date) FROM history WHERE entity_type='submission' GROUP BY entity_id")},
            "pending_targets": [r[0] for r in conn.execute(
                "SELECT batch_id FROM batch_state WHERE state='회수대상' ORDER BY batch_id") if r[0] not in open_ids],
            "review_count": sum(1 for r in conn.execute("SELECT batch_id FROM batch_state WHERE state='재검토'")
                                if r[0] not in open_ids),
            "plans": conn.execute("SELECT * FROM resupply_plan ORDER BY submission_id").fetchall(),
            "presets": {k: v[0] for k, v in presets.PRESETS.items()},
            "decisions": decisions.build(conn), "urgency": decisions.URGENCY, "urgency_help": decisions.URGENCY_HELP,
            "count_units": {"배치": "개", "사내 잔여재고": "건", "Task": "건"},
            "ai_configured": ai_configured(),
            "ai_provider": f"{ai.label()} {ai.model_name()}",
            "seed": SEED,
            "lots": conn.execute("SELECT * FROM lot_status ORDER BY lot_id").fetchall(),
            "intakes": conn.execute("SELECT * FROM reply_intake ORDER BY id DESC LIMIT 10").fetchall(),
            "unlinked": conn.execute("SELECT * FROM unlinked_customer ORDER BY batch_id, customer_id").fetchall(),
            "tasks": conn.execute("SELECT * FROM task ORDER BY id").fetchall(),
            "history": conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT 15").fetchall(),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@app.get("/submissions/{sid}", response_class=HTMLResponse)
def submission_page(request: Request, sid: int):
    conn = _conn()
    try:
        sub = conn.execute("SELECT * FROM submission WHERE id = ?", (sid,)).fetchone()
        if sub is None:
            raise HTTPException(404)
        ids = json.loads(sub["batch_ids"])
        m = ",".join("?" * len(ids))
        ctx = {
            "tasks": conn.execute("SELECT * FROM task WHERE submission_id = ? ORDER BY id", (sid,)).fetchall(),
            "ships": conn.execute(f"SELECT * FROM shipment_state WHERE batch_id IN ({m}) ORDER BY batch_id, ship_date",
                                  ids).fetchall(),
            "inv": conn.execute(f"SELECT * FROM inventory_hold WHERE batch_id IN ({m})", ids).fetchall(),
            "states": {r["batch_id"]: r["state"] for r in conn.execute(
                f"SELECT batch_id, state FROM batch_state WHERE batch_id IN ({m})", ids)},
            "decisions": {r["batch_id"]: r for r in conn.execute(
                "SELECT * FROM decision WHERE submission_id = ?", (sid,))},
            "plan": resupply.plan_row(conn, sid),
            "sub_business_date": (conn.execute(
                "SELECT business_date FROM history WHERE entity_type='submission' AND entity_id=? ORDER BY id LIMIT 1",
                (str(sid),)).fetchone() or [None])[0],
        }
        ctx["preview"] = _supply_preview(conn, sub, ids, ctx["states"], ctx["decisions"])
        ctx["deadline"] = workflow.decision_deadline(conn, sub)
        ctx["pending"] = workflow.pending_decision_batches(conn, sub)
        # 상신 이후 클레임 접수·정정으로 바뀐 수치(표시만 — 상신의 계산값은 그대로 둔다)
        ctx["changed"] = []
        for t in json.loads(sub["facts_json"])["targets"]:
            now_ = conn.execute("SELECT a.confirmed_qty, a.rate_prod, a.rate_ship, s.state FROM assessment a JOIN batch_state s"
                                " USING (batch_id) WHERE batch_id=?", (t["batch_id"],)).fetchone()
            if now_ and now_["confirmed_qty"] != t["confirmed_qty"]:
                ctx["changed"].append({"batch_id": t["batch_id"], "then_qty": t["confirmed_qty"], "now_qty": now_["confirmed_qty"],
                                       "then_prod": t["rate_prod"], "then_ship": t["rate_ship"], "now_prod": now_["rate_prod"],
                                       "now_ship": now_["rate_ship"], "state": now_["state"]})
    finally:
        conn.close()
    facts = json.loads(sub["facts_json"])
    check = json.loads(sub["ai_check"]) if sub["ai_check"] else {}
    summary = json.loads(sub["ai_json"]) if sub["ai_json"] else None
    # 원인 조사 단서는 '원료 로트' 칸에만 — 정보 공백 목록에서는 빼고 링크 한 줄로(예전 요약에 섞여 있어도 걸러 낸다)
    clues = [c for lc in facts.get("lot_clues") or [] for c in lc.get("clues", [])]
    questions = [q for q in (summary or {}).get("open_questions", [])
                 if not q.startswith("[원인 조사 단서") and not any(c in q for c in clues)]
    return templates.TemplateResponse(request, "submission.html", {
        **ctx, "sub": sub, "facts": facts, "ai": summary, "questions": questions, "n_clues": len(clues),
        "req_line": facts.get("request") or workflow.request_line(facts, sub["kind"]),
        "check": check,
        "stale_rows": [{"batch_id": t["batch_id"], "then": check["stale"]["basis"].get(t["batch_id"]), "now": t}
                       for t in facts["targets"]] if check.get("stale") else [],
        "decidable": sub["ai_status"] in ai.DECIDABLE, "STATUS_FAIL": ai.STATUS_FAIL,
        "draft": ai.draft_for_manual(facts)})


def _supply_preview(conn, sub, ids, states, decisions):
    """의결 전 '승인 시 예상 공급 영향' — 새 판정 로직 없이 기존 R8(rules.resupply_check)을 그대로 쓴다.
    대상 = 이 상신에서 이미 승인된 배치 + 아직 의결 전인 회수대상 배치(모두 승인된다고 가정한 예상치)."""
    if sub["kind"] != "회수상신":
        return None
    pending = [b for b in ids if states.get(b) == "회수대상" and b not in decisions]
    if not pending:
        return None
    batches = sorted(set(resupply.active_batches(conn, sub["id"])) | set(pending))
    return {"pending": pending, "batches": batches, **rules.resupply_check(conn, batches)}


@app.get("/plans/{sid}", response_class=HTMLResponse)
def plan_page(request: Request, sid: int):
    conn = _conn()
    try:
        comp = resupply.compare(conn, sid)
        if comp is None:
            raise HTTPException(404)
        types = resupply.RESUPPLY_TASKS + resupply.STAGE_TASKS + ("주문 상태 확인 요청",)
        ctx = {
            "sid": sid, "comp": comp, "stages": resupply.STAGES, "A17": resupply.A17,
            "requests": conn.execute("SELECT * FROM deferral_request WHERE submission_id=? ORDER BY rowid",
                                     (sid,)).fetchall(),
            "tasks": conn.execute(f"SELECT * FROM task WHERE submission_id=? AND type IN ({','.join('?' * len(types))})"
                                  " ORDER BY id", (sid, *types)).fetchall(),
            "history": conn.execute(
                "SELECT * FROM history WHERE (entity_type='plan' AND entity_id=?) OR (entity_type='deferral' AND entity_id LIKE ?)"
                " OR entity_type IN ('capex_check','customer_holding') ORDER BY id", (str(sid), f"{sid}/%")).fetchall(),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(request, "plan.html", ctx)


@app.get("/lots/{lot_id}", response_class=HTMLResponse)
def lot_page(request: Request, lot_id: str):
    conn = _conn()
    try:
        comp = lots.compare(conn, lot_id)
        if comp is None:
            raise HTTPException(404)
        types = ("추가검사",)
        tasks = conn.execute("SELECT * FROM task WHERE (type IN ('추가검사', '폐기 검토') AND related_id IN "
                             "(SELECT hold_id FROM inventory_hold WHERE kind='trace')) OR (type='품질위원회 보고' AND "
                             "related_id IN (SELECT batch_id FROM src_batches WHERE input_lot_ids=?)) ORDER BY id",
                             (lot_id,)).fetchall()
        hist = conn.execute(
            """SELECT * FROM history WHERE (entity_type='lot' AND entity_id=?)
               OR (entity_type IN ('inventory','inventory_qty') AND entity_id IN (SELECT hold_id FROM inventory_hold WHERE kind='trace'))
               OR (entity_type='claim' AND entity_id IN (SELECT claim_id FROM claim_addition))
               OR (entity_type IN ('batch', 'batch_flag') AND entity_id IN (SELECT batch_id FROM src_batches WHERE input_lot_ids=?)
                   AND (reason LIKE '%추가검사%' OR to_state='표본 불량률 경보'))
               OR (entity_type='claim_watch' AND entity_id IN (SELECT batch_id || '/' || customer_id FROM claim_watch
                   WHERE batch_id IN (SELECT batch_id FROM src_batches WHERE input_lot_ids=?))) ORDER BY id""",
            (lot_id, lot_id, lot_id)).fetchall()
    finally:
        conn.close()
    conn = _conn()
    try:
        clue = rules.lot_clues(conn, lot_id)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "lot.html", {"comp": comp, "tasks": tasks, "history": hist, "clue": clue})


@app.get("/intakes/{iid}", response_class=HTMLResponse)
def intake_page(request: Request, iid: int):
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM reply_intake WHERE id=?", (iid,)).fetchone()
        if row is None:
            raise HTTPException(404)
        task = conn.execute("SELECT * FROM task WHERE id=?", (row["task_id"],)).fetchone()
        hist = conn.execute("SELECT * FROM history WHERE entity_type='reply' AND entity_id=? ORDER BY id", (str(iid),)).fetchall()
    finally:
        conn.close()
    v = json.loads(row["validation_json"])
    return templates.TemplateResponse(request, "intake.html", {
        "row": row, "task": task, "spec": replies.SPECS[row["task_type"]], "v": v,
        "structured": json.loads(row["structured_json"]) if row["structured_json"] else None,
        "info": json.loads(row["ai_info"]) if row["ai_info"] else {}, "history": hist})


@app.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_page(request: Request, batch_id: str):
    conn = _conn()
    try:
        state = conn.execute("SELECT * FROM batch_state WHERE batch_id = ?", (batch_id,)).fetchone()
        if state is None:
            raise HTTPException(404)
        ctx = {
            "state": state,
            "batch": conn.execute("SELECT * FROM src_batches WHERE batch_id = ?", (batch_id,)).fetchone(),
            "a": conn.execute("SELECT * FROM assessment WHERE batch_id = ?", (batch_id,)).fetchone(),
            "claims": conn.execute("SELECT * FROM claims_effective WHERE batch_id = ? ORDER BY claim_date",
                                   (batch_id,)).fetchall(),
            "corrections": conn.execute("SELECT * FROM claim_correction WHERE batch_id = ? ORDER BY id",
                                        (batch_id,)).fetchall(),
            "ships": conn.execute("SELECT s.*, w.reason watch FROM shipment_state s LEFT JOIN claim_watch w"
                                  " USING (batch_id, customer_id) WHERE s.batch_id = ? ORDER BY s.ship_date",
                                  (batch_id,)).fetchall(),
            "alert": conn.execute("SELECT * FROM sample_alert WHERE batch_id = ?", (batch_id,)).fetchone(),
            "watch": conn.execute("SELECT * FROM claim_watch WHERE batch_id = ? ORDER BY customer_id", (batch_id,)).fetchall(),
            "history": conn.execute(
                """SELECT * FROM history WHERE (entity_type IN ('batch','batch_flag') AND entity_id = ?)
                   OR (entity_type = 'claim' AND entity_id IN (SELECT claim_id FROM src_claims WHERE batch_id = ?))
                   OR (entity_type = 'shipment' AND entity_id IN (SELECT shipment_id FROM shipment_state WHERE batch_id = ?))
                   OR (entity_type IN ('inventory','inventory_qty') AND entity_id = ?)
                   OR (entity_type = 'claim_watch' AND entity_id LIKE ?) ORDER BY id""",
                (batch_id, batch_id, batch_id, f"INV-{batch_id}", f"{batch_id}/%")).fetchall(),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(request, "batch.html", ctx)


@app.post("/business-day/next")
def next_business_day(actor: str = Form(""), confirm_text: str = Form("")):
    """업무일을 다음 영업일로 넘긴다(주말 건너뜀). 기한이 지난 작업은 지연 표시 → 결정 대기 맨 위."""
    if confirm_text != "넘기기":
        raise HTTPException(400, "확인 문구가 필요합니다")
    return _act(lambda c: workflow.advance_business_day(c, actor), "/#decide-queue")


@app.get("/effects", response_class=HTMLResponse)
def effects_page(request: Request):
    """개선 효과 비교(추정치·비교 기준·가정 표시)와 추가 데이터 추적성 점검."""
    conn = _conn()
    try:
        ctx = {"e": effects.build(conn), "audit": provenance.audit(conn), "fields": provenance.FIELDS}
    finally:
        conn.close()
    return templates.TemplateResponse(request, "effects.html", ctx)


@app.get("/glossary", response_class=HTMLResponse)
def glossary_page(request: Request, nature: str = ""):
    sections = glossary.grouped()
    # 출처 성격 집계는 규칙(R1~R10)을 빼고 센다 — 규칙은 따로 집계(가정 = A-1~A-32만)
    counts = {n: sum(1 for t in glossary.TERMS if t["nature"] == n and not glossary.is_rule(t)) for n in glossary.NATURES}
    rule_counts = {n: sum(1 for t in glossary.TERMS if t["nature"] == n and glossary.is_rule(t)) for n in glossary.NATURES}
    return templates.TemplateResponse(request, "glossary.html", {
        "sections": sections, "natures": glossary.NATURES, "nature_help": glossary.NATURE_HELP, "counts": counts,
        "rule_counts": rule_counts, "rules": [t for t in glossary.TERMS if glossary.is_rule(t)],
        "nature": nature if nature in glossary.NATURES else "",
        "due_table": bizcal.due_table(AS_OF), "as_of": AS_OF, "calendar": bizcal.BASIS,
        "nature_class": {glossary.FACT: "t-ok", glossary.CLAIM: "t-warn", glossary.ASSUME: "s-대기", glossary.MOCK: "t-muted"}})


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    conn = _conn()
    try:
        rows = conn.execute("SELECT * FROM history ORDER BY id").fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "history.html", {"rows": rows})


# ---------- 판정 · 상신 · 의결 ----------

@app.post("/assess")
def assess():
    return _act(workflow.run_assessment, "/")


@app.post("/submissions")
def submit():
    return _act(workflow.create_submission, lambda sid: f"/submissions/{sid}" if sid else "/")


@app.post("/resubmissions")
def resubmit():
    return _act(workflow.create_resubmission, lambda sid: f"/submissions/{sid}" if sid else "/")


@app.post("/submissions/{sid}/summary")
def manual_summary(sid: int, author: str = Form(...), headline: str = Form(""), summary: str = Form(...),
                   actions: str = Form(""), questions: str = Form("")):
    return _act(lambda c: workflow.save_manual_summary(c, sid, author, headline, summary, actions, questions),
                f"/submissions/{sid}")


@app.post("/submissions/{sid}/summary/regenerate")
def regenerate_summary(sid: int, actor: str = Form("")):
    """수치 변경으로 요약 갱신이 필요한 상신 — 사람이 눌렀을 때만 AI 요약을 다시 만든다."""
    return _act(lambda c: workflow.regenerate_summary(c, sid, actor), f"/submissions/{sid}")


@app.post("/submissions/{sid}/batches/{batch_id}/decide")
def decide_batch(sid: int, batch_id: str, decision: str = Form(...), decider: str = Form(...),
                 reason: str = Form(...)):
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    return _act(lambda c: workflow.decide_batch(c, sid, batch_id, decision == "approve", decider, reason),
                f"/submissions/{sid}#decide")


# ---------- 클레임 접수 · 배치 추적 ----------

@app.get("/claims", response_class=HTMLResponse)
def claims_page(request: Request, new: str = ""):
    conn = _conn()
    try:
        last = None
        if new:
            row = conn.execute("SELECT evidence_json FROM history WHERE entity_type='claim_intake' AND entity_id=?"
                               " ORDER BY id DESC LIMIT 1", (new,)).fetchone()
            last = json.loads(row[0]) if row else None
        ctx = {
            "customers": conn.execute("SELECT customer_id, customer_name FROM src_customers ORDER BY customer_id").fetchall(),
            "products": conn.execute("SELECT product_id, product_name FROM src_products ORDER BY product_id").fetchall(),
            "batches": conn.execute("SELECT batch_id, product_id, production_date, CAST(produced_qty AS INTEGER) produced_qty"
                                    " FROM src_batches ORDER BY batch_id").fetchall(),
            "claim_types": corrections.claim_types(conn),
            "today": db.current_business_date(conn),
            "assessed": conn.execute("SELECT COUNT(*) FROM assessment").fetchone()[0],
            "added": conn.execute(
                """SELECT c.*, s.state, a.rate_prod, a.rate_ship FROM claim_addition c
                   JOIN batch_state s USING (batch_id) LEFT JOIN assessment a USING (batch_id)
                   WHERE c.source = ? ORDER BY c.created_at DESC, c.claim_id DESC""", (corrections.CLAIM_INTAKE,)).fetchall(),
            "last": last,
        }
    finally:
        conn.close()
    return templates.TemplateResponse(request, "claims.html", ctx)


@app.post("/claims")
def register_claim(customer_id: str = Form(""), product_id: str = Form(""), batch_id: str = Form(""),
                   qty: str = Form(""), confirmed: str = Form(""), claim_type: str = Form(""), actor: str = Form(""),
                   occurred_date: str = Form(""), applied_date: str = Form("")):
    """새 클레임 접수(모의) → claim_addition 저장 → 불량률 재계산·판정 갱신."""
    if not qty.strip().isdigit():
        raise HTTPException(409, "수량은 1 이상의 정수")
    flag = {"True": True, "False": False}.get(confirmed.strip())

    def run(c):
        out = corrections.register_claim(c, customer_id, product_id, batch_id, int(qty), flag, claim_type, actor,
                                         occurred_date, applied_date)
        db.record(c, "claim_intake", out["claim_id"], None, out.get("result"), "시스템", "규칙엔진",
                  f"클레임 접수 결과 — {out['batch_id']} {out.get('result')}", out)
        c.commit()
        return out

    return _act(run, lambda out: f"/claims?new={out['claim_id']}")


@app.get("/external", response_class=HTMLResponse)
def external_page(request: Request, new: str = ""):
    conn = _conn()
    try:
        last = None
        if new:
            row = conn.execute("SELECT evidence_json FROM history WHERE entity_type='external_review' AND entity_id=?"
                               " ORDER BY id DESC LIMIT 1", (new,)).fetchone()
            last = json.loads(row[0]) if row else None
        ctx = {"items": conn.execute("SELECT * FROM external_info ORDER BY id DESC").fetchall(),
               "products": conn.execute("SELECT product_id, product_name FROM src_products ORDER BY product_id").fetchall(),
               "today": db.current_business_date(conn), "last": last}
    finally:
        conn.close()
    return templates.TemplateResponse(request, "external.html", ctx)


@app.post("/external")
def register_external(source: str = Form(""), published_date: str = Form(""), applied_date: str = Form(""),
                      summary: str = Form(""), product_id: str = Form(""), batch_ids: str = Form(""), actor: str = Form("")):
    """외부 정보(언론 보도) 접수 → 판단 재검토(R5: 판정 유지·근거 이력) → 모니터링/사실 확인 작업."""
    def run(c):
        out = workflow.register_external_info(c, source, published_date, summary, product_id,
                                              [b for b in batch_ids.replace(" ", ",").split(",")], actor, applied_date)
        db.record(c, "external_review", out["external_id"], None, "재검토", "시스템", "규칙엔진",
                  f"외부 정보 {out['external_id']} 판단 재검토 결과", out)
        c.commit()
        return out

    return _act(run, lambda out: f"/external?new={out['external_id']}")


@app.get("/trace/{batch_id}", response_class=HTMLResponse)
def trace_page(request: Request, batch_id: str):
    conn = _conn()
    try:
        t = trace.build(conn, batch_id)
    finally:
        conn.close()
    if t is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "trace.html", {"t": t, "tasks": t["tasks"]})


# ---------- 검사 정정 (조건 변경) ----------

@app.post("/corrections")
def correct_claim(claim_id: str = Form(...), new_confirmed: str = Form(""), new_qty: str = Form(""),
                  corrector: str = Form(...), reason: str = Form(...), evidence: str = Form(...),
                  next: str = Form("/")):
    if new_qty.strip() and not new_qty.strip().isdigit():
        raise HTTPException(400, "수량은 0 이상의 정수")
    qty = int(new_qty) if new_qty.strip() else None
    return _act(lambda c: corrections.apply_correction(c, claim_id, new_confirmed or None, qty, corrector, reason,
                                                       evidence), next)


# ---------- Task 회신 ----------

@app.post("/tasks/{task_id}/advance")
def advance(task_id: int, actor: str = Form(""), next: str = Form("/")):
    return _act(lambda c: workflow.advance_task(c, task_id, actor), next)


@app.post("/tasks/{task_id}/quarantine")
def quarantine(task_id: int, qty: int = Form(...), actor: str = Form(...), note: str = Form(""),
               next: str = Form("/")):
    """물류 격리 수량 회신(모의) → 사내 잔여재고 추정치를 실측치로."""
    return _act(lambda c: workflow.record_quarantine(c, task_id, qty, actor, note), next)


@app.post("/tasks/{task_id}/inspection")
def inspection_reply(task_id: int, result: str = Form(...), inspected_qty: int = Form(...), defect_qty: str = Form(""),
                     actor: str = Form(...), next: str = Form("/")):
    """추가검사 회신(모의): 합격 → 출하 가능 / 불합격 → 클레임 등록·R1 재판정."""
    defect = int(defect_qty) if defect_qty.strip().isdigit() else None
    return _act(lambda c: workflow.record_inspection(c, task_id, result == "pass", inspected_qty, defect, actor), next)


@app.post("/tasks/{task_id}/shipment-route")
def shipment_route_reply(task_id: int, decision: str = Form(...), qty: str = Form(""), actor: str = Form(...),
                         next: str = Form("/")):
    """출하 경로 확인 회신(모의): 출하 확인(수량) / 출하 없음."""
    q = int(qty) if qty.strip().isdigit() else None
    return _act(lambda c: workflow.record_unlinked_reply(c, task_id, decision == "shipped", q, actor), next)


@app.post("/tasks/{task_id}/reply-text")
def reply_text(task_id: int, raw_text: str = Form(...), submitted_by: str = Form(...)):
    """자유 형식 회신 → AI 구조화 → 규칙 검증 → 회신 화면."""
    return _act(lambda c: replies.intake(c, task_id, raw_text, submitted_by), lambda iid: f"/intakes/{iid}")


@app.post("/intakes/{iid}/manual")
async def intake_manual(iid: int, request: Request):
    form = dict(await request.form())
    actor = form.pop("actor", "")
    return _act(lambda c: replies.manual_structure(c, iid, form, actor), f"/intakes/{iid}")


@app.post("/intakes/{iid}/apply")
def intake_apply(iid: int, actor: str = Form(...)):
    return _act(lambda c: replies.apply(c, iid, actor), f"/intakes/{iid}")


@app.post("/tasks/{task_id}/approval")
def approval_reply(task_id: int, decision: str = Form(...), actor: str = Form(...), reason: str = Form(...),
                   next: str = Form("/")):
    """영업 승인·투자 승인 Task 회신(모의)."""
    return _act(lambda c: resupply.reply_approval(c, task_id, decision == "approve", actor, reason), next)


@app.post("/tasks/{task_id}/customer-reply")
def customer_reply(task_id: int, decision: str = Form(...), actor: str = Form(...), reason: str = Form(...),
                   next: str = Form("/")):
    """납기 연기 동의 요청 Task — 고객 회신(모의)."""
    return _act(lambda c: resupply.reply_customer(c, task_id, decision == "agree", actor, reason), next)


# ---------- 재공급 계획 ----------

@app.post("/plans/{sid}/close-b")
def close_stage_b(sid: int, actor: str = Form(...)):
    """(b) 보유량 회신 마감 → 남은 부족분만 (a)로."""
    return _act(lambda c: resupply.close_b(c, sid, actor), f"/plans/{sid}")


@app.post("/plans/{sid}/choose-c")
def choose_capex(sid: int, option_id: str = Form(...), actor: str = Form(...)):
    return _act(lambda c: resupply.choose_c(c, sid, option_id, actor), f"/plans/{sid}")


@app.post("/plans/{sid}/holding")
def holding_form(sid: int, pair: str = Form(...), held_qty: int = Form(...), actor: str = Form(...)):
    """(b) 화면에서 고객 보유량 회신 입력(모의). pair = '배치|고객'."""
    batch_id, _, customer_id = pair.partition("|")
    return _act(lambda c: workflow.record_customer_holding(c, batch_id, customer_id, held_qty, actor, "화면 모의 회신"),
                f"/plans/{sid}")


@app.post("/tasks/{task_id}/procurement-reply")
def procurement_reply(task_id: int, decision: str = Form(...), actor: str = Form(...), reason: str = Form(...),
                      next: str = Form("/")):
    """조달 확인 Task 회신(모의): 증설안 선행 조건 가능/불가."""
    return _act(lambda c: resupply.reply_procurement(c, task_id, decision == "feasible", actor, reason), next)


class HoldingReply(BaseModel):
    batch_id: str
    customer_id: str
    held_qty: int
    actor: str
    source: str = "모의 회신"


@app.post("/api/customer-holdings")
def customer_holding(reply: HoldingReply):
    """고객 보유량 회신 입력 지점. AI ② 회신 구조화가 검증 후 이 경로로 넣는다."""
    conn = _conn()
    try:
        workflow.record_customer_holding(conn, reply.batch_id, reply.customer_id, reply.held_qty,
                                         reply.actor, reply.source)
    except TransitionError as e:
        raise HTTPException(409, str(e))
    finally:
        conn.close()
    return {"status": "ok"}


# ---------- 관리 ----------

@app.post("/admin/reset")
def reset(confirm_text: str = Form("")):
    # 화면 확인 창에서 '초기화'를 직접 입력한 요청만 처리한다(실수 방지).
    if confirm_text != "초기화":
        raise HTTPException(400, "확인 문구 '초기화'가 필요합니다")
    return _act(db.reset, "/")


@app.post("/presets/{key}")
def run_preset(key: str, confirm_text: str = Form("")):
    """시연 프리셋 — 초기화 후 기준선(두 배치 승인)을 만들고 조건 변경을 실행한다. AI 호출 없음."""
    if key not in presets.PRESETS:
        raise HTTPException(404)
    if confirm_text != "프리셋":
        raise HTTPException(400, "확인 문구가 필요합니다")
    return _act(presets.PRESETS[key][1], lambda url: url)


@app.get("/api/state")
def api_state():
    """테스트·검증용 현재 상태 요약."""
    conn = _conn()
    try:
        q = lambda sql: [dict(r) for r in conn.execute(sql)]
        return JSONResponse({
            "db": str(DB_PATH),
            "batches": q("SELECT * FROM batch_state WHERE state != '정상' OR investigate_reason LIKE '%R6%'"
                         " OR review_from IS NOT NULL"),
            "assessment": q("SELECT batch_id, confirmed_qty, rate_prod, rate_ship, is_target FROM assessment"
                            " WHERE confirmed_qty > 1000"),
            "shipments": q("SELECT state, COUNT(*) n FROM shipment_state GROUP BY state"),
            "inventory": q("SELECT * FROM inventory_hold"),
            "submissions": q("SELECT id, kind, status, ai_source, ai_status, ai_check, batch_ids FROM submission"),
            "decisions": q("SELECT * FROM decision ORDER BY decided_at"),
            "corrections": q("SELECT * FROM claim_correction ORDER BY id"),
            "customer_holdings": q("SELECT * FROM customer_holding"),
            "plans": q("SELECT submission_id, state, overall, alternative, option_id FROM resupply_plan"),
            "deferrals": q("SELECT submission_id, customer_id, qty, state FROM deferral_request ORDER BY rowid"),
            "tasks": q("SELECT id, type, dept, state, title FROM task ORDER BY id"),
            "lots": q("SELECT * FROM lot_status"),
            "claim_additions": q("SELECT * FROM claim_addition"),
            "external_info": q("SELECT * FROM external_info ORDER BY id"),
            "pending_targets": sorted(r[0] for r in conn.execute("SELECT batch_id FROM batch_state WHERE state='회수대상'")
                                      if r[0] not in workflow._open_submission_batches(conn)),
            "unlinked": q("SELECT * FROM unlinked_customer"),
            "sample_alerts": q("SELECT * FROM sample_alert"),
            "decision_queue": decisions.build(conn),
            "provenance": provenance.audit(conn),
            "business_date": db.current_business_date(conn),
            "claim_watch": q("SELECT * FROM claim_watch ORDER BY batch_id, customer_id"),
            "intakes": q("SELECT id, task_id, task_type, source, status, validation_json, structured_json FROM reply_intake"),
            "shipments_added": q("SELECT * FROM shipment_state WHERE source != 'SBL'"),
            "ai_configured": ai_configured(),
            "ai_provider": f"{ai.label()} {ai.model_name()}",
            "history_count": conn.execute("SELECT COUNT(*) FROM history").fetchone()[0],
        })
    finally:
        conn.close()
