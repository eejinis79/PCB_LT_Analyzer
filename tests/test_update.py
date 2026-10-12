import datetime as dt
import importlib.util
import io
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("updater", ROOT / "update.py")
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


def make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("PCB_LT_Analyzer-branch/", "")
        for name, data in files.items():
            z.writestr(f"PCB_LT_Analyzer-branch/{name}", data)
    return buf.getvalue()


def test_apply_zip_updates_backs_up_and_keeps_db(tmp_path):
    (tmp_path / "screener").mkdir()
    (tmp_path / "screener" / "core.py").write_text("old")      # 바뀔 파일 (사용자가 고쳤을 수 있음)
    (tmp_path / "run_screener.py").write_text("same")          # 그대로
    (tmp_path / "stock_data.db").write_bytes(b"MYDATA")         # 절대 건드리면 안 됨
    (tmp_path / "my_notes.txt").write_text("keep")              # 저장소에 없는 파일은 유지
    z = make_zip({"screener/core.py": "new", "run_screener.py": "same", "update.py": "u",
                  "stock_data.db": "REMOTE", ".venv/x": "no"})
    res = updater.apply_zip(z, tmp_path, now=dt.datetime(2026, 10, 12, 9, 0, 0))
    assert res["updated"] == [str(Path("screener/core.py"))]
    assert res["added"] == ["update.py"]
    assert (tmp_path / "screener" / "core.py").read_text() == "new"
    assert (tmp_path / "_backup" / "20261012_090000" / "screener" / "core.py").read_text() == "old"
    assert (tmp_path / "stock_data.db").read_bytes() == b"MYDATA"
    assert (tmp_path / "my_notes.txt").read_text() == "keep"
    assert not (tmp_path / ".venv").exists()
    # 두 번째: 변화 없음
    assert updater.apply_zip(z, tmp_path) == {"added": [], "updated": [], "backup": None}
