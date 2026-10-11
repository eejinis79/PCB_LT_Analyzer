"""volume_spike_screener.py(단일 파일 배포본)가 패키지와 동기화되어 있는지 확인."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _builder():
    spec = importlib.util.spec_from_file_location("build_single_py", ROOT / "tools" / "build_single_py.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_single_file_is_up_to_date():
    expected = _builder().build()
    actual = (ROOT / "volume_spike_screener.py").read_text(encoding="utf-8")
    assert actual == expected, "패키지를 수정했다면 `python tools/build_single_py.py` 로 다시 생성하세요."
