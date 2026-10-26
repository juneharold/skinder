"""One local server owns recovery and matching for each user database."""

import fcntl
from pathlib import Path
from typing import BinaryIO


class DatabaseOwnership:
    def __init__(self, database: Path) -> None:
        # Symlink aliases of one database must contend for the same ownership lock.
        database = database.resolve()
        self.path = database.with_name(database.name + ".lock")
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            stream.close()
            raise RuntimeError("Another Skinder server is already using this user database.") from exc
        except BaseException:
            stream.close()
            raise
        self._file = stream

    def release(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        # Never unlink: a waiting owner may already hold this same inode open.
