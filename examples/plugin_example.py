import re


def register(api):
    rule_pack = api["rule_pack"]

    cluster_candidates = rule_pack.label_candidates.setdefault("cluster", [])
    if "k8s.cluster.name" not in cluster_candidates:
        cluster_candidates.insert(0, "k8s.cluster.name")

    @api["query_preprocessors"].register("unwrap_abs", priority=15)
    def unwrap_abs_rule(context):
        expr = context.clean_expr or context.promql_expr
        match = re.match(r"^\s*abs\((?P<body>.+)\)\s*$", expr, re.DOTALL)
        if not match:
            return None
        context.clean_expr = match.group("body").strip()
        api["append_unique"](
            context.warnings,
            "Plugin example approximated abs() by dropping the wrapper",
        )
        return "unwrapped abs() in plugin example"
