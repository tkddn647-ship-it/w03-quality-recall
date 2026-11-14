"""A05 업무 달력 — 월요일 시작, 월~금 영업일, 공휴일 없음.

업무일(앱이 하루씩 넘기는 운영 날짜)과 원본 기준일(AS_OF, 데이터 스냅샷 2026-07-01)은 다르다(가정 A-29).
작업 처리 목표일(가정 A-28)은 작업이 생긴 업무일 + 유형별 영업일 수다.
"""
from datetime import date, timedelta

WEEKDAYS = "월화수목금토일"
BASIS = "A05 업무 달력(월~금 영업일, 공휴일 없음)"

# 가정 A-28: 작업 유형별 처리 목표(영업일). 이미 나간 물건이 더 퍼지는 것을 막는 격리·통지를 가장 짧게,
# 회수와 별도 트랙(R4)인 원인 조사는 길게. (영업일, 왜 그 기한인지)
TASK_DUE_BDAYS = {
    "사내재고 격리": (0, "격리 전에는 같은 배치 재고가 출하될 수 있음"),
    "고객 통지": (1, "통지 전에는 고객이 해당 배치를 계속 판매할 수 있음"),
    "회수요청 철회 안내": (1, "해제된 회수요청이 고객에게 남아 있음"),
    "추가검사": (2, "같은 로트 재고가 검사 전까지 출하 보류 상태"),
    "출하 경로 확인 요청": (2, "출하가 확인되면 회수요청 대상이 늘어남"),
    "주문 상태 확인 요청": (2, "추정 납기 지난 주문이 연기 후보에서 빠져 있음"),
    "보유량 회신 수집": (3, "회신 전까지 재공급 필요량이 상한값"),
    "영업 승인": (3, "승인 전까지 납기 연기 단계가 멈춤"),
    "납기 연기 동의 요청": (3, "동의 전까지 대안 확정이 멈춤"),
    "조달 확인": (3, "회신 전까지 증설안 충족 여부를 확정할 수 없음"),
    "투자 승인": (3, "승인 전까지 증설 대안이 확정되지 않음"),
    "재검토": (3, "거절 사유 보완 전까지 배치가 보류 상태"),
    "클레임 배치 재확인": (3, "클레임 배치 근거가 확인되지 않음"),
    "품질위원회 보고": (3, "표본 불량률 경보가 위원회에 보고되지 않음"),
    "재공급 요청": (3, "재공급 일정 확정"),
    "재공급 생산": (3, "재생산 일정 확정"),
    "재공급 원료 확보": (3, "원료 확보 일정 확정"),
    "폐기 검토": (5, "격리 재고 처분 결정"),
    "회수 범위 확인": (1, "새 고객이면 회수요청이 빠진 출하분이 계속 유통될 수 있음"),
    "보도 지목 배치 사실 확인": (2, "보도가 지목한 배치의 클레임·검사 기록 확인(회수 근거는 아님, R5)"),
    "외부 정보 모니터링": (5, "보도 이후 고객 클레임 추이 확인(회수 근거는 아님, R5)"),
    "원인 조사": (10, "원인 조사(회수와 별도 트랙, R4)"),
}
DEFAULT_DUE = (5, "")
# 확인용: 이전(달력일) 값 — 영업일 환산 표에 쓴다. 사용자가 정한 값은 USER_SET.
PREVIOUS_CAL_DAYS = {"사내재고 격리": 0, "고객 통지": 1, "회수요청 철회 안내": 1, "추가검사": 2, "출하 경로 확인 요청": 2,
                     "주문 상태 확인 요청": 2, "보유량 회신 수집": 3, "영업 승인": 3, "납기 연기 동의 요청": 3, "조달 확인": 3,
                     "투자 승인": 3, "재검토": 3, "클레임 배치 재확인": 3, "품질위원회 보고": 3, "재공급 요청": 5,
                     "재공급 생산": 5, "재공급 원료 확보": 5, "폐기 검토": 7, "원인 조사": 14}
USER_SET = {"사내재고 격리", "고객 통지", "보유량 회신 수집", "원인 조사"}
# 가정 A-32: 품질위원회 의결 처리 목표 = 상신 업무일 + 1영업일(작업이 아니라 상신 단위). 지나면 '의결 지연 N영업일'.
DECISION_DUE_BDAYS = 1
DECISION_DUE_WHY = "의결 전에는 회수요청·사내재고 출하보류가 시작되지 않음"


def _d(s: str) -> date:
    return date.fromisoformat(s)


def is_business_day(s: str) -> bool:
    return _d(s).weekday() < 5


def next_business_day(s: str) -> str:
    d = _d(s) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def add_business_days(s: str, n: int) -> str:
    """s(영업일이 아니면 다음 영업일부터) + n영업일."""
    d = s if is_business_day(s) else next_business_day(s)
    for _ in range(n):
        d = next_business_day(d)
    return d


def business_days_between(a: str, b: str) -> int:
    """a → b 사이 영업일 수(b가 뒤면 양수, 앞이면 음수). 같은 날이면 0."""
    if a == b:
        return 0
    sign, lo, hi = (1, a, b) if a < b else (-1, b, a)
    n, d = 0, lo
    while d < hi:
        d = next_business_day(d)
        if d <= hi:
            n += 1
    return sign * n


def label(s: str) -> str:
    return f"{s}({WEEKDAYS[_d(s).weekday()]})"


def due_rule(task_type: str) -> tuple[int, str]:
    return TASK_DUE_BDAYS.get(task_type, DEFAULT_DUE)


def due_date(task_type: str, start: str) -> str:
    return add_business_days(start, due_rule(task_type)[0])


def due_table(start: str) -> list[dict]:
    """A-28 확인용 표: 이전 달력일 → 영업일, 업무일 start에 생긴 작업의 마감일."""
    rows = []
    for t, (n, why) in TASK_DUE_BDAYS.items():
        cal = PREVIOUS_CAL_DAYS.get(t)
        rows.append({"type": t, "prev_cal_days": cal,
                     "prev_due": (_d(start) + timedelta(days=cal)).isoformat() if cal is not None else None,
                     "bdays": n, "due": due_date(t, start), "why": why, "user_set": t in USER_SET})
    rows.insert(0, {"type": "위원회 의결 (상신 단위, A-32)", "prev_cal_days": None, "prev_due": None,
                    "bdays": DECISION_DUE_BDAYS, "due": add_business_days(start, DECISION_DUE_BDAYS),
                    "why": DECISION_DUE_WHY, "user_set": True})
    return rows
