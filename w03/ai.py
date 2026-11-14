"""AI ① 품질위원회 상신 요약 (DESIGN.md §3).

- 입력: 규칙 엔진이 계산한 facts(JSON)만. AI는 문장(요약·조치안·추가 질문)만 쓰고, 제목(요청 사항)·근거 수치·
  영향 고객·같은 로트 배치·정보 공백 목록은 assemble()이 계산값을 그대로 넣는다. AI는 수치를 계산하거나 승인 여부를 정하지 않는다.
- 출력: 정해진 스키마. 인용 수치(배치 불량률·고객 수량·같은 로트 배치)와 본문에 쓴 숫자를
  계산값과 대조하고, 어긋나면 '경고(수치 불일치)'로 표시한다(출력은 그대로 보여 준다). 숫자는 맞지만 회수를
  진행·실행·결정된 것처럼 쓴 문장(R3)은 '경고(표현)'.
- API 키가 없거나 호출이 실패하면(한도 초과 429 포함) 요약을 만들지 않고 '요약 실패 - 수동 작성' 상태가 된다.
- 공급자: AI_PROVIDER=gemini 이면 GEMINI_API_KEY로 Gemini(GEMINI_MODEL, 기본 Flash), 아니면 Anthropic Claude.
  공급자와 무관하게 같은 JSON 스키마·같은 검증·같은 수동 경로를 쓴다.
"""
import json
import os
import re
import time

import anthropic
import httpx

from .config import CLAUDE_MODEL, GEMINI_MODEL, RECALL_THRESHOLD_PCT

STATUS_OK = "통과"
STATUS_WARN = "경고(수치 불일치)"
STATUS_WORDING = "경고(표현)"         # 숫자는 맞지만 의결 전 문서가 회수를 결정된 것처럼 씀(R3)
STATUS_FAIL = "요약 실패 - 수동 작성"
STATUS_MANUAL = "수동 작성 완료"
STATUS_RULE = "규칙 요약(재상신)"       # 재상신은 정정 내역·재계산 수치를 규칙으로 요약(AI 미사용, 가정 A-15)
DECIDABLE = {STATUS_OK, STATUS_WARN, STATUS_WORDING, STATUS_MANUAL, STATUS_RULE}

# AI가 쓰는 것은 문장뿐이다(요약 문장·부서별 조치안·추가 확인 질문). 제목·배치별 근거 수치·영향 고객·같은 로트 배치·
# 정보 공백 목록은 assemble()이 계산값을 그대로 넣는다 — AI가 수치·목록을 옮겨 적지 않으므로 인용 불일치가 생기지 않는다.
# 부서별 조치안: 부서 이름은 코드가 고정으로 넣고(이 순서), AI는 부서마다 조치 문장 하나만 쓴다.
DEPTS = (("sales", "영업"), ("logistics", "물류"), ("quality", "품질"), ("production", "생산"), ("procurement", "조달"))
_DEPT_ACTIONS = {
    "type": "object",
    "properties": {key: {"type": "string"} for key, _ in DEPTS},
    "required": [key for key, _ in DEPTS],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "actions": _DEPT_ACTIONS,
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "actions", "open_questions"],
    "additionalProperties": False,
}

