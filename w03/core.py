"""상태 전이·Task 공통 도구. 모든 전이는 허용 목록으로 검사하고 history에 기록한다."""
import json

from . import bizcal
from .db import current_business_date, now, record

HOLD = "보류(재검토 대기)"
OPEN_TASK_STATES = ("대기", "진행", HOLD)

# 엔터티 → (테이블, 키 컬럼, 허용 전이)
ENTITIES = {
    "batch": ("batch_state", "batch_id", {
        "정상": {"회수대상"},
        "회수대상": {"회수의결", "보류", "재검토"},
        "회수의결": {"회수완료", "재검토"},
        "보류": {"회수대상", "재검토"},
        "재검토": {"회수대상", "회수의결", "정상"},
    }),
    "shipment": ("shipment_state", "shipment_id", {
        "정상": {"회수요청"},
        "회수요청": {"회수확인", "요청철회"},
        "회수확인": {"회수완료"},
    }),
    "inventory": ("inventory_hold", "hold_id", {
        "정상": {"출하보류", "추가검사필요"},
        "출하보류": {"처분완료", "보류해제"},
        "추가검사필요": {"출하가능", "검사불합격", "출하보류"},   # 같은 로트 역추적(확장)
        "검사불합격": {"출하보류"},
        "출하가능": {"출하보류"},
    }),
    "lot": ("lot_status", "lot_id", {
        "사용가능": {"사용정지"},
        "사용정지": {"사용가능"},
    }),
    "task": ("task", "id", {
        "대기": {"진행", HOLD, "취소"},
        "진행": {"완료", HOLD, "취소"},
        HOLD: {"대기", "취소"},
    }),
    # 재공급 계획: 대안을 (b) → (a) → (c) 순서로 적용한다(앞 단계에서 남은 부족분만 다음 단계로).
    "plan": ("resupply_plan", "submission_id", {
        "판정완료": {"(b) 회신대기", HOLD},
        "(b) 회신대기": {"(a) 승인대기", "(c) 선택대기", "대안확정", "판정완료", HOLD},
        "(a) 승인대기": {"(a) 고객동의대기", "(c) 선택대기", "판정완료", HOLD},
        "(a) 고객동의대기": {"대안확정", "(c) 선택대기", "판정완료", HOLD},
        "(c) 선택대기": {"(c) 조달확인대기", "(c) 승인대기", "대안소진", "판정완료", HOLD},
        "(c) 조달확인대기": {"(c) 승인대기", "(c) 선택대기", "대안소진", "판정완료", HOLD},
        "(c) 승인대기": {"대안확정", "(c) 선택대기", "대안소진", "판정완료", HOLD},
        "대안확정": {"(b) 회신대기", "(c) 선택대기", "판정완료", HOLD},
        "대안소진": {"(c) 선택대기", "판정완료", HOLD},
        HOLD: {"판정완료", "(b) 회신대기", "(a) 승인대기", "(a) 고객동의대기", "(c) 선택대기", "(c) 조달확인대기",
               "(c) 승인대기", "대안확정", "대안소진"},
    }),
}

SYSTEM_ACTOR = ("시스템", "규칙엔진")
AUTO = ("시스템", "워크플로")


class TransitionError(ValueError):
    pass


def marks(xs) -> str:
    return ",".join("?" * len(xs))


def set_state(conn, entity, key, to_state, actor_type, actor, reason, evidence=None):
    table, col, allowed = ENTITIES[entity]
    row = conn.execute(f"SELECT state FROM {table} WHERE {col} = ?", (key,)).fetchone()
    if row is None:
        raise TransitionError(f"{entity} {key} 없음")
    cur = row["state"]
    if to_state not in allowed.get(cur, set()):
        raise TransitionError(f"{entity} {key}: {cur} → {to_state} 전이는 허용되지 않음")
    conn.execute(f"UPDATE {table} SET state = ? WHERE {col} = ?", (to_state, key))
    record(conn, entity, str(key), cur, to_state, actor_type, actor, reason, evidence)


# ---------- Task ----------

def new_task(conn, sid, type_, title, dept, related_type, related_id, detail) -> int:
    today = current_business_date(conn)
    due = bizcal.due_date(type_, today)
    cur = conn.execute(
        "INSERT INTO task (created_at, type, title, dept, related_type, related_id, detail_json, submission_id, created_by,"
        " business_date, due_date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (now(), type_, title, dept, related_type, related_id, json.dumps(detail, ensure_ascii=False), sid, AUTO[1],
         today, due),
    )
    record(conn, "task", str(cur.lastrowid), None, "대기", *AUTO,
           f"[{dept}] {title} · 처리 목표 {bizcal.label(due)}(A-28 {bizcal.due_rule(type_)[0]}영업일)", detail)
    return cur.lastrowid


def open_task(conn, sid, type_, related_id):
    return conn.execute(
        f"SELECT * FROM task WHERE submission_id=? AND type=? AND related_id=? AND state IN ({marks(OPEN_TASK_STATES)})"
        " ORDER BY id DESC", (sid, type_, related_id, *OPEN_TASK_STATES)).fetchone()


def update_task(conn, row, title, detail, reason):
    if row["title"] == title and json.loads(row["detail_json"]) == detail:
        return
    conn.execute("UPDATE task SET title=?, detail_json=? WHERE id=?",
                 (title, json.dumps(detail, ensure_ascii=False), row["id"]))
    record(conn, "task", str(row["id"]), row["state"], row["state"], *AUTO, f"{reason}: {title}", detail)


def replace_task(conn, sid, type_, dept, related_type, related_id, title, detail) -> int:
    """열린 Task가 있으면 제목·내용을 갱신, 없으면 생성."""
    row = open_task(conn, sid, type_, related_id)
    if row is None:
        return new_task(conn, sid, type_, title, dept, related_type, related_id, detail)
    if row["title"] != title:
        conn.execute("UPDATE task SET title=?, detail_json=? WHERE id=?",
                     (title, json.dumps(detail, ensure_ascii=False), row["id"]))
        record(conn, "task", str(row["id"]), row["state"], row["state"], *AUTO,
               f"재판정 갱신: {row['title']} → {title}", detail)
    return row["id"]


def hold_task(conn, task_id, reason):
    row = conn.execute("SELECT state FROM task WHERE id=?", (task_id,)).fetchone()
    if row and row["state"] in ("대기", "진행"):
        set_state(conn, "task", task_id, HOLD, *AUTO, reason)


def resume_task(conn, task_id, reason):
    row = conn.execute("SELECT state FROM task WHERE id=?", (task_id,)).fetchone()
    if row and row["state"] == HOLD:
        set_state(conn, "task", task_id, "대기", *AUTO, reason)


def cancel_task(conn, task_id, reason):
    row = conn.execute("SELECT state FROM task WHERE id=?", (task_id,)).fetchone()
    if row and row["state"] in OPEN_TASK_STATES:
        set_state(conn, "task", task_id, "취소", *AUTO, reason)


def finish_task(conn, task_id, actor, reason):
    """회신형 Task(승인·동의·격리)를 대기/진행 → 완료로 닫는다."""
    row = conn.execute("SELECT state FROM task WHERE id=?", (task_id,)).fetchone()
    if row["state"] == "대기":
        set_state(conn, "task", task_id, "진행", "사람", actor, "회신 접수")
    set_state(conn, "task", task_id, "완료", "사람", actor, reason)
