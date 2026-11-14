"""규칙·가정·용어 사전 — 화면의 R1·A-5 같은 번호에 한 줄 설명(툴팁)을 붙이고, /glossary에서 출처 성격과 함께 보여 준다.

출처 성격
- 원본 사실: 원본 CSV·정책 문서에 그대로 있는 값·규정
- 부서 주장: 증설안 비고·RFP 요약·회의록처럼 부서가 적은 서술(검증되지 않은 조건)
- 가정: 원본에 없어 설계에서 정한 것(DESIGN.md 가정 A-x)
- 모의: 시연용 입력·회신(실제 업무 데이터가 아님)
화면 문장의 코드명(필드·문서 이름)은 한글 업무 용어로 보여 주고, 원래 이름은 툴팁에만 둔다(CODE_TERMS, terms()).
"""
import re

from markupsafe import Markup, escape

FACT, CLAIM, ASSUME, MOCK = "원본 사실", "부서 주장", "가정", "모의"
NATURES = (FACT, CLAIM, ASSUME, MOCK)
NATURE_HELP = {
    FACT: "원본 CSV·정책 문서에 그대로 있는 값·규정",
    CLAIM: "증설안 비고·RFP 요약·회의록 등 부서가 적은 서술 — 검증되지 않은 조건",
    ASSUME: "원본에 없어 설계에서 정한 것 — 바뀌면 결과가 달라질 수 있음",
    MOCK: "시연용 입력·회신 — 실제 업무 데이터가 아님",
}

