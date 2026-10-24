# matobserver

MatNexus용 재료 역공학(FEMU) 엔진. 시험 하중-변위 곡선에 LS-DYNA 해석을 맞춰 탄성계수와 진응력–진소성변형률 테이블을 식별하고, 솔버 중립 결과로 MatNexus에 제공한다. 프런트 없이 REST API로만 동작한다.

- 계획서: [docs/plan.md](docs/plan.md)
- 결정 기록: [docs/decisions.md](docs/decisions.md)
- 계약(작업 1에서 작성): `docs/contracts/`

현재 상태: 계획 단계. 1차 범위는 3점굽힘(육면체, STP).
