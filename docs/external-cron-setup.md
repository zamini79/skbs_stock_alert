# 외부 cron(cron-job.org)으로 GitHub 워크플로 안정 트리거

GitHub Actions 무료 예약(cron)은 부하 시 **지연·누락**된다(실측: 5분 주기가 수 시간 안 돎 → ±5% 급락을 놓침).
외부 무료 스케줄러(cron-job.org)가 5분마다 GitHub API의 `workflow_dispatch`를 호출해 **확실히** 실행시킨다.
서버는 따로 관리하지 않으며, 장시간 외 호출은 스크립트의 `within_market_hours()` 가드가 알아서 스킵한다.

## 1. GitHub PAT 발급 (fine-grained, 이 레포만)
1. GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate new token
2. Resource owner: `zamini79` / Repository access: **Only select repositories → `skbs_stock_alert`**
3. Repository permissions → **Actions: Read and write** (Metadata: Read는 자동 포함)
4. Expiration 설정 후 생성 → 토큰 문자열 복사(`github_pat_...`). **만료 시 갱신 필요.**
   - ⚠️ 토큰은 절대 코드/커밋에 넣지 말 것. cron-job.org에만 저장.

## 2. cron-job.org 작업 생성 (무료 가입)
- **URL**: `https://api.github.com/repos/zamini79/skbs_stock_alert/actions/workflows/stock-alert.yml/dispatches`
- **Method**: `POST`
- **Headers**:
  - `Accept: application/vnd.github+json`
  - `Authorization: Bearer <발급한_PAT>`
  - `X-GitHub-Api-Version: 2022-11-28`
  - `Content-Type: application/json`
- **Body**: `{"ref":"main"}`
- **Schedule**: 타임존 `Asia/Seoul`, 요일 월–금, 시 9–15, 분 `*/5` (매 5분)
  - 15:30 컷오프는 스크립트 가드가 처리하므로 시 범위는 9–15로 넉넉히 둬도 됨.
- 성공 응답은 **HTTP 204**(No Content) — cron-job.org에서 정상으로 처리됨.

## 3. 검증
터미널에서 PAT로 한 번 수동 호출(실행 1회 트리거됨):
```zsh
curl -i -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer <PAT>" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/zamini79/skbs_stock_alert/actions/workflows/stock-alert.yml/dispatches \
  -d '{"ref":"main"}'
# → HTTP/2 204 면 성공. GitHub Actions 탭에 workflow_dispatch 실행이 떠야 함.
```

## 참고
- 기존 워크플로의 `schedule` cron은 **백업으로 유지**(concurrency 그룹이 중복 실행 방지).
- 발송 쿨다운(하루 2회·단계별 1회)은 그대로 동작 → 외부 cron이 5분마다 쏴도 중복 안 됨.
- cron-job.org 외 대안: GitHub Actions의 안정성이 필요하면 EventBridge/Cloud Scheduler 등 유료 스케줄러도 동일 방식(API 호출)로 가능.
