"""실시간 이슈 브리핑 (news_top) 수집 모듈.

P0-1: 키 없는 안전 골격.
- 네이버 검색>뉴스 API 호출부는 env(NAVER_CLIENT_ID/SECRET) 없으면 skip + WARNING.
- 실제 DB write는 이 모듈에서 하지 않는다(dry-run은 stdout 출력만).
- 운영 진입(main.py run())과 분리된 별도 dry-run 진입(dryrun.py)으로만 실행.
"""
