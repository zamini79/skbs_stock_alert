# 프로젝트: SK바이오사이언스(302440) 주가 변동 자동 보고

## 목적
코스피 상장사 **SK바이오사이언스(종목코드 302440)** 의 주가가 전일 대비 ±5% 이상 움직이면,
그 원인을 공시·뉴스·시장 맥락을 근거로 자동 분석해 텔레그램 방으로 보고하는 IR 자동화 도구다.
회사 IR팀이 사장님께 드리던 "주가 ±5% 변동 시 원인 보고"를 자동화하는 것이 목표.

## 환경 (중요)
- **macOS / zsh** 환경이다. 명령어·경로·환경설정 안내는 모두 macOS/zsh 기준으로 작성할 것. (PowerShell 금지)
- Python 3. 외부 의존성은 `requests` 하나뿐 (`pip install -r requirements.txt`).
- API 키는 **코드에 하드코딩하지 않는다.** 모두 환경변수로 읽는다 (`.env.example` 참고, `~/.zshrc`에 export).

## 실행 / 테스트
```zsh
pip install -r requirements.txt
python3 stock_alert_302440.py          # 1회 실행 (현재가 확인 → 조건 충족 시 보고)
```
- 등락률이 임계치 미만이면 아무것도 보내지 않고 종료한다.
- 하루 최대 2회 보고(1차 ±4% 풀 보고서 / 2차 ±5% 요약 속보). `~/.stock_alert_302440_state.json`에
  당일 발송한 단계(`tiers`)를 기록해 중복·초과 발송을 막는다.

## 파이프라인 구조 (stock_alert_302440.py)
1. **감지** — `kis_token()` → `get_price()` : KIS API로 현재가·등락률 + **당일 고가/저가**와 그 등락률 조회
2. **트리거(2단계, 하루 최대 2회)** — `peak_rate` = 당일 고가/저가 중 전일종가 대비 절대값이 큰 쪽(부호 유지)
   → **장중 4% 찍고 되돌아온 경우도 포착**(현재가 기준 아님).
   - 1차: `abs(peak_rate) >= THRESHOLD(4.0)` 이고 1차 미발송 → `analyze()` 풀 원인분석 보고서.
   - 2차: `abs(peak_rate) >= THRESHOLD2(5.0)` 이고 1차 발송 후·2차 미발송 → `build_summary_report()` 데이터 요약 속보
     (인사말·원인분석·뉴스 없이 변동폭/지수/피어 블록만, **피어 기사 링크 없음** — 데이터만). 급등/급락 기사 링크는 1차 풀 보고서에만.
   - 한 번 실행(run)당 최대 1건 발송. <4%↑로 급등해 1차 미발송이면 그 run은 1차부터 보냄(2차는 다음 폴링에서).
3. **원인 수집**
   - `get_disclosures()` : OpenDART 당일 공시 목록
   - `get_news()` : 네이버 뉴스(다중 검색어·중복제거·시각필터·최신순)
   - `get_index()` : KOSPI(0001)·KOSDAQ(1001)·의약품 업종(0009) 지수값+등락률
   - `get_peers()` : 피어그룹(셀트리온·삼바·유한양행·녹십자·한미) 등락률
   - `get_investor_flow()` : 장중 외국인/기관 추정 순매수(수량×현재가 → 억원, 추정치)
4. **분석/조립** — `analyze()` : `_llm_narrative()`(LLM이 요약·원인·향후대응을 **JSON**으로 생성)
   + `build_report()`(수치는 코드가, 서술은 LLM이 채워 '사장님 보고서' HTML 조립)
5. **전송** — `send_telegram(parse_mode="HTML")`

