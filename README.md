# matobserver

MatNexus용 재료 역공학(FEMU) 엔진. 시험 하중-변위 곡선에 LS-DYNA 해석을 맞춰 탄성계수와 진응력–진소성변형률 테이블을 식별하고, 솔버 중립 결과로 MatNexus에 제공한다. 프런트 없이 REST API로만 동작한다.

- 계획서: [docs/plan.md](docs/plan.md)
- 결정 기록: [docs/decisions.md](docs/decisions.md)
- 계약(schema 1.1, 승인): [docs/contracts/](docs/contracts/)
- LS-DYNA 실행 환경 확인 목록: [docs/lsdyna-checklist.md](docs/lsdyna-checklist.md)

현재 상태: 1차(3점굽힘) 구현, G2(대리모델 가상 역식별) 검증 단계. LS-DYNA 실제 실행 확인(G3 전)은 남아 있음.

## 실행

```
pip install -e .[dev]                 # gmsh는 시스템 라이브러리 libGLU 필요
pytest -m "not lsdyna"                # LS-DYNA 없이 실행 가능한 전체 테스트
matobserver serve                     # REST API
matobserver worker                    # 잡 실행 워커(1개)
matobserver recipe add <레시피 폴더>   # 관리자: 지그 레시피 등록
```

설정은 `config/matobserver.example.yaml`을 복사해 쓰고, `MATOBSERVER_CONFIG` 환경 변수로 경로를 지정한다.
