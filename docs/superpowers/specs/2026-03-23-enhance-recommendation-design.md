# Enhance Recommendation — Design Spec

**Date**: 2026-03-23
**Scope**: CF 추천 offset 진행 + LB Radio 탐색 소스 + 파이프라인 주기 UI 제어
**Strategy**: 단일 PR (`feat/enhance-recommendation` 브랜치)

---

## 배경

현재 파이프라인은 LB CF 추천 API를 `offset=0`, `count=25` 고정으로 호출하여 매번 동일한 상위 25개만 가져온다. CF 모델 재학습 주기가 수주~월 단위이므로, 2-3주간 새로운 추천이 없는 상태가 지속된다.

**목표**: 취향 기반 80% + 탐색 20% 비율로 하루 15-25곡의 다양한 추천 확보.

---

## 1. state.db `settings` 테이블

### 스키마

```sql
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

### 저장할 설정값

| key | 용도 | 기본값 |
|-----|------|--------|
| `cf_offset` | CF 추천 현재 offset 위치 | `"0"` |
| `cf_first_mbid` | CF 모델 갱신 감지용 (첫 번째 MBID) | `""` |
| `pipeline_interval_hours` | 파이프라인 실행 주기 | env 기본값 (`"6"`) |

### state.py 함수 추가

```python
def get_setting(db_path: str, key: str, default: str = "") -> str:
    """settings 테이블에서 값 조회. 없으면 default 반환."""

def set_setting(db_path: str, key: str, value: str) -> None:
    """settings 테이블에 값 저장 (INSERT OR REPLACE)."""
```

`init_db()`에서 `settings` 테이블 CREATE IF NOT EXISTS 추가.

---

## 2. CF 추천 offset 진행

### listenbrainz.py 변경

`fetch_recommendations(username, token, count, offset=0)` — offset 파라미터 추가.

### run_pipeline() 변경 — 전체 흐름

```
target = cfg.listenbrainz.recommendation_count  # 기본 25
cf_target = round(target * 0.8)                 # 20
radio_target = target - cf_target               # 5
cf_exhausted = False

# --- CF 부분 ---
offset = int(get_setting(db, "cf_offset", "0"))
tracks = fetch_recommendations(username, token, count=cf_target, offset=offset)

if tracks:
    # 모델 갱신 감지: 첫 번째 MBID 비교
    new_first = tracks[0]["mbid"]
    old_first = get_setting(db, "cf_first_mbid", "")
    if old_first and new_first != old_first and offset > 0:
        # 모델이 갱신됨 → offset 리셋 후 다시 fetch
        offset = 0
        tracks = fetch_recommendations(username, token, count=cf_target, offset=0)
        new_first = tracks[0]["mbid"] if tracks else ""

    set_setting(db, "cf_first_mbid", new_first)
    set_setting(db, "cf_offset", str(offset + len(tracks)))
else:
    # CF 풀 소진 → Radio 목표 상향
    cf_exhausted = True
    radio_target += cf_target
```

---

## 3. LB Radio 탐색 소스

### 새 함수: listenbrainz.py

```python
def fetch_user_top_artists(
    username: str, range_: str = "quarter", count: int = 10
) -> list[dict]:
    """유저 탑 아티스트 조회. [{"artist_name": ..., "artist_mbid": ...}, ...]"""
    # GET /1/stats/user/{username}/artists?range={range_}&count={count}
    # range_ 실패 시 all_time 폴백

def fetch_lb_radio(prompt: str, mode: str = "easy") -> list[dict]:
    """LB Radio API 호출. JSPF 파싱하여 [{mbid, artist, track_name}, ...] 반환."""
    # GET /1/explore/lb-radio?prompt={prompt}&mode={mode}
    # JSPF 파싱:
    #   tracks[].identifier → recording MBID (URL에서 추출)
    #   tracks[].creator → artist
    #   tracks[].title → track_name
    # artist/title 비어있으면 lookup_recording(mbid)로 보강
```

### run_pipeline() 변경 — Radio 부분

```
top_artists = fetch_user_top_artists(username, range_="quarter", count=10)
if not top_artists:
    top_artists = fetch_user_top_artists(username, range_="all_time", count=10)

if top_artists:
    seed = random.choice(top_artists)
    prompt = f'artist:({seed["artist_name"]})'
    radio_tracks = fetch_lb_radio(prompt, mode="easy")
    radio_tracks = radio_tracks[:radio_target]
