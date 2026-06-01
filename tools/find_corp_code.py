#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenDART 고유번호(corp_code) 조회 헬퍼 — 일회성 스크립트

OpenDART의 corpCode.xml 다운로드 API는 전체 상장·비상장사의 고유번호 목록을
ZIP(내부에 CORPCODE.xml)으로 내려준다. 이 스크립트는 그 ZIP을 받아 압축을 풀고,
종목코드 302440(SK바이오사이언스)에 해당하는 8자리 고유번호(corp_code)를 찾아 출력한다.

찾은 값을 메인 스크립트(stock_alert_302440.py)의 DART_CORP_CODE 환경변수에 넣어두면
공시 조회 단계(get_disclosures)가 동작한다.

[실행]
  export DART_API_KEY="실제_OpenDART_인증키"   # ~/.zshrc 에 두는 걸 권장
  python3 tools/find_corp_code.py

  # 바로 ~/.zshrc 에 추가할 export 줄까지 출력됨. 복사해 넣고 source ~/.zshrc.
"""

import io
import os
import sys
import zipfile
import xml.etree.ElementTree as ET

import requests

# 메인 스크립트와 동일한 대상 종목
STOCK_CODE = "302440"          # SK바이오사이언스
STOCK_NAME = "SK바이오사이언스"

DART_API_KEY = os.environ.get("DART_API_KEY", "")
CORPCODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"


def download_corpcode_xml(api_key):
    """corpCode.xml(ZIP) 다운로드 → 내부 XML 바이트 반환"""
    r = requests.get(CORPCODE_URL, params={"crtfc_key": api_key}, timeout=30)
    r.raise_for_status()

    # 인증 실패 등은 ZIP이 아니라 JSON/XML 에러 메시지로 내려온다.
    # ZIP 매직넘버(PK\x03\x04)가 아니면 본문을 그대로 보여주고 종료.
    if not r.content.startswith(b"PK"):
        raise RuntimeError(
            "ZIP이 아닌 응답을 받았습니다(인증키 오류 가능). "
            f"응답 일부: {r.content[:300].decode('utf-8', 'replace')}"
        )

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        # 내부 파일명은 보통 CORPCODE.xml — 첫 .xml 파일을 사용
        xml_name = next((n for n in zf.namelist() if n.lower().endswith(".xml")), None)
        if xml_name is None:
            raise RuntimeError(f"ZIP 안에 XML이 없습니다: {zf.namelist()}")
        return zf.read(xml_name)


def find_corp_code(xml_bytes, stock_code):
    """종목코드로 corp_code 매칭. (corp_code, corp_name) 반환, 없으면 None"""
    root = ET.fromstring(xml_bytes)
    for item in root.iter("list"):
        # stock_code는 공백 패딩이 있을 수 있어 strip 후 비교
        code = (item.findtext("stock_code") or "").strip()
        if code == stock_code:
            corp_code = (item.findtext("corp_code") or "").strip()
            corp_name = (item.findtext("corp_name") or "").strip()
            return corp_code, corp_name
    return None


def main():
    if not DART_API_KEY:
        print("❌ 환경변수 DART_API_KEY 가 비어 있습니다.", file=sys.stderr)
        print('   export DART_API_KEY="발급받은_인증키"  후 다시 실행하세요.', file=sys.stderr)
        sys.exit(1)

    print(f"⏳ corpCode.xml 다운로드 중… (대상: {STOCK_NAME} / 종목코드 {STOCK_CODE})")
    try:
        xml_bytes = download_corpcode_xml(DART_API_KEY)
    except Exception as e:
        print(f"❌ 다운로드/압축해제 실패: {e}", file=sys.stderr)
        sys.exit(1)

    result = find_corp_code(xml_bytes, STOCK_CODE)
    if result is None:
        print(f"❌ 종목코드 {STOCK_CODE} 에 해당하는 고유번호를 찾지 못했습니다.", file=sys.stderr)
        print("   상장 폐지/종목코드 변경 여부를 확인하세요.", file=sys.stderr)
        sys.exit(1)

    corp_code, corp_name = result
    print(f"\n✅ 찾았습니다: {corp_name} ({STOCK_CODE})")
    print(f"   DART 고유번호(corp_code): {corp_code}")
    print("\n아래 줄을 ~/.zshrc 에 추가하고 'source ~/.zshrc' 하세요:")
    print(f'   export DART_CORP_CODE="{corp_code}"')


if __name__ == "__main__":
    main()
