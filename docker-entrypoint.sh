#!/bin/sh
# Start as root only long enough to give the data disk to the "app" user (a host's fresh disk
# belongs to root), then run the program as "app": a hole in the program cannot touch the
# system, only the notebook folder.
set -e
DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"
if [ "$(id -u)" = "0" ]; then
  chown -R app:app "$DATA_DIR"
  exec setpriv --reuid=app --regid=app --init-groups "$@"
fi
exec "$@"
