"""Hold the exclusive database lock throughout verification and replacement."""

import os
import subprocess
import sys

from zhiheng.db.maintenance import acquire_database_lock


def main() -> int:
    database = os.environ["ZHIHENG_DATABASE_PATH"]
    try:
        descriptor = acquire_database_lock(database, exclusive=True)
    except BlockingIOError:
        print("restore requires stopped database clients; serving lock is held", file=sys.stderr)
        return 1
    try:
        return subprocess.run(
            ["sh", "scripts/restore_database.sh"],
            check=False,
            # Keep exclusion even if the supervising Python process exits first.
            pass_fds=(descriptor,),
        ).returncode
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