SYSTEM = """당신은 화장품 제조사 Corevia의 품질팀 실무자로, 품질위원회 회수 상신 요약의 '문장'을 작성합니다.
- 제목, 배치별 근거 수치(확정 수량·두 분모 불량률), 영향 고객과 수량, 같은 로트 배치, 정보 공백 목록은 시스템이 계산값으로 직접 넣습니다.
  당신은 그 사이의 설명 문장만 씁니다. 수량·비율·건수·금액·날짜 같은 숫자와 배치·고객·로트 번호 목록을 쓰지 않습니다
  (예: "두 배치", "영향 고객 전원"처럼 씁니다). 예외는 회수 기준 '10% 이상'과 규칙 번호(R1 등)뿐입니다.
- summary: 위원회가 무엇을 왜 판단해야 하는지 3~5문장. 입력 facts에 있는 사실만 근거로 씁니다.
- 회수 실행은 품질위원회 의결 사항입니다(R3). 승인 여부를 단정하지 말고 위원회가 판단할 근거를 씁니다.
- 회수 실행은 위원회 판단 대상으로만 씁니다. '회수를 진행합니다', '회수를 실행합니다', '회수가 결정되었습니다'처럼
  회수가 진행·실행·결정된 것처럼 쓰지 않습니다(예: "위원회가 회수 실행 여부를 판단할 근거입니다"). 원인 조사는 별도 트랙이라
  '원인 조사를 진행합니다'는 쓸 수 있습니다.
- 아직 의결 전입니다. 부서별 조치안(actions)은 sales(영업)·logistics(물류)·quality(품질)·production(생산)·procurement(조달)
  칸마다 조치 문장 하나만 '승인 시 …' 조건부로 씁니다(예: sales → "승인 시 고객별 회수요청 통지"). 부서 이름은 시스템이 넣으므로
  문장 앞에 부서 이름이나 다른 제목을 붙이지 않습니다. 이미 진행 중이거나 실행한 것처럼 쓰지 않습니다.
  quality 칸에는 '승인 시 원인 조사 착수 — 원료 로트 단서 확인'을 넣습니다.
- 미확정 클레임과 언론 보도는 회수 근거가 아닙니다. 같은 로트를 쓴 다른 배치는 회수가 아니라 조사 대상입니다.
- 원인 조사는 회수와 별도 트랙이며, 원인으로 단정하지 않습니다. R4를 쓸 때는 반드시 주어(원인 조사)를 밝혀
  "원인 조사는 회수와 별도 트랙으로 진행합니다 (R4)"처럼 씁니다. '원인 규명을 기다리지 않고 진행합니다',
  '대기 없이 판단합니다'처럼 주어 없이 무엇이 진행되는지 모호한 문장은 회수가 진행되는 것으로 읽히므로 쓰지 않습니다.
- 회수 기준은 반드시 '10% 이상'이라고 씁니다('10% 초과', '10%를 넘는' 등 다른 표현을 쓰지 않습니다).
- 정책·규칙에 근거한 문장을 쓸 때만 그 문장 끝에 facts.policy의 해당 번호를 괄호로 답니다(예: "…회수대상입니다 (R1)").
  규칙 목록이나 규칙 문장을 따로 나열하지 않습니다. 근거가 없는 문장에는 번호를 달지 않습니다.
- open_questions: 위원회가 추가로 확인할 질문만 씁니다(없으면 빈 배열). 정보 공백 목록(facts.gaps)과
  원인 조사 단서(facts.lot_clues)는 시스템이 따로 보여 주므로 open_questions에 옮기지 않습니다.
- 입력의 필드 이름·코드명(defect_confirmed, produced_qty, shipped_qty, confirmed_qty, approval_authority_policy,
  quality_policy, calculation_definitions, lot_clues, facts 등)을 본문에 쓰지 않습니다. 한글 업무 용어로 씁니다
  (예: 규격이탈 확정, 생산량, 출하량, 확정 수량, 승인권한 규정, 품질 정책, 계산 정의서, 원인 조사 단서, 계산값).
- 한국어로, 위원회가 2분 안에 읽을 분량으로 씁니다."""


def _pct(v):
    return "계산 불가" if v is None else f"{v:.2f}%"


def basis_lines(facts: dict) -> list[str]:
    """상신 요약에 들어가는 계산값 문장(규칙 엔진 값 그대로 — AI가 쓰지 않음)."""
    imp = facts["impact"]
    lines = []
    for t in facts["targets"]:
        meets = any(v is not None and v >= RECALL_THRESHOLD_PCT for v in (t["rate_prod"], t["rate_ship"]))
        lines.append(f"{t['batch_id']}: 확정 불량 {t['confirmed_qty']:,}EA(확정 클레임 {t['confirmed_claims']}건), 불량률 "
                     f"{_pct(t['rate_prod'])}(생산 분모) / {_pct(t['rate_ship'])}(출하 분모) — "
                     + ("기준 10% 이상(R1)" if meets else "두 분모 모두 10% 미만"))
    lines.append("영향 고객(SBL 인도분): " + ", ".join(f"{c['customer_id']} {c['qty']:,}EA" for c in imp["customers"]))
    lines.append("사내 잔여재고 추정(A-5): " + ", ".join(f"{i['batch_id']} {i['est_qty']:,}EA" for i in imp["inventory"]))
    lines.append("같은 로트 배치(회수 아님 · 조사대상, R6): " + (", ".join(facts["same_lot_batches"]) or "없음"))
    return lines


