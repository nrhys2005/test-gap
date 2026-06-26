---
name: resolve-reviews
description: "PR 번호 또는 작업ID로 관련 PR의 리뷰 코멘트를 확인하고, 수정이 필요하면 코드를 수정한 뒤 코멘트에 답변+resolve 처리한다."
---

# Resolve Reviews — PR 리뷰 코멘트 처리

PR 번호 또는 작업 ID를 입력받아, 관련 PR에 달린 리뷰 코멘트를 확인하고 수정/답변/resolve 처리하는 스킬.

## 입력

PR 번호 또는 작업 ID: $ARGUMENTS

## 워크플로우

### Phase 1: PR 식별

PR 번호가 직접 입력된 경우 해당 PR 사용. 작업ID(`TG-XXX`)인 경우:
```bash
gh pr list --search "{작업ID}" --json number,title,headRefName,state --jq '.[]'
```

### Phase 2: 리뷰 코멘트 수집

GraphQL로 미해결 review thread를 조회한다.
```bash
gh api graphql -f query='
query($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(first: 20) {
            nodes { id databaseId author { login } body path line }
          }
        }
      }
    }
  }
}' -f owner=nrhys2005 -f repo=test-gap -F pr={PR번호}
```

### Phase 3: 코멘트 분석 및 분류

| 분류 | 설명 | 액션 |
|------|------|------|
| **FIX** | 코드 수정이 필요 | 코드 수정 → 답변 → resolve |
| **ACK** | 타당하지만 범위 밖 | 답변만 → resolve |
| **SKIP** | 이미 수정됨 | 답변만 → resolve |

### Phase 4: 코드 수정 (FIX 항목)

수정 후 품질 게이트 확인:
```bash
pytest -q && ruff check src tests
```
통과 시 커밋 & push.

### Phase 5: 리뷰어 검증 (FIX 항목이 있는 경우)

Reviewer 에이전트 실행 → APPROVE 확인 (최대 2라운드).

### Phase 6: 코멘트 답변 및 Resolve

```bash
gh api repos/{owner}/{repo}/pulls/{pr_number}/comments/{comment_id}/replies \
  -f body="{답변}"

gh api graphql -f query='
mutation($id: ID!) {
  resolveReviewThread(input: {threadId: $id}) {
    thread { isResolved }
  }
}' -f id="{thread_id}"
```

### Phase 7: 요약 코멘트

PR에 전체 처리 내용을 요약하는 코멘트를 남긴다.
```bash
gh pr comment {PR번호} --body "리뷰 처리 완료: FIX N건 / ACK M건 / SKIP K건"
```

### Phase 8: 작업 메모

처리 결과를 메모리에 기록(필요 시)하고, 다음 단계(추가 push, merge 대기 등) 안내.

## 에러 핸들링

| 에러 | 전략 |
|------|------|
| 인증 실패 (403) | `gh auth setup-git` 안내 |
| 코드 수정 후 테스트 실패 | 해당 코멘트 FIX 보류 + 사용자 보고 |
| 코멘트 답변 권한 없음 | 답변 본문을 사용자에게 출력 |
| thread already resolved | 무시하고 다음으로 |
