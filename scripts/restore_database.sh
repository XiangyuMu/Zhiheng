#!/usr/bin/env sh
set -eu
umask 077

: "${ZHIHENG_BACKUP_PATH:?set ZHIHENG_BACKUP_PATH}"
: "${ZHIHENG_DATABASE_PATH:?set ZHIHENG_DATABASE_PATH}"
: "${ZHIHENG_BACKUP_PASSWORD:?set ZHIHENG_BACKUP_PASSWORD}"
: "${ZHIHENG_ERASE_JOURNAL_PATH:?set ZHIHENG_ERASE_JOURNAL_PATH}"
: "${ZHIHENG_SECRET_KEY:?set ZHIHENG_SECRET_KEY}"

for sidecar in "$ZHIHENG_DATABASE_PATH-wal" "$ZHIHENG_DATABASE_PATH-shm" "$ZHIHENG_DATABASE_PATH-journal"; do
  if test -e "$sidecar"; then
    printf '%s\n' 'restore requires stopped database clients and checkpointed SQLite sidecars' >&2
    exit 1
  fi
done

test -r "${ZHIHENG_BACKUP_PATH}.hmac"
expected=$(mktemp "${ZHIHENG_BACKUP_PATH}.hmac.XXXXXX")
trap 'rm -f "$expected"' EXIT
python -c 'import hashlib,hmac,os,sys; data=open(sys.argv[1],"rb").read(); sys.stdout.buffer.write(hmac.new(os.environ["ZHIHENG_BACKUP_PASSWORD"].encode(),data,hashlib.sha256).digest())' \
  "$ZHIHENG_BACKUP_PATH" > "$expected"
cmp -s "$expected" "${ZHIHENG_BACKUP_PATH}.hmac"

tmp=$(mktemp "${ZHIHENG_DATABASE_PATH}.restore.XXXXXX")
trap 'rm -f "$tmp" "$tmp-wal" "$tmp-shm" "$tmp-journal" "$expected"' EXIT
openssl enc -d -aes-256-cbc -pbkdf2 -in "$ZHIHENG_BACKUP_PATH" \
  -out "$tmp" -pass env:ZHIHENG_BACKUP_PASSWORD
sqlite3 "$tmp" "PRAGMA integrity_check;" | grep -qx ok
export ZHIHENG_DATABASE_URL="sqlite:///$tmp"
python scripts/upgrade_database.py "$tmp"
export ZHIHENG_REPLAY_EXTERNAL_ERASE_JOURNAL=1
python scripts/replay_erase_ledger.py
sqlite3 "$tmp" "PRAGMA wal_checkpoint(TRUNCATE); PRAGMA journal_mode=DELETE; VACUUM;" > /dev/null
sqlite3 "$tmp" "PRAGMA integrity_check;" | grep -qx ok
for sidecar in "$ZHIHENG_DATABASE_PATH-wal" "$ZHIHENG_DATABASE_PATH-shm" "$ZHIHENG_DATABASE_PATH-journal"; do
  if test -e "$sidecar"; then
    printf '%s\n' 'database became active during restore; replacement refused' >&2
    exit 1
  fi
done
mv "$tmp" "$ZHIHENG_DATABASE_PATH"
