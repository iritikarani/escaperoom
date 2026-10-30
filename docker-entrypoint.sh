#!/bin/sh
# Make the database folder writable, then run the bot as the unprivileged
# "bot" user. Needed on hosts such as Fly.io, where a mounted volume is owned
# by root (Docker's named volumes copy the image's ownership; Fly's do not).
set -e

DATA_DIR="$(dirname "${DATABASE_PATH:-/data/escape_room.db}")"
mkdir -p "$DATA_DIR"

if [ "$(id -u)" = "0" ]; then
    chown -R bot:bot "$DATA_DIR"
    exec setpriv --reuid=bot --regid=bot --init-groups "$@"
fi
exec "$@"
