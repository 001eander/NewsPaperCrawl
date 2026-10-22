"""
单实例锁测试
============
验证锁被持有时另一个进程无法获取,释放后可再次获取。
用子进程模拟第二个爬虫进程(文件锁的互斥语义是跨进程的)。
"""

import subprocess
import sys
from pathlib import Path

from crawler.adapters.instance_lock import InstanceLock

ROOT = Path(__file__).resolve().parents[1]

_CHILD = """
import sys
from pathlib import Path

from crawler.adapters.instance_lock import AlreadyRunningError, InstanceLock

lock = InstanceLock(Path(sys.argv[1]))
try:
    lock.acquire()
except AlreadyRunningError:
    print("blocked")
else:
    print("acquired")
    lock.release()
"""


def _child(lock_path: Path) -> str:
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(lock_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_second_process_is_blocked_then_released(tmp_path):
    path = tmp_path / "articles.lock"
    lock = InstanceLock(path)
    lock.acquire()
    try:
        assert _child(path) == "blocked"
    finally:
        lock.release()

    assert _child(path) == "acquired"