# (코드, 이름, 한 줄 설명, 출처 성격, 출처)
_TERMS = [
    # ---- 판단 규칙 ----
    ("R1", "회수 판정 기준", "확정 불량 수량 ÷ 배치 생산량 또는 출하량 중 하나라도 10% 이상이면 회수대상", FACT,
     "원본 사실: 규격이탈 확정 조건·배치 불량률 10% 이상(quality_policy · calculation_definitions 1.9) + "
     "가정 A-2: 생산량·출하량 두 분모 중 하나라도 10% 이상이면 회수대상", ASSUME),
    ("R2", "조사대상(미확정·보류)", "미확정 클레임 진행 중이거나 원본 QC 결과가 보류인 배치는 조사대상 표시만 — 회수 근거 아님", FACT,
     "quality_policy"),
    ("R3", "회수 실행은 위원회 의결", "회수 실행은 품질위원회가 의결한다 — 시스템·AI는 상신만", FACT, "approval_authority_policy"),
    ("R4", "원인 조사 별도 트랙", "회수 요건을 충족하면 원인 규명을 기다리지 않고, 원인 조사는 별도 작업으로 진행", FACT, "quality_policy"),
    ("R5", "언론 보도는 근거 아님", "언론 보도는 회수 판단 근거로 쓰지 않는다", FACT, "quality_policy · ext_news_quality_incident"),
    ("R6", "같은 로트 조사대상", "회수대상과 같은 원료 로트를 쓴 배치에 조사대상 표시(상태는 그대로)", ASSUME, "가정 A-3"),
    ("R7", "정정 경로", "클레임 정정 → 불량률 재계산 → 여전히 10% 이상이면 유지, 미만이면 재검토·재상신", ASSUME, "가정 A-7·A-14"),
    ("R8", "재공급 판정", "필요량 ÷ L-02 1기간 여유량 ≤ 1이면 가능, 넘으면 대안필요 · 원료 로트·발주로 조달 판정, 둘 중 나쁜 쪽", ASSUME,
     "입력은 strat_capacity_cv·material_lot_master_cv(원본), 판정 기준은 설계(A-6·A-8)"),
    ("R9", "원료 로트 역추적", "회수의결 배치의 원료 로트를 사용정지하고, 같은 로트 정상 배치 재고는 출하 전 추가검사", ASSUME,
     "가정 A-20·A-22·A-23·A-27"),
    ("R10", "출하 연결 미확인 보류", "확정 클레임은 있지만 그 배치를 받은 출하 기록(SBL)이 없는 고객은 회수요청 대신 보류·확인 요청", ASSUME,
     "가정 A-21"),
    # ---- 가정 ----
    ("A-1", "상태 흐름·Task 종류", "배치·출하·재고·Task의 상태 단계와 작업 종류는 설계에서 정했다", ASSUME, "설계"),
    ("A-2", "두 분모 중 하나 기준", "생산량 분모·출하량 분모 중 어느 한쪽이라도 10% 이상이면 회수대상", ASSUME, "calculation_definitions 해석"),
    ("A-3", "같은 로트 → 조사대상", "같은 원료 로트를 쓴 배치는 조사대상으로 본다", ASSUME, "설계"),
    ("A-4", "고객 인도분 = SBL 연결분", "고객이 받은 배치 수량은 출하-배치 연결표(SBL) 기준", ASSUME, "shipment_batch_link_cv 해석"),
    ("A-5", "사내 잔여재고 추정", "사내 재고 = 생산량 − SBL 출하 합(실재고 자료가 없어 추정치)", ASSUME, "원본에 재고 자료 없음"),
    ("A-6", "생산능력 단위 미확정", "L-02 생산능력의 기간 단위가 원본에 없음 — 판정은 '필요 ÷ 1기간 여유'라 단위와 무관", ASSUME,
     "strat_capacity_cv"),
    ("A-7", "정정 경로", "정정 후 재계산 결과로 판정 유지/재검토를 나눈다", ASSUME, "설계"),
    ("A-8", "재공급 필요량 상한값", "고객 보유량 회신 전에는 출하분 전량을 재공급 필요량으로 본다(상한값)", ASSUME, "설계"),
    ("A-9", "정정 누적", "정정은 별도 테이블에 쌓고 최신 정정값을 쓴다 — 원본 불변", ASSUME, "설계"),
    ("A-10", "납기 연기 승인권자 = 영업 임원", "원본에 납기 연기 권한이 없어 '재협상·판가 = 영업 임원'에서 유추", ASSUME,
     "approval_authority_policy 유추"),
    ("A-11", "추정 납기", "주문일 + 영업팀별 주문→출하 소요일 중앙값 — 달력일(국내1 60일·국내2 61일·해외 61일, 영업일로는 43일)", ASSUME,
     "sales_orders_cv × sales_shipments_cv의 날짜 차(주말 포함 달력일)"),
    ("A-12", "미출하 주문·납기 경과 제외", "출하 기록 없는 주문을 미출하로 보고, 추정 납기가 이미 지난 주문은 확인 대상으로 분리", ASSUME, "설계"),
    ("A-13", "연기 거절 → 다음 순번", "고객이 납기 연기를 거절하면 그 고객 주문을 빼고 다음 순번으로 다시 고른다", ASSUME, "설계"),
    ("A-14", "재상신 = 회수 해제 여부", "재상신은 회수를 해제할지 묻는다(해제 승인 → 정상, 해제 거절 → 회수 유지)", ASSUME, "설계"),
    ("A-15", "재상신 요약은 규칙 기반", "재상신 요약은 AI 없이 정정 내역·재계산 수치로 만든다", ASSUME, "설계"),
    ("A-16", "증설 효과 비교", "증설안은 (a)까지 적용하고 남은 부족분과 추가 능력을 비교(리드타임 공백 미반영)", ASSUME, "설계"),
    ("A-17", "연기는 순서 변경", "납기 연기는 생산능력을 만들지 않고 이후 기간 여유량을 잠식한다", ASSUME, "설계"),
    ("A-18", "증설안 조건부 충족", "증설안 비고에 '원료 재계약·처방 재검증 필요'가 있으면 조달 확인 회신 전까지 '조건부 충족'으로 둔다", ASSUME,
     "설계 판단 — 근거는 부서 주장 'OPT-02 비고'(strat_capex_plan_cv.note)"),
    ("A-19", "(b) 마감 시 미회신분", "보유량 회신을 마감하면 미회신 고객분은 상한값으로 남긴다", ASSUME, "설계"),
    ("A-20", "역추적 대상", "회수의결 배치와 같은 원료 로트를 쓴 정상 배치의 사내 재고(추정)", ASSUME, "설계"),
    ("A-21", "확인된 출하 추가", "출하 경로 확인으로 확인된 출하는 앱 테이블에 추가(SBL 원본 불변), 수량 상한 있음", ASSUME, "설계"),
    ("A-22", "재공급 가능 재고", "같은 로트 재고 중 추가검사 합격분만 재공급 필요량에서 뺀다", ASSUME, "설계"),
    ("A-23", "추가검사 불합격 → 클레임", "불합격 불량 수량은 확정 클레임으로 등록하고 배치 분모로 재판정", ASSUME, "설계"),
    ("A-24", "회신 배치 자동 지정", "회신에 배치가 없고 대상 배치가 하나뿐이면 그 배치로 본다", ASSUME, "설계"),
    ("A-25", "AI 수량은 원문 대조", "AI가 뽑은 수량은 회신 원문에 같은 숫자가 있어야 통과", ASSUME, "설계"),
    ("A-26", "'확장 없음' 비교", "같은 로트 재고를 검사 없이 정상 재고로 취급한 가상 비교", ASSUME, "설계"),
    ("A-27", "표본 불량률 경보", "추가검사 불합격이면 R1과 별개로 격리·폐기 검토·위원회 보고·고객 모니터링", ASSUME, "설계"),
    ("A-28", "작업 처리 목표일", "작업이 생긴 업무일 + 유형별 영업일(격리 당일, 고객 통지 1, 보유량 회신 3, 원인 조사 10영업일 등)", ASSUME,
     "품질팀 지정 + 환산(용어집 A-28 표) — 원본에 회수 업무 기한 없음, A05 업무 달력"),
    ("A-29", "업무일 넘기기·지연", "업무일은 원본 기준일(2026-07-01)에서 영업일 단위로 넘기고, 처리 목표일이 지난 열린 작업은 '지연' 표시", ASSUME,
     "A05 업무 달력 — 원본 데이터 판정(납기·발주 기준)은 원본 기준일 그대로"),
    ("A-30", "과잉 대응 비교 범위", "대표 비교 = 같은 원료 로트 배치 전부 회수 · 기준 연도(2026년) 생산분 전부 회수(보도 반응), 전 기간 전면 회수는 상한(참고) — SBL 인도분, 유통기한·판매 소진 미반영", ASSUME,
     "개선 효과 화면 비교용 가상 시나리오 — 보도는 제품·배치를 지목하지 않음(R5)"),
    ("A-31", "회수 후 클레임·외부 정보 처리", "회수의결 이후 같은 배치 클레임은 새 상신 없이 '이미 회수 진행 중' + 회수 범위 확인 작업 · 언론 보도는 판단 재검토만(R5), 지목 없으면 모니터링, 지목하면 사실 확인", ASSUME,
     "설계 — 처리 목표: 회수 범위 확인 1·보도 지목 배치 확인 2·모니터링 5영업일(A-28)"),
    ("A-32", "위원회 의결 처리 목표", "품질위원회 의결 처리 목표 = 상신 업무일 + 1영업일 — 지나면 '의결 지연 N영업일'로 결정 대기 맨 위", ASSUME,
     "품질팀 지정 — 원본에 의결 기한 없음, A05 업무 달력(용어집 A-28 표 첫 줄)"),
    # ---- 부서 주장 ----
    ("OPT-01 비고", "증설안 OPT-01 비고", "'원료 재계약·처방 재검증 불필요, 리드타임 약 9개월, C-201 요구량 전량 소화 가능' — 부서 서술", CLAIM,
     "strat_capex_plan_cv.note"),
    ("OPT-02 비고", "증설안 OPT-02 비고", "'원료(M-B) 재계약·처방 재검증 필요, 리드타임 6~8주, 요구량 약 40%만 소화' — 부서 서술", CLAIM,
     "strat_capex_plan_cv.note → A-18의 근거"),
    ("RFP 라인 제약", "더마 전용 라인 서술", "'더마 물량은 F1 더마 전용 라인에서만 생산 가능' — RFP 요약 문서의 서술", CLAIM,
     "rfp_c201.md (재생산 라인 L-02는 m_product_cv.production_line으로 확인)"),
    # ---- 원본 데이터 용어 ----
    ("SBL", "출하-배치 연결표", "어느 출하가 어느 배치에서 나갔는지 잇는 원본 표(shipment_batch_link_cv)", FACT, "shipment_batch_link_cv"),
    ("A05", "데이터 이용 및 확장 기준(대회 문서)", "기준일 2026-07-01 · 업무 달력 월~금(공휴일 없음) · 발생일·적용일 분리 · 알려진 자료 한계(로트 투입>입고 등)", FACT,
     "대회 공통 지침 A05 v1.5"),
    ("L-02", "PL-02 재생산 라인", "PL-02(더마)를 만드는 F1 라인 — 가동률 97%, 여유 = 생산능력 × (1 − 가동률)", FACT,
     "strat_capacity_cv · m_product_cv"),
    # ---- 모의 ----
    ("모의 의결", "위원회 의결(모의)", "품질위원회 승인·거절은 시연용 화면 입력 — 실제 의결 아님", MOCK, "화면 입력"),
    ("모의 회신", "부서·고객 회신(모의)", "격리 수량·보유량·추가검사·조달·납기 동의 회신은 시연용 입력", MOCK, "화면 입력·프리셋"),
    ("클레임 접수(모의)", "화면 클레임 접수", "/claims에서 넣은 클레임은 시연용 입력 — 원본 클레임과 구분해 저장", MOCK, "claim_addition"),
    ("시연 프리셋", "시연 프리셋", "초기화 후 정해진 시나리오를 자동 실행 — 수치는 모의 데이터", MOCK, "presets.py"),
]

