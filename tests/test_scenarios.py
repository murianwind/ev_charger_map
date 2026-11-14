"""tests/features/*.feature의 모든 시나리오를 pytest 테스트로 등록한다.
스텝 정의는 conftest.py에 있다."""
from pytest_bdd import scenarios

scenarios("features")
