"""추가 데이터 추적성 점검 (A05 §6: 추가 행에는 식별자·적용 시점·원본 연결·생성 이유를 남긴다).

원본 CSV는 바꾸지 않고, 앱이 만든 데이터(정정·모의 회신·추가 클레임·검사 결과·의결 등)만 점검한다.
"""
# (종류, 출처 성격, 테이블, 조건, 식별자, 적용 시점(업무일), 원본 연결, 생성 이유)
SPECS = [
    ("검사 정정", "사람 입력(모의)", "claim_correction", "1=1",
     "'K-' || id", "business_date", "claim_id || ' (원본 qc_claims_cv)'", "reason || ' · 근거 ' || evidence"),
    ("추가 클레임", "모의 입력·검사 결과", "claim_addition", "1=1",
     "claim_id", "applied_date", "batch_id || ' · ' || origin_ref", "reason"),
    ("고객 보유량 회신", "모의 회신", "customer_holding", "1=1",
     "batch_id || '/' || customer_id", "business_date", "'SBL ' || batch_id || '→' || customer_id || ' · ' || source",
     "actor || ' — 보유 ' || held_qty || 'EA'"),
    ("격리 실측·검사 수량", "모의 회신", "inventory_hold", "measured_qty IS NOT NULL",
     "hold_id", "measured_business_date", "measured_source", "'실측 ' || measured_qty || 'EA로 추정 ' || est_qty || 'EA 대체'"),
    ("확인 회신 출하", "모의 회신", "shipment_state", "source != 'SBL'",
     "shipment_id", "business_date", "origin_ref", "reason"),
    ("회신 접수(구조화)", "모의 회신", "reply_intake", "1=1",
     "'회신 #' || id", "business_date", "'Task #' || task_id", "task_type || ' 자유 형식 회신 — ' || submitted_by"),
    ("위원회 의결", "모의 의결", "decision", "1=1",
     "submission_id || '/' || batch_id", "business_date", "'상신 #' || submission_id", "result || ': ' || reason"),
    ("납기 연기 동의 회신", "모의 회신", "deferral_request", "state IN ('동의', '거절')",
     "submission_id || '/' || customer_id", "replied_business_date", "'Task #' || task_id", "state || ': ' || reply_reason"),
    ("표본 불량률 경보", "검사 결과(모의)", "sample_alert", "1=1",
     "batch_id", "business_date", "origin_ref", "data_source || ' · 표본 불량률 ' || rate_pct || '%'"),
    ("외부 정보(언론 보도)", "외부 정보(미확정)", "external_info", "1=1",
     "id", "applied_date", "origin_ref", "reason"),
    ("클레임 모니터링 강화", "계산 결과", "claim_watch", "1=1",
     "batch_id || '/' || customer_id", "business_date", "'SBL ' || batch_id || '→' || customer_id", "reason"),
]
FIELDS = ("식별자", "적용 시점", "원본 연결", "생성 이유")


def audit(conn) -> list[dict]:
    out = []
    for kind, nature, table, where, *exprs in SPECS:
        cols = ", ".join(f"({e}) AS f{i}" for i, e in enumerate(exprs))
        rows = conn.execute(f"SELECT {cols} FROM {table} WHERE {where}").fetchall()
        missing = {f: sum(1 for r in rows if r[i] in (None, "")) for i, f in enumerate(FIELDS)}
        out.append({"kind": kind, "nature": nature, "table": table, "rows": len(rows), "missing": missing,
                    "ok": not any(missing.values()),
                    "samples": [dict(zip(FIELDS, r)) for r in rows[:2]]})
    return out
