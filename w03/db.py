"""SQLite 연결·스키마·원본 적재. 원본 CSV는 읽기만 한다."""
import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from .config import AS_OF, DB_PATH, SOURCE_DIR, SOURCE_TABLES

KST = timezone(timedelta(hours=9))
SCHEMA_VERSION = 9   # 스키마가 바뀌면 올린다 — 기동 시 버전이 다르면 재적재

SCHEMA = """
-- 앱 상태: business_date = 현재 업무일(A05 업무 달력, 하루씩 넘긴다 — 원본 기준일 AS_OF와 별개, 가정 A-29)
CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS batch_state (
    batch_id TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT '정상',
    investigate_flag INTEGER NOT NULL DEFAULT 0,
    investigate_reason TEXT NOT NULL DEFAULT '',
    review_from TEXT            -- 재검토로 들어오기 직전 상태(재상신에서 '회수 유지'면 이 상태로 복귀)
);
-- 검사 정정 (원본 불변 — 정정값은 여기 누적, 최신 정정이 유효값, 가정 A-9)
CREATE TABLE IF NOT EXISTS claim_correction (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT, batch_id TEXT, field TEXT,
    original_value TEXT, previous_value TEXT, corrected_value TEXT,
    corrector TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL, created_at TEXT,
    business_date TEXT                      -- 적용 업무일
);
-- 재공급 계획 (상신 단위)
CREATE TABLE IF NOT EXISTS resupply_plan (
    submission_id INTEGER PRIMARY KEY,
    state TEXT, overall TEXT, alternative TEXT, option_id TEXT,
    detail_json TEXT, updated_at TEXT
);
-- 대안 (a) 납기 연기 동의 요청 (고객별, 모의 회신)
CREATE TABLE IF NOT EXISTS deferral_request (
    submission_id INTEGER, customer_id TEXT,
    order_ids TEXT, qty INTEGER, amount INTEGER,
    state TEXT NOT NULL DEFAULT '요청', task_id INTEGER, reply_reason TEXT,
    replied_business_date TEXT,             -- 모의 고객 회신 업무일
    PRIMARY KEY (submission_id, customer_id)
);
CREATE TABLE IF NOT EXISTS assessment (
    batch_id TEXT PRIMARY KEY,
    confirmed_claims INTEGER, confirmed_qty INTEGER,
    produced_qty INTEGER, shipped_qty INTEGER,
    rate_prod REAL, rate_ship REAL,
    recall_prod INTEGER, recall_ship INTEGER,
    is_target INTEGER, warning TEXT, computed_at TEXT
);
-- 고객 인도분 (SBL 연결분, A-4)
CREATE TABLE IF NOT EXISTS shipment_state (
    shipment_id TEXT PRIMARY KEY,
    batch_id TEXT, customer_id TEXT, ship_date TEXT, shipped_qty INTEGER,
    state TEXT NOT NULL DEFAULT '정상',
    source TEXT NOT NULL DEFAULT 'SBL',     -- SBL | 확인 회신(출하 경로 확인으로 추가, 가정 A-21)
    business_date TEXT, origin_ref TEXT, reason TEXT   -- 앱 추가 행만: 적용 업무일 · 출처(Task) · 생성 이유
);
-- 사내 잔여재고 = produced_qty − SBL 출하 합 (추정, A-5). 물류 격리 회신이 오면 실측치로 바꾼다.
CREATE TABLE IF NOT EXISTS inventory_hold (
    hold_id TEXT PRIMARY KEY,
    batch_id TEXT, est_qty INTEGER, basis TEXT,
    measured_qty INTEGER, qty_basis TEXT NOT NULL DEFAULT '추정',
    measured_by TEXT, measured_at TEXT,
    measured_business_date TEXT, measured_source TEXT,   -- 실측 회신 업무일 · 출처(격리/추가검사 Task, 모의)
    state TEXT NOT NULL DEFAULT '정상',
    kind TEXT NOT NULL DEFAULT 'recall',    -- recall(회수의결 배치) | trace(같은 원료 로트 역추적)
    submission_id INTEGER
);
-- 원료 로트 상태 (역추적 확장): 회수의결 배치의 투입 로트 → 사용정지
CREATE TABLE IF NOT EXISTS lot_status (
    lot_id TEXT PRIMARY KEY, state TEXT, reason TEXT, submission_id INTEGER, updated_at TEXT
);
-- 추가 클레임 (원본 불변 — 추가검사 불합격 등으로 등록, claims_effective에 합쳐진다)
CREATE TABLE IF NOT EXISTS claim_addition (
    claim_id TEXT PRIMARY KEY, claim_date TEXT, customer_id TEXT, product_id TEXT, batch_id TEXT, lot_id TEXT,
    claim_type TEXT, defect_confirmed TEXT, qty_affected TEXT, status TEXT, source TEXT, created_at TEXT,
    applied_date TEXT,                     -- 적용일(판정에 반영된 업무일). claim_date = 발생일 (A05: 발생일·적용일 분리)
    origin_ref TEXT, reason TEXT           -- 출처(화면 접수 / 추가검사 Task #n) · 생성 이유
);
-- 정보 부족: 확정 클레임은 있으나 해당 배치 SBL 출하 기록이 없는 고객
CREATE TABLE IF NOT EXISTS unlinked_customer (
    batch_id TEXT, customer_id TEXT, claims INTEGER, claim_qty INTEGER,
    state TEXT NOT NULL DEFAULT '출하 연결 미확인', confirmed_qty INTEGER, task_id INTEGER, submission_id INTEGER,
    PRIMARY KEY (batch_id, customer_id)
);
-- 추가검사 불합격 → 표본 불량률 경보(R1 판정과 별개, 가정 A-27). 검사 수치의 출처(프리셋 모의 / 화면 모의 회신)를 함께 둔다.
CREATE TABLE IF NOT EXISTS sample_alert (
    batch_id TEXT PRIMARY KEY, lot_id TEXT, inspected_qty INTEGER, defect_qty INTEGER, rate_pct REAL,
    data_source TEXT, report_task_id INTEGER, dispose_task_id INTEGER, submission_id INTEGER, created_at TEXT,
    business_date TEXT, origin_ref TEXT
);
-- 표본 불량률 경보 배치의 기출하 고객 → 클레임 모니터링 강화 표시(회수요청 아님)
CREATE TABLE IF NOT EXISTS claim_watch (
    batch_id TEXT, customer_id TEXT, shipped_qty INTEGER, reason TEXT, created_at TEXT,
    business_date TEXT,
    PRIMARY KEY (batch_id, customer_id)
);
-- 외부 정보(언론 보도) 접수 — 회수 근거가 아님(R5). 판단 재검토 근거와 모니터링·사실 확인 작업의 출처
CREATE TABLE IF NOT EXISTS external_info (
    id TEXT PRIMARY KEY, source TEXT, published_date TEXT, applied_date TEXT, summary TEXT, product_id TEXT,
    batch_ids TEXT, actor TEXT, created_at TEXT, business_date TEXT, origin_ref TEXT, reason TEXT
);
-- 자유 형식 회신 접수 → AI 구조화 → 규칙 검증 → 사람 반영
CREATE TABLE IF NOT EXISTS reply_intake (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER, task_type TEXT, raw_text TEXT, submitted_by TEXT, created_at TEXT,
    structured_json TEXT, source TEXT, ai_info TEXT,
    validation_json TEXT, status TEXT,
    applied_by TEXT, applied_at TEXT,
    business_date TEXT                      -- 접수 업무일
);
-- 고객 보유량 회신 (3단계 AI ② 회신 구조화가 채울 자리). 있으면 재공급 필요 수량을 상한값 대신 이 값으로 계산.
CREATE TABLE IF NOT EXISTS customer_holding (
    batch_id TEXT, customer_id TEXT, held_qty INTEGER NOT NULL,
    reported_at TEXT, actor TEXT, source TEXT,
    business_date TEXT,                     -- 회신 업무일
    PRIMARY KEY (batch_id, customer_id)
);
-- ai_status: 통과 | 경고(수치 불일치) | 경고(표현) | 요약 실패 - 수동 작성 | 수동 작성 완료
CREATE TABLE IF NOT EXISTS submission (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT '회수상신',     -- 회수상신 | 재상신(정정 후 기준 미달 배치의 회수 해제 여부)
    created_at TEXT, batch_ids TEXT, facts_json TEXT,
    ai_json TEXT, ai_source TEXT, ai_status TEXT, ai_check TEXT,
    status TEXT NOT NULL DEFAULT '상신'
);
-- 품질위원회 배치별 의결 (모의 입력)
CREATE TABLE IF NOT EXISTS decision (
    submission_id INTEGER, batch_id TEXT, result TEXT,
    decider TEXT, reason TEXT NOT NULL, decided_at TEXT,
    business_date TEXT,                     -- 의결 업무일(모의)
    PRIMARY KEY (submission_id, batch_id)
);
CREATE TABLE IF NOT EXISTS task (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT, type TEXT, title TEXT, dept TEXT,
    related_type TEXT, related_id TEXT,
    business_date TEXT, due_date TEXT,      -- 생긴 업무일 · 처리 목표일(가정 A-28, 영업일)
    delayed_at TEXT,                        -- 업무일을 넘겨 기한이 지난 날(지연 표시, 상태는 그대로)
    state TEXT NOT NULL DEFAULT '대기',
    detail_json TEXT, submission_id INTEGER, created_by TEXT
);
-- at = 기록 시각(실제), business_date = 업무일(가상, app_state.business_date)
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT, business_date TEXT, entity_type TEXT, entity_id TEXT,
    from_state TEXT, to_state TEXT,
    actor_type TEXT, actor TEXT, reason TEXT, evidence_json TEXT
);
"""


