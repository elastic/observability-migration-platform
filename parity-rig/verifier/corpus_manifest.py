"""Pinned benchmark corpus manifest builder.

The PM benchmark UI can run very large marketplace/dashboard corpora, but a
"top N today" corpus is noisy: upstream dashboards disappear/change and a bigger
number is not necessarily broader coverage. This helper creates a reproducible,
stratified manifest from the tools repo's catalogs:

* top dashboards by downloads
* deterministic long-tail slices by rank range
* datasource quotas (Prometheus, Loki, CloudWatch, ...)
* explicit bug seeds

The output is plain JSON and can be committed or fed to external benchmark
tooling. It is intentionally independent of the tools repo runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DashboardEntry:
    id: str
    name: str = ""
    downloads: int = 0
    tags: tuple[str, ...] = ()
    org_slug: str = ""

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "downloads": self.downloads,
            "tags": list(self.tags),
            "orgSlug": self.org_slug,
        }


@dataclass
class CorpusManifest:
    top: list[DashboardEntry] = field(default_factory=list)
    long_tail: dict[str, list[DashboardEntry]] = field(default_factory=dict)
    datasource_strata: dict[str, list[DashboardEntry]] = field(default_factory=dict)
    bug_seeds: list[str] = field(default_factory=list)

    def all_ids(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        sections: list[list[DashboardEntry]] = [self.top]
        sections.extend(self.long_tail.values())
        sections.extend(self.datasource_strata.values())
        for section in sections:
            for entry in section:
                if entry.id not in seen:
                    seen.add(entry.id)
                    out.append(entry.id)
        for seed in self.bug_seeds:
            if seed not in seen:
                seen.add(seed)
                out.append(seed)
        return out

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "grafana": {
                "top": [entry.to_jsonable() for entry in self.top],
                "long_tail": {
                    name: [entry.to_jsonable() for entry in entries]
                    for name, entries in self.long_tail.items()
                },
                "datasource_strata": {
                    name: [entry.to_jsonable() for entry in entries]
                    for name, entries in self.datasource_strata.items()
                },
                "bug_seeds": list(self.bug_seeds),
                "all_ids": self.all_ids(),
            }
        }


def load_catalog(path: Path) -> list[DashboardEntry]:
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, list):
        raise ValueError("Grafana catalog must be a JSON array")
    entries: list[DashboardEntry] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("id") in (None, ""):
            continue
        entries.append(
            DashboardEntry(
                id=str(item.get("id")),
                name=str(item.get("name") or ""),
                downloads=int(item.get("downloads") or 0),
                tags=tuple(str(t) for t in (item.get("tags") or [])),
                org_slug=str(item.get("orgSlug") or item.get("org_slug") or ""),
            )
        )
    return entries


def load_datasource_map(path: Path | None) -> dict[str, list[str]]:
    if path is None:
        return {}
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError("Datasource map must be a JSON object of dashboard id -> datasource list")
    return {
        str(k): [str(vv).lower() for vv in (v if isinstance(v, list) else [])]
        for k, v in raw.items()
    }


def _ranked(entries: list[DashboardEntry]) -> list[DashboardEntry]:
    return sorted(entries, key=lambda e: (-e.downloads, e.id))


def _stable_sample(entries: list[DashboardEntry], count: int, seed: str) -> list[DashboardEntry]:
    ordered = sorted(
        entries,
        key=lambda e: hashlib.sha256(f"{seed}:{e.id}".encode()).hexdigest(),
    )
    return _ranked(ordered[: max(count, 0)])


def _parse_range_spec(spec: str) -> tuple[str, int, int, int]:
    """Parse ``name:start:end:count`` or ``start:end:count``."""
    parts = spec.split(":")
    if len(parts) == 3:
        start, end, count = (int(p) for p in parts)
        return f"rank_{start}_{end}", start, end, count
    if len(parts) == 4:
        name = parts[0]
        start, end, count = (int(p) for p in parts[1:])
        return name, start, end, count
    raise ValueError(f"invalid long-tail spec {spec!r}; expected start:end:count or name:start:end:count")


def _parse_quota(spec: str) -> tuple[str, int]:
    name, sep, value = spec.partition("=")
    if not sep or not name:
        raise ValueError(f"invalid datasource quota {spec!r}; expected datasource=count")
    return name.lower(), int(value)


def build_manifest(
    catalog: list[DashboardEntry],
    *,
    datasource_map: dict[str, list[str]] | None = None,
    top_count: int = 0,
    long_tail_specs: list[str] | None = None,
    datasource_quotas: list[str] | None = None,
    bug_seeds: list[str] | None = None,
    seed: str = "obs-migrate-corpus-v1",
) -> CorpusManifest:
    ranked = _ranked(catalog)
    manifest = CorpusManifest()
    manifest.top = ranked[: max(top_count, 0)]

    for spec in long_tail_specs or []:
        name, start, end, count = _parse_range_spec(spec)
        # Ranks are 1-based and inclusive of start, exclusive of end.
        window = ranked[max(start - 1, 0): max(end - 1, 0)]
        manifest.long_tail[name] = _stable_sample(window, count, f"{seed}:{name}:{start}:{end}")

    ds_map = datasource_map or {}
    by_id = {entry.id: entry for entry in ranked}
    for spec in datasource_quotas or []:
        datasource, count = _parse_quota(spec)
        candidates = [
            by_id[dash_id]
            for dash_id, sources in ds_map.items()
            if dash_id in by_id and datasource in sources
        ]
        manifest.datasource_strata[datasource] = _ranked(candidates)[: max(count, 0)]

    manifest.bug_seeds = [str(seed_id) for seed_id in (bug_seeds or [])]
    return manifest


def _load_bug_seeds(path: Path | None, inline: list[str]) -> list[str]:
    seeds = list(inline or [])
    if path is None:
        return seeds
    raw = json.loads(Path(path).read_text())
    if isinstance(raw, list):
        seeds.extend(str(item) for item in raw)
    elif isinstance(raw, dict):
        seeds.extend(str(item) for item in raw.get("grafana", []))
    else:
        raise ValueError("bug seed file must be a JSON array or object with grafana list")
    return seeds


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verifier.corpus_manifest",
        description="Build a pinned, stratified Grafana benchmark corpus manifest.",
    )
    parser.add_argument("--grafana-catalog", type=Path, required=True)
    parser.add_argument("--grafana-datasource-map", type=Path)
    parser.add_argument("--top", type=int, default=0)
    parser.add_argument("--long-tail", action="append", default=[],
                        help="Rank slice as start:end:count or name:start:end:count")
    parser.add_argument("--datasource-quota", action="append", default=[],
                        help="Datasource quota as name=count")
    parser.add_argument("--bug-seed", action="append", default=[])
    parser.add_argument("--bug-seeds-file", type=Path)
    parser.add_argument("--seed", default="obs-migrate-corpus-v1")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    manifest = build_manifest(
        load_catalog(args.grafana_catalog),
        datasource_map=load_datasource_map(args.grafana_datasource_map),
        top_count=args.top,
        long_tail_specs=args.long_tail,
        datasource_quotas=args.datasource_quota,
        bug_seeds=_load_bug_seeds(args.bug_seeds_file, args.bug_seed),
        seed=args.seed,
    )
    args.output.write_text(json.dumps(manifest.to_jsonable(), indent=2))
    print(json.dumps({"grafana_ids": len(manifest.all_ids()), "output": str(args.output)}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

