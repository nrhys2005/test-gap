# TestGap

> **AI가 생성한 테스트를 실제로 실행해 본 다음, 통과한 것만 제안한다.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange)](#로드맵)

TestGap은 PR 또는 로컬 브랜치의 **변경된 코드 중 테스트가 부족한 부분만** 식별하고, 사용자가 선택한 LLM(Claude / GPT / Gemini / 로컬 Ollama)으로 pytest 테스트를 생성한 뒤, **실제로 pytest를 돌려 통과한 테스트만** 제안합니다.

---

## 왜 TestGap인가

| 기존 도구 | 한계 | TestGap 차별점 |
| --- | --- | --- |
| GitHub Copilot, Cursor | 테스트 검증을 안 함 — 깨진 테스트도 그냥 제안 | 실행 통과한 케이스만 채택 |
| CodiumAI / Qodo | 자체 LLM 강제, 클로즈드 | LiteLLM으로 100+ provider 자유 선택 (로컬 Ollama 포함) |
| 코드 전체 스캔 도구 | 한 번에 너무 많은 결과, 노이즈 큼 | diff 단위 — PR/브랜치 변경분만 |
| 수동 작성 | 매번 미뤄지고 결국 누락 | 1 명령어로 미커버 함수 식별 → 생성 → 검증 |

### 핵심 약속

1. **검증된 테스트만 제안** — pytest 실행에서 통과 못 한 코드는 절대 보여주지 않음
2. **공급자 무관** — `.testgap.yml`에 모델 이름만 적으면 끝. API 키는 환경변수에서 자동 인식
3. **Diff 중심** — 전체 코드베이스 스캔이 아니라 `git diff base..HEAD` 단위로 작게

---

## 설치

```bash
# PyPI alpha (권장)
pip install --pre testgap[llm]
```

PyPI: <https://pypi.org/project/testgap/>

> `--pre` 가 필요한 이유는 현재 alpha 릴리스(`0.1.0aN`)이기 때문입니다. 안정 버전(`0.1.0`) 이후로는 `pip install testgap[llm]` 만으로 설치됩니다.

### 소스에서 설치 (최신 main / 개발용)

```bash
git clone https://github.com/nrhys2005/test-gap.git
cd test-gap
pip install -e ".[llm]"
```

**필수 환경**
- Python 3.10 이상
- pytest 프로젝트 (또는 pytest 호환 레이아웃)
- git 저장소
- LLM API 키 (Anthropic / OpenAI / Gemini 중 하나, 또는 로컬 Ollama)

---

## 빠른 시작 (3분)

```bash
# 1. 프로젝트로 이동 + API 키 설정
cd my-project
export ANTHROPIC_API_KEY=sk-ant-...

# 2. .testgap.yml 생성 (인터랙티브 wizard)
testgap init

# 3. 브랜치에서 작업
git checkout -b feature/x
# ... 코드 작성 ...

# 4. 변경분 중 미커버 코드에 대해 테스트 제안
testgap diff
```

위 단계가 끝나면 변경된 함수 중 테스트가 부족한 곳을 식별하고, 각 함수마다 LLM이 생성 → pytest 실행 → 통과 케이스만 제안합니다.

---

## 사용법

### `testgap init`

프로젝트 환경을 자동 감지하고 `.testgap.yml`을 생성합니다.

```
$ testgap init

Analyzing /Users/me/my-project
✓ pytest detected (pyproject.toml)
✓ source path: src/
✓ test directory: tests/

Available LLM providers:
┌──────────────────────────────────┬──────────────────────────┐
│ model                            │ status                   │
├──────────────────────────────────┼──────────────────────────┤
│→ anthropic/claude-sonnet-4-6     │ ANTHROPIC_API_KEY found  │
│  openai/gpt-4o                   │ OPENAI_API_KEY missing   │
│  ollama/qwen2.5-coder            │ local, no key required   │
└──────────────────────────────────┴──────────────────────────┘

Use suggested model anthropic/claude-sonnet-4-6? [Y/n]
✓ wrote .testgap.yml
✓ added .testgap/ to .gitignore

Next steps:
  testgap diff --review   suggest tests for uncovered changes
```

**옵션**
- `--path, -p PATH` — 대상 디렉토리 (기본: 현재 디렉토리)
- `--yes, -y` — 모든 prompt에 기본값으로 응답 (CI/스크립트용)

**자동 감지 시그널**
- pytest: `[tool.pytest.ini_options]`, `pytest.ini`, `conftest.py`, `pytest in deps`
- src 레이아웃: `src/<pkg>/__init__.py`, `package_dir` 설정
- flat 레이아웃: 루트 직속 `<pkg>/__init__.py`

### `testgap diff`

변경된 코드 중 미커버 함수를 식별하고 테스트를 생성/검증합니다.

```
$ testgap diff

Analyzing diff in /Users/me/my-project
base origin/main → head HEAD
changed lines: 24   covered: 11   diff coverage: 45.8%

[1/2] processor.py::refund_partial
  uncovered lines: 42, 43, 44, 47, 48
  ✓ 2/2 tests passed   $0.0381

[2/2] processor.py::issue_invoice
  uncovered lines: 71, 72, 75, 78, 82, 83, 88
  ! 1 kept / 1 discarded   $0.0412   [retried]
    · test_issue_invoice_handles_zero_amount

LLM cost this run: $0.0793
```

**옵션**
| 옵션 | 설명 |
| --- | --- |
| `--base, -b REF` | 비교 기준 ref. 기본: `origin/HEAD` → `main` → `master` |
| `--head REF` | 비교 대상. 기본: `HEAD` |
| `--max-functions, -n N` | 처리할 함수 수 제한 (비용/시간 절약) |
| `--path, -p PATH` | 프로젝트 루트 |

**동작 흐름**

1. **Base 해석** — `--base` 없으면 `origin/HEAD` → `main` → `master` 순으로 탐색
2. **변경 라인 수집** — `git diff base..HEAD`로 추가/수정된 Python 라인 집합 구성
3. **커버리지 측정** — `pytest --cov`로 어떤 라인이 실행되는지 확인
4. **미커버 라인 → 함수 그룹핑** — AST로 enclosing function/method 식별
5. **함수당 LLM 호출** — 대상 함수 본문 + 기존 테스트 2~3개(few-shot) + 미커버 라인 위치로 프롬프트 구성
6. **테스트 실행 검증** — 생성된 코드를 임시 파일로 작성 → pytest 실행 (30초 timeout)
7. **부분 통과 처리** — 통과한 케이스만 채택. 실패 케이스는 1회 재생성 (이전 실패 메시지를 프롬프트에 첨부)
8. **결과 보고** — 채택된 테스트와 비용 출력

**종료 코드**
- `0` — 모든 함수에서 최소 1개 테스트 채택
- `1` — 모든 함수에서 0개 채택 (또는 환경 오류)

> `succeeded`는 "부분 채택도 성공", `fully_passed`는 "전부 통과"를 의미합니다 (Python API에서 구분 사용).

---

## 설정 (`.testgap.yml`)

```yaml
version: 1

project:
  language: python              # MVP는 python만
  test_framework: pytest        # 자동 감지
  source_paths: ["src/"]        # 자동 감지
  test_paths: ["tests/"]        # 자동 감지

coverage:
  threshold: 80                 # 전체 커버리지 목표 (CI에서 사용)
  diff_threshold: 90            # PR 변경분 별도 임계치 (더 엄격)
  exclude:
    - "**/migrations/**"
    - "**/__init__.py"

llm:
  model: "anthropic/claude-sonnet-4-6"
  max_cost_per_run: 2.0         # USD, 초과 시 즉시 중단
  max_retries: 2                # LLM 네트워크 에러 재시도 (테스트 실행 재시도와 별개)

generation:
  style: "match_existing"       # 기존 테스트 스타일 모방
  include_docstrings: true
  max_tests_per_function: 3
  test_timeout_seconds: 30      # 함수당 pytest timeout
```

설정 파일은 `testgap init`이 생성합니다. 직접 작성한 경우 schema validation 에러로 안내합니다.

---

## LLM 공급자

[LiteLLM](https://docs.litellm.ai/) 추상화로 100+ provider 지원. `.testgap.yml`의 `llm.model`을 바꾸고 해당 API 키만 환경변수에 두면 됩니다.

| 모델 | 환경변수 | 비고 |
| --- | --- | --- |
| `anthropic/claude-sonnet-4-6` | `ANTHROPIC_API_KEY` | 기본 권장 |
| `anthropic/claude-opus-4-7` | `ANTHROPIC_API_KEY` | 더 정확, 더 비쌈 |
| `openai/gpt-4o` | `OPENAI_API_KEY` | |
| `openai/gpt-4o-mini` | `OPENAI_API_KEY` | 저비용 |
| `gemini/gemini-2.0-flash` | `GEMINI_API_KEY` | |
| `ollama/qwen2.5-coder` | — | 로컬, 무료, 인터넷 불요 |
| `ollama/codellama:13b` | — | 로컬 |

**우선순위 (모델 결정)**

1. `.testgap.yml`의 `llm.model`
2. `TESTGAP_MODEL` 환경변수
3. 기본값: `testgap init` wizard가 가용 API 키 기반으로 추천

**비용 가드**

- `max_cost_per_run`을 초과하면 즉시 중단하고 지금까지 채택한 결과만 보고
- 재시도가 한도를 넘으면 1차 결과만 보존 (`retry skipped: budget would exceed ...`)
- 함수당 LLM 호출은 최대 2회 (네트워크 재시도 제외)

---

## 동작 예시 — 부분 통과 처리

LLM이 3개 테스트를 생성했고 2개만 통과한 경우:

```
1차 generation: 3 tests → 2 pass, 1 fail
  ↓
실패 1건만 재생성 (이전 실패 코드 + pytest output을 프롬프트에 첨부, ≤500 토큰)
  ↓
2차 generation: 1 test → 1 pass
  ↓
최종 채택: 3 tests (1차의 2개 + 2차의 1개)
```

전부 실패한 1건도 재생성 후 실패한다면, **통과한 케이스만 그대로 제시**하고 실패 케이스는 폐기합니다. 깨진 테스트가 사용자 코드베이스에 절대 들어가지 않는 것이 TestGap의 핵심 약속입니다.

---

## 자주 묻는 질문

**Q. 내 코드를 LLM 공급자에 보내나요?**
네. `testgap diff`가 호출하는 LLM은 대상 함수의 본문과 같은 모듈의 기존 테스트 2~3개를 전송합니다. **민감한 코드는 로컬 모델(Ollama)** 사용을 권장합니다. v0.2부터 시크릿 패턴 자동 마스킹이 추가됩니다.

**Q. 기존 테스트 파일을 수정하나요?**
v0.1에서는 새 파일에만 추가합니다. (충돌 회피) 기존 파일 append 옵션은 v0.2에서 제공.

**Q. CI에서 쓸 수 있나요?**
v0.1은 로컬 사용 위주입니다. v0.2의 GitHub Action 패키지(`testgap-action`)를 기다려주세요. JSON 출력(`--json`)도 v0.2에서.

**Q. pytest 외 다른 프레임워크는?**
MVP는 pytest 한정. unittest/nose2는 사용자 요청 수집 후 결정 (`v1.0` 다언어와 함께).

**Q. `testgap init`이 모호한 레이아웃을 만나면?**
AI 호출 없이 인터랙티브 prompt를 띄워 사용자가 선택합니다. CI에서는 `--yes`로 기본값 선택.

**Q. 비용이 얼마나 드나요?**
함수당 대략 $0.04~0.05 (claude-sonnet 기준, 평균적인 함수 크기). `max_cost_per_run`으로 절대 한도를 강제합니다. 매 실행 끝에 누적 비용을 출력합니다.

**Q. 생성된 테스트가 마음에 안 들면?**
v0.1은 자동 적용 없이 콘솔 출력만 합니다. `--review` 인터랙티브 모드 (a 적용 / s 건너뜀 / r 재생성 / e 에디터 편집 / q 종료)는 다음 릴리스에서 제공.

---

## 로드맵

### v0.1.0 — Diff Review MVP (현재 alpha)

- ✅ `testgap init` (pytest/src 레이아웃 자동 감지)
- ✅ `testgap diff` (비-인터랙티브, 자동 생성/검증/제안)
- ✅ pytest + coverage.py 한정
- ✅ LiteLLM provider 추상화
- ✅ 실행 통과 검증 + 부분 통과 처리 + 1회 재생성
- ✅ `max_cost_per_run` 비용 가드
- ✅ `testgap diff --review` 인터랙티브 모드 (a/s/r/e/q)
- ✅ PyPI alpha 배포 (`pip install --pre testgap`)
- ⏳ `.testgap/logs/` 세션 로그

### v0.2.0 — CI Integration

- `testgap diff --fix` (자동 적용)
- `testgap scan` (전체 미커버 리포트)
- GitHub Action (`testgap-action`)
- PR sticky comment 모드
- 기존 테스트 파일 append 옵션
- `--json` 출력 (머신 판독용)

### v0.3.0 — Scale

- `testgap backfill` (대량 백필, 비용 예산 분배)
- CI commit/block 모드
- `@testgap apply` PR 멘션 트리거
- 컨피그 프로파일 (`.testgap.dev.yml`)

### v1.0.0 — Multi-language

- 언어 plugin 추상화 (Java, TypeScript)
- 실사용 케이스 기반 인터페이스 확정

---

## 보안 / 프라이버시

- 분석 대상 함수와 같은 모듈의 기존 테스트 2~3개가 LLM으로 전송됩니다 (코드 외부 전송).
- 로컬에서만 처리하려면 `ollama/*` 모델을 선택하세요.
- 시크릿 마스킹(`os.environ`, AWS/GCP/Azure 키 패턴, `SECRET_KEY`/`PASSWORD`/`TOKEN` 등)은 v0.2 로드맵.
- 모든 LLM 호출은 `.testgap/logs/<timestamp>.log`에 기록됩니다 (`.testgap/`는 `init` 시 자동으로 `.gitignore`에 추가).

---

## 비-목표 (Non-goals)

다음은 의도적으로 다루지 않습니다.

- E2E / 통합 테스트 생성 (단위 테스트만)
- 기존 테스트 리팩토링 / 중복 제거
- 코드 품질 분석 / 린트
- 자체 LLM 호스팅 (사용자가 가져옴)

---

## 개발 / 컨트리뷰션

```bash
git clone https://github.com/nrhys2005/test-gap.git
cd test-gap
pip install -e ".[dev,llm]"

# 테스트
pytest -q

# 린트
ruff check src tests

# CLI 직접 실행
python -m testgap --help
```

**아키텍처 (요약)**

```
testgap/
├── cli.py              # typer 진입점
├── pipeline.py         # diff → generate → validate 오케스트레이션
├── config/             # .testgap.yml 스키마 / 로더 / init wizard
├── detect/             # pytest / src layout / test dir 자동 감지
├── coverage/           # git diff + coverage.py + AST 그룹핑
├── generator/          # 프롬프트 구성 / LiteLLM 호출 / 응답 파싱
├── validator/          # pytest subprocess + 결과 판정
└── cost/               # 토큰 카운팅 / 비용 한도
```

자세한 설계 문서: [Notion 기획서](https://www.notion.so/TestGap-38ae1632267c8096bbfaf6b11a56ddff) (private)

---

## 라이선스

MIT — [LICENSE](LICENSE) 참고.
