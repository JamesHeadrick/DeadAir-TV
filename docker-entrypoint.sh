#!/bin/sh
# Starts as root only long enough to make /config and /data writable, then
# runs the app as an unprivileged user (PUID/PGID, default 1000:1000).
# This way a plain `docker compose up` works even when Docker creates the
# bind-mounted folders itself (owned by root).
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
    for dir in /config /data; do
        mkdir -p "$dir"
        if [ "$(stat -c %u "$dir")" != "$PUID" ]; then
            echo "deadair: giving $dir to $PUID:$PGID"
            chown -R "$PUID:$PGID" "$dir"
        fi
    done
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

# Already running as a non-root user (e.g. `user:` in compose): just start.
exec "$@"
