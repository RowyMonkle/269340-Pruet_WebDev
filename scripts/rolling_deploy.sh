#!/usr/bin/env sh
# Rebuild the API image and restart the replicas one at a time, so nginx
# always has one healthy replica to send traffic to.
#
# Usage: sh scripts/rolling_deploy.sh
#        COMPOSE="docker compose -p other" sh scripts/rolling_deploy.sh
set -eu

COMPOSE=${COMPOSE:-docker compose}
# nginx re-resolves replica IPs every 5s (resolver valid=5s in nginx.conf)
DNS_SETTLE=${DNS_SETTLE:-6}

# Only the "migrate" service has a build section; api and api_2 reuse the same image
# (ticweb_api:local), so building "api" would say "No services to build" and ship nothing.
echo "[deploy] building new api image"
$COMPOSE build migrate

for svc in api api_2; do
    echo "[deploy] recreating $svc"
    $COMPOSE up -d --no-deps --force-recreate "$svc"
    cid=$($COMPOSE ps -q "$svc")

    status=starting
    for _ in $(seq 1 60); do
        status=$(docker inspect -f '{{.State.Health.Status}}' "$cid")
        [ "$status" = healthy ] && break
        sleep 1
    done
    if [ "$status" != healthy ]; then
        echo "[deploy] $svc did not become healthy, stopping rollout"
        exit 1
    fi

    echo "[deploy] $svc healthy, waiting ${DNS_SETTLE}s for nginx to pick it up"
    sleep "$DNS_SETTLE"
done

echo "[deploy] done"
