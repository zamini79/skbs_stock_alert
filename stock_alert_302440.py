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
THRESHOLD    = 5.0               # ±5%
STATE_FILE   = os.path.expanduser("~/.stock_alert_302440_state.json")

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


def get_kospi(token):
    """시장 맥락: 코스피 지수 등락률"""
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET,
        "tr_id": "FHPUP02100000",
    }
    params = {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": "0001"}
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-index-price",
                         headers=headers, params=params, timeout=10)
        r.raise_for_status()
        out = r.json()["output"]
        return float(out["bstp_nmix_prdy_ctrt"])  # 코스피 등락률(%)
    except Exception:
        logging.warning("코스피 지수 조회 실패 — 시장 맥락 없이 진행", exc_info=True)
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
# 3) 네이버 뉴스 — 당일 헤드라인
# ─────────────────────────────────────────────────────────────
def get_news(query, count=8):
    try:
        r = requests.get(
            "https://openapi.naver.com/v1/search/news.json",
            headers={"X-Naver-Client-Id": NAVER_ID, "X-Naver-Client-Secret": NAVER_SECRET},
            params={"query": query, "display": count, "sort": "date"},
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
    except Exception:
        logging.warning("뉴스 조회 실패 — 뉴스 없이 진행", exc_info=True)
        return []
    out = []
    for i in items:
        # <b> 강조 태그 제거 + HTML 엔티티(&quot; &amp; 등) 디코드 → 원문 제목
        title = html.unescape(i["title"].replace("<b>", "").replace("</b>", ""))
        out.append({"title": title, "link": i.get("link", "")})
    return out


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


def _basic_report(change_rate, price, kospi_rate, disclosures, news, direction):
    """ANTHROPIC_API_KEY 미설정 시 — AI 분석 없이 수집 원자료만 정리한 기본 보고서(텔레그램 HTML).

    동적 텍스트(공시·뉴스 제목)는 html.escape 처리하고, 뉴스는 클릭 가능한 링크로 렌더링한다.
    """
    disc = [html.escape(d) for d in disclosures] if disclosures else ["- 당일 신규 공시 없음"]
    lines = [
        f"📊 {STOCK_NAME} 주가 {direction} ({change_rate:+.2f}%)",
        "⚠️ AI 분석 미수행(LLM API 키 미설정 또는 호출 실패) — 수집된 원자료만 전달합니다.",
        "",
        f"■ 현재가: {price:,}원 / 전일대비 {change_rate:+.2f}%",
        f"■ 코스피 등락률: {kospi_rate if kospi_rate is not None else '조회불가'}%",
        "",
        "■ 당일 공시:",
        *disc,
        "",
        "■ 당일 뉴스:",
        *_news_lines_html(news),
    ]
    return "\n".join(lines)


def _key_set(key):
    """환경변수 키가 실제 설정됐는지(빈 값/한글 플레이스홀더 아님) 판정."""
    return bool(key) and not key.startswith("여기에")


def _analysis_prompt(change_rate, price, kospi_rate, disclosures, news, direction):
    """바이오·제약 섹터 맥락의 원인 분석 프롬프트(제공처 공통)."""
    return f"""당신은 상장 바이오·제약 기업의 IR 애널리스트입니다.
아래 데이터를 근거로 {STOCK_NAME}({STOCK_CODE})의 주가 {direction} 원인을 분석하세요.

[주가 현황]
- 현재가: {price:,}원 / 전일대비 {change_rate:+.2f}%
- 코스피 지수 등락률: {kospi_rate if kospi_rate is not None else "조회불가"}%

[당일 공시]
{chr(10).join(disclosures) if disclosures else "- 당일 신규 공시 없음"}

[당일 뉴스 헤드라인]
{chr(10).join(_news_lines_plain(news))}

[분석 지침]
- 바이오·백신 섹터 특성(임상 결과, 품목허가, 기술수출/공급계약, 식약처·FDA, 모회사 SK케미칼 이슈 등)을 우선 고려.
- 코스피 지수가 같은 방향으로 크게 움직였다면 '개별 이슈'가 아닌 '시장 전반 영향' 가능성을 명시.
- 추정임을 분명히 하고, 근거가 약하면 신뢰도를 낮게 평가.

[출력 형식] — 텔레그램 보고용, 간결하게
📊 {STOCK_NAME} 주가 {direction} ({change_rate:+.2f}%)
■ 추정 원인:
■ 근거:
■ 신뢰도: (상/중/하 + 한 줄 사유)
"""


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


def analyze(change_rate, price, kospi_rate, disclosures, news):
    """원인 분석 보고서(텔레그램 HTML 형식 문자열) 반환.

    제공처 우선순위: Anthropic(Claude) → Google(Gemini) → 기본 보고서.
    어느 LLM도 키가 없거나 호출이 실패하면 _basic_report()로 안전하게 폴백한다.
    """
    direction = "상승" if change_rate > 0 else "하락"

    if _key_set(ANTHROPIC_KEY):
        name, call = "Claude", _call_claude
    elif _key_set(GEMINI_API_KEY):
        name, call = "Gemini", _call_gemini
    else:
        return _basic_report(change_rate, price, kospi_rate, disclosures, news, direction)

    prompt = _analysis_prompt(change_rate, price, kospi_rate, disclosures, news, direction)
    try:
        ai_text = call(prompt)
    except Exception:
        logging.warning("%s 분석 실패 — 기본 보고서로 폴백", name, exc_info=True)
        return _basic_report(change_rate, price, kospi_rate, disclosures, news, direction)
    # AI 본문은 html.escape로 안전화(HTML 파싱 깨짐 방지) 후, 뉴스 원문 링크를 하이퍼링크로 첨부.
    footer = "\n".join(["", "📰 관련 뉴스:", *_news_lines_html(news)]) if news else ""
    return html.escape(ai_text) + footer


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
        kospi = get_kospi(token)              # 실패해도 None 반환(보조)
        disclosures = get_disclosures()       # 실패해도 [] 반환(보조)
        news = get_news(STOCK_NAME)           # 실패해도 [] 반환(보조)
        report = analyze(p["change_rate"], p["price"], kospi, disclosures, news)
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
