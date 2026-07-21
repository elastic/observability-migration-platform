// Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
// SPDX-License-Identifier: Elastic-2.0

package extensions

// Export with:
// cue export examples/cue/datadog-field-profile.cue -e profile --out yaml

profile: {
	name:            "custom"
	metric_index:    "metrics-*"
	logs_index:      "logs-*"
	timestamp_field: "@timestamp"
	metrics_dataset_filter: ""
	logs_dataset_filter:    ""

	metric_map: {
		"system.cpu.user":       "system.cpu.user.pct"
		"system.mem.usable":     "system.memory.actual.used.bytes"
		"trace.flask.request.hits": "trace.flask.request.hits"
	}

	tag_map: {
		host:           "host.name"
		env:            "deployment.environment"
		service:        "service.name"
		status:         "log.level"
		kube_namespace: "kubernetes.namespace"
	}

	// Optional: when set, log queries use this map and do not inherit tag_prefix.
	log_tag_map: {
		host:    "host.name"
		service: "service.name"
		status:  "log.level"
	}

	metric_prefix: ""
	metric_suffix: ""
	tag_prefix:    ""
}
