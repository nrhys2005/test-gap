---
name: review
description: "작업의 구현 결과를 리뷰한다."
---

# /review — 코드 리뷰

## 입력

작업 ID: $ARGUMENTS

## 워크플로우

1. `git diff main..HEAD`로 변경사항 확인
2. `pytest -q`와 `ruff check src tests` 결과 확인 (재실행 가능)
3. `.claude/agents/reviewer.md` 지시에 따라 **Reviewer 에이전트** 실행
4. APPROVE / REQUEST_CHANGES / REDESIGN 판정

## 완료 조건

- 판정과 근거 명확히 제시
- 추가된 테스트 케이스 목록
- 변경된 공개 API/CLI 동작 요약
