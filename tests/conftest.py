"""테스트 공통: 앱 시작 시 시작 상태 자동 생성(백그라운드 AI 상신)을 끈다 — 각 테스트가 상태를 직접 만든다."""
import os

os.environ["W03_AUTOSEED"] = "0"
