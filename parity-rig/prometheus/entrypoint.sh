#!/bin/sh
set -eu

# Materialize the templated prometheus.yml at container start. We can't use
# env-var substitution in Prometheus config directly (the supported subset
# is limited and varies by version), so we sed the two placeholders ourselves.
TEMPLATE=/etc/prometheus/prometheus.yml
RENDERED=/tmp/prometheus.yml

sed -e "s|__ES_URL__|${ELASTICSEARCH_ENDPOINT}|g" \
    -e "s|__KEY__|${KEY}|g" \
    "$TEMPLATE" > "$RENDERED"

exec /bin/prometheus \
    --config.file="$RENDERED" \
    --storage.tsdb.retention.time=1h \
    --enable-feature=remote-write-receiver \
    --web.listen-address=:9090