def computed_questions(facts: dict) -> list[str]:
    """정보 공백(계산값): SBL 출하 기록 없는 클레임 고객 + facts.gaps."""
    return [f"{g['customer_id']}: 확정 클레임 {g['claims']}건({g['qty']:,}EA)이 있으나 SBL 출하 기록 없음 — 영업·물류 보유 확인"
            for g in facts["impact"]["claim_customers_without_sbl"]] + list(facts["gaps"])


def dept_actions(actions) -> list[dict]:
    """부서 이름은 코드가 고정(영업·물류·품질·생산·조달 순서)하고 AI의 조치 문장만 넣는다.
    (목록 형식 출력이 와도 AI가 쓴 부서 이름은 쓰지 않고 순서대로 문장만 가져온다.)"""
    if isinstance(actions, dict):
        texts = [actions.get(key, "") for key, _ in DEPTS]
    else:
        texts = [a.get("action", "") if isinstance(a, dict) else str(a) for a in (actions or [])][:len(DEPTS)]
    return [{"dept": name, "action": (texts[i] if i < len(texts) else "").strip() or "—"} for i, (_, name) in enumerate(DEPTS)]


def assemble(written: dict, facts: dict) -> dict:
    """AI 문장 + 계산값(제목·근거 수치·목록)을 합친 상신 요약. 목록·수치는 facts에서 그대로 가져온다."""
    from .workflow import request_line
    extra = list(written.get("open_questions", []))
    return {
        "headline": (facts.get("request") or request_line(facts))["headline"],
        "basis_lines": basis_lines(facts),
        "summary": written.get("summary", ""),
        "actions": dept_actions(written.get("actions")),
        "open_questions": computed_questions(facts) + extra,
        "ai_questions": extra,
        "cited_batches": [{"batch_id": t["batch_id"], "confirmed_qty": t["confirmed_qty"],
                           "rate_prod_pct": t["rate_prod"], "rate_ship_pct": t["rate_ship"]} for t in facts["targets"]],
        "cited_customers": [{"customer_id": c["customer_id"], "qty": c["qty"]} for c in facts["impact"]["customers"]],
        "same_lot_batches": list(facts["same_lot_batches"]),
        "written_by_ai": ["summary", "actions", "ai_questions"],
    }


# ---------- 공급자 ----------

AI_SOURCES = ("claude", "gemini")      # submission.ai_source · reply_intake.source 에 들어가는 AI 출처 값
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# 사이트 게이트웨이가 120초에 끊으므로(504) 재시도까지 포함해 그 전에 끝내고 수동 경로로 넘긴다.
GEMINI_TIMEOUT_SEC = 95
# 무료 등급은 503(일시적 과부하)이 잦다 — 503에 한해 짧게 기다렸다가 최대 2회 다시 부른다(95초 안에서).
GEMINI_RETRY_DELAYS_SEC = (1.5, 3.0)
_KEY_ENV = {"claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"), "gemini": ("GEMINI_API_KEY",)}
_LABEL = {"claude": "Claude", "gemini": "Gemini"}


class AIFailure(Exception):
    """호출 실패 — 사유는 화면·이력에 그대로 보여 준다(키 값은 담지 않는다).
    code는 짧은 실패 코드(503·429·API 키 없음·응답 시간 초과 …), retries는 503 자동 재시도 횟수."""

    def __init__(self, reason: str, code: str, retries: int = 0):
        if retries:
            reason += f" (503 자동 재시도 {retries}회 후)"
        super().__init__(reason)
        self.code, self.retries = code, retries

    def info(self) -> dict:
        return {"reason": str(self), "code": self.code, "retries": self.retries, "provider": provider()}


