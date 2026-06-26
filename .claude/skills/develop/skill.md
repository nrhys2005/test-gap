---
name: develop
description: "작업의 구현 계획에 따라 모듈과 테스트를 구현한다."
---

# /develop — 코드 구현

## 입력

작업 ID: $ARGUMENTS

## 전제 조건

- `.plans/{작업ID}.md` 존재 (없으면 `/plan` 먼저)

## 워크플로우

1. `.plans/{작업ID}.md` 읽기
2. `.claude/agents/developer.md` 지시에 따라 **Developer 에이전트** 실행 (`isolation: "worktree"`)
3. 모듈 구현 → 테스트 추가 → 품질 게이트 통과
4. `feat/{작업ID}-*` 브랜치에 커밋

## 품질 게이트

다음 두 조건을 모두 만족해야 완료로 간주한다.

```bash
pytest -q                       # 전부 통과
ruff check src tests            # 위반 0건
```

(선택) dogfood:
```bash
testgap diff --base main        # 자기 자신에 testgap 적용
```

## 완료 조건

- 품질 게이트 통과
- 계획서에 명시된 모든 산출물 생성
- 커밋 메시지 컨벤션 준수 (`feat`, `fix`, `test`, `docs`, `refactor`)
