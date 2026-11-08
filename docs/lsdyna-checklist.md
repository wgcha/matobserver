# LS-DYNA 실행 환경 확인 목록 (G3 전)

개발 환경에는 LS-DYNA가 없어, 실제 실행이 필요한 항목은 `@pytest.mark.lsdyna`로 분리되어 기본 테스트에서 제외된다. LS-DYNA가 설치된 PC(로컬 최대 4코어)에서 아래 순서로 확인한다.

## 1. 설정

1. `config/matobserver.example.yaml`을 복사해 실행 설정 파일을 만든다(예: `matobserver.local.yaml`).
2. 사내 배치 스크립트를 지정한다.

```yaml
solver:
  lsdyna:
    script: "D:/tools/lsdyna/run_batch.bat"     # 사내 SMP/MPP 배치 스크립트(필수)
    batch_command: ['{script}', -i, '{main}', -n, '{ncpu}']   # 스크립트 인자 형식에 맞게 수정
    ncpu: 4
    precision: double                           # single | double
```

3. 환경 변수로 설정 파일을 알려 준다.

```
set MATOBSERVER_CONFIG=D:\matobserver\matobserver.local.yaml
```

## 2. 실행 테스트

```
pytest -m lsdyna -v
```

| 테스트 | 확인 내용 | 통과 기준 |
| --- | --- | --- |
| `test_s6_bending_sample_deck_implicit_matches_beam_theory` | 3점굽힘 샘플 덱 implicit 정상 종료, F-δ 추출 | 초기 기울기가 보 이론과 ≤5% |
| `test_s11_explicit_check_run_records_ke_ie` | explicit 재현 검사 덱(ELFORM 1, IHQ 6) 실행, KE/IE 기록 | 정상 종료, 에너지 기록 |
| `test_tet_specimen_runs_without_negative_volume[tet4]` | 사면체 시편 실행 | 음의 체적 없음 |
| `test_tet10_mesh_fixture_runs` | tet10 `*ELEMENT_SOLID` 2줄 형식·절점 순서(`tests/fixtures/mesh/specimen_tet10.k`) | 정상 읽기·종료 |
| `test_s9_single_element_real_run` | 1요소 단축 왕복(`single_element_uniaxial`) | 입력 테이블 재현 |

## 3. 추정값으로 둔 설정 (실제 출력과 대조 후 수정)

| 설정 키 | 현재 값 | 확인 방법 |
| --- | --- | --- |
| `solver.yaml` → `rcforc_side_codes` | `{slave: 0, master: 1}` | 샘플 실행의 binout rcforc metadata `side`와 압자 접촉 반력 부호 대조 |
| binout 변수명 | elout/solid `sig_xx`, nodout `x_displacement` | 실제 binout 키 목록 출력 |
| `solver.lsdyna.termination_patterns` | Normal/Error termination(공백 포함 2종) | d3hsp·messag 끝부분 |
| `solver.lsdyna.license_error_patterns` | license, lmgrd, FlexNet, Unable to checkout | 라이선스 없는 상태로 1회 실행한 로그 |
| `solver.lsdyna.version_pattern` | `R\d+…(SMP|MPP)` | d3hsp 머리말 |
| 초기 관통·수렴 실패 문구 | 정규식 기본값 | 의도적으로 관통·발산시킨 덱의 d3hsp |
| `solver.yaml` → `long_format` | `auto` | 고정밀 카드에서 ` +` long 형식 키워드 읽기 |
| `*PARAMETER` 자유 형식 `R name,value` | `%.10e` | 샘플 덱에서 값 반영 여부 |
| d3plot 유효소성변형률 읽기 | field_max | 유효 범위(εp_valid) 값과 비교 |

## 4. 결과 처리

- 실패 항목은 로그(d3hsp, messag)와 함께 기록하고, 설정만 고치면 되는지 코드 수정이 필요한지 판단한다.
- 설정 변경으로 해결되면 결정 기록(`docs/decisions.md`)에 1줄 남긴다. 계약 변경이 필요하면 MINOR 개정으로 처리한다.
