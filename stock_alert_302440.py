#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SK바이오사이언스(코스피 302440) 주가 ±5% 변동 감지 → 원인 분석 → 텔레그램 보고

[흐름]
  1) 한국투자증권(KIS) API로 현재가/전일종가 조회 → 등락률 계산
  2) 장중 최대 변동폭(peak)이 |peak| >= THRESHOLD(5%)면 트리거(하루 1회 풀 보고서):
       - 재료: OpenDART 당일 공시 + 네이버 뉴스 + KIS 지수/피어
  3) 위 재료를 Claude/Gemini에 보내 "원인 분석 보고서" 생성 (바이오 섹터 맥락 반영)
  4) 텔레그램 봇으로 지정 방에 전송 — 본 보고서 + '관련 뉴스'를 별도 메시지 2건으로
  5) 하루 중복 알림 방지 — 상태 파일에 보고한 날짜를 기록

[필요 키 — 모두 무료]
  - KIS Developers (한국투자증권 계좌 + 앱키/시크릿): https://apiportal.koreainvestment.com
  - OpenDART 인증키: https://opendart.fss.or.kr
  - 네이버 검색 API (Client ID/Secret): https://developers.naver.com
  - 텔레그램 봇 토큰(@BotFather) + chat_id
  - Anthropic API 키: https://console.anthropic.com

[실행]
  pip install requests
  python3 stock_alert_302440.py