# 6번째 값(선택)은 함께 섞인 두 번째 출처 성격(예: R1 = 원본 사실 + 가정 A-2). 집계는 첫 번째 성격 기준.
TERMS = [{"code": t[0], "name": t[1], "short": t[2], "nature": t[3], "source": t[4], "also": t[5] if len(t) > 5 else None}
         for t in _TERMS]
TOOLTIPS = {t["code"]: f"{t['name']} — {t['short']} [{t['nature']}]" for t in TERMS}
# 화면 텍스트에서 툴팁을 붙일 코드(R1·A-5·A05·SBL 형태만 — 한국어 일반 단어는 건드리지 않는다)
AUTO_CODES = sorted((c for c in TOOLTIPS if c.replace("-", "").isalnum() and c.isascii()), key=len, reverse=True)


def is_rule(t: dict) -> bool:
    return t["code"].startswith("R") and t["code"][1:].isdigit()


def grouped() -> list[tuple[str, list[dict]]]:
    sections = [("판단 규칙", [t for t in TERMS if is_rule(t)]),
                ("가정", [t for t in TERMS if t["code"].startswith("A-")]),
                ("부서 주장", [t for t in TERMS if t["nature"] == CLAIM and not t["code"].startswith("A-")]),
                ("원본·대회 기준", [t for t in TERMS if t["code"] in ("SBL", "A05", "L-02")]),
                ("모의 처리", [t for t in TERMS if t["nature"] == MOCK])]
    return sections


