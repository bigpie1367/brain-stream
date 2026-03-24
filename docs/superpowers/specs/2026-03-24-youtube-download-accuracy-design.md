# YouTube Download Accuracy Fix

## Problem

LB 파이프라인에서 YouTube 음악 다운로드 시 요청한 곡과 다른 곡이 다운로드되는 문제.
두 가지 버그가 동시에 작동하여 잘못된 음원 + 잘못된 곡명이 결합됨.

### Bug 1: YouTube 선택에 title 유사도 검증 없음

`_select_best_entry()` (downloader.py)의 scoring에 요청 track_name과 YouTube title 간 유사도 비교가 없음.
채널 보너스(-200)만으로 엉뚱한 곡이 선택됨.

**로그 증거:**

| 요청 | YouTube 선택 | 원인 |
|------|-------------|------|
| "Believe Her" (Imagine Dragons) | "Believer (Official Music Video)" | 채널 보너스로 선택 |
| "Hard (Illmana Remix)" (Rihanna) | "Fuck Off (Hard Drums Remix)" | title 검증 없이 통과 |
| "Love the Way You Lie" (Eminem) | "Love The Way You Lie (Part II)" (Rihanna) | 다른 버전 선택 |

### Bug 2: iTunes canonical title이 원래 곡명을 덮어씀

`_enrich_track()` (tagger.py)에서 iTunes `trackName`을 canonical_title로 사용할 때 track_name 유사도 검증 없음.
아티스트 유사도(0.4)만 확인하므로 같은 아티스트의 다른 곡 이름이 매칭됨.

**로그 증거:**

| 요청 | iTunes canonical | 결과 |
|------|-----------------|------|
| "Believe Her" | "Believer (Live in Vegas)" | 곡명 변경됨 |
| "Dead in the Water (Drop Lamond remix)" | "Lights (Drop Lamond Remix)" | 완전히 다른 곡명 |

## Solution

### Fix 1: `_select_best_entry` title 유사도 강화

**파일**: `src/pipeline/downloader.py`

#### 1a. 곡명 추출 헬퍼 추가

`_extract_track_title(yt_title: str, artist: str) -> str`:
- YouTube title에서 아티스트명 제거 (대소문자 무시)
- " - " 구분자 처리 (예: "Artist - Track Name" → "Track Name")
- **noise 괄호만 제거** — 다음 키워드를 포함하는 괄호/브래킷만 strip:
  `official|video|audio|lyrics|lyric|visualizer|remaster(ed)?|upgrade|hd|4k|mv|music\s+video`
- "(Part II)", "(feat. ...)", "(Remix)" 등 음악적 의미가 있는 괄호는 유지
- 앞뒤 공백/구두점 정리

#### 1b. 필터 단계 (strict 필터링 직후)

- 추출된 곡명과 요청 `track_name`을 `_normalize()` 후 `SequenceMatcher` 비교
- 유사도 **0.3 미만**인 후보는 필터링 (live/cover 필터와 동일 패턴)
- 전부 필터링되면 → `ytsearch1:` 폴백으로 진행 (기존 폴백 경로 유지). 폴백 결과도 0.3 미만이면 실패 반환
- 필터링된 후보 수와 각 유사도를 로그에 기록

#### 1c. Scoring 단계

`score()` 함수에 title 유사도 패널티 추가:
- `(1 - title_similarity) * 800` 패널티
- 기존 scoring 대비 영향: cover +1000, live +500, **title dissimilarity 0~800**, channel -200~0, duration diff
- 선택된 결과의 title similarity를 "selected YouTube result" 로그에 추가

#### 1d. 유사도 비교 시 양쪽 모두 정규화

YouTube title과 요청 `track_name` 모두 `_extract_track_title()` (noise 괄호 제거) → `_normalize()` 처리 후 비교.
요청 track_name에도 "(Manhattan Clique extended radio edit)" 같은 부가정보가 있을 수 있으므로 비대칭 처리 방지.

단, `_extract_track_title`의 아티스트명 제거는 YouTube title에만 적용 (요청 track_name에는 아티스트명이 포함되지 않으므로).

#### 1e. 적용 범위

- `_select_best_entry()`를 통한 자동 선택에만 적용
- 수동 다운로드 (`download_track_by_id`)는 사용자가 직접 영상을 선택한 것이므로 title 검증 미적용

#### 검증 케이스

`_normalize()` 후 Python `SequenceMatcher` 실제 비율:

| YouTube title | 추출+정규화 결과 | 요청 정규화 | 유사도 | 결과 |
|---|---|---|---|---|
| "Three Days Grace - Animal I Have Become (Official Video)" | "animal i have become" | "animal i have become" | 1.0 | 패널티 0 ✓ |
| "Fuck Off (Hard Drums Remix)" | "fuck off hard drums remix" | "hard illmana remix" | ~0.28 | 필터링됨 ✓ |
| "Imagine Dragons - Believer (Official Music Video)" | "believer" | "believe her" | ~0.84 | 통과, 패널티 128 |
| "Rihanna - Love The Way You Lie (Part II) (Audio) ft. Eminem" | "love the way you lie part ii" | "love the way you lie" | ~0.83 | 통과, 낮은 패널티 ✓ |

**참고**: `_normalize()`는 `_normalize_for_match()`와 기능적으로 동일 (lowercase + alphanumeric/space만 유지). downloader.py에서는 기존 `_normalize()` 사용, tagger.py에서는 기존 `_normalize_for_match()` 사용.

### ~~Fix 2: iTunes/Deezer canonical title 유사도 검증~~ (철회)

수동 검증에서 iTunes canonical title 검증을 적용하면 **잘못 다운로드된 음원과 태그가 불일치하는 문제**가 더 심각해짐을 발견. YouTube에서 "Believer"를 다운로드했는데 태그에 "Believe Her"를 강제로 쓰는 결과.

iTunes/Deezer가 반환한 trackName은 **다운로드된 실제 음원에 맞는 정보**이므로 그대로 적용하는 것이 올바름. 잘못된 곡 선택 문제는 Fix 1 (YouTube title 유사도)에서 해결해야 함.

## Thresholds Summary

| 위치 | 임계값 | 용도 |
|------|--------|------|
| `_select_best_entry` 필터 | 0.3 | YouTube 후보 중 title 유사도 최소 기준 |
| `_select_best_entry` scoring | 800 weight | title 비유사도 패널티 가중치 |

## Scope

- downloader.py: `_extract_track_title` 헬퍼 추가, `_select_best_entry` 필터+scoring 수정
- 수동 다운로드 (`download_track_by_id`): title 검증 미적용 (사용자가 직접 선택)
- 기존 API, DB 스키마, 파일 구조 변경 없음
