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
- 괄호/브래킷 부가정보 제거: `(Official Video)`, `[4K Upgrade]`, `(Remastered 2023)` 등
- 앞뒤 공백/구두점 정리

#### 1b. 필터 단계 (strict 필터링 직후)

- 추출된 곡명과 요청 `track_name`을 `_normalize()` 후 `SequenceMatcher` 비교
- 유사도 **0.3 미만**인 후보는 필터링 (live/cover 필터와 동일 패턴)
- 전부 필터링되면 → `download_track()`에서 실패 반환 (`None, None`)
- 필터링된 후보 수를 로그에 기록

#### 1c. Scoring 단계

`score()` 함수에 title 유사도 패널티 추가:
- `(1 - title_similarity) * 800` 패널티
- 기존 scoring 대비 영향: cover +1000, live +500, **title dissimilarity 0~800**, channel -200~0, duration diff

#### 검증 케이스

| YouTube title | 추출 결과 | vs 요청 | 유사도 | 결과 |
|---|---|---|---|---|
| "Three Days Grace - Animal I Have Become (Official Video)" | "Animal I Have Become" | "Animal I Have Become" | ~1.0 | 패널티 0 ✓ |
| "Fuck Off (Hard Drums Remix)" | "Fuck Off" | "Hard (Illmana Remix)" | ~0.15 | 필터링됨 ✓ |
| "Imagine Dragons - Believer (Official Music Video)" | "Believer" | "Believe Her" | ~0.67 | 통과, 패널티 264 |
| "Rihanna - Love The Way You Lie (Part II)" | "Love The Way You Lie (Part II)" | "Love the Way You Lie" | ~0.75 | 통과, 낮은 패널티 ✓ |

### Fix 2: iTunes/Deezer canonical title 유사도 검증

**파일**: `src/pipeline/tagger.py`

#### 변경 대상: `_enrich_track()` 내 iTunes/Deezer 결과 처리

- iTunes/Deezer가 반환한 `trackName`과 요청 `track_name`을 `_normalize_for_match()` 후 `SequenceMatcher` 비교
- 유사도 **0.5 미만**이면 `canonical_title`로 채택하지 않음 (빈 문자열 유지)
- `album`, `artwork_url`, `artistName`은 그대로 사용
- Deezer에도 동일 적용

#### 검증 케이스

| 요청 track | iTunes trackName | 유사도 | 결과 |
|---|---|---|---|
| "Believe Her" | "Believer (Live in Vegas)" | ~0.35 | 차단 ✓ |
| "Dead in the Water (Drop Lamond remix)" | "Lights (Drop Lamond Remix)" | ~0.45 | 차단 ✓ |
| "Leave Out All the Rest" | "Leave Out All The Rest" | ~0.97 | 통과 ✓ |
| "Feel Good Inc." | "Feel Good Inc. (feat. David Jolicoeur...)" | ~0.55 | 통과 ✓ |
| "another stupid song" | "i love you" | ~0.1 | 차단 ✓ |

## Thresholds Summary

| 위치 | 임계값 | 용도 |
|------|--------|------|
| `_select_best_entry` 필터 | 0.3 | YouTube 후보 중 title 유사도 최소 기준 |
| `_select_best_entry` scoring | 800 weight | title 비유사도 패널티 가중치 |
| `_enrich_track` canonical_title | 0.5 | iTunes/Deezer trackName 채택 기준 |

## Scope

- downloader.py: `_extract_track_title` 헬퍼 추가, `_select_best_entry` 필터+scoring 수정
- tagger.py: `_enrich_track` 내 canonical_title 검증 추가
- 기존 API, DB 스키마, 파일 구조 변경 없음