"""

import os
import json
import time
import html
import logging
import datetime
import email.utils
import requests


# ─────────────────────────────────────────────────────────────
# .env 자동 로딩 (의존성 없이 표준 라이브러리만 사용)
#   - 스크립트와 같은 폴더의 .env 를 읽어 환경변수로 채운다.
#   - 이미 셸(~/.zshrc 등)에 설정된 값이 있으면 그쪽을 우선한다(덮어쓰지 않음).
#   - .env 가 없으면 조용히 넘어간다.
# ─────────────────────────────────────────────────────────────
def load_dotenv():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            # 주석(#) 분리 후 양끝 따옴표 제거
            val = val.split("#", 1)[0].strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


load_dotenv()


# ─────────────────────────────────────────────────────────────
# 설정 — 환경변수로 두는 걸 권장 (키를 코드에 직접 박지 마세요)
# zsh 예: export KIS_APP_KEY="..."  를 ~/.zshrc 에 추가
#   또는 .env 파일에 KEY=VALUE 형식으로 적어두면 자동 로딩됨(.env.example 참고)
# ─────────────────────────────────────────────────────────────
STOCK_CODE   = "302440"          # SK바이오사이언스
STOCK_NAME   = "SK바이오사이언스"
THRESHOLD    = 5.0               # ±5% — 변동 보고 트리거(하루 1회, 풀 보고서)
# 장 운영 시간 가드(KST 평일). cron이 지연 실행돼도 장시간 외엔 보고하지 않도록 스크립트가 직접 차단.
MARKET_OPEN  = os.environ.get("MARKET_OPEN", "09:00")   # HH:MM (KST)
MARKET_CLOSE = os.environ.get("MARKET_CLOSE", "15:30")  # HH:MM (KST)
STATE_FILE   = os.path.expanduser("~/.stock_alert_302440_state.json")
TOKEN_FILE   = os.path.expanduser("~/.stock_alert_302440_token.json")  # KIS 토큰 캐시(~24h 재사용)

# 뉴스 수집 — 검색어 다양화(종목명 + 바이오 이슈), 시각 필터, 최종 건수
NEWS_QUERIES = [
    STOCK_NAME,
    f"{STOCK_NAME} 백신",
    f"{STOCK_NAME} 임상",
    f"{STOCK_NAME} 계약",
]
NEWS_MAX_AGE_HOURS = float(os.environ.get("NEWS_MAX_AGE_HOURS", "24"))  # 최근 N시간 기사만
NEWS_LIMIT         = int(os.environ.get("NEWS_LIMIT", "8"))            # 보고 포함 최대 건수
_MIN_DT = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)

# 보고서 '1. 상승/하락 원인' 분석용 뉴스 검색어 그룹 (4개 차원)
# 검색은 넓게(QUERIES) 하되, 제목에 관련어(TOKENS)가 없는 기사는 버려 노이즈 제거.
COMPANY_QUERIES = [STOCK_NAME, "SK바사", "SK바이오사이언스 백신", "SK바이오사이언스 공급"]  # 당사
COMPANY_TOKENS  = ["SK바이오사이언스", "SK바사"]   # 제목에 이 중 하나 필수(SK바이오팜·반도체 IDT 노이즈 차단)
MACRO_QUERIES   = ["뉴욕증시 마감", "뉴욕증시", "미국증시"]       # 글로벌 매크로·개장 전
MACRO_TOKENS    = ["뉴욕", "나스닥", "다우", "S&P", "연준", "Fed", "FOMC", "금리", "유가", "환율", "엔캐리", "미국"]
MARKET_QUERIES  = ["코스피 마감", "코스닥 마감", "사이드카"]      # 장중 국내 시장
MARKET_TOKENS   = ["코스피", "코스닥", "증시", "사이드카", "서킷브레이커"]
SECTOR_QUERIES  = ["제약 바이오", "백신", "임상", "신약"]         # 제약·바이오 섹터
SECTOR_TOKENS   = ["제약", "바이오", "백신", "임상", "FDA", "신약", "식약처", "의약품", "팬데믹", "전염병"]
ANALYSIS_NEWS_AGE_HOURS = 36    # 전일~당일 포괄
ANALYSIS_NEWS_LIMIT     = 6     # 그룹별 분석에 넘길 헤드라인 수

# 시장/업종/피어 — 보고서 2·3번 항목용 (모두 기존 KIS 키로 조회)
KOSPI_CODE         = "0001"
KOSDAQ_CODE        = "1001"
PHARMA_SECTOR_CODE = "0009"   # KOSPI 의약품(제약) 업종 지수
PEER_STOCKS = [               # 주요 제약·바이오 피어그룹 (이름, 종목코드)
    ("셀트리온",        "068270"),
    ("삼성바이오로직스", "207940"),
    ("유한양행",        "000100"),
    ("GC녹십자",        "006280"),
    ("한미약품",        "128940"),
]

KIS_APP_KEY    = os.environ.get("KIS_APP_KEY", "여기에_앱키")
KIS_APP_SECRET = os.environ.get("KIS_APP_SECRET", "여기에_앱시크릿")
KIS_BASE       = "https://openapi.koreainvestment.com:9443"  # 실전투자

DART_API_KEY   = os.environ.get("DART_API_KEY", "여기에_DART키")
DART_CORP_CODE = os.environ.get("DART_CORP_CODE", "여기에_8자리_고유번호")
# ↑ SK바이오사이언스의 DART 고유번호(corp_code)는 종목코드와 다릅니다.
#   https://opendart.fss.or.kr 의 corpCode.xml 다운로드 API로 1회 조회해 채워두세요.

NAVER_ID       = os.environ.get("NAVER_CLIENT_ID", "여기에_네이버ID")
NAVER_SECRET   = os.environ.get("NAVER_CLIENT_SECRET", "여기에_네이버시크릿")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "여기에_봇토큰")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "여기에_chat_id")
# 관리자 통지용 방(미설정 시 일반 보고 방으로 폴백). 보고 누락 등 핵심 실패만 여기로 알림.
TELEGRAM_ADMIN_CHAT = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "")

ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "여기에_앤트로픽키")
CLAUDE_MODEL   = "claude-sonnet-4-6"  # 최신 모델명은 docs.claude.com 에서 확인

# Google Gemini — 무료 키: https://aistudio.google.com (Anthropic 대체용, 크레딧 불필요)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# 무료 티어에서 호출되는 모델로 지정. 구형(gemini-2.0-*)은 무료 쿼터가 0(limit:0)이라 동작 안 함.
# 호출 실패(쿼터/과부하) 시 analyze()가 기본 보고서로 폴백. 모델 교체는 GEMINI_MODEL 환경변수로.
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")

LOG_FILE = os.environ.get("LOG_FILE", os.path.expanduser("~/.stock_alert_302440.log"))


# ─────────────────────────────────────────────────────────────
# 로깅 — stdout + 파일 동시 기록 (단계별 진행/실패 추적)
# ─────────────────────────────────────────────────────────────
def setup_logging():
    handlers = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    except OSError:
        # 파일 핸들러 생성 실패(권한 등)는 치명적이지 않음 — stdout 로깅은 유지
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def alert_admin(message):
    """핵심 단계 실패를 관리자에게 best-effort로 통지(실패해도 예외 전파 안 함)."""
    chat = TELEGRAM_ADMIN_CHAT or TELEGRAM_CHAT
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith("여기에") or not chat or chat.startswith("여기에"):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat, "text": f"🚨 [{STOCK_NAME} 주가알림 오류] {message}"},
            timeout=10,
        )
    except Exception:
        logging.exception("관리자 알림 전송 실패")


# ─────────────────────────────────────────────────────────────
# 1) KIS — 주가 조회
# ─────────────────────────────────────────────────────────────
def kis_token():
    """KIS 접근토큰 발급. 토큰은 ~24h 유효하므로 파일에 캐시해 재사용한다.

    KIS는 토큰을 '1분당 1회'만 발급하고 잦은 재발급은 403을 반환하므로,
    5분 간격 폴링에서도 캐시된 유효 토큰(만료 10분 전까지)을 재사용한다.
    """
    try:
        with open(TOKEN_FILE) as f:
            c = json.load(f)
        if c.get("token") and c.get("expires_at", 0) - 600 > time.time():
            return c["token"]
    except Exception:
        pass  # 캐시 없음/손상 → 신규 발급

    # 토큰 발급은 감지 파이프라인의 첫 호출이라 여기서 죽으면 실행 전체가 실패(CI 실패 메일).
    # KIS의 일시적 네트워크 오류(타임아웃·RemoteDisconnected)를 흡수하도록 짧은 백오프 재시도.
    data = None
    for attempt in range(3):
        try:
            r = requests.post(
                f"{KIS_BASE}/oauth2/tokenP",
                json={"grant_type": "client_credentials",
                      "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            break
        except Exception:
            if attempt < 2:
                logging.warning("KIS 토큰 발급 실패 — 재시도(%d/2)", attempt + 1, exc_info=True)
                time.sleep(1.5 * (attempt + 1))   # 1.5s → 3s 백오프
                continue
            raise                                  # 3회 모두 실패 시 전파(설계대로 비정상 종료)
    token = data["access_token"]
    try:
        with open(TOKEN_FILE, "w") as f:
            json.dump({"token": token, "expires_at": time.time() + int(data.get("expires_in", 86400))}, f)
    except OSError:
        logging.warning("토큰 캐시 저장 실패 — 계속 진행", exc_info=True)
    return token


def _kis_quote(token, path, tr_id, params, attempts=3):
    """KIS 시세 GET 공통 — 'output'이 올 때까지 짧게 재시도(일시적 한도·블립 흡수). 실패 시 raise."""
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET,
        "tr_id": tr_id,
    }
    last = None
    for i in range(attempts):
        try:
            r = requests.get(f"{KIS_BASE}{path}", headers=headers, params=params, timeout=10)
            r.raise_for_status()
            j = r.json()
            if isinstance(j.get("output"), dict) and j["output"]:
                return j["output"]
            last = j.get("msg1") or "output 없음"
        except Exception as e:
            last = e
        if i < attempts - 1:
            time.sleep(0.6)   # 초당 호출 한도/일시 오류 완화
    raise RuntimeError(f"KIS 응답 이상({tr_id}): {last}")


def get_price(token, code):
    """현재가·등락률 + 당일 고가/저가 및 그 전일대비 등락률 반환.

    peak_rate = 당일 고가/저가 중 전일종가 대비 절대값이 큰 쪽(부호 유지).
    장중 4% 찍고 되돌아온 경우도 트리거하기 위해 main()은 change_rate가 아닌 peak_rate로 판정.
    """
    out = _kis_quote(token, "/uapi/domestic-stock/v1/quotations/inquire-price",
                     "FHKST01010100", {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
    price = int(out["stck_prpr"])                 # 현재가
    prev  = int(out["stck_sdpr"])                 # 기준가(전일 종가)
    high  = int(out["stck_hgpr"])                 # 당일 고가
    low   = int(out["stck_lwpr"])                 # 당일 저가
    rate  = lambda v: (v - prev) / prev * 100 if prev else 0.0
    high_rate, low_rate = rate(high), rate(low)
    peak_rate = high_rate if abs(high_rate) >= abs(low_rate) else low_rate
    return {
        "price": price,
        "change_rate": float(out["prdy_ctrt"]),   # 현재가 기준 등락률(%)
        "volume": int(out["acml_vol"]),           # 누적 거래량
        "prev_close": prev,
        "high": high, "low": low,
        "high_rate": high_rate, "low_rate": low_rate,  # 고가/저가의 전일대비 등락률(%)
        "peak_rate": peak_rate,                   # 장중 최대 변동폭(절대값 큰 쪽, 부호 유지)
    }


def get_index(token, code):
    """국내 지수 조회 → {'value': 지수값, 'rate': 등락률%} 또는 None(실패 시).

    code: 0001=KOSPI, 1001=KOSDAQ, 0009=KOSPI 의약품 업종.
    """
    try:
        out = _kis_quote(token, "/uapi/domestic-stock/v1/quotations/inquire-index-price",
                         "FHPUP02100000", {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code})
        return {"value": float(out["bstp_nmix_prpr"]),       # 지수 현재값
                "rate": float(out["bstp_nmix_prdy_ctrt"])}   # 전일대비 등락률(%)
    except Exception:
        logging.warning("지수 조회 실패(code=%s) — 해당 항목 생략", code, exc_info=True)
        return None


def get_peers(token):
    """피어그룹 등락률 [{'name', 'rate'}]. 개별 종목 실패는 건너뛴다(보조)."""
    out = []
    for name, code in PEER_STOCKS:
        try:
            out.append({"name": name, "rate": get_price(token, code)["change_rate"]})
        except Exception:
            logging.warning("피어 시세 실패(%s/%s) — 건너뜀", name, code, exc_info=True)
    return out


def get_investor_flow(token, code, price):
    """장중 외국인/기관 '추정' 순매수를 억원으로 환산해 반환 {'foreign_eok','institution_eok'}.

    KIS investor-trend-estimate(HHPTJ04160200)는 증권사 MTS와 동일한 '실시간 추정 가집계'로,
    순매수 '수량(주)'만 제공한다 → 현재가를 곱해 금액(억원)으로 환산(추정치). 실패/미집계 시 None.
    """
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET,
        "tr_id": "HHPTJ04160200",
    }
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/investor-trend-estimate",
                         headers=headers, params={"MKSC_SHRN_ISCD": code}, timeout=10)
        r.raise_for_status()
        rows = r.json().get("output2") or []
        if not rows:
            return None
        last = rows[-1]  # 최신 시간대 = 당일 누적 추정치
        frgn_qty = int(last.get("frgn_fake_ntby_qty") or 0)
        orgn_qty = int(last.get("orgn_fake_ntby_qty") or 0)
        return {"foreign_eok": frgn_qty * price / 1e8,
                "institution_eok": orgn_qty * price / 1e8}
    except Exception:
        logging.warning("수급(외국인/기관) 조회 실패 — 수급 생략", exc_info=True)
        return None


# ─────────────────────────────────────────────────────────────
# 2) OpenDART — 당일 공시
# ─────────────────────────────────────────────────────────────
def get_disclosures():
    today = datetime.date.today().strftime("%Y%m%d")
    try:
        r = requests.get(
            "https://opendart.fss.or.kr/api/list.json",
            params={"crtfc_key": DART_API_KEY, "corp_code": DART_CORP_CODE,
                    "bgn_de": today, "end_de": today, "page_count": 20},
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("list", [])
        return [f"- {i['report_nm']} ({i['flr_nm']}, {i['rcept_dt']})" for i in items]
    except Exception:
        logging.warning("공시 조회 실패 — 공시 없이 진행", exc_info=True)
        return []


# ─────────────────────────────────────────────────────────────
# 3) 네이버 뉴스 — 검색어 다양화 + 중복 제거 + 시각 필터 + 최신순
# ─────────────────────────────────────────────────────────────
def _naver_news_search(query, display=10):
    """네이버 뉴스 단일 검색 — raw 아이템 리스트 반환. 실패해도 [] 로 안전 처리."""
    try:
        r = requests.get(
            "https://openapi.naver.com/v1/search/news.json",
            headers={"X-Naver-Client-Id": NAVER_ID, "X-Naver-Client-Secret": NAVER_SECRET},
            params={"query": query, "display": display, "sort": "date"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("items", [])
    except Exception:
        logging.warning("뉴스 검색 실패(query=%s) — 건너뜀", query, exc_info=True)
        return []


def _parse_pub(s):
    """pubDate(RFC822) → tz-aware datetime. 실패 시 None."""
    try:
        dt = email.utils.parsedate_to_datetime(s)
        return dt.replace(tzinfo=datetime.timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None


def get_news(queries=None, max_age_hours=None, limit=None, must_include=None):
    """여러 검색어로 뉴스를 모아 중복 제거 + 시각 필터 + (선택)제목 관련어 필터 + 최신순 정렬.

    must_include: 토큰 리스트. 제목에 이 중 하나도 없으면 버린다(분야 무관 노이즈 제거).
    반환: [{"title", "link"}] (최신순, 최대 limit건). 어떤 단계가 실패해도 안전하게 폴백.
    """
    if isinstance(queries, str):
        queries = [queries]
    queries = NEWS_QUERIES if queries is None else queries
    max_age_hours = NEWS_MAX_AGE_HOURS if max_age_hours is None else max_age_hours
    limit = NEWS_LIMIT if limit is None else limit

    collected, seen_links, seen_titles = [], set(), set()
    for q in queries:
        for i in _naver_news_search(q):
            # <b> 강조 태그 제거 + HTML 엔티티 디코드 → 원문 제목
            title = html.unescape(i.get("title", "").replace("<b>", "").replace("</b>", "")).strip()
            if not title:
                continue
            if must_include and not any(tok in title for tok in must_include):
                continue                                  # 분야 관련어 없는 기사 제외
            link = i.get("link", "") or i.get("originallink", "")
            norm = "".join(title.split())                 # 공백 무시 정규화 제목
            if (link and link in seen_links) or norm in seen_titles:
                continue                                  # 중복 제거
            if max_age_hours and not _is_recent(i.get("pubDate", ""), max_age_hours):
                continue                                  # 시각 필터
            seen_links.add(link)
            seen_titles.add(norm)
            collected.append({"title": title, "link": link, "_pub": i.get("pubDate", "")})

    collected.sort(key=lambda c: _parse_pub(c["_pub"]) or _MIN_DT, reverse=True)  # 최신순
    return [{"title": c["title"], "link": c["link"]} for c in collected[:limit]]


def get_related_news(limit=None):
    """📰 '관련 뉴스' 푸터 전용 — 당사(COMPANY) 기사를 먼저 채우고, 모자라면 섹터(SECTOR)
    기사로 보충한다. 두 단계 모두 제목 관련어 필터(must_include)를 적용하므로
    종목·섹터 무관 노이즈(민원·공약·세미나 등)는 제외된다.

    기존 푸터는 must_include 없는 get_news()라 사장님 보고서에 무관 기사가 노출됐다.
    """
    limit = NEWS_LIMIT if limit is None else limit
    company = get_news(COMPANY_QUERIES, NEWS_MAX_AGE_HOURS, limit, COMPANY_TOKENS)
    if len(company) >= limit:
        return company[:limit]

    seen_links = {n.get("link", "") for n in company if n.get("link")}
    seen_titles = {"".join(n["title"].split()) for n in company}
    merged = list(company)
    for n in get_news(SECTOR_QUERIES, NEWS_MAX_AGE_HOURS, limit, SECTOR_TOKENS):
        link, norm = n.get("link", ""), "".join(n["title"].split())
        if (link and link in seen_links) or norm in seen_titles:
            continue                                  # 당사 단계와 중복 제거
        merged.append(n)
        if len(merged) >= limit:
            break
    return merged[:limit]


def get_peer_news_links(peers, max_age_hours=None):
    """급등/급락(|등락률| >= 7%) 피어에 대해 관련 기사 1건씩 검색 → {name: {title, link}}.

    '3. 주요 제약사 동향'에서 급등/급락 글자 뒤에 원인 기사 링크를 붙이기 위한 보조 수집.
    실패·미검색 시 해당 피어는 빠진다(전체 흐름 영향 없음).
    """
    age = NEWS_MAX_AGE_HOURS if max_age_hours is None else max_age_hours
    out = {}
    for p in peers:
        if abs(p.get("rate", 0.0)) < 7.0:        # _move_word의 급등/급락 임계치와 동일
            continue
        name = p["name"]
        # 종목명으로 검색하되, 제목에 종목명이 든 기사 우선. 없으면 무필터로 1건 폴백.
        arts = get_news([name], age, 1, [name]) or get_news([name], age, 1, None)
        if arts:
            out[name] = arts[0]
    return out


def _is_recent(pub_date_str, max_age_hours):
    """pubDate가 최근 max_age_hours 이내인지. 파싱 실패 시 True(보수적으로 포함)."""
    dt = _parse_pub(pub_date_str)
    if dt is None:
        return True
    age_h = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() / 3600.0
    return age_h <= max_age_hours


# ─────────────────────────────────────────────────────────────
# 4) LLM(Claude / Gemini) — 원인 분석 보고서 생성
# ─────────────────────────────────────────────────────────────
def _news_lines_html(news):
    """뉴스 항목(dict 리스트)을 텔레그램 HTML 하이퍼링크 줄로 변환. 제목 클릭 시 기사로 이동."""
    if not news:
        return ["- 수집된 기사 없음"]
    out = []
    for n in news:
        title = html.escape(n["title"])
        link = html.escape(n.get("link", ""), quote=True)
        out.append(f'• <a href="{link}">{title}</a>' if link else f"• {title}")
    return out


def _news_lines_plain(news):
    """AI 프롬프트용 평문 뉴스 줄(제목만)."""
    if not news:
        return ["- 수집된 기사 없음"]
    return [f"- {n['title']}" for n in news]


def _move_word(rate):
    """등락률 → 표현어. |등락|>=7% 급등/급락, 그 외 상승/하락."""
    if abs(rate) >= 7.0:
        return "급등" if rate > 0 else "급락"
    return "상승" if rate > 0 else "하락"


def _fmt_idx(idx):
    """지수 dict({value,rate}) → '8,390.14 (+8.07% 상승)' / 없으면 '조회불가'."""
    if not idx:
        return "조회불가"
    return f"{idx['value']:,.2f} ({idx['rate']:+.2f}% {_move_word(idx['rate'])})"


def _flow_line(flow):
    """수급 dict({foreign_eok,institution_eok}) → 헤더 한 줄. 없으면 '집계 전' 표기."""
    if not flow:
        return "(현재 수급: 집계 전/조회불가)"
    return (f"(현재 수급(장중 추정): 외국인 {flow['foreign_eok']:+,.0f}억 원, "
            f"기관 {flow['institution_eok']:+,.0f}억 원)")


def _key_set(key):
    """환경변수 키가 실제 설정됐는지(빈 값/한글 플레이스홀더 아님) 판정."""
    return bool(key) and not key.startswith("여기에")


def _narrative_prompt(price_info, market, peers, disclosures, news, direction, news_ctx):
    """사장님 보고서의 서술 부분(요약·4대 원인)을 JSON으로 생성하는 프롬프트.

    news_ctx: {'company','macro','market','sector'} 각각 뉴스 dict 리스트.
    '1. 상승/하락 원인'은 인과 중심·초간결·수치 배제·하십시오체로 4개 차원 작성.
    """
    rate = price_info["change_rate"]
    peak = price_info.get("peak_rate", rate)
    today_kst = (datetime.datetime.now(datetime.timezone.utc)
                 + datetime.timedelta(hours=9)).strftime("%Y년 %m월 %d일")

    def block(items):
        return chr(10).join(_news_lines_plain(items))

    return f"""당신은 상장 바이오·제약 기업 {STOCK_NAME}({STOCK_CODE})의 IR 애널리스트입니다.
