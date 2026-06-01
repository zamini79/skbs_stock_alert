# SK바이오사이언스(302440) 주가 변동 자동 보고

주가가 전일 대비 ±5% 이상 움직이면 원인을 자동 분석해 텔레그램으로 보고하는 IR 자동화 도구.

## 파일 구성
- `stock_alert_302440.py` — 메인 스크립트 (감지 → 원인분석 → 텔레그램)
- `CLAUDE.md` — Claude Code가 자동으로 읽는 프로젝트 컨텍스트/지침
- `.env.example` — 필요한 API 키 목록 (모두 무료 발급)
- `requirements.txt` — 의존성 (`requests`)

## 1. Claude Code 설치 (macOS)
네이티브 설치 방식이 가장 간단하다(Node.js 불필요):
```zsh
curl -fsSL https://claude.ai/install.sh | bash
```
또는 Homebrew: `brew install --cask claude-code`
설치 후 확인: `claude --version`

> 유료 Anthropic 계정(Pro/Max/Team/Enterprise) 또는 API 크레딧이 있는 Console 계정이 필요하다.
> 최신 설치 안내: https://docs.claude.com/en/docs/claude-code/overview

## 2. 이 프로젝트에서 작업 이어가기
```zsh
cd skbs_stock_alert
claude
```
폴더에 들어가 `claude`를 실행하면, Claude Code가 같은 폴더의 `CLAUDE.md`를 자동으로 읽어
프로젝트 목적·구조·컨벤션·남은 작업을 파악한 상태로 시작한다.

바로 시켜볼 만한 작업 예시:
- `CLAUDE.md의 TODO 1번대로 DART 고유번호 조회 헬퍼를 만들어줘`
- `보고 전에 IR 담당자 검수 단계를 넣는 옵션을 추가해줘`
- `단계별 로깅과 에러 처리를 강화해줘`
- `이 흐름을 n8n 워크플로로 옮기는 설계를 정리해줘`

## 3. 키 설정
`.env.example`을 참고해 각 키를 발급받아 `~/.zshrc`에 추가:
```zsh
export KIS_APP_KEY="..."
export KIS_APP_SECRET="..."
# ... 나머지 키들
```
적용: `source ~/.zshrc`

## 4. 실행 / 테스트
```zsh
pip install -r requirements.txt
python3 stock_alert_302440.py
```

## 5. 장중 자동 실행 (선택)
평일 09~15시 5분 간격, crontab(`crontab -e`):
```
*/5 9-15 * * 1-5 /usr/bin/python3 /절대경로/stock_alert_302440.py >> ~/stock_alert.log 2>&1
```
맥은 절전 시 cron이 멈추므로, 24시간 운영이 필요하면 클라우드로 옮기는 것을 권장.

## 주의
상장사 IR 자료이며 AI 원인 분석은 **추정**이다. 사장님 방으로 직행시키기 전,
`TELEGRAM_CHAT_ID`를 IR 담당자 검수용 방으로 두고 확인 후 전달하는 운영을 권한다.
