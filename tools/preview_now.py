#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""보고서 미리보기 — send_now.py와 동일 파이프라인을 돌리되, 텔레그램 발송 대신
보고서 본문을 콘솔로 출력한다(네트워크 차단 환경에서 LLM 분석 품질만 점검용)."""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stock_alert_302440 as sa


def strip_html(s: str) -> str:
    s = re.sub(r"<a [^>]*href=\"([^\"]*)\"[^>]*>(.*?)</a>", r"\2 (\1)", s, flags=re.S)
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    import html
    return html.unescape(s)


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
    news = sa.get_related_news()
    news_ctx = {
        "company": sa.get_news(sa.COMPANY_QUERIES, sa.ANALYSIS_NEWS_AGE_HOURS, sa.ANALYSIS_NEWS_LIMIT, sa.COMPANY_TOKENS),
        "macro":   sa.get_news(sa.MACRO_QUERIES,   sa.ANALYSIS_NEWS_AGE_HOURS, sa.ANALYSIS_NEWS_LIMIT, sa.MACRO_TOKENS),
        "market":  sa.get_news(sa.MARKET_QUERIES,  sa.ANALYSIS_NEWS_AGE_HOURS, sa.ANALYSIS_NEWS_LIMIT, sa.MARKET_TOKENS),
        "sector":  sa.get_news(sa.SECTOR_QUERIES,  sa.ANALYSIS_NEWS_AGE_HOURS, sa.ANALYSIS_NEWS_LIMIT, sa.SECTOR_TOKENS),
    }
    report = sa.analyze(p, market, peers, disclosures, news, news_ctx)
    print("\n" + "=" * 70)
    print(strip_html(report))
    print("=" * 70)
    news_msg = sa.build_news_message(news)
    if news_msg:
        print("[별도 메시지] 관련 뉴스")
        print(strip_html(news_msg))
        print("=" * 70)
    print(f"[미발송] 장중 최대 {p['peak_rate']:+.2f}%, 현재 {p['change_rate']:+.2f}%")


if __name__ == "__main__":
    main()