## 구현 세부 (코드를 읽어야 알 수 있는 것 — stock_alert_302440.py)
- **KIS `tr_id`/엔드포인트 매핑** (변경 시 깨지기 쉬움):
  - `get_price()` → `tr_id=FHKST01010100`, `inquire-price`, 시장구분 `J`. 필드 `stck_prpr`(현재가)·`prdy_ctrt`(등락률)·`acml_vol`(거래량)·`stck_hgpr`(고가)·`stck_lwpr`(저가)·`stck_sdpr`(전일종가=기준가). 고가/저가 등락률과 `peak_rate`를 계산해 반환.
  - `get_index()` → `tr_id=FHPUP02100000`, `inquire-index-price`, 시장구분 `U`. 코드: KOSPI `0001`·KOSDAQ `1001`·의약품업종 `0009`.
    필드: 지수값 `bstp_nmix_prpr`, 등락률 `bstp_nmix_prdy_ctrt`. 응답에 업종명 필드 없음(0009=의약품은 지수값대로 확정).
    참고: **2026년 KOSPI는 ~8,000대**(전년比 +165% 강세장, 1월 ~4,200 → 6월 ~8,900). 값이 커 보여도 정상.
  - 피어그룹(`PEER_STOCKS`)·업종코드(`PHARMA_SECTOR_CODE=0009`)는 설정 상수. 시세는 `get_price()` 재사용.
  - `get_investor_flow()` → `tr_id=HHPTJ04160200`, `investor-trend-estimate`, 파라미터 `MKSC_SHRN_ISCD`(주의: FID_ 아님).
    응답 `output2` 배열의 **추정 순매수 수량**(`frgn_fake_ntby_qty`/`orgn_fake_ntby_qty`)만 제공 → 현재가 곱해 억원 환산(추정).
    증권사 MTS의 장중 실시간 수급과 동일 성격. 장 마감 후엔 거의 0.
- **KIS_BASE는 실전투자 도메인**(`openapi.koreainvestment.com:9443`). 모의투자 키를 쓰면 동작하지 않음.
- **환경변수 미설정 시 fail-fast 안 함**: 모든 키가 `여기에_...` 한글 플레이스홀더 문자열로 폴백한다.
  키가 비면 에러 없이 잘못된 요청을 보내므로, 디버깅 시 인증 실패/이상 응답을 먼저 의심할 것.
