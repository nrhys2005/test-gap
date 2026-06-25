---
name: harness
description: "작업 기반 개발 파이프라인 오케스트레이터. Planner→Plan Validator→Developer→Reviewer 전체 흐름을 관리한다."
---

# Harness — 작업 기반 개발 파이프라인

Planner, Plan Validator, Developer, Reviewer 에이전트가 협업하여 TestGap 작업을 구현하는 파이프라인.

## 에이전트 구성

| 에이전트 | 파일 | 역할 |
|----------|------|------|
| planner | `.claude/agents/planner.md` | 작업 분석, 구현 계획 수립 |
| plan-validator | `.claude/agents/plan-validator.md` | 계획 기술적 검증 |
| developer | `.claude/agents/developer.md` | 모듈/테스트 구현 |
| reviewer | `.claude/agents/reviewer.md` | 결과 검토, QA |

## 파이프라인 흐름

```
Planner → .plans/{작업ID}.md
  ↓
Plan Validator
  ├─ APPROVE → Developer
  ├─ REFINE  → Planner 재호출 (최대 2회)
  └─ ESCALATE → 사용자 보고
Developer (worktree 격리)
  ├─ src/testgap/ 수정 + tests/ 추가
  ├─ pytest -q && ruff check 통과
  └─ feat/{작업ID}-* 브랜치에 커밋
Reviewer
  ├─ APPROVE → 완료 보고
  ├─ REQUEST_CHANGES → Developer 수정 (최대 2회)
  └─ REDESIGN → Planner 재호출
```

## 입력

작업 ID (예: `TG-001`) 또는 작업 설명: $ARGUMENTS

### 옵션
| 옵션 | 설명 |
|------|------|
| `--auto` | 사용자 승인 없이 전체 실행 (ESCALATE 제외) |
| `--no-worktree` | Developer를 worktree 격리 없이 실행 (소규모 변경) |

## 워크플로우

### Phase 0: 브랜치 준비
```bash
git checkout main && git pull origin main 2>/dev/null || true
```

### Phase 1: 계획 수립
**Planner 에이전트** 실행:
- `.plans/{작업ID}.md` 에 구현 계획 저장
- 수정 대상 모듈, 신규 모듈, 테스트 전략, 성공 기준 명시

### Phase 1.5: 계획 검증
**Plan Validator 에이전트** 실행:
- 검증 결과를 `.plans/{작업ID}.validation.md` 에 저장
- **APPROVE** → Phase 2
- **REFINE** → Planner 재호출 (최대 2회)
- **ESCALATE** → 사용자 보고 (항상)

### Phase 2: 개발
**Developer 에이전트** 실행 (`isolation: "worktree"` 기본):
1. `feat/{작업ID}-description` 브랜치 생성
2. 모듈/테스트 구현
3. 품질 게이트: `pytest -q && ruff check src tests`
4. (선택) dogfood: `testgap diff --base main`
5. 커밋: `feat({작업ID}): 설명` 또는 컨벤션에 맞는 type

### Phase 3: 리뷰
**Reviewer 에이전트** 실행:
- **APPROVE** → Phase 4
- **REQUEST_CHANGES** → Developer 수정 (최대 2회)
- **REDESIGN** → Planner 재호출

### Phase 4: 완료 보고
- 변경 파일, 테스트 결과, 추가/수정된 케이스 보고
- (선택) PR 생성: `gh pr create`
- memory 업데이트

## 에러 핸들링

| 에러 | 전략 |
|------|------|
| pytest 실패 | Developer 재시도 → REQUEST_CHANGES |
| ruff 위반 | 자동 수정 시도(`ruff check --fix`) → 재검증 |
| dogfood 시 LLM 키 없음 | dogfood 단계 skip, 경고만 |
| import 깨짐 | `pip install -e ".[dev]"` 재실행 |
| worktree 생성 실패 | `--no-worktree`로 폴백 안내 |
