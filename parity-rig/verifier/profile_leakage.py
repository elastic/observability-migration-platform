# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Detect field-name spellings that don't match the requested field profile.

A curated pack or the base translation is "profile-leaking" when its emitted
ES|QL references a field namespaced for a *different* profile than the one the
operator asked for (e.g. `labels.pod` under `otel`). Such a reference is a hard
`Unknown column` error on a real target of the requested profile.
"""
from __future__ import annotations

import re

# Tokens that must NOT appear for a given profile (label + metric namespaces).
# Each entry: (regex, why). Anchored on a word boundary so `mylabels.x` is safe.
_FORBIDDEN = {
    "otel": [
        (r"(?<![\w.])labels\.[A-Za-z_]", "native label namespace `labels.` under otel"),
        (r"(?<![\w.])prometheus\.labels\.", "prometheus label namespace under otel"),
        (r"(?<![\w.])prometheus\.metrics\.", "prometheus metric namespace under otel"),
        (r"(?<![\w.])metrics\.[A-Za-z_]", "native metric namespace `metrics.` under otel"),
        (r"(?<![\w.])prometheus\.[A-Za-z_][\w]*\.(counter|value|rate)\b",
         "remote_write metric leaf under otel"),
    ],
    "prometheus_native": [
        (r"(?<![\w.])prometheus\.labels\.", "prometheus label namespace under prometheus_native"),
        (r"(?<![\w.])prometheus\.metrics\.", "prometheus metric namespace under prometheus_native"),
        (r"(?<![\w.])prometheus\.[A-Za-z_][\w]*\.(counter|value|rate)\b",
         "remote_write metric leaf under prometheus_native"),
    ],
    "prometheus_metrics": [
        (r"(?<![\w.])labels\.[A-Za-z_]", "native label namespace under prometheus_metrics"),
        (r"(?<![\w.])metrics\.[A-Za-z_]", "native metric namespace under prometheus_metrics"),
        (r"(?<![\w.])prometheus\.[A-Za-z_][\w]*\.(counter|value|rate)\b",
         "remote_write metric leaf under prometheus_metrics"),
    ],
    "prometheus_remote_write": [
        (r"(?<![\w.])labels\.[A-Za-z_]", "native label namespace under prometheus_remote_write"),
        (r"(?<![\w.])metrics\.[A-Za-z_]", "native metric namespace under prometheus_remote_write"),
        (r"(?<![\w.])prometheus\.metrics\.", "nested metric namespace under prometheus_remote_write"),
    ],
    "passthrough": [
        (r"(?<![\w.])labels\.[A-Za-z_]", "native label namespace under passthrough"),
        (r"(?<![\w.])prometheus\.labels\.", "prometheus label namespace under passthrough"),
        (r"(?<![\w.])metrics\.[A-Za-z_]", "native metric namespace under passthrough"),
        (r"(?<![\w.])prometheus\.metrics\.", "nested metric namespace under passthrough"),
    ],
}

# Params and system columns that legitimately contain none of the above.
_STRIP_STRINGS = re.compile(r"\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'")

# The full dotted field path starting at a flagged token, so the violation
# string always names the offending field in full (e.g. prometheus.labels.instance).
_FIELD_TOKEN = re.compile(r"[\w.]+")


def check_profile_leakage(query: str, profile: str) -> list[str]:
    if profile == "auto":
        return []  # auto resolves to a concrete profile at migrate time
    rules = _FORBIDDEN.get(profile)
    if not rules:
        return []
    scrubbed = _STRIP_STRINGS.sub('""', query or "")
    violations: list[str] = []
    for pattern, why in rules:
        for m in re.finditer(pattern, scrubbed):
            field = _FIELD_TOKEN.match(scrubbed, m.start())
            snippet = field.group(0) if field else scrubbed[m.start():m.start() + 24]
            violations.append(f"{why}: {snippet}")
    return violations


def extract_esql_queries(native_json: dict) -> list[str]:
    out: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and re.search(r"(?m)^\s*(TS|FROM)\s+\S", node):
            out.append(node)

    walk(native_json)
    return out