# 유효 클레임 = 원본 + 최신 정정값. 판정·영향 계산은 모두 이 뷰를 읽는다.
CLAIMS_VIEW = """
CREATE VIEW claims_effective AS
SELECT c.claim_id, c.claim_date, c.customer_id, c.product_id, c.batch_id, c.lot_id, c.claim_type, c.status,
       COALESCE((SELECT k.corrected_value FROM claim_correction k
                 WHERE k.claim_id = c.claim_id AND k.field = 'defect_confirmed' ORDER BY k.id DESC LIMIT 1),
                c.defect_confirmed) AS defect_confirmed,
       COALESCE((SELECT k.corrected_value FROM claim_correction k
                 WHERE k.claim_id = c.claim_id AND k.field = 'qty_affected' ORDER BY k.id DESC LIMIT 1),
                c.qty_affected) AS qty_affected,
       c.defect_confirmed AS src_defect_confirmed, c.qty_affected AS src_qty_affected,
       (SELECT COUNT(*) FROM claim_correction k WHERE k.claim_id = c.claim_id) AS corrections,
       '원본' AS origin
FROM src_claims c
UNION ALL
SELECT a.claim_id, a.claim_date, a.customer_id, a.product_id, a.batch_id, a.lot_id, a.claim_type, a.status,
       COALESCE((SELECT k.corrected_value FROM claim_correction k
                 WHERE k.claim_id = a.claim_id AND k.field = 'defect_confirmed' ORDER BY k.id DESC LIMIT 1),
                a.defect_confirmed),
       COALESCE((SELECT k.corrected_value FROM claim_correction k
                 WHERE k.claim_id = a.claim_id AND k.field = 'qty_affected' ORDER BY k.id DESC LIMIT 1),
                a.qty_affected),
       a.defect_confirmed, a.qty_affected,
       (SELECT COUNT(*) FROM claim_correction k WHERE k.claim_id = a.claim_id),
       '추가(' || a.source || ')'
FROM claim_addition a;
"""


