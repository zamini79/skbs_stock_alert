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
import datetime
import requests

# ─────────────────────────────────────────────────────────────
# 설정 — 환경변수로 두는 걸 권장 (키를 코드에 직접 박지 마세요)
# zsh 예: export KIS_APP_KEY="..."  를 ~/.zshrc 에 추가
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

ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "여기에_앤트로픽키")
CLAUDE_MODEL   = "claude-sonnet-4-6"  # 최신 모델명은 docs.claude.com 에서 확인


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
        out = r.json()["output"]
        return float(out["bstp_nmix_prdy_ctrt"])  # 코스피 등락률(%)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# 2) OpenDART — 당일 공시
# ─────────────────────────────────────────────────────────────
def get_disclosures():
    today = datetime.date.today().strftime("%Y%m%d")
    r = requests.get(
        "https://opendart.fss.or.kr/api/list.json",
        params={"crtfc_key": DART_API_KEY, "corp_code": DART_CORP_CODE,
                "bgn_de": today, "end_de": today, "page_count": 20},
        timeout=10,
    )
    items = r.json().get("list", [])
    return [f"- {i['report_nm']} ({i['flr_nm']}, {i['rcept_dt']})" for i in items]


# ─────────────────────────────────────────────────────────────
# 3) 네이버 뉴스 — 당일 헤드라인
# ─────────────────────────────────────────────────────────────
def get_news(query, count=8):
    r = requests.get(
        "https://openapi.naver.com/v1/search/news.json",
        headers={"X-Naver-Client-Id": NAVER_ID, "X-Naver-Client-Secret": NAVER_SECRET},
        params={"query": query, "display": count, "sort": "date"},
        timeout=10,
    )
    items = r.json().get("items", [])
    out = []
    for i in items:
        title = i["title"].replace("<b>", "").replace("</b>", "").replace("&quot;", '"')
        out.append(f"- {title}")
    return out


# ─────────────────────────────────────────────────────────────
# 4) Claude — 원인 분석 보고서 생성
# ─────────────────────────────────────────────────────────────
def analyze(change_rate, price, kospi_rate, disclosures, news):
    direction = "상승" if change_rate > 0 else "하락"
    prompt = f"""당신은 상장 바이오·제약 기업의 IR 애널리스트입니다.
아래 데이터를 근거로 {STOCK_NAME}({STOCK_CODE})의 주가 {direction} 원인을 분석하세요.

[주가 현황]
- 현재가: {price:,}원 / 전일대비 {change_rate:+.2f}%
- 코스피 지수 등락률: {kospi_rate if kospi_rate is not None else "조회불가"}%

[당일 공시]
{chr(10).join(disclosures) if disclosures else "- 당일 신규 공시 없음"}

[당일 뉴스 헤드라인]
{chr(10).join(news) if news else "- 수집된 기사 없음"}

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


# ─────────────────────────────────────────────────────────────
# 5) 텔레그램 전송
# ─────────────────────────────────────────────────────────────
def send_telegram(text):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT, "text": text},
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
    token = kis_token()
    p = get_price(token, STOCK_CODE)
    print(f"[{datetime.datetime.now():%H:%M}] {STOCK_NAME} {p['price']:,}원 ({p['change_rate']:+.2f}%)")

    if abs(p["change_rate"]) < THRESHOLD:
        return
    if already_alerted_today():
        print("오늘 이미 보고함 — 스킵")
        return

    kospi = get_kospi(token)
    disclosures = get_disclosures()
    news = get_news(STOCK_NAME)
    report = analyze(p["change_rate"], p["price"], kospi, disclosures, news)

    send_telegram(report)
    mark_alerted()
    print("✅ 텔레그램 보고 완료")


if __name__ == "__main__":
    main()