def provider() -> str:
    return "gemini" if os.environ.get("AI_PROVIDER", "").strip().lower() == "gemini" else "claude"


def configured() -> bool:
    """키 값은 노출하지 않고 현재 공급자의 키 설정 여부만 본다."""
    return any(os.environ.get(k) for k in _KEY_ENV[provider()])


def model_name() -> str:
    return GEMINI_MODEL if provider() == "gemini" else CLAUDE_MODEL


def label(source: str | None = None) -> str:
    return _LABEL.get(source or provider(), "AI")


def _rate_limited(detail: str) -> str:
    return f"한도 초과(429): {detail} — 잠시 후 다시 시도하거나 수동 작성"


def failure_label(info: dict | None) -> str:
    """'AI 구조화 실패: 503' 처럼 사람 작성 경로의 작성자 라벨에 붙일 짧은 사유."""
    info = info or {}
    code = info.get("code") or info.get("reason") or "사유 미상"
    return f"AI 구조화 실패: {code}" + (f", 재시도 {info['retries']}회" if info.get("retries") else "")


def _claude(system: str, prompt: str, schema: dict, max_tokens: int, effort: str) -> tuple[str, dict]:
    try:
        response = anthropic.Anthropic().beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            thinking={"type": "adaptive"},
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIStatusError as e:
        if e.status_code == 429:
            raise AIFailure(_rate_limited(e.message), "429")
        raise AIFailure(f"API 오류 {e.status_code}: {e.message}", str(e.status_code))
    except anthropic.APIConnectionError:
        raise AIFailure("API 연결 실패", "연결 실패")
    if response.stop_reason == "refusal":
        raise AIFailure("모델이 요청을 거절함(refusal)", "거절")
    if response.stop_reason == "max_tokens":
        raise AIFailure("출력 길이 초과(max_tokens)", "출력 길이 초과")
    text = next((b.text for b in response.content if b.type == "text"), "")
    return text, {"provider": "claude", "model": response.model, "request_id": response._request_id, "retries": 0}


def _gemini(system: str, prompt: str, schema: dict, max_tokens: int, budget: float | None = None) -> tuple[str, dict]:
    """budget: 이 호출에 쓸 수 있는 시간(초). 없으면 GEMINI_TIMEOUT_SEC(자동 재생성은 남은 시간만 쓴다)."""
    deadline = time.monotonic() + min(budget or GEMINI_TIMEOUT_SEC, GEMINI_TIMEOUT_SEC)
    retries = 0
    while True:
        remaining = max(deadline - time.monotonic(), 1.0)
        try:
            r = httpx.post(
                GEMINI_URL.format(model=GEMINI_MODEL),
                headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},   # 키는 헤더로만 보낸다(URL·로그에 남기지 않음)
                json={"systemInstruction": {"parts": [{"text": system}]},
                      "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                      "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema,
                                           "maxOutputTokens": max_tokens}},
                timeout=httpx.Timeout(remaining, connect=min(10.0, remaining)),
            )
        except httpx.TimeoutException:
            raise AIFailure(f"응답 시간 초과({GEMINI_TIMEOUT_SEC}초) — 잠시 후 다시 시도하거나 수동 작성",
                            "응답 시간 초과", retries)
        except httpx.HTTPError:
            raise AIFailure("API 연결 실패", "연결 실패", retries)
        if r.status_code != 503 or retries >= len(GEMINI_RETRY_DELAYS_SEC):
            break
        delay = GEMINI_RETRY_DELAYS_SEC[retries]
        if deadline - time.monotonic() < delay + 5:     # 다시 부를 시간이 없으면 그대로 실패 처리
            break
        retries += 1
        time.sleep(delay)
    if r.status_code >= 400:
        try:
            detail = r.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            detail = r.text[:300]
        if r.status_code == 429:
            raise AIFailure(_rate_limited(detail), "429", retries)
        raise AIFailure(f"API 오류 {r.status_code}: {detail}", str(r.status_code), retries)
    body = r.json()
    block = (body.get("promptFeedback") or {}).get("blockReason")
    candidates = body.get("candidates") or []
    if block or not candidates:
        raise AIFailure(f"모델이 요청을 거절함({block or '응답 없음'})", "거절", retries)
    finish = candidates[0].get("finishReason")
    if finish == "MAX_TOKENS":
        raise AIFailure("출력 길이 초과(max_tokens)", "출력 길이 초과", retries)
    if finish not in (None, "STOP"):
        raise AIFailure(f"모델이 요청을 거절함({finish})", "거절", retries)
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    return text, {"provider": "gemini", "model": body.get("modelVersion") or GEMINI_MODEL,
                  "request_id": body.get("responseId"), "retries": retries}