# ---- 코드명 → 한글 업무 용어 (원래 이름은 툴팁) ----
CODE_TERMS = {
    "defect_confirmed=True": "규격이탈 확정", "defect_confirmed='True'": "규격이탈 확정",
    "defect_confirmed=False": "미확정", "defect_confirmed": "확정 여부",
    "approval_authority_policy": "승인권한 규정", "quality_policy": "품질 정책",
    "calculation_definitions": "계산 정의서", "ext_news_quality_incident": "언론 보도 자료",
    "produced_qty": "생산량", "shipped_qty": "출하량", "confirmed_qty": "확정 수량", "qty_affected": "영향 수량",
    "rate_prod_pct": "불량률(생산 분모)", "rate_ship_pct": "불량률(출하 분모)",
    "rate_prod": "불량률(생산 분모)", "rate_ship": "불량률(출하 분모)", "qc_result": "원본 QC 결과",
    "lot_clues": "원인 조사 단서", "same_lot_batches": "같은 로트 배치",
    "claim_customers_without_sbl": "SBL 출하 기록 없는 클레임 고객",
    "shipment_batch_link_cv": "출하-배치 연결표 원본", "strat_capacity_cv": "생산능력 원본",
    "strat_capex_plan_cv.note": "증설안 비고", "strat_capex_plan_cv": "증설안 원본",
    "material_lot_master_cv": "원료 로트 원본", "sales_orders_cv": "주문 원본", "sales_shipments_cv": "매출 출하 원본",
    "m_product_cv.production_line": "제품 원본의 생산 라인", "m_product_cv": "제품 원본", "qc_claims_cv": "QC 클레임 원본",
    "claim_addition": "추가 클레임 기록", "presets.py": "시연 프리셋 코드",
    # 원본 키(배치 추적 화면) · 원본 파일
    "qc_claims_cv.batch_id": "QC 클레임 원본의 배치 번호", "prod_batch_log_cv.input_lot_ids": "생산 배치 원본의 투입 로트",
    "material_lot_master_cv.lot_id": "원료 로트 원본의 로트 번호", "shipment_batch_link_cv.batch_id": "출하-배치 연결표의 배치 번호",
    "shipment_batch_link_cv.customer_id": "출하-배치 연결표의 고객 번호",
    "claim_addition.batch_id": "추가 클레임 기록의 배치 번호", "input_lot_ids": "투입 로트", "prod_batch_log_cv": "생산 배치 원본",
    "m_customer_cv": "고객 원본", "src_claims": "QC 클레임 원본", "rfp_c201.md": "RFP 요약 문서(C-201)",
    "strat_capex_plan": "증설안 원본",
    # 앱 테이블(추가 데이터 추적성 점검표)
    "claim_correction": "검사 정정 기록", "customer_holding": "고객 보유량 회신 기록", "inventory_hold": "사내재고 상태 기록",
    "shipment_state": "출하 상태 기록", "reply_intake": "회신 접수 기록", "deferral_request": "납기 연기 요청 기록",
    "sample_alert": "표본 경보 기록", "external_info": "외부 정보 기록", "claim_watch": "클레임 모니터링 기록",
}

