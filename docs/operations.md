# 운영 가이드

- **버전**: 2.0.0
- **작성일**: 2026-03-21

---

## 1. 사전 요구사항

- Docker 및 Docker Compose 설치
- ListenBrainz 계정 및 API 토큰 ([발급](https://listenbrainz.org/profile/))
- 로컬 포트 8080 사용 가능

Note: `.dockerignore` 파일이 빌드 컨텍스트에서 `docs/`, `data/`, `logs/`, `.worktrees/` 등을 제외하여 이미지 크기를 최적화합니다.

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
| `NAVIDROME_URL` | `http://navidrome:4533/navidrome` | Navidrome URL |
| `NAVIDROME_USER` | `admin` | Navidrome 사용자명 |
| `NAVIDROME_PASSWORD` | — | Navidrome 비밀번호 **(필수)** |

> **Note**: 런타임 설정(파이프라인 주기, CF offset)은 환경변수가 아닌 SQLite settings 테이블로 관리됩니다. Web UI 또는 `/api/settings/pipeline-interval` API로 재시작 없이 변경 가능합니다.

---

## 3. 배포

### 3.1 로컬 개발

```bash
docker compose -f docker-compose.local.yml down && docker compose -f docker-compose.local.yml up --build -d
```

### 3.2 서버 배포 (이미지 전용)

서버에 필요한 파일: `docker-compose.prod.yml`, `.env`

```bash
docker compose -f docker-compose.prod.yml down && docker compose -f docker-compose.prod.yml up -d
```

Watchtower가 5분마다 GHCR 신규 이미지를 자동으로 감지하고 업데이트합니다.

### 3.3 Navidrome 초기 설정

brainstream이 `/navidrome/` 경로로 Navidrome 웹 UI를 프록시하므로, 포트 개방 없이 brainstream을 통해 접근합니다:

```
http://localhost:8080/navidrome/
```

최초 접속 시 관리자 계정을 생성하고 `NAVIDROME_USER` / `NAVIDROME_PASSWORD`와 일치하는 값으로 설정합니다.

### 3.4 서비스 확인

```bash
# 상태 확인
docker compose -f docker-compose.prod.yml ps

# brainstream 로그
docker compose -f docker-compose.prod.yml logs -f brainstream

# navidrome 로그
docker compose -f docker-compose.prod.yml logs -f navidrome
```

### 3.5 헬스체크

```bash
# 컨테이너 liveness 확인
curl http://localhost:8080/health
# {"status": "ok"}
```

Docker Compose에서 HEALTHCHECK로 사용 가능:
```yaml
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
  interval: 30s
  timeout: 5s
  retries: 3
```

---

## 4. 자체호스팅 (외부 공개) 설정

### 4.1 nginx 리버스 프록시

외부 인터넷에서 brainstream에 접근하려면 nginx를 통해 HTTPS로 노출합니다.
navidrome(:4533)은 직접 외부에 노출하지 않고, brainstream의 `/rest/*` 프록시를 통해 Subsonic API를 제공합니다.

**nginx 설정 예시** (`/etc/nginx/sites-available/brainstream`):

```nginx
server {
    listen 443 ssl;
    server_name stream.example.com;

    ssl_certificate     /etc/letsencrypt/live/stream.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/stream.example.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;

        # SSE 스트림을 위한 버퍼링 비활성화
        proxy_buffering    off;
        proxy_cache        off;
    }
}

server {
    listen 80;
    server_name stream.example.com;
    return 301 https://$host$request_uri;
}
```

적용:

```bash
sudo ln -s /etc/nginx/sites-available/brainstream /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### 4.2 방화벽 설정

navidrome(:4533)은 `docker-compose.yml`에 `ports` 선언이 없으므로 호스트에 포트가 바인딩되지 않습니다. Docker 내부 네트워크에서만 접근 가능하며 별도 방화벽 설정이 불필요합니다.

외부 Subsonic 앱(Amperfy, DSub 등)의 API 요청은 brainstream의 `/rest/*` 프록시를 통해 내부에서 navidrome으로 중계됩니다:

```
stream.example.com/rest/ping → brainstream:8080 → navidrome:4533 (내부)
```

### 4.3 Amperfy 연동 (iOS/macOS Subsonic 클라이언트)

Amperfy 등 Subsonic 호환 앱에서 brainstream의 `/rest/*` 프록시를 통해 Navidrome 라이브러리에 접근합니다.
navidrome을 직접 가리키지 않고 brainstream 주소를 서버로 입력합니다.

**Amperfy 서버 설정:**

| 항목 | 값 |
|------|----|
| 서버 URL | `https://stream.example.com` |
| 사용자명 | Navidrome 계정 사용자명 (예: `admin`) |
| 비밀번호 | Navidrome 계정 비밀번호 |
| API 종류 | Subsonic |

설정 후 연결 테스트가 성공하면 라이브러리가 자동으로 동기화됩니다.

연결 확인용 curl 테스트:

```bash
# token = md5(password + salt), salt는 임의 문자열
curl "https://stream.example.com/rest/ping?u=admin&t={md5_token}&s={salt}&v=1.16.1&c=amperfy&f=json"
# 응답: {"subsonic-response":{"status":"ok",...}}
```

---

## 5. 일상 운영

### 5.1 소스 코드 변경 시

```bash
# Python 소스 변경 (src/ 파일) — 로컬
./restart_local_docker.sh

# Python 소스 변경 — 서버 (이미지 재빌드 필요)
# git push → CI → Watchtower 자동 배포

# 환경변수 변경 (.env) — 재시작만
docker compose -f docker-compose.prod.yml down && docker compose -f docker-compose.prod.yml up -d
```

### 5.2 파이프라인 수동 실행

```bash
# API로 즉시 실행
curl -X POST http://localhost:8080/api/pipeline/run

# 결과 확인
curl http://localhost:8080/api/downloads | python3 -m json.tool
```

### 5.3 설정 관리

```bash
# 파이프라인 주기 조회
curl http://localhost:8080/api/settings/pipeline-interval

# 파이프라인 주기 변경 (1-24시간)
curl -X PUT http://localhost:8080/api/settings/pipeline-interval \
  -H "Content-Type: application/json" \
  -d '{"interval_hours": 3}'
```

> Web UI의 Settings 드롭다운에서도 동일하게 변경 가능합니다. 변경 사항은 SQLite settings 테이블에 즉시 저장되며 재시작 없이 적용됩니다.

### 5.4 다운로드 이력 확인

```bash
# API
curl http://localhost:8080/api/downloads | python3 -m json.tool

# SQLite 직접 조회 (state.db는 named volume — docker compose exec 사용)
docker compose -f docker-compose.prod.yml exec brainstream \
  sqlite3 /app/db/state.db "SELECT artist, track_name, status, attempts FROM downloads ORDER BY rowid DESC LIMIT 20;"

# 실패 목록만
docker compose -f docker-compose.prod.yml exec brainstream \
  sqlite3 /app/db/state.db "SELECT artist, track_name, error_msg, attempts FROM downloads WHERE status='failed';"
```

### 5.5 라이브러리 파일 확인

```bash
# 특정 아티스트 폴더 확인
ls data/music/{아티스트명}/

# 태그 확인 (ffprobe 사용 시)
docker compose -f docker-compose.prod.yml exec brainstream \
  ffprobe -v quiet -print_format json -show_format "/app/data/music/Radiohead/Pablo Honey/Creep.flac"
```

---

## 6. 로그

### 6.1 로그 위치

| 로그 | 경로 | 설명 |
|------|------|------|
| music-bot 앱 로그 | `data/logs/music-bot.log` | 구조화 로그 (JSON) |
| 로테이션된 로그 | `data/logs/music-bot.log.1` ~ `.5` | 자동 로테이션된 이전 로그 |
| Docker 로그 | `docker compose logs` | 컨테이너 stdout/stderr |

### 6.2 로그 로테이션

앱 로그는 `RotatingFileHandler`로 자동 로테이션된다:
- **파일 크기**: 50MB per file
- **백업 파일**: 최대 5개 (총 ~300MB)
- 현재 로그: `music-bot.log`, 로테이션: `.1` (최근) ~ `.5` (오래된 순)

### 6.3 로그 확인

```bash
# 실시간 앱 로그
docker compose logs -f brainstream

# 최근 에러만
docker compose logs brainstream | grep '"level":"error"'
```

---

## 7. 트러블슈팅

### yt-dlp 타임아웃 오류

yt-dlp 호출에 타임아웃이 적용되어 있다:

| 작업 | 타임아웃 | 설명 |
|------|---------|------|
| 메타데이터 추출 | 60초 | ytsearch5 검색 등 |
| 파일 다운로드 | 600초 (10분) | 실제 음원 다운로드 |
| 소켓 타임아웃 | 30초 | 각 네트워크 호출 |
| 추출기 재시도 | 3회 | 일시적 실패 자동 재시도 |

타임아웃 발생 시 다음 후보 영상으로 자동 retry된다.

### API 요청이 429 (Too Many Requests) 반환

POST/DELETE 엔드포인트에 Rate Limiting이 적용되어 있다:
- `POST /api/download`, `POST /api/rematch/apply`, `POST /api/edit/`, `DELETE /api/downloads/`: 10 req/min
- `POST /api/pipeline/run`: 2 req/min
- 인메모리 슬라이딩 윈도우 (서버 재시작 시 리셋)
- 해결: 요청 간격을 넓혀서 재시도. `Retry-After` 헤더 확인

### Graceful Shutdown 관련

- `docker stop`은 SIGTERM → graceful shutdown (워커 종료 신호 전파 및 최대 30s 대기, best-effort)
- Docker `stop_grace_period: 40s` 설정 (30s join + 10s 버퍼)
- 30s 내 미완료 시 Docker가 SIGKILL 전송 → `downloading` 상태 잡은 재시작 시 `mark_failed` 후 재시도
- `docker kill`은 즉시 SIGKILL → 반드시 재시작 복구 경로로 처리됨

### 추천 트랙이 반복됨 (같은 곡이 계속 나옴)

CF 추천은 `cf_offset`을 증가시키며 페이지네이션하므로, offset이 LB 추천 총 개수를 초과하면 동일한 트랙이 반복될 수 있습니다. offset을 초기화하려면:

```bash
# cf_offset 초기화 (다음 파이프라인 실행 시 처음부터 다시 시작)
docker compose exec brainstream \
  sqlite3 /app/db/state.db "UPDATE settings SET value='0' WHERE key='cf_offset';"
```

또는 Web UI Settings에서 파이프라인을 수동 실행하면 자동으로 새 추천을 가져옵니다.

### 트랙이 다운로드되지 않음

1. LB 추천이 있는지 확인:
   ```bash
   curl -H "Authorization: Token {YOUR_TOKEN}" \
     "https://api.listenbrainz.org/1/cf/recommendation/user/{username}/recording?count=5"
   ```
2. state.db에서 실패 원인 확인:
   ```bash
   docker compose exec brainstream sqlite3 /app/db/state.db "SELECT artist, track_name, error_msg FROM downloads WHERE status='failed';"
   ```
3. 로그 확인: `docker compose logs -f brainstream`

### 앨범이 Navidrome에서 2개로 분리됨

`tagger.py`의 `_enrich_track`에서 `mb_albumid`를 태그에 기록하지 않는지 확인. mb_albumid 태그가 있으면 Navidrome이 별개 앨범으로 취급함.

```bash
# 파일 FLAC 태그에 mb_albumid가 없는지 확인 (metaflac 사용)
docker compose exec brainstream metaflac --list "/app/data/music/아티스트/앨범/트랙.flac" | grep -i album
# MUSICBRAINZ_ALBUMID 항목이 없어야 정상
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

# Subsonic API brainstream 프록시로 테스트
curl "http://localhost:8080/api/subsonic/ping"
```

### 컨테이너 재시작 후 모듈 캐시 문제

Python 소스를 `docker cp`로 교체해도 uvicorn 프로세스가 모듈을 캐싱하고 있음.
반드시 컨테이너를 재시작해야 변경사항이 반영됨:

```bash
docker compose -f docker-compose.local.yml restart brainstream
```

### 재시작 후 다운로드 순서가 이상함 / 잡이 재처리되지 않음

재시작 시 `_reload_pending_jobs()`가 자동으로 미완료 잡을 재큐함.

- `pending` 잡 → 그대로 재큐 (순서 보존)
- `downloading` 잡 → 크래시로 중단된 것으로 간주, `attempts` 증가 후 재큐 (`attempts >= 3`이면 `failed`로 방치)

재큐 여부 확인:
```bash
# 재시작 직후 로그에서 확인
docker compose logs brainstream | grep "pending jobs reloaded"

# failed로 방치된 잡 확인
docker compose exec brainstream \
  sqlite3 /app/db/state.db "SELECT artist, track_name, attempts, error_msg FROM downloads WHERE status='failed';"
```

### staging 디렉토리에 .part 파일이 쌓임

잡 시작 시 자동으로 `staging/{mbid}.*` 파일을 정리하므로 일반적으로 누적되지 않음.
수동으로 정리하려면:

```bash
docker compose exec brainstream find /app/data/staging -name "*.part" -delete
```

---

## 8. 백업

```bash
# 상태 DB 백업
cp data/state.db data/state.db.backup

# 전체 음악 라이브러리 백업
tar -czf music-backup-$(date +%Y%m%d).tar.gz data/music/
```

---

## 9. 서비스 종료

```bash
# 데이터 보존하며 종료 (권장)
docker compose down

# 데이터 포함 완전 삭제 (주의)
docker compose down -v
rm -rf data/
```
