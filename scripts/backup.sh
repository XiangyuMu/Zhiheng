#!/usr/bin/env sh
set -eu
umask 077

: "${ZHIHENG_DATABASE_PATH:?set ZHIHENG_DATABASE_PATH}"
: "${ZHIHENG_BACKUP_PATH:?set ZHIHENG_BACKUP_PATH}"
: "${ZHIHENG_BACKUP_PASSWORD:?set ZHIHENG_BACKUP_PASSWORD}"

tmp=$(mktemp "${ZHIHENG_BACKUP_PATH}.sqlite.XXXXXX")
trap 'rm -f "$tmp"' EXIT
sqlite3 "$ZHIHENG_DATABASE_PATH" ".backup '$tmp'"
openssl enc -aes-256-cbc -pbkdf2 -salt -in "$tmp" \
  -out "$ZHIHENG_BACKUP_PATH" -pass env:ZHIHENG_BACKUP_PASSWORD
python -c 'import hashlib,hmac,os,sys; data=open(sys.argv[1],"rb").read(); sys.stdout.buffer.write(hmac.new(os.environ["ZHIHENG_BACKUP_PASSWORD"].encode(),data,hashlib.sha256).digest())' \
  "$ZHIHENG_BACKUP_PATH" > "${ZHIHENG_BACKUP_PATH}.hmac"
chmod 600 "$ZHIHENG_BACKUP_PATH"
chmod 600 "${ZHIHENG_BACKUP_PATH}.hmac"
