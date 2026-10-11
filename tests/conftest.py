import os
import tempfile

# 테스트가 사용자 폴더(계정 금고·캐시)를 건드리지 않도록 임시 폴더 사용
os.environ.setdefault("VSS_HOME", tempfile.mkdtemp(prefix="vss_test_"))
