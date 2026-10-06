"""Create an encrypted, manifest-bound full backup in a configured restic repo.

Repository initialization is a separate operator action. No implicit creation,
retention pruning, credential generation, or erase-journal rollback occurs here.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from zhiheng.backup import stage_backup, verify_backup_bundle


def main() -> None:
    database = Path(os.environ["ZHIHENG_DATABASE_PATH"])
    object_root = Path(os.environ["ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH"])
    if not os.environ.get("RESTIC_REPOSITORY"):
        raise SystemExit("RESTIC_REPOSITORY must be explicitly configured")
    if not (os.environ.get("RESTIC_PASSWORD") or os.environ.get("RESTIC_PASSWORD_FILE")):
        raise SystemExit("configure RESTIC_PASSWORD or RESTIC_PASSWORD_FILE")
    binary = os.environ.get("ZHIHENG_RESTIC_BINARY", "restic")
    with tempfile.TemporaryDirectory(prefix="zhiheng-backup-") as temporary:
        parent = Path(temporary)
        stage_backup(database, object_root, parent / "bundle")
        verify_backup_bundle(parent / "bundle")
        result = subprocess.run(
            [binary, "backup", "--json", "--tag", "zhiheng-bundle-v1", "bundle"],
            cwd=parent,
            capture_output=True,
            text=True,
            check=True,
        )
        summaries = [
            item for line in result.stdout.splitlines()
            if (item := json.loads(line)).get("message_type") == "summary"
        ]
        if len(summaries) != 1 or not summaries[0].get("snapshot_id"):
            raise RuntimeError("restic did not return one completed snapshot")
        manifest = json.loads((parent / "bundle" / "manifest.json").read_text(encoding="utf-8"))
        print(
            json.dumps(
                {
                    "snapshot_id": summaries[0]["snapshot_id"],
                    "format_version": manifest["format_version"],
                    "provider_secret_recovery": manifest.get("provider_secret_recovery"),
                }
            )
        )


if __name__ == "__main__":
    main()
