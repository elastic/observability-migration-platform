# Local Lab And Fixtures

This directory holds local lab assets, bundled dashboard fixtures, and service
configuration used for demos and validation.

## Layout

- `docker-compose.yml` — local stack for Elasticsearch, Kibana, Grafana,
  Prometheus, Loki, Alloy, and helper services
- `grafana/dashboards/` — bundled Grafana dashboard JSON used for migration demos
- `datadog/dashboards/` — bundled Datadog dashboard JSON used for migration demos
- `nginx/` — sample service config used by the local stack
- `alloy/`, `otel/`, `prometheus/`, `loki/` — telemetry and collector configs

## Notes

- Some dashboards in this tree are third-party fixtures; see
  `THIRD_PARTY_NOTICES.md`.
- `grafana/dashboards/home.json` is used as the local Grafana home dashboard by
  `infra/docker-compose.yml`.
- The full local workflow is documented in `docs/local-otlp-validation.md`.
