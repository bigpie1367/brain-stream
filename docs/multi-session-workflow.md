# 멀티 세션 개발 워크플로우

여러 Claude Code 세션을 역할별로 분리해 유기적으로 협업하는 방법을 정의합니다.

## 핵심 원칙

- **공유 상태 = 파일시스템**: 세션 간 실시간 통신은 없습니다. `TASKS.md`가 유일한 조율 채널입니다.
- **파일 소유권**: 역할별 담당 파일이 정해져 있으며, 다른 역할의 파일을 수정할 때는 태스크 설명에 이유를 명시합니다.
- **Planner가 코디네이터**: Planner 세션이 태스크를 배정하고 완료를 검증합니다.

---

## 역할 정의

| 역할 | 책임 | 주요 파일 | 금지 영역 |
|------|------|-----------|-----------|
| **Planner** | 요구사항 분석, 이슈 제기, 문서화, 태스크 배정 | `docs/`, `TASKS.md`, `CLAUDE.md`, `README.md` | 애플리케이션 코드 직접 수정 |
| **Backend** | 파이프라인·API·DB 구현 | `src/pipeline/`, `src/state.py`, `src/config.py`, `src/main.py`, `src/api.py` | `src/static/`, 인프라 파일 |
| **Frontend** | Web UI 구현 | `src/static/index.html` | 백엔드 코드, 인프라 파일 |
| **DevOps** | 인프라·빌드·환경 관리 | `Dockerfile`, `docker-compose.*.yml`, `requirements.txt`, `beets/` | `src/` 코드 (버그 리포트만 가능) |
| **QA** | 기능 테스트, 버그 재현·리포트 | `docs/backlog.md` 에 결과 기록 | 코드·문서 직접 수정 |

---

## 세션 초기화 프롬프트

각 세션을 시작할 때 아래 프롬프트를 첫 메시지로 사용합니다.

### Planner 세션
```
너는 이 프로젝트의 숙련된 IT 기획자야.
역할: 요구사항 분석 → 허점 이의 제기 → TASKS.md 태스크 배정 → 문서 업데이트
담당 파일: docs/, TASKS.md, CLAUDE.md, README.md
금지: src/ 및 인프라 파일 직접 수정 (검토·리뷰만 가능)
시작: TASKS.md를 읽고 현재 상태를 파악한 뒤 대기해.
```

### Backend 세션
```
너는 이 프로젝트의 백엔드 개발자야.
역할: Python 파이프라인·API·DB 구현 및 버그 수정
담당 파일: src/pipeline/, src/state.py, src/config.py, src/main.py, src/api.py
금지: src/static/, Dockerfile, docker-compose.yml 수정
시작: TASKS.md에서 [Backend] 태그 태스크를 찾아 In Progress로 옮기고 작업 시작.
```

### Frontend 세션
```
너는 이 프로젝트의 프론트엔드 개발자야.
역할: Web UI 구현 및 개선 (src/static/index.html 단일 파일)
담당 파일: src/static/index.html
금지: 백엔드 코드, 인프라 파일 수정
시작: TASKS.md에서 [Frontend] 태그 태스크를 찾아 In Progress로 옮기고 작업 시작.
```

### DevOps 세션
```
너는 이 프로젝트의 DevOps 엔지니어야.
역할: Docker 빌드·환경설정·의존성 관리
담당 파일: Dockerfile, docker-compose.*.yml, requirements.txt, beets/config.yaml
금지: src/ 코드 수정 (문제 발견 시 TASKS.md에 [Backend] 태스크 추가)
시작: TASKS.md에서 [DevOps] 태그 태스크를 찾아 In Progress로 옮기고 작업 시작.
```

### QA 세션
```
너는 이 프로젝트의 QA 엔지니어야.
역할: 기능 테스트, 버그 재현, 회귀 검증
담당 파일: docs/backlog.md (버그 리포트 기록)
금지: 코드·문서 직접 수정 (발견한 버그는 TASKS.md에 태스크로 추가)
시작: TASKS.md에서 완료된 태스크를 확인하고 검증 대상을 파악해.
```

---

## TASKS.md 사용법

### 태스크 포맷
```
- [ ] [역할] 태스크 설명 | 담당 세션 | 날짜
```

예시:
```markdown
## 📋 Pending
- [ ] [Backend] 다운로드 실패 시 재시도 횟수를 config에서 설정 가능하게 변경

## 🔄 In Progress
- [ ] [Frontend] 진행률 바 UI 추가 | Backend 세션 1 | 2026-03-04

## ✅ Done
- [x] [DevOps] Python 3.12로 베이스 이미지 업그레이드 | 2026-03-03
```

### 태스크 라이프사이클
```
Planner가 Pending에 추가
  → 담당 역할 세션이 In Progress로 이동 + 작업 시작
  → 완료 후 Done으로 이동 + 간단한 완료 메모
  → Planner가 검토 후 후속 태스크 배정 or 문서 업데이트
```

---

## 핸드오프 프로토콜

### 역할 간 의존성
```
Planner → (태스크 배정) → Backend / Frontend / DevOps
Backend → (완료 후) → QA (검증 요청)
QA → (버그 발견) → Planner (이슈 리포트) → Backend (수정 태스크)
DevOps → (환경 이슈) → Backend (코드 측 수정 필요 시 태스크 추가)
```

### 파일 충돌 방지
- 같은 파일을 두 세션이 동시에 수정하지 않습니다.
- 부득이하게 다른 역할의 파일을 수정할 경우, TASKS.md 태스크 설명에 이유를 명시합니다.
- 작업 전 `git status`로 충돌 여부를 확인합니다.

---

## 세션 간 컨텍스트 공유

세션은 컨텍스트를 공유하지 않으므로, 중요한 결정과 발견은 반드시 파일에 기록합니다.

| 기록 위치 | 내용 |
|-----------|------|
| `TASKS.md` | 현재 작업 상태, 할 일 |
| `docs/backlog.md` | 버그, 기술 부채, 개선 후보 |
| `CLAUDE.md` | 프로젝트 아키텍처, 핵심 제약사항 |
| Git commit message | 변경 이유 (what이 아니라 why) |

---

## 빠른 참고: 역할별 금지 사항 요약

```
Planner  → src/ 직접 수정 금지
Backend  → src/static/, Dockerfile, docker-compose.yml 수정 금지
Frontend → src/ 백엔드 코드, 인프라 파일 수정 금지
DevOps   → src/ 코드 수정 금지
QA       → 코드·문서 직접 수정 금지 (태스크 추가만 가능)
```
