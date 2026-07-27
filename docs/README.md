# Documentation Guide

Use this index to find the shortest path to the right document. The docs are
split by audience:

- **Operator docs** — you installed the `obs-migrate` CLI and want to migrate
  your own Grafana or Datadog assets into Kibana. No repo checkout needed.
- **Contributor docs** — you cloned this repo and want to change, test, or
  verify the migration engine.

## Operator Docs

Read in this order: install and run a sample, then reach for the command
reference and the source/target pages for your environment.

| Path | Use when |
|---|---|
| `../README.md` | You want install steps, requirements, and a first offline sample migration |
| `command-contract.md` | You want the canonical `obs-migrate` command reference: subcommands, flags, and safe invocation examples |
| `sources/grafana.md` | You are migrating from Grafana: adapter capabilities, flags, and workflow boundaries |
| `sources/datadog.md` | You are migrating from Datadog: adapter capabilities, flags, and workflow boundaries |
| `targets/kibana.md` | You want what lands in Kibana: native API / YAML emit / compile / upload runtime |
| `../SUPPORT.md` | You want help via issues and what context to include |

## Contributor Docs

Everything below assumes a repo checkout. Start with `../CONTRIBUTING.md` for
setup and PR expectations.

### Architecture

| Path | Use when |
|---|---|
| `architecture.md` | You want the repo-level architecture, boundaries, and package map |
| `architecture/asset-model.md` | You need the canonical IR and result contracts |
| `architecture/tooling-matrix.md` | You want guidance on YAML, Pydantic, CUE, Hypothesis, and parser tooling |

### Working In The Repo

| Path | Use when |
|---|---|
| `contributing/dev-commands.md` | You need repo-checkout commands: verification gates, `scripts/` lab lifecycle, pytest |
| `contributing/import-paths.md` | You need the canonical Python import paths |
| `contributing/add-source.md` | You are adding a new source adapter |
| `contributing/add-asset-type.md` | You are adding a new shared asset type |
| `dashboards/README.md` | You want the dashboard YAML schema, lint, and layout validation tooling |
| `../scripts/README.md` | You want an inventory of repo-maintained helper scripts and where they fit |

### Testing And Verification

| Path | Use when |
|---|---|
| `testing.md` | You want the test & quality infrastructure: the confidence pyramid, every gate, and how to extend them |
| `contributing/dev-commands.md` | You want the runnable form of those gates and scripts |
| `local-otlp-validation.md` | You want the local validation lab and OTLP data flow |

### Generated Docs

These files are regenerated from templates and runtime data — edit the
matching `*.tpl.md` (or the generator), never the output:

| Path | Use when |
|---|---|
| `pipeline-trace.md` | You want the shared pipeline overview and cross-source audit summary |
| `sources/grafana-trace.md` | You want auto-generated Grafana per-dashboard traces |
| `sources/datadog-trace.md` | You want auto-generated Datadog per-dashboard traces |

Regenerate them with:

```bash
python scripts/audit_pipeline.py --update-docs
```

### Governance And Compliance

| Path | Use when |
|---|---|
| `../CONTRIBUTING.md` | You want contributor setup, verification, documentation rules, and PR expectations |
| `../SECURITY.md` | You need to report a security vulnerability responsibly |
| `../CODE_OF_CONDUCT.md` | You want community standards and how to report conduct issues |
| `licenses/dependencies.md` | You want the generated dependency license inventory enforced by CI |