def now() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def current_business_date(conn) -> str:
    """현재 업무일. 앱 상태가 없으면 원본 기준일."""
    try:
        row = conn.execute("SELECT value FROM app_state WHERE key='business_date'").fetchone()
    except sqlite3.OperationalError:
        return AS_OF
    return row[0] if row else AS_OF


def set_business_date(conn, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO app_state VALUES ('business_date', ?)", (value,))


def record(conn, entity_type, entity_id, from_state, to_state, actor_type, actor, reason, evidence=None):
    """모든 상태 변경은 여기로 기록한다 (누가·언제·근거)."""
    conn.execute(
        "INSERT INTO history (at, business_date, entity_type, entity_id, from_state, to_state, actor_type, actor,"
        " reason, evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (now(), current_business_date(conn), entity_type, entity_id, from_state, to_state, actor_type, actor, reason,
         json.dumps(evidence, ensure_ascii=False) if evidence is not None else None),
    )


def load_sources(conn) -> dict:
    """원본 CSV(파이프 구분)를 src_* 테이블로 복사한다. 값은 TEXT 그대로 둔다."""
    counts = {}
    for table, rel in SOURCE_TABLES.items():
        path = SOURCE_DIR / rel
        with open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f, delimiter="|"))
        header, body = rows[0], rows[1:]
        cols = ", ".join(f'"{c}" TEXT' for c in header)
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.execute(f'CREATE TABLE "{table}" ({cols})')
        marks = ", ".join("?" for _ in header)
        conn.executemany(f'INSERT INTO "{table}" VALUES ({marks})', body)
        counts[table] = len(body)
    return counts


def needs_reset(conn) -> bool:
    return conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION


def reset(conn) -> dict:
    """운영 테이블을 비우고 원본을 다시 적재한다(시나리오 재실행용)."""
    conn.execute("DROP VIEW IF EXISTS claims_effective")
    for t in ("batch_state", "assessment", "shipment_state", "inventory_hold", "customer_holding", "submission",
              "decision", "task", "history", "claim_correction", "resupply_plan", "deferral_request",
              "lot_status", "claim_addition", "unlinked_customer", "reply_intake", "sample_alert", "claim_watch",
              "app_state", "external_info"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    set_business_date(conn, AS_OF)
    counts = load_sources(conn)
    conn.executescript(CLAIMS_VIEW)
    conn.execute(
        "INSERT INTO batch_state (batch_id) SELECT batch_id FROM src_batches"
    )
    conn.execute(
        "INSERT INTO shipment_state (shipment_id, batch_id, customer_id, ship_date, shipped_qty)"
        " SELECT shipment_id, batch_id, customer_id, ship_date, CAST(shipped_qty AS INTEGER) FROM src_sbl"
    )
    record(conn, "system", "데이터룸", None, "적재", "시스템", "적재기",
           "원본 CSV를 SQLite로 복사(원본 파일은 변경하지 않음)", {"rows": counts, "source": str(SOURCE_DIR)})
    conn.commit()
    return counts