- **에러 처리 정책 (TODO #3 완료)**: 보조 수집(`get_kospi`·`get_disclosures`·`get_news`)은 실패해도
  각각 `None`/`[]`를 반환하고 경고 로그만 남긴 뒤 진행한다(전체 중단 안 함). `analyze()`는 Claude API
  실패 시 `_basic_report()`로 폴백. **관리자 알림(`alert_admin`)은 트리거(±5%) 이후 보고 단계 실패 시에만**
  발송 — 5분마다 도는 단순 시세조회 실패로 도배하지 않기 위함. 감지 단계(`kis_token`/`get_price`) 실패는
  로그만 남기고 비정상 종료(cron/CI에 실패로 기록).
- **로깅**: `setup_logging()`이 stdout + 파일(`LOG_FILE`, 기본 `~/.stock_alert_302440.log`)에 동시 기록.
  현재가 로그(`logging.info`)는 임계치와 무관하게 매 실행 출력 → 5분 폴링 시 로그로 시세 추적 가능.
- **`.env` 자동 로딩**: `load_dotenv()`가 스크립트 폴더의 `.env`를 읽어 환경변수로 채운다(표준 라이브러리만).
  단 **이미 셸/CI에 설정된 환경변수가 우선**(덮어쓰지 않음). GitHub Actions는 `.env` 없이 Secrets로 주입.
- **상태 파일 형식**: `~/.stock_alert_302440_state.json` = `{"date": "YYYY-MM-DD", "tiers": [1, 2]}`.
  날짜가 오늘과 다르면 빈 상태로 간주(리셋). `tiers`에 발송 완료한 단계(1·2)를 누적 기록해 단계별 1회·하루 2회를 판정.
- **텔레그램은 HTML 모드 전송**(`send_telegram(parse_mode="HTML")`): 뉴스 제목을 `<a>` 하이퍼링크로 발송하고
  모든 동적 텍스트를 `html.escape` 처리. AI 보고서 본문도 escape 후 '관련 뉴스' 링크 푸터를 붙인다.
- `CLAUDE_MODEL`은 `claude-sonnet-4-6`으로 하드코딩. 모델 교체 시 이 상수만 수정.

## 코드 컨벤션
- 주석·로그·보고서 출력은 **한국어**.
- 새 함수는 단일 책임으로, `main()`의 파이프라인 순서를 따른다.
- 외부 호출에는 `timeout`을 반드시 지정하고 실패 시 안전하게 처리(특히 보조 데이터 수집).
- **분석 프롬프트는 바이오·제약 섹터 맥락을 유지한다**: 임상 결과, 품목허가, 기술수출/공급계약,
  식약처·FDA 이슈, 모회사 SK케미칼 관련 등. 코스피가 같은 방향으로 크게 움직였으면 '개별 이슈'가
  아닌 '시장 전반 영향' 가능성을 명시하게 할 것.

## API 키 (모두 무료 발급, 환경변수명)
- `KIS_APP_KEY`, `KIS_APP_SECRET` — 한국투자증권 KIS Developers (계좌 필요)
- `DART_API_KEY` — OpenDART 인증키
- `DART_CORP_CODE` — SK바이오사이언스의 **DART 고유번호 = `01319899`**(8자리). 종목코드(302440)와 다름.
  `tools/find_corp_code.py`로 조회한 값. 비어 있으면 공시 단계가 동작하지 않는다.
- `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` — 네이버 검색 API
- `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` — @BotFather 봇 토큰 + 대상 방 chat_id
- `TELEGRAM_ADMIN_CHAT_ID` — (선택) 핵심 실패 통지용 방. 미설정 시 `TELEGRAM_CHAT_ID`로 폴백.
- 원인 분석 LLM (둘 중 **하나만 있어도 동작**, 우선순위 Anthropic > Gemini, 둘 다 없으면 기본 보고서 폴백):
  - `ANTHROPIC_API_KEY` — Claude API (크레딧 필요).
  - `GEMINI_API_KEY` — Google Gemini (https://aistudio.google.com, **무료·카드 불필요**). 현재 운영 키.
  - `GEMINI_MODEL` — (선택) 기본 `gemini-2.5-flash-lite`. ⚠️ 구형 `gemini-2.0-*`은 무료 쿼터가
    `limit:0`이라 동작 안 함. 무료 티어는 `*-flash-lite` 계열에서 호출됨(풀 flash는 503 잦음).
- `LOG_FILE` — (선택) 로그 파일 경로. 기본 `~/.stock_alert_302440.log`.

## 현재 상태 (완료)
- 4단계 전체 흐름이 한 파일에 구현됨. 환경변수/`.env` 기반 설정, 중복 알림 방지(쿨다운) 적용.
- **TODO #1 완료** — `tools/find_corp_code.py`로 `corp_code`(01319899) 확보.
- **TODO #3 완료** — 보조 수집 안전 폴백, AI 실패 폴백, 파일 로깅, 관리자 알림.
- **TODO #4 완료** — GitHub Actions 스케줄링(`.github/workflows/stock-alert.yml`): 평일 KST 09:00~15:55
  5분 간격(cron UTC `0-6`) + 수동 실행. 키는 GitHub Secrets, 하루 1회 쿨다운은 `actions/cache`로 보존.
- **TODO #5 완료** — 뉴스 클릭 링크 + 정확도 보강: 다중 검색어(`NEWS_QUERIES` = 종목명 + 백신/임상/계약),
  중복 제거(링크·정규화 제목), 시각 필터(`NEWS_MAX_AGE_HOURS`, 기본 24h), 최신순 정렬, 상한 `NEWS_LIMIT`(기본 8).

## 남은 작업 (TODO — 우선순위 순)
1. **검수용 라우팅**(구 #2): 현재는 보고 방에 직접 전송한다. AI 원인 분석은 추정이므로,
   사장님 방 직행 전 IR 담당자 검수 단계(담당자 방 선발송 → 승인 시 전달)를 넣는 옵션을 고려.
2. (선택) **n8n 포팅**(구 #6): 노코드 유지보수가 필요하면 동일 흐름을 n8n 워크플로로 옮긴다.

운영 메모: `TELEGRAM_CHAT_ID`가 개인방이면 그룹방(`-100…`)으로 교체 권장. GitHub 무료 스케줄은
부하 시 5~15분 지연/누락 가능(±5% 일일 감지엔 무방).

## 주의사항
- 상장사 IR 자료다. **AI 분석은 추정**임을 보고서에 항상 명시(코드의 프롬프트에 반영돼 있음).
- KIS API는 실전/모의 키가 다르고 호출 한도(rate limit)가 있으니 폴링 간격에 유의.
- 키를 절대 코드·로그·커밋에 노출하지 말 것. `.env`와 상태 파일은 `.gitignore`로 제외돼 있음.
