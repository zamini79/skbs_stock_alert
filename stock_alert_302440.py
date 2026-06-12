#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SK바이오사이언스(코스피 302440) 주가 ±5% 변동 감지 → 원인 분석 → 텔레그램 보고

[흐름]
  1) 한국투자증권(KIS) API로 현재가/전일종가 조회 → 등락률 계산
  2) |등락률| >= THRESHOLD 면 트리거:
       - OpenDART에서 당일 공시 목록 조회
       - 네이버 뉴스 검색으로 당일 기사 헤드라인 수집
       - KIS로 코스피 지수 등락(시장 맥락) 조회
  3) 위 재료를 Claude API에 보내 "원인 분석 보고서" 생성 (바이오 섹터 맥락 반영)
  4) 텔레그램 봇으로 지정 방에 전송
  5) 하루 중복 알림 방지(쿨다운) — 상태 파일에 기록

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
THRESHOLD    = 2.0               # ±2% (※ 동작 점검용 임시값 — 점검 후 5.0으로 원복)
STATE_FILE   = os.path.expanduser("~/.stock_alert_302440_state.json")

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
    r = requests.post(
        f"{KIS_BASE}/oauth2/tokenP",
        json={"grant_type": "client_credentials",
              "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def get_price(token, code):
    """현재가, 전일대비 등락률 반환"""
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST01010100",
    }
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
    r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
                     headers=headers, params=params, timeout=10)
    r.raise_for_status()
    out = r.json()["output"]
    return {
        "price": int(out["stck_prpr"]),          # 현재가
        "change_rate": float(out["prdy_ctrt"]),   # 전일대비 등락률(%)
        "volume": int(out["acml_vol"]),           # 누적 거래량
    }


def get_index(token, code):
    """국내 지수 조회 → {'value': 지수값, 'rate': 등락률%} 또는 None(실패 시).

    code: 0001=KOSPI, 1001=KOSDAQ, 0009=KOSPI 의약품 업종.
    """
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET,
        "tr_id": "FHPUP02100000",
    }
    params = {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code}
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-index-price",
                         headers=headers, params=params, timeout=10)
        r.raise_for_status()
        out = r.json()["output"]
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


def get_news(queries=None, max_age_hours=None, limit=None):
    """여러 검색어로 뉴스를 모아 중복 제거 + 시각 필터 + 최신순 정렬 후 반환.

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


def _narrative_prompt(price_info, market, peers, disclosures, news, direction):
    """사장님 보고서의 서술 부분(요약·원인·향후대응)을 JSON으로 생성하는 프롬프트."""
    rate = price_info["change_rate"]
    idx = lambda i: f"{i['value']:,.2f} ({i['rate']:+.2f}%)" if i else "조회불가"
    pharma = market.get("pharma")
    peer_txt = ", ".join(f"{p['name']} {p['rate']:+.2f}%" for p in peers) or "조회불가"
    return f"""당신은 상장 바이오·제약 기업 {STOCK_NAME}({STOCK_CODE})의 IR 애널리스트입니다.
아래 데이터를 근거로 금일 주가 {direction}({rate:+.2f}%)에 대한 사장님 보고용 분석을 작성하세요.

[지표]
- 당사: {price_info['price']:,}원 ({rate:+.2f}%)
- KOSPI {idx(market.get('kospi'))} / KOSDAQ {idx(market.get('kosdaq'))} / 의약품업종 {(f"{pharma['rate']:+.2f}%" if pharma else '조회불가')}
- 수급(장중 추정): {_flow_line(market.get('flow'))}
- 피어: {peer_txt}

[당일 공시]
{chr(10).join(disclosures) if disclosures else "- 없음"}

[당일 뉴스]
{chr(10).join(_news_lines_plain(news))}

[지침]
- 바이오·백신 섹터 특성(임상, 품목허가, 기술수출/공급계약, 식약처·FDA, 모회사 SK케미칼 등) 우선 고려.
- 지수·피어가 같은 방향으로 크게 움직였으면 '개별 이슈'보다 '시장 전반/업종 영향' 가능성을 명시.
- 모두 추정이며, 근거가 약하면 단정하지 말 것.

