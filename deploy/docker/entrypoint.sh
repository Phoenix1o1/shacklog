#!/bin/sh
# Start ShackLog in the container as its own unprivileged user.
#
# Docker creates a missing bind-mount folder (deploy/shacklog-data) owned by root, so the
# program couldn't create its database there on the first start. This runs as root just
# long enough to hand /data to the shacklog user, then drops privileges for good.
set -eu
if [ "$(id -u)" = 0 ]; then
  if [ "$(stat -c %u /data)" != "$(id -u shacklog)" ]; then
    chown -R shacklog:shacklog /data
  fi
  exec setpriv --reuid=shacklog --regid=shacklog --init-groups "$@"
fi
exec "$@"