def _generate(system: str, prompt: str, schema: dict, max_tokens: int, effort: str,
              budget: float | None = None) -> tuple[dict, dict]:
    """현재 공급자로 스키마 JSON을 받는다. (출력, 정보) — 키 없음·호출 실패·파싱 실패는 AIFailure."""
    src = provider()
    if not configured():
        raise AIFailure(f"API 키 없음({_KEY_ENV[src][0]} 미설정)", "API 키 없음")
    if src == "gemini":
        text, info = _gemini(system, prompt, schema, max_tokens, budget)
    else:
        text, info = _claude(system, prompt, schema, max_tokens, effort)
    try:
        return json.loads(text), info
    except json.JSONDecodeError:
        raise AIFailure("AI 출력 JSON 파싱 실패", "JSON 파싱 실패", info.get("retries", 0))


# ---------- 검증 ----------

def _numbers_in(obj) -> list[float]:
    out = []
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            out += _numbers_in(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _numbers_in(v)
    elif isinstance(obj, str):
        try:
            out.append(float(obj))
        except ValueError:
            pass
    return out


_ID = re.compile(r"[A-Za-z]+(?:-[A-Za-z0-9]+)+")          # B-02-2602-01, LOT-B-2602-P, C-101, L-02 …
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{4}Q\d")
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def text_numbers_not_in_facts(text: str, facts: dict) -> list[str]:
    """본문 숫자 중 facts에서 찾을 수 없는 값. ID·날짜와 10 미만 정수(건수 등)는 제외한다."""
    allowed = set(_numbers_in(facts)) | {RECALL_THRESHOLD_PCT}
    text = _DATE.sub(" ", _ID.sub(" ", text))
    missing = []
    for tok in _NUM.findall(text):
        v = float(tok.replace(",", ""))
        if "." not in tok and v < 10:
            continue
        if not any(abs(v - a) <= 0.01 for a in allowed):
            missing.append(tok)
    return sorted(set(missing))


def verify(out: dict, facts: dict) -> list[str]:
    """AI 출력의 수치를 규칙 엔진 계산값과 대조한다. 빈 목록이면 통과."""
    problems = []
    expected = {t["batch_id"]: t for t in facts["targets"]}
    cited = {c["batch_id"]: c for c in out.get("cited_batches", [])}
    for bid in sorted(expected.keys() - cited.keys()):
        problems.append(f"회수대상 {bid} 누락")
    for bid in sorted(cited.keys() - expected.keys()):
        problems.append(f"회수대상이 아닌 배치 {bid} 인용")
    for bid in sorted(expected.keys() & cited.keys()):
        e, c = expected[bid], cited[bid]
        if c["confirmed_qty"] != e["confirmed_qty"]:
            problems.append(f"{bid} 확정 수량 {c['confirmed_qty']:,} ≠ 계산값 {e['confirmed_qty']:,}")
        for key, src in (("rate_prod_pct", "rate_prod"), ("rate_ship_pct", "rate_ship")):
            if e[src] is not None and (c[key] is None or abs(c[key] - e[src]) > 0.01):
                problems.append(f"{bid} {key} {c[key]} ≠ 계산값 {e[src]}")

    exp_cust = {c["customer_id"]: c["qty"] for c in facts["impact"]["customers"]}
    got_cust = {c["customer_id"]: c["qty"] for c in out.get("cited_customers", [])}
    for cid in sorted(exp_cust.keys() - got_cust.keys()):
        problems.append(f"영향 고객 {cid} 누락")
    for cid in sorted(got_cust.keys() - exp_cust.keys()):
        problems.append(f"영향 고객이 아닌 {cid} 인용")
    for cid in sorted(exp_cust.keys() & got_cust.keys()):
        if got_cust[cid] != exp_cust[cid]:
            problems.append(f"{cid} 인도 수량 {got_cust[cid]:,} ≠ 계산값 {exp_cust[cid]:,}")

    if sorted(out.get("same_lot_batches", [])) != sorted(facts["same_lot_batches"]):
        problems.append(f"같은 로트 배치 {out.get('same_lot_batches')} ≠ 계산값 {facts['same_lot_batches']}")

    body = " ".join([out.get("headline", ""), out.get("summary", "")]
                    + [a["action"] for a in out.get("actions", [])] + out.get("open_questions", []))
    req = facts.get("request")
    if req:
        need = req["batch_ids"] + [f"{req['customer_qty']:,}", f"{req['inventory_est_qty']:,}", req["supply_result"]]
        lacking = [x for x in need if x not in out.get("headline", "")]
        if lacking:
            problems.append("제목에 요청 사항(배치·영향 수량·사내재고 추정·공급 판정) 누락: " + ", ".join(lacking))
    written = [out.get("summary", "")] + [a["action"] for a in out.get("actions", [])] \
        + list(out.get("ai_questions", out.get("open_questions", [])))
    for phrase in preempting_phrases(" ".join(written)):
        problems.append(f"{WORDING_PREFIX}: 의결 전에 회수가 결정된 것처럼 읽힘(R3) — '{phrase}'")
    for phrase in subjectless_phrases(" ".join(written)):
        problems.append(f"{WORDING_PREFIX}: 주어 없이 진행을 말해 회수가 진행되는 것으로 읽힘(R3) — '{phrase}'"
                        " → '원인 조사는 회수와 별도 트랙으로 진행합니다 (R4)'처럼 주어를 밝혀야 함")
    stray = text_numbers_not_in_facts(body, facts)
    if stray:
        problems.append("본문 숫자 중 계산값에 없는 값: " + ", ".join(stray))
    wrong = sorted(set(_THRESHOLD_WORDING.findall(body)))
    if wrong:
        problems.append("기준 표현은 '10% 이상'이어야 함: " + ", ".join(wrong))
    return problems


WORDING_PREFIX = "표현 경고"
# 의결 전 상신에서 회수를 진행·실행·결정된 것처럼 쓴 표현(R3). '회수 실행은 …', '회수 실행 여부', '회수 진행 상황'처럼
# 명사로 쓴 것과 '승인 시 …' 조건부 문장은 해당하지 않는다. 원인 조사의 '진행'은 앞에 '회수'가 없어 걸리지 않는다.
_PREEMPT = re.compile(
    r"회수(?:를|가|도)?\s*(?:바로\s*|즉시\s*|곧\s*)?(?:진행|실행|결정|확정|착수|시행|개시)"
    r"(?:합니다|한다|하며|하고|하겠|할 예정|해야|했|하였|되었|됐|됩니다|된다|되며|됨|함|중입니다|중이)"
    r"|회수(?:를|가)?\s*(?:결정|확정)(?:을|이)\s*(?:내렸|했|되었|됐)")
_CONDITIONAL = re.compile(r"승인\s*시|승인되면|승인하면|승인 후|승인할 경우|의결\s*시|의결되면|의결하면|의결 후")


def preempting_phrases(text: str) -> list[str]:
    """의결 전 문서가 회수 결정을 앞서가는 표현. 같은 문장 앞쪽에 '승인 시' 같은 조건이 있으면 제외."""
    found = []
    for m in _PREEMPT.finditer(text):
        start = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
        if not _CONDITIONAL.search(text[start:m.start()]):
            found.append(m.group(0))
    return found


# 주어 없이 '기다리지 않고 진행/판단', '대기 없이 진행'처럼 쓴 문장(무엇이 진행되는지 모호 → 회수 진행으로 읽힘).
# '기다리지 않고 원인 조사를 진행'처럼 바로 뒤에 목적어(주체)가 오면 해당하지 않는다.
_SUBJECTLESS = re.compile(
    r"(?:기다리지\s*않고|기다릴\s*필요\s*없이|대기\s*없이|대기하지\s*않고)\s*(?:바로\s*|곧바로\s*|즉시\s*|곧\s*)?"
    r"(?:진행|판단|실행|추진|착수|시행)(?:하|되|해|할|합|됩|된|함|됨|했)[가-힣]*")


def subjectless_phrases(text: str) -> list[str]:
    """주어 없이 진행·판단을 말하는 표현. '승인 시' 같은 조건부 문장은 제외."""
    found = []
    for m in _SUBJECTLESS.finditer(text):
        start = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
        if not _CONDITIONAL.search(text[start:m.start()]):
            found.append(m.group(0))
    return found


def summary_status(problems: list[str]) -> str:
    numeric = [p for p in problems if not p.startswith(WORDING_PREFIX)]
    return STATUS_WARN if numeric else (STATUS_WORDING if problems else STATUS_OK)


# 회수 기준을 '10% 이상'이 아닌 말로 쓴 경우(초과·넘는·이하·미만 등)
_THRESHOLD_WORDING = re.compile(r"10\s*%\s*(?:초과|를\s*넘|을\s*넘|가\s*넘|넘|보다\s*높)")


# ---------- 생성 ----------

# 표현 경고(R3)가 나오면 사람에게 보이기 전에 자동으로 다시 생성하는 횟수. 사이트 게이트웨이가 120초에 끊으므로
# 첫 호출부터 SUMMARY_BUDGET_SEC 안에서만 다시 부른다(직전 호출 시간으로 남은 시간을 가늠).
WORDING_RETRIES = 2
SUMMARY_BUDGET_SEC = 95


def _has_wording(problems: list[str]) -> bool:
    return any(p.startswith(WORDING_PREFIX) for p in problems)


def committee_summary(facts: dict) -> tuple[dict | None, str | None, str, dict]:
    """(요약 또는 None, 출처, 상태, 검증 정보). 표현 경고면 최대 WORDING_RETRIES번 자동 재생성 — 그래도 남으면 경고 표시."""
    prompt = "facts:\n" + json.dumps(facts, ensure_ascii=False, indent=1)
    start, last_dur, attempts, earlier = time.monotonic(), 0.0, [], []
    for n in range(1 + WORDING_RETRIES):
        remaining = SUMMARY_BUDGET_SEC - (time.monotonic() - start)
        if n and remaining < last_dur * 1.5 + 5:          # 다시 부를 시간이 없으면 직전 결과로 끝낸다
            break
        t0 = time.monotonic()
        try:
            written, info = _generate(SYSTEM, prompt, SCHEMA, 16000, "medium", budget=remaining if n else None)
        except AIFailure as e:
            if not attempts:
                return None, None, STATUS_FAIL, e.info()
            break                                          # 재생성 실패 → 직전(경고 남은) 결과를 쓴다
        last_dur = time.monotonic() - t0
        out = assemble(written, facts)
        problems = verify(out, facts)  # 목록·수치는 계산값이라 일치 — AI 문장 속 숫자·기준·표현은 그대로 대조
        attempts.append((out, info, problems))
        if not _has_wording(problems):
            break
        earlier.append([p for p in problems if p.startswith(WORDING_PREFIX)])
    out, info, problems = attempts[-1]
    info["problems"] = problems
    info["wording_retries"] = len(attempts) - 1
    if info["wording_retries"]:
        info["wording_retry_reasons"] = earlier[:info["wording_retries"]]
    return out, info["provider"], summary_status(problems), info


def draft_for_manual(facts: dict) -> dict:
    """수동 작성 화면의 초안 — 규칙 엔진 계산값을 문장으로 옮긴 것(사람이 검토·수정 후 저장)."""
    t, imp, rs = facts["targets"], facts["impact"], facts["resupply"]
    pct = lambda v: "계산 불가" if v is None else f"{v:.2f}%"
    customers = ", ".join(f"{c['customer_id']} {c['qty']:,}EA" for c in imp["customers"])
    same_lot = ", ".join(facts["same_lot_batches"]) or "없음"
    questions = [f"{g['customer_id']}: 확정 클레임 {g['claims']}건({g['qty']:,}EA)이 있으나 SBL 출하 기록 없음 — 영업·물류 보유 확인"
                 for g in imp["claim_customers_without_sbl"]] + facts["gaps"]   # 원인 조사 단서는 화면 '원료 로트' 칸에만
    from .workflow import request_line
    return {
        "headline": (facts.get("request") or request_line(facts))["headline"],
        "summary": " ".join(f"{x['batch_id']}: 확정 불량 {x['confirmed_qty']:,}EA, 불량률 {pct(x['rate_prod'])}(생산 분모) / "
                            f"{pct(x['rate_ship'])}(출하 분모)로 기준 10% 이상(R1)." for x in t)
                   + f" 영향 고객: {customers}. 같은 로트 배치({same_lot})는 조사대상.",
        "actions": [
            {"dept": "영업", "action": f"승인 시 고객별 회수요청 통지 — {customers}"},
            {"dept": "물류", "action": "승인 시 사내 잔여재고 출하보류 — " + ", ".join(
                f"{i['batch_id']} 약 {i['est_qty']:,}EA(추정, A-5)" for i in imp["inventory"])},
            {"dept": "품질", "action": f"승인 시 원인 조사 착수(회수와 별도 트랙, R4) — 로트 {', '.join(facts['lots'])} 원료 로트 단서 확인, 같은 로트 배치 {same_lot}"},
            {"dept": "생산", "action": f"승인 시 {rs['production']['line_id']} 재생산 검토 — 판정 {rs['production']['result']}"},
            {"dept": "조달", "action": f"승인 시 {rs['material']['material_id']} 확보 — 판정 {rs['material']['result']}"},
        ],
        "open_questions": questions,
    }


# ---------- AI ② 회신 구조화 ----------

REPLY_SYSTEM = """당신은 품질 회수 운영팀의 회신 접수 담당입니다. 고객·영업·조달·품질 담당자가 보낸 자유 형식 회신을
정해진 JSON 필드로 옮깁니다.
- 회신 원문에 **명시된 사실만** 옮깁니다. 원문에 없는 값은 반드시 null로 둡니다.
- Task 정보는 맥락 참고용입니다. 원문에 없는 고객·배치·수량을 Task 정보로 채우지 않습니다.
- 수량은 원문에 적힌 숫자 그대로(쉼표 제거) 정수로 옮깁니다. 단위 환산·추정·반올림을 하지 않습니다.
- 동의/가능/합격 같은 판단은 원문이 분명할 때만 true/false 또는 해당 값으로, 애매하면 null입니다.
- 날짜는 원문 표현 그대로 문자열로 옮깁니다.
- evidence에는 값을 뽑은 근거가 되는 원문 문장을 그대로 인용합니다."""


def _nullable(kind):
    if isinstance(kind, list):
        return {"anyOf": [{"type": "string", "enum": kind}, {"type": "null"}]}
    t = {"str": "string", "int": "integer", "bool": "boolean"}[kind]
    return {"anyOf": [{"type": t}, {"type": "null"}]}


def reply_schema(fields: dict) -> dict:
    props = {k: _nullable(v) for k, v in fields.items()}
    props["evidence"] = {"type": "string"}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def structure_reply(task_type: str, fields: dict, context: dict, raw_text: str) -> tuple[dict | None, dict]:
    """자유 형식 회신 → 스키마 JSON. (결과 또는 None, 정보). 키가 없거나 실패하면 None — 사람이 폼으로 구조화."""
    prompt = (f"Task 종류: {task_type}\nTask 정보(참고용, 원문에 없으면 채우지 말 것): "
              f"{json.dumps(context, ensure_ascii=False)}\n\n회신 원문:\n{raw_text}")
    try:
        return _generate(REPLY_SYSTEM, prompt, reply_schema(fields), 8000, "low")
    except AIFailure as e:
        return None, e.info()
