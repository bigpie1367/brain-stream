# 운영 가이드

- **버전**: 1.0.0
- **작성일**: 2026-03-04

---

## 1. 사전 요구사항

- Docker 및 Docker Compose 설치
- ListenBrainz 계정 및 API 토큰 ([발급](https://listenbrainz.org/profile/))
- 로컬 포트 8080, 4533 사용 가능

---

## 2. 초기 설정

### 2.1 환경변수 설정

`config.yaml` 없이 `.env` 파일만으로 동작합니다:

```bash
cp .env.example .env
```

`.env` 파일 편집:

```
LB_TOKEN=your_listenbrainz_token
NAVIDROME_PASSWORD=your_navidrome_password
```

비민감 설정(`LB_USERNAME`, `NAVIDROME_USER` 등)은 `docker-compose.prod.yml`에 하드코딩되어 있습니다.

**지원 환경변수 전체 목록:**

| 환경변수 | 기본값 | 설명 |
|----------|--------|------|
| `LB_USERNAME` | (prod compose에 하드코딩) | ListenBrainz 사용자명 |
| `LB_TOKEN` | — | ListenBrainz API 토큰 **(필수)** |
| `NAVIDROME_URL` | `http://navidrome:4533` | Navidrome URL |
| `NAVIDROME_USER` | `admin` | Navidrome 사용자명 |
| `NAVIDROME_PASSWORD` | — | Navidrome 비밀번호 **(필수)** |

---

## 3. 배포

### 3.1 로컬 개발

```bash
./restart_local_docker.sh
# = docker compose -f docker-compose.local.yml down && up --build -d
```

### 3.2 서버 배포 (이미지 전용)

서버에 필요한 파일: `docker-compose.prod.yml`, `restart_production_docker.sh`, `.env`

```bash
./restart_production_docker.sh
# = docker compose -f docker-compose.prod.yml down && up -d
```

Watchtower가 5분마다 GHCR 신규 이미지를 자동으로 감지하고 업데이트합니다.

### 3.3 Navidrome 초기 설정

1. `http://localhost:4533` 접속
2. 최초 접속 시 관리자 계정 생성 (`NAVIDROME_USER` / `NAVIDROME_PASSWORD`와 일치시킬 것)

### 3.4 서비스 확인

```bash
# 상태 확인
docker compose -f docker-compose.prod.yml ps

# brainstream 로그
docker compose -f docker-compose.prod.yml logs -f brainstream

# navidrome 로그
docker compose -f docker-compose.prod.yml logs -f navidrome
```

---

## 4. 일상 운영

### 4.1 소스 코드 변경 시

```bash
# Python 소스 변경 (src/ 파일) — 로컬
./restart_local_docker.sh

# beets 설정 변경 (beets/config.yaml) — 이미지에 번들됨, 재빌드 후 push 필요
# git push → CI → Watchtower 자동 배포

# 환경변수 변경 (.env) — 재시작만
docker compose -f docker-compose.prod.yml down && docker compose -f docker-compose.prod.yml up -d
```

### 4.2 파이프라인 수동 실행

```bash
# API로 즉시 실행
curl -X POST http://localhost:8080/api/pipeline/run

# 결과 확인
curl http://localhost:8080/api/downloads | python3 -m json.tool
```

### 4.3 다운로드 이력 확인

```bash
# API
curl http://localhost:8080/api/downloads | python3 -m json.tool

# SQLite 직접 조회
sqlite3 data/state.db "SELECT artist, track_name, status, attempts FROM downloads ORDER BY rowid DESC LIMIT 20;"

# 실패 목록만
sqlite3 data/state.db "SELECT artist, track_name, error_msg, attempts FROM downloads WHERE status='failed';"
```

### 4.4 beets 라이브러리 확인

```bash
# 전체 목록
docker exec brainstream-1 beet list -f '$artist - $title [$album]'

# 특정 아티스트
docker exec brainstream-1 beet list -f '$path' artist:Radiohead

# 앨범 없는 트랙 확인
docker exec brainstream-1 beet list -f '$artist - $title' album:''
```

---

## 5. 로그

### 5.1 로그 위치

| 로그 | 경로 | 설명 |
|------|------|------|
| music-bot 앱 로그 | `data/logs/music-bot.log` | 구조화 로그 (JSON) |
| beets import 로그 | `data/logs/beets-import.log` | beets 임포트 결과 |
| Docker 로그 | `docker compose logs` | 컨테이너 stdout/stderr |

### 5.2 로그 확인

```bash
# 실시간 앱 로그
docker compose logs -f brainstream

# beets import 로그
tail -f data/logs/beets-import.log

# 최근 에러만
docker compose logs brainstream | grep '"level":"error"'
```

---

## 6. 트러블슈팅

### 트랙이 다운로드되지 않음

1. LB 추천이 있는지 확인:
   ```bash
   curl -H "Authorization: Token {YOUR_TOKEN}" \
     "https://api.listenbrainz.org/1/cf/recommendation/user/{username}/recording?count=5"
   ```
2. state.db에서 실패 원인 확인:
   ```bash
   sqlite3 data/state.db "SELECT artist, track_name, error_msg FROM downloads WHERE status='failed';"
   ```
3. 로그 확인: `docker compose logs -f brainstream`

### beets가 skip을 반복함

```bash
tail -100 data/logs/beets-import.log
```

- `no strong recommendation` → `strong_rec_thresh` 값 완화 검토 (현재 0.15)
- `No matching recordings found` → `beets/config.yaml`에 `musicbrainz` 플러그인 포함 여부 확인

### 앨범이 Navidrome에서 2개로 분리됨

`tagger.py`의 `_enrich_track`에서 `mb_albumid`를 태그에 기록하고 있지 않은지 확인.

```bash
# 파일 태그 확인
docker exec brainstream-1 beet list -f '$mb_albumid' artist:"아티스트"
# 비어 있어야 정상
```

### 앨범아트가 없음

```bash
# Cover Art Archive 직접 테스트
curl -L "https://coverartarchive.org/release/{mb_albumid}/front" -o /tmp/test.jpg
```

### Navidrome 스캔이 실패함

```bash
# Navidrome 상태 확인
docker compose ps navidrome
docker compose logs navidrome

# Subsonic API 직접 테스트
curl "http://localhost:4533/rest/ping?u=admin&t={token}&s={salt}&v=1.16.1&c=test&f=json"
```

### 컨테이너 재시작 후 모듈 캐시 문제

Python 소스를 `docker cp`로 교체해도 uvicorn 프로세스가 모듈을 캐싱하고 있음.
반드시 컨테이너를 재시작해야 변경사항이 반영됨:

```bash
docker restart music-bot-temp-music-bot-1
```

---

## 7. 백업

```bash
# 상태 DB 백업
cp data/state.db data/state.db.backup

# 전체 음악 라이브러리 백업
tar -czf music-backup-$(date +%Y%m%d).tar.gz data/music/
```

---

## 8. 서비스 종료

```bash
docker compose down

# 데이터 보존하며 종료 (권장)
docker compose down

# 데이터 포함 완전 삭제 (주의)
docker compose down -v
rm -rf data/
```