else:
    radio_tracks = []

# Radio 실패 시 CF 폴백 (CF가 소진되지 않은 경우에만)
if not radio_tracks and not cf_exhausted:
    cur_offset = int(get_setting(db, "cf_offset", "0"))
    extra = fetch_recommendations(username, token,
                                  count=radio_target, offset=cur_offset)
    radio_tracks = extra
    if extra:
        set_setting(db, "cf_offset", str(cur_offset + len(extra)))
# CF 소진 + Radio 실패 → 이번 실행은 빈 결과 (로그 에러, 다음 주기에 재시도)
```

### source 통일

CF 추천, Radio 추천 모두 `source = "listenbrainz"`로 통일. 별도 구분 없음.

---

## 4. 스케줄러 동적 주기

### schedule 라이브러리 제거 → 직접 시간 비교

- `main.py`에서 `import schedule` 제거
- `requirements.txt`에서 `schedule` 제거

```python
def _run_scheduler(cfg):
    last_run = time.time()  # 초기 실행은 별도 스레드에서 이미 수행
    while not _shutdown_event.is_set():
        _shutdown_event.wait(60)
        interval = int(get_setting(cfg.state_db, "pipeline_interval_hours",
                                   str(cfg.scheduler.interval_hours)))
        if time.time() - last_run >= interval * 3600:
            run_pipeline(cfg)
            last_run = time.time()
```

---

## 5. API 엔드포인트

### GET /api/settings/pipeline-interval

```python
@app.get("/api/settings/pipeline-interval")
async def get_pipeline_interval():
    value = get_setting(_cfg.state_db, "pipeline_interval_hours",
                        str(_cfg.scheduler.interval_hours))
    return {"interval_hours": int(value)}
```

### PUT /api/settings/pipeline-interval

```python
class IntervalUpdate(BaseModel):
    interval_hours: int = Field(ge=1, le=24)

@app.put("/api/settings/pipeline-interval")
async def set_pipeline_interval(body: IntervalUpdate):
    set_setting(_cfg.state_db, "pipeline_interval_hours",
                str(body.interval_hours))
    return {"interval_hours": body.interval_hours}
```

Rate limit: PUT에 기존 POST 엔드포인트와 동일한 슬라이딩 윈도우 적용 (10회/분).

---

## 6. UI 변경 — 파이프라인 실행 주기

### 실행 버튼 옆 드롭다운 추가

```
[▶ Run Pipeline]  Every [ 6h ▾ ]
```

선택지: `[1, 2, 3, 6, 12, 24]` 시간.

### 동작

- 페이지 로드 시 `GET /api/settings/pipeline-interval`로 현재값 fetch → 드롭다운 초기값
- 변경 시 즉시 `PUT /api/settings/pipeline-interval` 호출
- 성공: 선택값 유지
- 실패: 이전 값으로 롤백 + 에러 토스트

---

## 7. 에러 처리

| 실패 상황 | 대응 |
|-----------|------|
| CF API 타임아웃/에러 | 로그 경고, Radio만으로 진행 |
| CF 빈 응답 (풀 소진) | Radio 목표를 100%로 상향 |
| Radio API 500 에러 | 로그 경고, CF만으로 진행 |
| Radio 빈 플레이리스트 | CF 폴백 (추가 offset fetch) |
| 탑 아티스트 API 실패 | Radio 스킵, CF 100% |
| CF + Radio 둘 다 실패 | 로그 에러, 이번 실행 스킵 |
| settings 테이블 읽기 실패 | 기본값 사용 (offset=0, interval=env값) |

---

## 8. 변경 파일 요약

| 파일 | 변경 내용 |
|------|----------|
| `src/state.py` | `settings` 테이블, `get_setting()`, `set_setting()` |
| `src/pipeline/listenbrainz.py` | `fetch_recommendations()` offset 파라미터, `fetch_user_top_artists()`, `fetch_lb_radio()` |
| `src/main.py` | `run_pipeline()` CF offset + Radio 로직, `_run_scheduler()` 동적 주기 |
| `src/api.py` | `GET/PUT /api/settings/pipeline-interval` |
| `src/static/index.html` | 파이프라인 주기 드롭다운 |
| `requirements.txt` | `schedule` 패키지 제거 |
