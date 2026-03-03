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

### 2.1 설정 파일

```bash
cp config.yaml.example config.yaml
```

`config.yaml`을 편집:

```yaml
listenbrainz:
  username: "your_lb_username"
  token: "your_lb_token"
  recommendation_count: 25   # 1회 파이프라인에서 처리할 추천 수

download:
  staging_dir: /app/data/staging
  prefer_flac: true           # true: FLAC 우선, false: Opus만

navidrome:
  url: "http://navidrome:4533"
  username: "admin"           # Navidrome 관리자 계정
  password: "your_password"

scheduler:
  interval_hours: 6           # 자동 실행 주기 (시간)
```

### 2.2 환경변수 오버라이드 (선택)

`config.yaml` 대신 환경변수로 민감 정보 주입 가능:

```bash
export LB_USERNAME="your_lb_username"
export LB_TOKEN="your_lb_token"
export NAVIDROME_USER="admin"
export NAVIDROME_PASSWORD="your_password"
```

또는 `.env` 파일 생성 (`.gitignore`에 포함 권장):

```
LB_USERNAME=your_lb_username
LB_TOKEN=your_lb_token
NAVIDROME_USER=admin
NAVIDROME_PASSWORD=your_password
```

---

## 3. 배포

### 3.1 최초 실행

```bash
docker compose up --build -d
```

### 3.2 Navidrome 초기 설정

1. `http://localhost:4533` 접속
2. 최초 접속 시 관리자 계정 생성 (config.yaml의 username/password와 일치시킬 것)

### 3.3 서비스 확인

```bash
# 상태 확인
docker compose ps

# music-bot 로그
docker compose logs -f music-bot

# navidrome 로그
docker compose logs -f navidrome
```

---

## 4. 일상 운영

### 4.1 소스 코드 변경 시

```bash
# Python 소스 변경 (src/ 파일)
docker compose up --build -d

# beets 설정 변경 (beets/config.yaml) — 재빌드 불필요
docker restart music-bot-temp-music-bot-1

# config.yaml 변경 — 재빌드 불필요, 재시작만
docker restart music-bot-temp-music-bot-1
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
docker exec music-bot-temp-music-bot-1 beet list -f '$artist - $title [$album]'

# 특정 아티스트
docker exec music-bot-temp-music-bot-1 beet list -f '$path' artist:Radiohead

# 앨범 없는 트랙 확인
docker exec music-bot-temp-music-bot-1 beet list -f '$artist - $title' album:''
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
docker compose logs -f music-bot

# beets import 로그
tail -f data/logs/beets-import.log

# 최근 에러만
docker compose logs music-bot | grep '"level":"error"'
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
3. 로그 확인: `docker compose logs -f music-bot`

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
docker exec music-bot-temp-music-bot-1 beet list -f '$mb_albumid' artist:"아티스트"
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
