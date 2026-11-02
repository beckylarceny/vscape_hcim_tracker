#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
    mkdir -p /app/reports
    chown -R 1000:1000 /app/reports
    groups="$(id -G tracker | tr ' ' ',')"
    if [ -S /var/run/docker.sock ]; then
        socket_gid="$(stat -c '%g' /var/run/docker.sock)"
        case ",$groups," in
            *",$socket_gid,"*) ;;
            *) groups="$groups,$socket_gid" ;;
        esac
    fi
    exec setpriv --reuid=1000 --regid=1000 --groups "$groups" "$@"
fi

exec "$@"