연동된 네이버 뉴스를 근거로 금일 국내 증시 변동 원인과 당사·섹터 동향을 '초간결 요약'으로 작성하세요.
(오늘은 {today_kst}입니다. 금일 당사 주가는 장중 전일 대비 최대 {peak:+.2f}% {direction}, 현재 {rate:+.2f}%)

[당사 관련 뉴스]
{block(news_ctx.get('company'))}

[글로벌 매크로·뉴욕증시 뉴스]
{block(news_ctx.get('macro'))}

[국내 증시 뉴스(코스피/코스닥/사이드카)]
{block(news_ctx.get('market'))}

[제약·바이오 섹터 뉴스]
{block(news_ctx.get('sector'))}

[작성 조건 — 엄수]
- 인과관계 중심: 뉴스를 나열하지 말고, 그 뉴스로 인해 '상승'했는지 '하락'했는지 원인-결과를 명확히 연결할 것.
- 극단적 간결성: 각 항목 2~3문장 이내 핵심만.
- 수치 데이터 전면 배제: 지수 종가, 순매수 대금, 환율, '올해 몇 번째' 등 통계성 수치/누적횟수 일절 기재 금지.
- 일정 병기(예외): FOMC·한은 금통위·선물/옵션 만기·주요 경제지표 발표·실적발표 등 '예정된 일정'을 원인/대응으로 언급할 때는,
  제공된 뉴스에서 그 일자가 확인되면 반드시 괄호로 병기할 것. 예) "FOMC 회의(현지시간 17일)를 앞둔 경계감".
  뉴스에서 일자가 확인되지 않으면 날짜는 적지 말 것(추측 금지). 통계성 수치 배제 원칙과 별개로 '일정 날짜'는 허용한다.