# 처리 이력 '대상' 열: 기록 종류(entity_type) → 한글 (원래 이름은 툴팁)
ENTITY_LABELS = {
    "batch": "배치", "batch_flag": "조사대상 표시", "claim": "클레임", "claim_intake": "클레임 접수 결과", "submission": "상신",
    "submission_summary": "상신 요약", "task": "작업", "reply": "회신", "deferral": "납기 연기", "unlinked": "출하 연결 미확인",
    "inventory": "사내재고", "inventory_qty": "사내재고 수량", "shipment": "출하", "plan": "재공급 계획", "system": "시스템",
    "external_info": "외부 정보", "external_review": "외부 정보 재검토", "customer_holding": "고객 보유량",
    "claim_watch": "클레임 모니터링", "capex_check": "증설안 조달 확인", "business_date": "업무일", "lot": "원료 로트",
    "correction": "검사 정정", "decision": "의결",
}
_CODE_RE = re.compile(r"(?<![A-Za-z0-9_.])(" + "|".join(re.escape(k) for k in sorted(CODE_TERMS, key=len, reverse=True))
                      + r"|facts(?:\.[a-z_]+)?)(?![A-Za-z0-9_])")


_FACT_KEYS = {"gaps": "정보 공백", "policy": "규칙 번호", "request": "요청 사항", "targets": "회수대상 배치",
              "impact": "영향 범위", "resupply": "재공급 판정", "lots": "원료 로트"}


def _kor(code: str) -> str:
    key = code[len("facts."):] if code.startswith("facts.") else code
    return CODE_TERMS.get(key) or _FACT_KEYS.get(key, "계산값")


def plain(text) -> str:
    """코드명을 한글 업무 용어로 바꾼 문자열(툴팁 없이 — 입력란 초안 등)."""
    return _CODE_RE.sub(lambda m: _kor(m.group(1)), str(text)) if text else text


def terms(text):
    """화면 표시용: 코드명 → <span title="원래 이름: …">한글</span>. 나머지는 이스케이프한다."""
    if text is None or text == "":
        return text
    out, last, raw = [], 0, str(text)
    for m in _CODE_RE.finditer(raw):
        out.append(escape(raw[last:m.start()]))
        code = m.group(1)
        out.append(Markup('<span class="code-term" title="원래 이름: {}">{}</span>').format(code, _kor(code)))
        last = m.end()
    out.append(escape(raw[last:]))
    return Markup("").join(out)


def entity(code):
    """처리 이력 대상 종류 → 한글(원래 이름은 툴팁)."""
    label = ENTITY_LABELS.get(code)
    if not label:
        return code
    return Markup('<span class="code-term" title="원래 이름: {}">{}</span>').format(code, label)


def yes_no(v, yes="예", no="아니오"):
    """True/False·'True'/'False' → 한글. 그 밖의 값은 그대로(수량은 천 단위 쉼표)."""
    if v in (True, "True", "true"):
        return yes
    if v in (False, "False", "false"):
        return no
    if isinstance(v, int):
        return f"{v:,}"
    return v
