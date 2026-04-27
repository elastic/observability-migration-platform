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

	metric_prefix: ""
	metric_suffix: ""
	tag_prefix:    ""
}