[출력] 아래 JSON 객체만 출력(설명·코드펜스 금지). 각 값은 한국어로 간결하게:
{{"summary": "현 추이와 핵심 원인 한 줄 요약",
  "cause_internal": "당사 호재/이슈 또는 수급 특징",
  "cause_external": "매크로·뉴욕증시·지정학 등 대외 변수(없으면 '특이사항 없음')",
  "outlook": "향후 대응/모니터링 포인트"}}"""


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


def _llm_narrative(price_info, market, peers, disclosures, news, direction):
    """서술 부분 dict({summary,cause_internal,cause_external,outlook}) 반환.

    제공처 우선순위 Claude > Gemini. 키 없거나 호출/JSON 파싱 실패 시 None
    → build_report가 수치·뉴스만으로 보고서를 구성한다.
    """
    if _key_set(ANTHROPIC_KEY):
        name, call = "Claude", _call_claude
    elif _key_set(GEMINI_API_KEY):
        name, call = "Gemini", _call_gemini
    else:
        return None
    prompt = _narrative_prompt(price_info, market, peers, disclosures, news, direction)
    try:
        raw = call(prompt).strip()
        # 코드펜스/언어태그 제거 후 첫 '{' ~ 마지막 '}' 구간만 파싱
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
        data = json.loads(raw)
        return {k: str(data.get(k, "")).strip()
                for k in ("summary", "cause_internal", "cause_external", "outlook")}
    except Exception:
        logging.warning("%s 분석/JSON 파싱 실패 — 수치·뉴스만으로 보고", name, exc_info=True)
        return None


def build_report(price_info, market, peers, narrative, news, direction):
    """사장님 보고용 텔레그램 HTML 리포트 조립. 수치는 코드가, 서술은 narrative(LLM)가 채운다."""
    price, rate = price_info["price"], price_info["change_rate"]
    n = narrative or {}
    esc = lambda v, default: html.escape(str(v).strip()) if v and str(v).strip() else default
    summary    = esc(n.get("summary"), "관련 공시·뉴스는 하단 참조 (AI 요약 미수행)")
    cause_int  = esc(n.get("cause_internal"), "하단 당일 공시·뉴스 참조")
    cause_ext  = esc(n.get("cause_external"), "특이사항 없음")
    outlook    = esc(n.get("outlook"), "장중 주가 추이 모니터링 지속")

    pharma = market.get("pharma")
    pharma_line = f"{pharma['rate']:+.2f}% {_move_word(pharma['rate'])}" if pharma else "조회불가"
    peer_lines = [f" {html.escape(p['name'])}: {p['rate']:+.2f}% {_move_word(p['rate'])}" for p in peers] \
        or [" - 조회불가"]

    lines = [
        "사장님, 안녕하세요.",
        "금일 주가 동향 보고드립니다.",
        "",
        f"금일 당사 주가는 <b>{price:,}원</b>으로 전일 대비 <b>{rate:+.2f}% {direction}</b> 중입니다.",
        summary,
        _flow_line(market.get("flow")),
        "",
        "<b>1. 상승/하락 원인</b>",
        f" • [당사·수급] {cause_int}",
        f" • [대외 변수] {cause_ext}",
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
        f" {outlook}",
        "",
        "<b>📰 관련 뉴스</b>",
        *_news_lines_html(news),
        "",
        "감사합니다.",
        "⚠️ 본 원인 분석은 AI 추정이며 투자판단의 근거가 아닙니다.",
    ]
    return "\n".join(lines)


def analyze(price_info, market, peers, disclosures, news):
    """사장님 보고용 HTML 리포트 문자열 생성. LLM이 없거나 실패해도 수치·뉴스로 보고한다."""
    direction = _move_word(price_info["change_rate"])
    narrative = _llm_narrative(price_info, market, peers, disclosures, news, direction)
    return build_report(price_info, market, peers, narrative, news, direction)


# ─────────────────────────────────────────────────────────────
# 5) 텔레그램 전송
# ─────────────────────────────────────────────────────────────
def send_telegram(text, parse_mode=None):
    payload = {"chat_id": TELEGRAM_CHAT, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
        payload["disable_web_page_preview"] = True  # 링크 미리보기로 메시지가 비대해지는 것 방지
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json=payload,
        timeout=10,
    ).raise_for_status()


# ─────────────────────────────────────────────────────────────
# 중복 알림 방지(하루 1회)
# ─────────────────────────────────────────────────────────────
def already_alerted_today():
    if not os.path.exists(STATE_FILE):
        return False
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("date") == str(datetime.date.today())
    except Exception:
        return False


def mark_alerted():
    with open(STATE_FILE, "w") as f:
        json.dump({"date": str(datetime.date.today())}, f)


# ─────────────────────────────────────────────────────────────
def main():
    setup_logging()

    # 감지 단계 — 매 실행(5분 간격) 도는 부분. 실패는 로그만 남기고 종료(관리자 도배 방지).
    token = kis_token()
    p = get_price(token, STOCK_CODE)
    logging.info("%s %s원 (%+.2f%%)", STOCK_NAME, f"{p['price']:,}", p["change_rate"])

    if abs(p["change_rate"]) < THRESHOLD:
        return
    if already_alerted_today():
        logging.info("오늘 이미 보고함 — 스킵")
        return

    # 여기서부터는 트리거됨 — 실패하면 '±%d%% 보고 누락'이므로 관리자에게 통지.
    try:
        market = {                            # 지수/업종/수급 (개별 실패는 None, 보조)
            "kospi":  get_index(token, KOSPI_CODE),
            "kosdaq": get_index(token, KOSDAQ_CODE),
            "pharma": get_index(token, PHARMA_SECTOR_CODE),
            "flow":   get_investor_flow(token, STOCK_CODE, p["price"]),
        }
        peers = get_peers(token)              # 피어그룹 등락률(보조)
        disclosures = get_disclosures()       # 실패해도 [] 반환(보조)
        news = get_news()                     # 다중 검색어·중복제거·시각필터(보조)
        report = analyze(p, market, peers, disclosures, news)
        send_telegram(report, parse_mode="HTML")
        mark_alerted()
        logging.info("✅ 텔레그램 보고 완료")
    except Exception as e:
        logging.exception("보고 단계 실패 — 트리거됐으나 전송하지 못함")
        alert_admin(f"±{THRESHOLD}% 트리거됐으나 보고 실패: {type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("실행 실패")
        raise
