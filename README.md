# PCB_LT_Analyzer — Volume Spike Screener

평소보다 **거래량이 터졌는데 가격은 거의 움직이지 않은** 종목을 KOSPI / KOSDAQ 에서 찾는 데스크톱 GUI 입니다.
거래량 배수와 가격 허용 범위를 슬라이더로 조정하면 결과가 즉시 다시 계산됩니다.

- 시세: KRX 정보데이터시스템 (KRX 계정 로그인, [pykrx](https://github.com/sharebook-kr/pykrx) 사용)
- 공시: OpenDART API (선택 종목의 최근 90일 공시, 위험/호재 공시 색상 표시)
![screenshot](docs/screenshot.png)

- 다른 상승 후보 스크리닝 방법 검토: [`docs/screening_strategies_review.md`](docs/screening_strategies_review.md)

## 설치 · 실행

```bash
pip install -r requirements.txt
cp .env.example .env        # KRX_ID / KRX_PW / DART_API_KEY 입력 (또는 GUI '계정' 버튼에서 입력)
python run_screener.py      # KRX 실데이터
python run_screener.py --demo   # 계정/네트워크 없이 합성 데이터로 UI 확인
```

> **DART 는 ID/PW 가 아니라 API 인증키를 씁니다.** opendart.fss.or.kr 에 로그인 → *인증키 신청/관리* 에서 40자리 키를 발급받아 넣으세요.
> 계정 정보는 `.env`(git 제외)에만 저장되며, 저장하지 않으면 실행 중 메모리에만 있습니다.

## 화면 구성

| 영역 | 내용 |
|---|---|
| 상단 | 데이터 소스(KRX/데모), 기준일, 데이터 불러오기, 계정, CSV 내보내기 |
| 왼쪽 | 조건 카드 — ① 거래량 ② 가격 ③ 유동성·규모 ④ 시장·제외, 프리셋 저장/불러오기 |
| 가운데 | 결과 요약 칩, 종목 검색, 결과 테이블(컬럼 클릭 정렬, 더블클릭 → 네이버 금융) |
| 아래 | 선택 종목 캔들+거래량 차트(파랑 음영=평소 구간, 주황=급증 구간), DART 공시 목록 |

## 조건 정의

```
[ ...... 기준 구간(평소) base_days ...... ][ 최근 구간 recent_days ]
                                        ↑ 직전 종가 = 등락률 기준점        ↑ 기준일
```

| 파라미터 | 의미 | 기본값 |
|---|---|---|
| 기준 구간 | 평소 거래량을 계산하는 과거 영업일 수 (중앙값/평균 선택) | 20일, 중앙값 |
| 최근 구간 | 거래량 급증을 확인할 영업일 수 | 1일 |
| 거래량 배수 | 최근 구간 평균 거래량 ÷ 평소 거래량 | ≥ 3배 |
| 기간 등락률 | 최근 구간 직전 종가 대비 기준일 종가 | ±2% 이내 |
| 일별 등락률 | 최근 구간 각 날짜 등락률 절대값 최대 | 3% 이내 |
| 일중 변동폭 | (고가−저가)/전일종가 최대 | 6% 이내 |
| 박스권(선택) | 기준 구간 (최고−최저 종가)/평균 | 15% 이내 |
| 유동성 | 최근 평균 거래대금, 주가, 시가총액 범위 | 5억↑, 1,000원↑ |
| 제외 | 우선주, 스팩, 최근 구간 거래정지 종목 | 제외 |

점수 = `log2(거래량배수) × 10 / (1 + |기간등락률| + 0.5 × 일중변동폭)` — 거래량이 클수록, 가격이 덜 움직일수록 높음.

## 데이터 수집 방식

날짜별 '전종목 시세'를 시장별로 한 번씩 받아(약 60영업일 × 2시장 ≈ 120회 호출) 패널로 합칩니다.
지난 영업일 데이터는 `.cache/krx/` 에 저장되어 두 번째 실행부터는 거의 즉시 로드됩니다.
조건 슬라이더 조정은 네트워크 호출 없이 메모리에서만 다시 계산합니다.

## 테스트

```bash
python -m pytest -q
```

## 구조

```
run_screener.py            # 실행 진입점
screener/core.py           # 스크리닝 로직 (pandas, GUI 독립)
screener/data/krx.py       # KRX 로그인 + 시세 패널 수집/캐시
screener/data/dart.py      # OpenDART 공시 조회·분류
screener/data/demo.py      # 오프라인 합성 데이터
screener/gui/              # PySide6 GUI (테마, 위젯, 차트, 테이블 모델, 워커)
docs/screening_strategies_review.md   # 추가 스크리닝 방법 검토
```

> 본 도구는 종목 후보를 찾는 보조 도구이며 매수/매도 추천이 아닙니다. 자동 주문 기능은 포함하지 않습니다.
