import os
import sys
import tempfile

# 必须在导入 app 之前指定独立数据库,所有测试模块共享
os.environ.setdefault(
    "DATABASE_PATH", os.path.join(tempfile.mkdtemp(prefix="trajtest_"), "test.db")
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
