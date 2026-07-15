# Dashboard Interaction Testing Framework

Date: 2026-07-13

## Purpose

Build a deterministic browser-testing framework that proves migrated dashboards
continue to work after users interact with filters, Grafana-derived template
variables, Kibana classic controls, and Kibana ES|QL variable controls.

The framework extends the existing render audit. It does not replace schema
validation, live ES|QL validation, the Dashboards API verifier, semantic parity
checks, or visual regression checks.

The first implementation PR is stacked on PR #289. It delivers the framework,
a synthetic Kibana capability dashboard, then validates two full migrated
dashboards sequentially:

1. `infra/grafana/dashboards/redis-11835.json`
2. `infra/grafana/dashboards/k8s-views-global.json`

`infra/grafana/dashboards/express-14565.json` is the next dashboard after the
first PR.

## Background

Kibana variable controls are first-class ES|QL parameters. The relevant target
behaviors are documented in Elastic's
[variable-control overview](https://www.elastic.co/search-labs/blog/kibana-dashboard-interactivity-variable-controls-overview):

- query-backed value parameters use `?name`;
- field and function identifier parameters use `??name`;
- multi-value filtering uses `MV_CONTAINS` and is available from Kibana 9.3;
- static interval values can drive `TBUCKET`;
- variable controls can affect selected visualizations rather than every panel;
- `Any`, `LIKE`, datasource replacement through `FROM`, and chained controls
  remain unsupported or limited.

The repository already has:

- a deterministic local Elasticsearch and Kibana 9.5 Docker stack;
- Native Dashboard-as-Code upload;
- telemetry-contract generation and deterministic synthetic seeding;
- static browser render classification;
- per-panel element classification;
- pure interaction planning and before/after regression comparison;
- an `agent-browser` verifier walker for HAR and screenshot evidence;
- a visual regression harness.

The missing layer is a reliable live browser driver that changes controls,
waits for all affected panels to settle, and joins UI evidence to the exact
Lens ES|QL requests and responses.

## Goals

1. Exercise each deterministic control option independently.
2. Exercise explicitly declared high-risk control combinations without
   generating the full Cartesian product.
3. Verify browser state, panel render state, and network/query state together.
4. Distinguish product regressions, migration gaps, data gaps, and framework
   failures.
5. Reuse existing render-audit classification and telemetry contracts.
6. Produce machine-readable reports and human-debuggable browser artifacts.
7. Run against Kibana 9.5 locally without SSO.
8. Support a one-full-dashboard-at-a-time rollout.
9. Represent every relevant Kibana control capability, including capabilities
   obs-migrate does not yet emit.

## Non-goals

- Implement missing Grafana translators for function, interval, or multi-value
  ES|QL controls in this PR.
- Treat autonomous LLM execution as a merge gate.
- Replace existing default-state render audit or visual parity gates.
- Run a full control-value Cartesian product.
- Make Serverless SSO browser runs required for pull requests.
- Auto-update assertions, expected gaps, or visual baselines.
- Hide an unsupported translation by making a synthetic Kibana scenario pass.

## Decisions

### Browser implementation

Use Python Playwright.

Reasons:

- the repository is Python-first and has no `package.json`;
- it integrates with pytest, `uv`, current license/SBOM gates, and Python
  dataclasses;
- it can reuse the existing pure-Python render-audit logic directly;
- it provides semantic locators, response interception, screenshots, video,
  and traces;
- it avoids making `agent-browser` process stability part of the nightly gate.

Chrome DevTools MCP and `agent-browser` remain investigation and evidence tools.
They do not run the authoritative nightly interaction suite.

### CI policy

Start as nightly and `workflow_dispatch` only. Promote it to a required pull
request gate after a measured stability period. Promotion requires:

- ten consecutive successful scheduled runs;
- no unresolved framework timeout or selector failures;
- stable runtime within the workflow budget;
- reviewed console-error allowlists;
- deterministic telemetry for all required options.

### Interaction depth

For each control:

- test the default state;
- test every deterministic seeded option independently;
- test reset/clear when the control supports it;
- restore a clean baseline before every option;
- run only manifest-declared combinations after independent coverage.

Local telemetry intentionally limits option cardinality. A scenario must not
enumerate an unbounded production control list.

## Architecture

The framework has three layers.

### Pure audit layer

A new `observability_migration/targets/kibana/interaction_audit.py` module owns:

- capability and scenario dataclasses;
- interaction planning;
- option-expansion policy;
- expected affected/unaffected panel sets;
- evidence dataclasses;
- classifications;
- aggregation and JSON report generation.

This module has no Playwright dependency and is covered by ordinary unit tests.

Existing functions in `render_audit.py` remain authoritative for:

- panel segmentation and identification;
- render-error versus field/data-gap classification;
- expected visualization kind;
- expected data state;
- render snapshots and before/after regression comparison.

The interaction layer calls these functions rather than reimplementing them.

### Browser I/O layer

A new `observability_migration/targets/kibana/interaction_driver.py` module is
the only Playwright-dependent production module. It owns:

- browser and context lifecycle;
- dashboard navigation;
- semantic control discovery;
- control-type-specific selection adapters;
- panel identity discovery;
- request and response interception;
- control and panel settling;
- console collection;
- screenshots and traces;
- extraction of visible legends, empty states, and error markers.

It exposes a narrow protocol to the pure audit layer so its orchestration can be
unit-tested with a fake driver.

### Scenario layer

A new `observability_migration/targets/kibana/interaction_scenarios.py` module
loads and validates versioned YAML scenario manifests under:

```text
parity-rig/interaction/scenarios/
```

The manifest stores only expectations that cannot be derived safely. The
framework derives control definitions, defaults, query dependencies, and
panel IDs from source metadata, `ControlIR`, migration reports, uploaded Native
IR, and the live page.

## Capability Catalog

Every capability receives a stable ID and one result category.

### Result categories

- `migrated_live`: obs-migrate emitted the feature and the uploaded dashboard
  passed UI and network assertions.
- `kibana_only`: Kibana 9.5 supports the feature and the synthetic capability
  dashboard passed, but obs-migrate does not emit it.
- `source_only`: the behavior is resolved at translation time and has no
  interactive Kibana equivalent.
- `migration_gap`: Kibana supports the behavior, but translation is missing,
  degraded, or intentionally refused.

### Capabilities

#### Query-backed value variable

Token: `?name`

Assertions:

- expected options populate;
- default is selected;
- every option can be selected;
- only dependent panels dispatch a new query;
- request parameters contain the selected value;
- responses are successful and contain rows where data is required;
- dependent panels render;
- intentionally independent panels remain unaffected.

#### Multi-value variable

Token: `?name` used with `MV_CONTAINS`

Assertions:

- each option works alone;
- one manifest-declared pair works;
- request parameters preserve all selected values;
- clearing and reselecting is stable;
- no `Any` behavior is inferred.

Current migration status: `migration_gap`.

#### Field variable

Token: `??name`

Assertions:

- each allowed field is selectable;
- request parameters bind an identifier;
- the query keeps a stable output alias;
- the response contains the stable alias;
- legends/accessors change to the chosen field;
- concrete-plus-variable collision cases degrade without invalid-column errors.

#### Function variable

Token: `??name` in a function position

Assertions:

- each allowlisted function is selectable;
- the dispatched query binds the selected function;
- response shape and displayed values remain valid;
- no arbitrary function name is accepted.

Current migration status: `migration_gap`.

#### Interval variable

Token: a value parameter passed to `TBUCKET`

Assertions:

- every static interval is selectable;
- bucket metadata matches the selected interval;
- chart density changes;
- query and panel remain valid.

Current migration status: `migration_gap`.

#### Classic options-list control

Assertions:

- every seeded option filters its intended panel set;
- single and supported multiple selections work;
- filter pills and requests reflect the selected values;
- reset restores baseline.

#### Classic range-slider control

Assertions:

- minimum, midpoint, and maximum values are exercised;
- intended panels update;
- bounds and reset behavior remain valid.

#### Global dashboard interactions

Assertions:

- KQL/query-bar filter;
- Add filter;
- dashboard time-range change;
- refresh;
- control reset;
- state persistence after reload where Kibana promises persistence.

#### Negative and limited capabilities

The framework reports explicit coverage gaps for:

- `Any` selection where unsupported;
- `LIKE` parameterization where unsupported;
- datasource replacement through `FROM`;
- chained variable controls;
- invalid/unbound parameters;
- unsupported function names;
- control queries that fail or exceed the configured timeout.

## Scenario Manifest

Each scenario manifest is versioned and contains:

```yaml
version: 1
dashboard:
  slug: redis-11835
  source: infra/grafana/dashboards/redis-11835.json
  control_schema: infra/grafana/dashboards/control_schemas/redis-11835.json
controls:
  instance:
    capability: esql_value
    option_strategy: every_seeded
    affects: all_query_panels
  namespace:
    capability: esql_value
    option_strategy: every_seeded
    expected_gap: chained_control
combinations: []
console_allowlist_profile: local_no_security
```

The concrete schema may add typed fields during implementation, but it must
retain these principles:

- no duplicated query text;
- no hard-coded generated Kibana UUIDs;
- panel expectations use stable source panel identities or generated mappings;
- expected gaps are explicit and reviewed;
- option values come from deterministic seed data;
- combinations are opt-in.

## End-to-End Data Flow

For each dashboard:

1. Read the committed source dashboard and scenario manifest.
2. Migrate with the current runtime feature probes.
3. Validate dashboard schema and Native Dashboard-as-Code output.
4. Upload through the native Dashboards API.
5. Generate the telemetry contract.
6. Recreate and seed target data for every declared control option.
7. Join source variables, `ControlIR`, native `pinned_panels`, live controls,
   and panel query dependencies into an interaction plan.
8. Open a clean dashboard view with a fixed time window.
9. Capture baseline UI, panel, console, and request state.
10. For each control and each option:
    - restore/reload the clean baseline;
    - select one option;
    - wait for expected affected requests;
    - wait for loading markers to clear;
    - capture UI, panel, network, response, and screenshot evidence;
    - classify the result.
11. Run manifest-declared high-risk combinations.
12. Write the per-dashboard and aggregate reports.

## Browser Settling

`networkidle` and fixed sleeps are insufficient for Kibana Lens.

An interaction settles only when:

1. every expected affected panel has dispatched a matching
   `/internal/search/esql_async` request;
2. those requests have completed or failed;
3. panel loading indicators have disappeared;
4. the panel DOM has remained stable for a short bounded quiescence window.

Requests are correlated to panel identity using Kibana request context and
opaque identifiers. Panel titles are evidence, not identity keys.

Every wait is bounded. A timeout becomes `framework_error` and preserves a
trace, screenshot, DOM snapshot, pending-request list, and console messages.

## Assertions

Each interaction has three assertion layers.

### Browser state

- selected value is visible;
- control reports the expected selection count;
- reset and reload behavior match the scenario;
- no incompatible-selection warning is present unless expected.

### Panel state

- expected affected panels re-query;
- expected unaffected panels do not re-query;
- panels remain rendered;
- chart kind remains valid;
- expected legend/accessor changes occur;
- required-data panels are not unexpectedly empty;
- no invalid-column or Lens accessor error appears.

### Query and response state

- request URL and status are expected;
- request query contains the expected parameter location;
- request parameters contain the selected value or identifier;
- no identifier is incorrectly bound as a value;
- response contains required columns;
- field-variable responses preserve the stable alias;
- row count is positive when required;
- Elasticsearch execution errors are captured verbatim in artifacts.

## Failure Classification

- `interaction_regression`: panel rendered before the interaction and broke
  afterward.
- `query_contract_error`: wrong or missing parameter, wrong query shape,
  unexpected non-2xx response, missing required column, or invalid response.
- `field_gap`: target mapping lacks a required field.
- `data_gap`: fields exist but seeded/target data does not satisfy the query.
- `unexpected_empty`: execution succeeds but a data-required panel is empty.
- `coverage_gap`: a supported Kibana capability is not emitted or cannot be
  represented by current migration.
- `framework_error`: selector, browser, timeout, auth, request-correlation, or
  artifact failure.

Only `field_gap` and `data_gap` are warnings by default. An explicitly declared
`coverage_gap` is non-blocking but appears in coverage summaries. Undeclared
coverage gaps fail the scenario review gate.

## Console and Network Policy

The local security-disabled stack produces known optional-feature errors, such
as unavailable user profiles or assistant capabilities. Allowlisting is by
endpoint, method, and expected status, not by broad message text.

Rules:

- a panel query failure always fails;
- an unallowlisted console error fails;
- an allowlist entry must include a rationale;
- new allowlist entries require review;
- response bodies containing credentials or sensitive data are not persisted.

## Artifacts

Runtime artifacts live under:

```text
.tmp/interaction-audit/<run-id>/<dashboard-slug>/
```

Per interaction:

- selected-control JSON;
- correlated request metadata and redacted body;
- response status, columns, row count, and error summary;
- before/after render snapshots;
- screenshot;
- console and framework logs.

On failure:

- Playwright trace;
- additional panel screenshot;
- accessibility snapshot;
- pending request inventory.

The aggregate JSON report records:

- dashboard and stack versions;
- capabilities covered;
- migrated-live, Kibana-only, source-only, and gap counts;
- interactions attempted/passed/warned/failed;
- panel denominators;
- option and combination coverage;
- artifact paths.

Artifacts are ignored and uploaded by CI. They are not committed.

## Synthetic Kibana Capability Dashboard

A generated capability dashboard validates the driver and target behavior
independently of migration. It includes:

- query-backed single-value filtering;
- multi-value filtering with `MV_CONTAINS`;
- interval switching through `TBUCKET`;
- function switching;
- field switching with a stable alias;
- classic options-list filtering;
- range-slider filtering;
- an intentionally unaffected panel;
- one controlled negative case for each failure classifier.

Passing this dashboard proves the framework can exercise Kibana. It does not
claim obs-migrate supports every capability. The report marks unimplemented
migration features as `kibana_only` and `migration_gap`.

## Full Dashboard Rollout

### Redis 11835

Source:

```text
infra/grafana/dashboards/redis-11835.json
```

Expected source variables:

- `namespace`;
- `pod_name`;
- `instance`;
- datasource variable, recorded as source-only/unsupported for target switching.

Expected migrated behavior:

- three query-backed value controls;
- every seeded option is exercised;
- `instance` affects all twelve query panels;
- namespace and pod control behavior records the current chained-control gap;
- four existing panel warnings remain visible but do not become interaction
  regressions.

Redis must pass completely before K8s implementation begins.

### K8s Views Global

Source:

```text
infra/grafana/dashboards/k8s-views-global.json
```

Expected source variables:

- `cluster`;
- `job`;
- custom `resolution`;
- datasource variable.

Expected migrated behavior:

- cluster and job value controls;
- cluster affects all query panels;
- job affects its declared subset;
- every seeded option is exercised independently;
- one declared cluster-plus-job combination is exercised;
- resolution is reported as a migration gap rather than silently frozen;
- existing field/data gaps retain warning classification.

### Express 14565

Deferred to the next PR. It validates:

- two controls bound to one panel;
- two variables resolving to the same target field;
- interaction behavior amid existing migration warnings and not-feasible
  panels.

## Test Strategy

### Unit tests

Cover:

- manifest validation;
- capability result categories;
- control and option planning;
- independent-option expansion;
- combination expansion;
- dependency mapping;
- request-to-panel correlation;
- settling state machine;
- failure classification;
- report aggregation;
- redaction;
- console allowlist matching.

### Adapter tests

Use fake Playwright page/context objects or a controlled local HTML fixture to
cover:

- semantic control discovery;
- selection adapters;
- retries;
- response interception;
- trace lifecycle;
- screenshot and snapshot handling;
- bounded timeouts.

### Framework self-test

The synthetic capability dashboard includes passing and intentionally failing
cases. The self-test must prove each failure class is detected.

### Live dashboard tests

Run Redis, then K8s, through the complete migrate, upload, seed, interact, and
report pipeline.

### Existing gates

Continue to run:

- `make test`;
- `make lint`;
- `make typecheck`;
- dashboard schema validation;
- Native Dashboard API validation;
- default-state render audit;
- scoped live ES|QL validation.

## Dependency and Tooling Changes

Add Playwright as an optional locked Python dependency. The default unit-test
path must not download browser binaries.

Add:

- a browser dependency sync/install command;
- `make test-interactions`;
- a local orchestration script;
- a dedicated nightly/manual workflow;
- license dependency and CycloneDX SBOM refreshes.

The workflow uses Python 3.12 and a pinned Playwright Chromium build. Docker
starts Elasticsearch and Kibana 9.5. Teardown runs unconditionally.

## CI Workflow

The initial workflow:

- triggers nightly and through `workflow_dispatch`;
- can select a scenario slug;
- starts the local no-SSO 9.5 stack;
- migrates, uploads, and seeds one dashboard at a time;
- runs the synthetic capability dashboard first;
- runs Redis completely;
- starts K8s only after Redis completes;
- continues within each dashboard to collect complete failure evidence;
- uploads reports, screenshots, and traces;
- publishes denominator and capability summaries;
- is non-required during the stability period.

## Documentation Changes

Update:

- `docs/testing.md` with the new interaction gate, classifications, artifact
  contract, and one-dashboard rollout;
- `docs/command-contract.md` with commands, environment variables, and CI
  invocation;
- `CONTRIBUTING.md` if browser dependency setup changes the contributor
  workflow;
- the mirrored browser-debugging skills only if their command contract changes.

## Branch and PR Strategy

The implementation branch is stacked on PR #289:

```text
test/dashboard-interaction-framework
```

The first PR may remain draft while PR #289 is open. After #289 merges, rebase
the branch onto `main` so the final upstream diff contains only framework and
scenario changes.

Within the PR, commits remain logically staged:

1. pure models and unit tests;
2. Playwright adapter and adapter tests;
3. synthetic capability dashboard and self-test;
4. Redis scenario and live verification;
5. K8s scenario and live verification;
6. nightly workflow and documentation.

## Acceptance Criteria

The design is complete when implementation demonstrates all of the following:

1. The synthetic dashboard exercises every cataloged Kibana control capability.
2. Unsupported migration features appear as explicit coverage gaps.
3. Redis exercises every seeded option and all twelve panels remain correctly
   rendered for applicable instance selections.
4. K8s exercises every seeded cluster and job option plus one declared
   combination.
5. The framework detects an intentionally introduced invalid-column/accessor
   failure.
6. The framework detects wrong value-versus-identifier binding.
7. Affected and unaffected panel sets are verified from actual requests.
8. No correctness decision relies only on a screenshot.
9. Reports include stable denominators and complete artifacts.
10. Unit, lint, typecheck, schema, Native API, default render, and interaction
    gates pass.
11. The nightly/manual workflow completes without SSO.
12. No browser artifacts, auth state, secrets, or generated telemetry are
    committed.