- 100% 팩트 기반: 기사로 검증되지 않은 추측성 전망·자의적 의견 배제, 당일 확인된 인과관계만.
- 문체: '하십시오체'로 사족 없이.
- 해당 사항이 뉴스에 없으면 '특이사항 없음'으로 적을 것.

[출력] 아래 JSON 객체만 출력(설명·코드펜스 금지). 각 값은 위 조건을 지켜 한국어로:
{{"summary": "현 추이와 핵심 원인 한 줄 요약",
  "cause_company": "당사 주가 변동 원인 — 특정 뉴스가 당사 주가 상승/하락에 미친 영향과 핵심 이유",
  "cause_macro": "대외 변수 및 투심 영향 — 뉴욕증시 마감 등이 국내 개장 전 투자심리에 미친 상승/하락 요인",
  "cause_market": "장중 국내 시장 변동 원인 — 코스피/코스닥 등락을 이끈 핵심 원인 및 사이드카 발동 여부",
  "cause_sector": "제약·바이오 섹터 동향 및 원인 — 특정 이슈가 섹터 투자심리 호조/악화에 미친 원인"}}"""


def _call_claude(prompt):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": CLAUDE_MODEL, "max_tokens": 1024,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    r.raise_for_status()
    return "".join(b["text"] for b in r.json()["content"] if b["type"] == "text")


def _call_gemini(prompt):
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        headers={"content-type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


def _llm_narrative(price_info, market, peers, disclosures, news, direction, news_ctx):
    """서술 부분 dict({summary,cause_company,cause_macro,cause_market,cause_sector}) 반환.

    제공처 우선순위 Claude > Gemini. 키 없거나 호출/JSON 파싱 실패 시 None
    → build_report가 수치·뉴스만으로 보고서를 구성한다.
    """
    if _key_set(ANTHROPIC_KEY):
        name, call = "Claude", _call_claude
    elif _key_set(GEMINI_API_KEY):
        name, call = "Gemini", _call_gemini
    else:
        return None
    prompt = _narrative_prompt(price_info, market, peers, disclosures, news, direction, news_ctx)
    try:
        raw = call(prompt).strip()
        # 코드펜스/언어태그 제거 후 첫 '{' ~ 마지막 '}' 구간만 파싱
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
        data = json.loads(raw)
        return {k: str(data.get(k, "")).strip()
                for k in ("summary", "cause_company", "cause_macro",
                          "cause_market", "cause_sector")}
    except Exception:
        logging.warning("%s 분석/JSON 파싱 실패 — 수치·뉴스만으로 보고", name, exc_info=True)
        return None


def _peer_line(p, peer_news):
    """피어 한 줄. 급등/급락이면 그 글자 뒤에 관련 기사 링크를 붙인다."""
    word = _move_word(p["rate"])
    line = f" {html.escape(p['name'])}: {p['rate']:+.2f}% {word}"
    if word in ("급등", "급락"):
        art = (peer_news or {}).get(p["name"])
        if art and art.get("link"):
            title = html.escape(art["title"])
            link = html.escape(art["link"], quote=True)
            line += f' (<a href="{link}">{title}</a>)'
    return line


OUTLOOK_FIXED = "시장 상황을 면밀히 모니터링하고, 추가 변동사항이 있을 경우 신속히 보고 드리겠습니다."


def build_report(price_info, market, peers, narrative, direction, peer_news=None):
    """사장님 보고용 텔레그램 HTML 리포트 조립. 수치는 코드가, 서술은 narrative(LLM)가 채운다.

    '관련 뉴스'는 본 보고서에 넣지 않고 main()이 별도 메시지로 발송한다.
    '4. 향후 대응'은 고정 문구(OUTLOOK_FIXED)를 사용한다.
    """
    price, rate = price_info["price"], price_info["change_rate"]
    peak = price_info.get("peak_rate", rate)
    hi, lo = price_info.get("high_rate", rate), price_info.get("low_rate", rate)
    n = narrative or {}
    esc = lambda v, default: html.escape(str(v).strip()) if v and str(v).strip() else default
    summary       = esc(n.get("summary"), "관련 공시·뉴스 참조 (AI 요약 미수행)")
    cause_company = esc(n.get("cause_company"), "당일 공시·뉴스 참조")
    cause_macro   = esc(n.get("cause_macro"), "특이사항 없음")
    cause_market  = esc(n.get("cause_market"), "특이사항 없음")
    cause_sector  = esc(n.get("cause_sector"), "특이사항 없음")

    pharma = market.get("pharma")
    pharma_line = f"{pharma['rate']:+.2f}% {_move_word(pharma['rate'])}" if pharma else "조회불가"
    peer_lines = [_peer_line(p, peer_news) for p in peers] or [" - 조회불가"]

    lines = [
        "사장님, 안녕하세요.",
        "금일 주가 동향 보고드립니다.",
        "",
        f"금일 당사 주가는 장중 전일 대비 <b>최대 {peak:+.2f}% {direction}</b>을 기록했습니다.",
        f"(현재가 {price:,}원 / 현재 {rate:+.2f}% · 장중 고가 {hi:+.2f}% · 저가 {lo:+.2f}%)",
        summary,
        "",
        "<b>1. 상승/하락 원인</b>",
        f" • 당사 주가 변동 원인: {cause_company}",
        f" • 글로벌 매크로·개장 전 동향: {cause_macro}",
        f" • 장중 국내 시장 변동: {cause_market}",
        f" • 제약·바이오 섹터 동향: {cause_sector}",
        "",
        "<b>2. 국내 지수 및 업종 현황</b>",
        f" KOSPI: {_fmt_idx(market.get('kospi'))}",
        f" KOSDAQ: {_fmt_idx(market.get('kosdaq'))}",
        f" 제약(의약품) 업종: {pharma_line}",
        "",
        "<b>3. 주요 제약사 동향</b>",
        *peer_lines,
        "",
        "<b>4. 향후 대응</b>",
        f" {OUTLOOK_FIXED}",
        "",
        "감사합니다.",
    ]
    return "\n".join(lines)


def build_news_message(news):
    """관련 뉴스를 본 보고서와 분리해 보내는 별도 텔레그램 메시지. 뉴스 없으면 None."""
    if not news:
        return None
    return "\n".join(["<b>📰 관련 뉴스</b>", *_news_lines_html(news)])


def analyze(price_info, market, peers, disclosures, news, news_ctx):
    """사장님 보고용 HTML 리포트 문자열 생성. LLM이 없거나 실패해도 수치로 보고한다."""
    direction = _move_word(price_info["peak_rate"])   # 장중 최대 변동폭 기준 표현
    narrative = _llm_narrative(price_info, market, peers, disclosures, news, direction, news_ctx)
    peer_news = get_peer_news_links(peers)            # 급등/급락 피어 기사 링크(보조)
    return build_report(price_info, market, peers, narrative, direction, peer_news)


# ─────────────────────────────────────────────────────────────
# 5) 텔레그램 전송
# ─────────────────────────────────────────────────────────────
def send_telegram(text, parse_mode=None):
    """TELEGRAM_CHAT_ID(콤마로 여러 방 지정 가능)의 모든 방으로 전송.

    일부 방이 실패해도 나머지는 보내고, '전부 실패'일 때만 예외를 던진다
    (한 곳이라도 도달하면 main()이 보고 완료로 처리해 중복 전송을 막음).
    """
    chats = [c.strip() for c in str(TELEGRAM_CHAT).split(",") if c.strip()]
    sent = 0
    for chat in chats:
        payload = {"chat_id": chat, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
            payload["disable_web_page_preview"] = True  # 링크 미리보기로 메시지 비대해짐 방지
        for attempt in range(3):                        # 일시적 네트워크 타임아웃 대비 재시도
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json=payload, timeout=15,
                ).raise_for_status()
                sent += 1
                break
            except Exception:
                if attempt < 2:
                    time.sleep(1.5)
                    continue
                logging.warning("텔레그램 전송 실패(chat=%s) — 나머지 방은 계속", chat, exc_info=True)
    if sent == 0:
        raise RuntimeError("모든 텔레그램 대상 전송 실패")


# ─────────────────────────────────────────────────────────────
# 장 운영 시간 가드 (GitHub cron 지연 실행 대비 — 코드 차원 방어)
# ─────────────────────────────────────────────────────────────
def within_market_hours(now_kst=None):
    """현재(KST)가 평일 장 운영 시간(MARKET_OPEN~MARKET_CLOSE)인지. 주말·장시간 외면 False.

    GitHub Actions 예약(cron)은 부하 시 수 시간 지연 실행될 수 있어, cron 창만 믿으면
    장 마감 후에도 보고가 나간다(실측: KST 18시·19시 발송). 시각 비교는 UTC+9로 직접 계산해
    러너 TZ 설정과 무관하게 동작한다.
    """
    now = now_kst or (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=9))
    if now.weekday() >= 5:                        # 5=토, 6=일
        return False
    to_min = lambda s: int(s.split(":")[0]) * 60 + int(s.split(":")[1])
    cur = now.hour * 60 + now.minute
    return to_min(MARKET_OPEN) <= cur <= to_min(MARKET_CLOSE)


# ─────────────────────────────────────────────────────────────
# 중복 알림 방지(하루 1회)
# ─────────────────────────────────────────────────────────────
def already_alerted_today():
    """오늘 이미 보고했는지. 상태 파일의 날짜가 오늘과 같으면 True."""
    if not os.path.exists(STATE_FILE):
        return False
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("date") == str(datetime.date.today())
    except Exception:
        return False


def mark_alerted():
    """오늘 날짜로 보고 완료를 기록한다."""
    with open(STATE_FILE, "w") as f:
        json.dump({"date": str(datetime.date.today())}, f)


# ─────────────────────────────────────────────────────────────
def main():
    setup_logging()

    # 장 운영 시간 가드 — cron이 지연 실행돼도 평일 09:00~15:30(KST) 밖이면 아무것도 하지 않음.
    # (IGNORE_MARKET_HOURS=1 로 수동 점검 시 우회 가능)
    if os.environ.get("IGNORE_MARKET_HOURS") != "1" and not within_market_hours():
        logging.info("장 운영 시간(평일 %s~%s KST) 외 — 스킵", MARKET_OPEN, MARKET_CLOSE)
        return

    # 오늘 이미 보고했으면 KIS 호출 전에 즉시 종료 — 발송 후 남은 폴링의 불필요한 KIS 호출 방지.
    if already_alerted_today():
        logging.info("오늘 이미 보고함 — 스킵(KIS 호출 생략)")
        return

    # 감지 단계 — 매 실행(5분 간격) 도는 부분. KIS 일시 장애(타임아웃·연결끊김·이상응답)는
    # 재시도로도 안 되면 '이번 폴링만 조용히 스킵(정상 종료)'한다. 5분 뒤 다음 폴링이 자동 복구하므로
    # CI 실패(=All jobs have failed 메일)로 도배하지 않기 위함. 진짜 장기 장애면 보고 부재로 드러난다.
    try:
        token = kis_token()
        p = get_price(token, STOCK_CODE)
    except (requests.exceptions.RequestException, RuntimeError) as e:
        logging.warning("감지 단계 일시 실패 — 이번 폴링 스킵(다음 5분 폴링이 재시도): %s", e)
        return
    logging.info("%s %s원 (현재 %+.2f%% / 장중 고가 %+.2f%% · 저가 %+.2f%%)",
                 STOCK_NAME, f"{p['price']:,}", p["change_rate"], p["high_rate"], p["low_rate"])

    # 트리거는 현재가가 아니라 '장중 최대 변동폭'(고가/저가 중 큰 쪽)으로 판정 —
    # 장중 5% 찍고 되돌아온 경우도 놓치지 않기 위함. 하루 1회만 발송.
    abs_peak = abs(p["peak_rate"])
    if abs_peak < THRESHOLD:
        return

    # 여기서부터는 트리거됨 — 실패하면 보고 누락이므로 관리자에게 통지.
    try:
        market = {                            # 지수/업종 (개별 실패는 None, 보조)
            "kospi":  get_index(token, KOSPI_CODE),
            "kosdaq": get_index(token, KOSDAQ_CODE),
            "pharma": get_index(token, PHARMA_SECTOR_CODE),
        }
        peers = get_peers(token)              # 피어그룹 등락률(보조)
        disclosures = get_disclosures()       # 실패해도 [] 반환(보조)
        news = get_related_news()             # 관련 뉴스(당사 우선 + 섹터 보충, 노이즈 제외) — 별도 메시지로 발송
        # '1. 상승/하락 원인' 분석용 — 4개 차원 뉴스 그룹(전일~당일)
        news_ctx = {
            "company": get_news(COMPANY_QUERIES, ANALYSIS_NEWS_AGE_HOURS, ANALYSIS_NEWS_LIMIT, COMPANY_TOKENS),
            "macro":   get_news(MACRO_QUERIES,   ANALYSIS_NEWS_AGE_HOURS, ANALYSIS_NEWS_LIMIT, MACRO_TOKENS),
            "market":  get_news(MARKET_QUERIES,  ANALYSIS_NEWS_AGE_HOURS, ANALYSIS_NEWS_LIMIT, MARKET_TOKENS),
            "sector":  get_news(SECTOR_QUERIES,  ANALYSIS_NEWS_AGE_HOURS, ANALYSIS_NEWS_LIMIT, SECTOR_TOKENS),
        }
        report = analyze(p, market, peers, disclosures, news, news_ctx)
        send_telegram(report, parse_mode="HTML")
        mark_alerted()                        # 본 보고 도달 → 중복 방지 확정
        logging.info("✅ 텔레그램 보고 완료 (peak %+.2f%%)", p["peak_rate"])

        # 관련 뉴스는 별도 메시지로 발송(best-effort — 실패해도 본 보고는 이미 나감).
        news_msg = build_news_message(news)
        if news_msg:
            try:
                send_telegram(news_msg, parse_mode="HTML")
                logging.info("✅ 관련 뉴스 별도 메시지 발송 완료")
            except Exception:
                logging.warning("관련 뉴스 메시지 전송 실패(본 보고는 발송됨)", exc_info=True)
    except Exception as e:
        logging.exception("보고 단계 실패 — 트리거됐으나 전송하지 못함")
        alert_admin(f"트리거(peak {p['peak_rate']:+.2f}%)됐으나 보고 실패: {type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("실행 실패")
        raise
