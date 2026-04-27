# Asset Model

## Shared Status Vocabulary

All migrated assets use the unified `AssetStatus` enum:

| Status | Meaning |
|---|---|
| `translated` | Successfully migrated |
| `translated_with_warnings` | Migrated with semantic approximations |
| `manual_required` | Needs human review/action |
| `not_feasible` | Cannot be automatically migrated |
| `blocked` | Blocked by dependencies |
| `skipped` | Intentionally skipped (e.g. row panels) |

### Source Mapping

| Grafana Status | Datadog Status | Shared Status |
|---|---|---|
| `migrated` | `ok` | `translated` |
| `migrated_with_warnings` | `warning` | `translated_with_warnings` |
| `requires_manual` | — | `manual_required` |
| `not_feasible` | `blocked` | `not_feasible` |

## DashboardIR

The top-level container. Every source adapter produces a `DashboardIR`
containing panels, controls, alerts, annotations, links, and transforms.

## QueryIR vs TargetQueryPlan

The query representation is split into two contracts:

- **QueryIR**: Source-agnostic semantic intent (what the query means).
- **TargetQueryPlan**: Target-specific rendering (how the query runs on Kibana/ES).

This separation exists because the same semantic intent may render differently
on different targets (e.g. PROMQL vs ES|QL on Elastic Serverless).

## Source Extensions

Every shared contract has a `source_extension: dict` field for
source-specific metadata that does not belong in the common fields.
This prevents source-specific details from leaking into shared contracts
while preserving them for debugging and reporting.
