#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""강제 발송 도구 — 임계치(THRESHOLD)와 무관하게 '현재 시점' 보고서를 즉시 텔레그램으로 전송.

용도: 수동 점검/시연 등으로 등락폭이 ±기준 미만이어도 보고서를 한 번 보내고 싶을 때.
실제 운영 자동 발송은 stock_alert_302440.py(main)가 담당하며 이 파일은 건드리지 않는다.

[실행] (프로젝트 루트에서)
  python3 tools/send_now.py
"""

import os
import sys

# 프로젝트 루트를 import 경로에 추가 (tools/ 하위에서 실행해도 동작)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stock_alert_302440 as sa


def main():
    sa.setup_logging()
    token = sa.kis_token()
    p = sa.get_price(token, sa.STOCK_CODE)

    market = {
        "kospi":  sa.get_index(token, sa.KOSPI_CODE),
        "kosdaq": sa.get_index(token, sa.KOSDAQ_CODE),
        "pharma": sa.get_index(token, sa.PHARMA_SECTOR_CODE),
    }
    peers = sa.get_peers(token)
    disclosures = sa.get_disclosures()
    news = sa.get_news()
    news_ctx = {
        "company": sa.get_news(sa.COMPANY_QUERIES, sa.ANALYSIS_NEWS_AGE_HOURS, sa.ANALYSIS_NEWS_LIMIT, sa.COMPANY_TOKENS),
        "macro":   sa.get_news(sa.MACRO_QUERIES,   sa.ANALYSIS_NEWS_AGE_HOURS, sa.ANALYSIS_NEWS_LIMIT, sa.MACRO_TOKENS),
        "market":  sa.get_news(sa.MARKET_QUERIES,  sa.ANALYSIS_NEWS_AGE_HOURS, sa.ANALYSIS_NEWS_LIMIT, sa.MARKET_TOKENS),
        "sector":  sa.get_news(sa.SECTOR_QUERIES,  sa.ANALYSIS_NEWS_AGE_HOURS, sa.ANALYSIS_NEWS_LIMIT, sa.SECTOR_TOKENS),
    }

    report = sa.analyze(p, market, peers, disclosures, news, news_ctx)
    sa.send_telegram(report, parse_mode="HTML")
    print(f"✅ 강제 발송 완료 → {sa.TELEGRAM_CHAT} "
          f"(장중 최대 {p['peak_rate']:+.2f}%, 현재 {p['change_rate']:+.2f}%)")


if __name__ == "__main__":
    main()
