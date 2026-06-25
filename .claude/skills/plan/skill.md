---
name: plan
description: "작업의 구현 계획을 수립한다."
---

# /plan — 구현 계획 수립

## 입력

작업 ID 또는 설명: $ARGUMENTS

## 워크플로우

1. `.claude/agents/planner.md` 지시에 따라 **Planner 에이전트**를 실행한다
2. 관련 파일 분석:
   - `src/testgap/` 모듈 구조
   - 기존 `tests/` 패턴
   - `.testgap.yml` 스키마, `pyproject.toml` 의존성
   - Notion 기획서(메모리에서 링크 참조)
3. `.plans/{작업ID}.md` 에 구현 계획 저장

## 완료 조건

- `.plans/{작업ID}.md` 생성
- 요구사항 요약, 수정 대상 파일, 구현 단계, 테스트 전략, 성공 기준 포함
