"""
单实例锁
========
同一阶段的爬取进程用文件锁互斥:后启动的进程直接失败退出,不会与前者
重复消费同一批 pending。

锁由操作系统在进程退出(含崩溃)时自动释放,不会留下永久锁;锁文件本身
可以保留,里面写持有者 pid 便于排查。锁文件放在数据库旁边,共享同一
DATA_DIR 的容器/进程看到的是同一把锁。
"""

import os
from pathlib import Path
from typing import Self

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class AlreadyRunningError(RuntimeError):
    """同阶段的另一个实例已持有锁"""


class InstanceLock:
    """基于文件建议锁的跨平台互斥(non-blocking)"""

    def __init__(self, path: Path):
        self._path = path
        self._fh = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = self._path.open("a+b")
        try:
            if os.name == "nt":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise AlreadyRunningError(
                f"同一阶段已有实例在运行(锁文件 {self._path})"
            ) from None

        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n".encode())
            fh.flush()
        except OSError:
            pass  # 锁已拿到,写 pid 失败不影响互斥
        self._fh = fh

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *args) -> None:
        self.release()
