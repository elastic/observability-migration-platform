# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Parser for Datadog log search syntax.

Datadog log search supports:
    - Free-text terms and quoted phrases
    - Attribute filters: @field:value, @field:>100, @field:[1 TO 100]
    - Reserved tag filters: service:web, status:error, host:app01
    - Boolean operators: AND, OR
    - Negation: - prefix
    - Wildcards: * and ?
    - Grouping: parentheses

Reference: https://docs.datadoghq.com/logs/explorer/search_syntax/
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol, TypeAlias, cast

from lark import Lark, Transformer
from lark.exceptions import LarkError

from observability_migration.core.verification.field_capabilities import (
    is_numeric_field,
    is_string_field,
)

from .models import (
    LogAttributeFilter,
    LogBoolOp,
    LogNot,
    LogQuery,
    LogRange,
    LogTerm,
    LogWildcard,
)
from .parser_results import (
    ParserDiagnostic,
    ParserDiagnosticCode,
    ParserResult,
)

_TEMPLATE_VAR_RE = re.compile(r"\$\w+(?:\.\w+)*")
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_]\w*$")
_NUMERIC_RE = re.compile(r"^-?(?:\d+(?:\.\d+)?|\.\d+)$")
_COMPARISON_RE = re.compile(r"^(>=|<=|>|<)\s*(-?(?:\d+(?:\.\d+)?|\.\d+))$")


class LogParseError(Exception):
    pass


RESERVED_TAGS = {
    "host", "service", "status", "source", "env", "version",
    "trace_id", "span_id", "filename",
}

_TOKEN_RE = re.compile(
    r"""
    ("(?:[^"\\]|\\.)*")          # quoted string
    |(\bAND\b)                   # AND
    |(\bOR\b)                    # OR
    |(\bNOT\b)                   # NOT
    |([()]  )                    # parens
    |(-(?=[@\w"]))               # negation prefix
    |(@[\w.]+:\[(?:[^\]])*\])    # attribute range filter @field:[low TO high]
    |(@[\w.]+:\((?:[^)])*\))     # attribute filter with grouped values @field:(a OR b)
    |(@[\w.]+:[^\s,)]+)          # attribute filter @field:value
    |([\w.*?]+:(?:"(?:[^"\\]|\\.)*"|[^\s,)]+))  # reserved-tag or key:value filter
    |((?:\$\w+(?:\.\w+)*)|[\w.*?/\\]+)  # plain term or template var
    |\s+                         # whitespace
    """,
    re.VERBOSE,
)

_ATOM_TOKEN_TYPES = {"QUOTED", "ATTR_RANGE", "ATTR", "KV", "TERM"}
_BOOL_EXPR_GRAMMAR = r"""
?start: or_chain
?or_chain: and_chain ("OR" and_chain)*
?and_chain: not_expr ("AND" not_expr)*
?not_expr: ("NOT" | NEG) not_expr -> negate
         | atom
?atom: ATOM                         -> atom
     | "(" or_chain ")"
ATOM: "ATOM"
NEG: "-"
%import common.WS
%ignore WS
"""
_LOG_BOOL_PARSER = Lark(_BOOL_EXPR_GRAMMAR, parser="lalr", maybe_placeholders=False)


def parse_log_query(raw: str) -> LogQuery:
    """Parse a Datadog log search string into a LogQuery AST.

    After calling, check ``LogTokenizeWarning.skipped_chars`` for any
    characters the tokenizer could not match (potential data loss).
    """
    raw_stripped = raw.strip()
    if not raw_stripped or raw_stripped == "*":
        return LogQuery(raw=raw)

    LogTokenizeWarning.reset()
    tokens = _tokenize(raw_stripped)
    if not tokens:
        return LogQuery(raw=raw)

    ast = _parse_tokens_with_lark(tokens)
    return LogQuery(raw=raw, ast=ast)


def parse_log_query_result(raw: str) -> ParserResult:
    """Parse log query and capture degradation diagnostics."""
    query = parse_log_query(raw)
    diagnostics: list[ParserDiagnostic] = []

    if LogTokenizeWarning.skipped_chars:
        sample = [f"{pos}:{char}" for pos, char in LogTokenizeWarning.skipped_chars[:8]]
        diagnostics.append(
            ParserDiagnostic(
                code=ParserDiagnosticCode.LOG_TOKENIZER_SKIPPED_CHARS.value,
                message="Datadog log tokenizer skipped unsupported characters",
                detail={
                    "count": len(LogTokenizeWarning.skipped_chars),
                    "sample": sample,
                },
            )
        )

    if LogTokenizeWarning.fallback_used:
        diagnostics.append(
            ParserDiagnostic(
                code=ParserDiagnosticCode.LOG_BOOLEAN_FALLBACK.value,
                message="Datadog log parser used boolean fallback and may alter precedence",
            )
        )

    raw_stripped = str(raw or "").strip()
    if query.ast is None and raw_stripped not in {"", "*"}:
        diagnostics.append(
            ParserDiagnostic(
                code=ParserDiagnosticCode.LOG_PARSE_ERROR.value,
                message="Datadog log parser produced an empty AST for a non-empty query",
                detail={"query": raw_stripped[:160]},
            )
        )

    degraded = bool(diagnostics)
    return ParserResult(
        value=query,
        diagnostics=diagnostics,
        degraded=degraded,
        lossless=not degraded,
    )


class LogTokenizeWarning:
    """Tracks characters skipped during log query tokenization."""
    skipped_chars: list[tuple[int, str]] = []
    fallback_used: bool = False

    @classmethod
    def reset(cls):
        cls.skipped_chars = []
        cls.fallback_used = False


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            LogTokenizeWarning.skipped_chars.append((pos, text[pos]))
            pos += 1
            continue
        pos = m.end()
        if m.group(1) is not None:
            tokens.append(("QUOTED", m.group(1)[1:-1]))
        elif m.group(2) is not None:
            tokens.append(("AND", "AND"))
        elif m.group(3) is not None:
            tokens.append(("OR", "OR"))
        elif m.group(4) is not None:
            tokens.append(("NOT", "NOT"))
        elif m.group(5) is not None:
            tokens.append(("PAREN", m.group(5).strip()))
        elif m.group(6) is not None:
            tokens.append(("NEG", "-"))
        elif m.group(7) is not None:
            tokens.append(("ATTR_RANGE", m.group(7)))
        elif m.group(8) is not None:
            tokens.append(("ATTR", m.group(8)))
        elif m.group(9) is not None:
            tokens.append(("ATTR", m.group(9)))
        elif m.group(10) is not None:
            tokens.append(("KV", m.group(10)))
        elif m.group(11) is not None:
            tokens.append(("TERM", m.group(11)))
    return tokens


class _BoolExprTransformer(Transformer):
    def __init__(self, atoms: list[Any]) -> None:
        super().__init__()
        self._atoms = iter(atoms)

    def atom(self, _items):
        return next(self._atoms)

    def negate(self, items):
        return LogNot(child=items[-1])

    def and_chain(self, items):
        return _collapse_bool("AND", items)

    def or_chain(self, items):
        return _collapse_bool("OR", items)


def _collapse_bool(op: str, items: list[Any]) -> Any:
    children: list[Any] = []
    for item in items:
        if isinstance(item, LogBoolOp) and item.op == op:
            children.extend(item.children)
        else:
            children.append(item)
    if len(children) == 1:
        return children[0]
    return LogBoolOp(op=op, children=children)


def _parse_tokens_with_lark(tokens: list[tuple[str, str]]) -> Any | None:
    expression, atoms = _build_lark_expression(tokens)
    if not expression or not atoms:
        return None
    try:
        tree = _LOG_BOOL_PARSER.parse(expression)
    except LarkError:
        LogTokenizeWarning.fallback_used = True
        return _collapse_bool("AND", atoms)
    try:
        return _BoolExprTransformer(atoms).transform(tree)
    except (LarkError, StopIteration):
        LogTokenizeWarning.fallback_used = True
        return _collapse_bool("AND", atoms)


def _build_lark_expression(tokens: list[tuple[str, str]]) -> tuple[str, list[Any]]:
    expression_parts: list[str] = []
    atoms: list[Any] = []
    open_groups = 0
    expect_operand = True

    for token_type, token_value in tokens:
        if token_type in _ATOM_TOKEN_TYPES:
            if not expect_operand:
                expression_parts.append("AND")
            atoms.append(_atom_node_from_token(token_type, token_value))
            expression_parts.append("ATOM")
            expect_operand = False
            continue

        if token_type == "PAREN":
            if token_value == "(":
                if not expect_operand:
                    expression_parts.append("AND")
                expression_parts.append("(")
                open_groups += 1
                expect_operand = True
            elif open_groups > 0 and not expect_operand:
                expression_parts.append(")")
                open_groups -= 1
                expect_operand = False
            elif token_value == ")":
                LogTokenizeWarning.fallback_used = True
            continue

        if token_type in {"NEG", "NOT"}:
            if not expect_operand:
                expression_parts.append("AND")
            expression_parts.append("-" if token_type == "NEG" else "NOT")
            expect_operand = True
            continue

        if token_type in {"AND", "OR"} and not expect_operand:
            expression_parts.append(token_type)
            expect_operand = True

    while expression_parts and expression_parts[-1] in {"AND", "OR", "NOT", "-", "("}:
        trailing = expression_parts.pop()
        if trailing == "(":
            open_groups = max(open_groups - 1, 0)

    if not expression_parts:
        return "", []

    if expression_parts[-1] in {"ATOM", ")"}:
        if open_groups > 0:
            LogTokenizeWarning.fallback_used = True
        expression_parts.extend(")" for _ in range(open_groups))

    return " ".join(expression_parts), atoms


def _atom_node_from_token(token_type: str, token_value: str) -> Any:
    if token_type == "QUOTED":
        return LogTerm(value=token_value, quoted=True)
    if token_type == "ATTR_RANGE":
        return _parse_attr_range(token_value)
    if token_type == "ATTR":
        return _parse_attr_filter(token_value)
    if token_type == "KV":
        return _parse_kv_filter(token_value)
    if token_type == "TERM" and ("*" in token_value or "?" in token_value):
        return LogWildcard(attribute="", pattern=token_value)
    return LogTerm(value=token_value, quoted=False)


def _parse_or(tokens: list[tuple[str, str]], pos: int) -> tuple[Any, int]:
    left, pos = _parse_and(tokens, pos)
    children = [left]
    while pos < len(tokens) and tokens[pos][0] == "OR":
        pos += 1
        right, pos = _parse_and(tokens, pos)
        children.append(right)
    if len(children) == 1:
        return children[0], pos
    return LogBoolOp(op="OR", children=children), pos


def _parse_and(tokens: list[tuple[str, str]], pos: int) -> tuple[Any, int]:
    left, pos = _parse_not(tokens, pos)
    children = [left]
    while pos < len(tokens):
        if tokens[pos][0] == "OR" or (tokens[pos][0] == "PAREN" and tokens[pos][1] == ")"):
            break
        if tokens[pos][0] == "AND":
            pos += 1
        child, pos = _parse_not(tokens, pos)
        children.append(child)
    if len(children) == 1:
        return children[0], pos
    return LogBoolOp(op="AND", children=children), pos


def _parse_not(tokens: list[tuple[str, str]], pos: int) -> tuple[Any, int]:
    if pos < len(tokens) and tokens[pos][0] == "NOT":
        pos += 1
        child, pos = _parse_atom(tokens, pos)
        return LogNot(child=child), pos
    if pos < len(tokens) and tokens[pos][0] == "NEG":
        pos += 1
        child, pos = _parse_atom(tokens, pos)
        return LogNot(child=child), pos
    return _parse_atom(tokens, pos)


def _parse_atom(tokens: list[tuple[str, str]], pos: int) -> tuple[Any, int]:
    if pos >= len(tokens):
        return LogTerm(value="", quoted=False), pos

    tok_type, tok_val = tokens[pos]

    if tok_type == "PAREN" and tok_val == "(":
        pos += 1
        inner, pos = _parse_or(tokens, pos)
        if pos < len(tokens) and tokens[pos] == ("PAREN", ")"):
            pos += 1
        return inner, pos

    if tok_type == "QUOTED":
        return LogTerm(value=tok_val, quoted=True), pos + 1

    if tok_type == "ATTR_RANGE":
        return _parse_attr_range(tok_val), pos + 1

    if tok_type == "ATTR":
        return _parse_attr_filter(tok_val), pos + 1

    if tok_type == "KV":
        return _parse_kv_filter(tok_val), pos + 1

    if tok_type == "TERM":
        if "*" in tok_val or "?" in tok_val:
            return LogWildcard(attribute="", pattern=tok_val), pos + 1
        return LogTerm(value=tok_val, quoted=False), pos + 1

    return LogTerm(value=tok_val, quoted=False), pos + 1


def _parse_attr_filter(text: str) -> LogAttributeFilter:
    """Parse `@field:value` or `@field.subfield:value`."""
    colon = text.index(":")
    attr = text[:colon]
    value = text[colon + 1:]
    if attr.startswith("@"):
        attr = attr[1:]
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if value.startswith("(") and value.endswith(")") and not re.search(r"\bOR\b", value, re.IGNORECASE):
        value = value[1:-1].strip()
    return LogAttributeFilter(attribute=attr, value=value, is_tag=False)


def _parse_kv_filter(text: str) -> LogAttributeFilter:
    """Parse `key:value` (reserved tags or custom tags)."""
    colon = text.index(":")
    key = text[:colon]
    value = text[colon + 1:]
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if value.startswith("(") and value.endswith(")") and not re.search(r"\bOR\b", value, re.IGNORECASE):
        value = value[1:-1].strip()
    is_tag = key.lower() in RESERVED_TAGS
    return LogAttributeFilter(attribute=key, value=value, is_tag=is_tag)


def _parse_attr_range(text: str) -> LogRange:
    """Parse `@field:[low TO high]`."""
    colon = text.index(":")
    attr = text[:colon]
    if attr.startswith("@"):
        attr = attr[1:]
    bracket_part = text[colon + 1:].strip()

    low_inclusive = bracket_part.startswith("[")
    high_inclusive = bracket_part.endswith("]")
    inner = bracket_part.lstrip("[{").rstrip("]}")

    parts = re.split(r"\s+TO\s+", inner, flags=re.IGNORECASE)
    if len(parts) == 2:
        return LogRange(
            attribute=attr, low=parts[0].strip(), high=parts[1].strip(),
            low_inclusive=low_inclusive, high_inclusive=high_inclusive,
        )
    return LogRange(attribute=attr, low=inner, high=inner)


# ---------------------------------------------------------------------------
# ES|QL / KQL translation helpers
# ---------------------------------------------------------------------------

# KQL only recognizes a backslash escape in front of these characters (plus
# whitespace and the reserved keywords). A backslash before anything else —
# most commonly Datadog's escaped forward slash ``\/`` — is a hard parse error
# in the Elasticsearch KQL grammar ("token recognition error"). Datadog log
# search escapes a broader set of characters, so we must drop the backslashes
# that KQL does not understand when bridging a free-text term into KQL().
_KQL_ESCAPABLE_CHARS = set('\\():<>"*{}')


def _normalize_kql_escapes(value: str) -> str:
    """Drop backslashes that KQL cannot parse, keeping valid KQL escapes.

    e.g. Datadog ``felix\\/int_dataplane.go`` -> ``felix/int_dataplane.go``
    (forward slash never needs escaping in KQL), while a genuine ``\\(`` or
    ``\\"`` escape is preserved.
    """
    if "\\" not in value:
        return value
    out: list[str] = []
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch == "\\" and i + 1 < n:
            nxt = value[i + 1]
            if nxt in _KQL_ESCAPABLE_CHARS or nxt.isspace():
                out.append(ch)
                out.append(nxt)
            else:
                # backslash KQL would choke on; keep only the escaped char
                out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def log_ast_to_kql(node: Any, field_map: dict[str, str] | None = None) -> str:
    """Convert a log search AST to a KQL string for use in ES|QL WHERE KQL().

    *field_map* translates Datadog attribute names (e.g. ``status``,
    ``source``) to the target schema (e.g. ``log.level``,
    ``service.name``).  When omitted, raw Datadog names pass through.
    """
    fm = field_map or {}

    if node is None:
        return "*"

    if isinstance(node, LogTerm):
        if _TEMPLATE_VAR_RE.search(node.value):
            return ""
        value = _normalize_kql_escapes(node.value)
        if node.quoted:
            return f'"{value}"'
        return value

    if isinstance(node, LogAttributeFilter):
        if _TEMPLATE_VAR_RE.search(node.value):
            return ""
        field = fm.get(node.attribute, node.attribute)
        if node.negated:
            return f"NOT {field}: {node.value}"
        return f"{field}: {node.value}"

    if isinstance(node, LogRange):
        attr = fm.get(node.attribute, node.attribute)
        return f"{attr} >= {node.low} AND {attr} <= {node.high}"

    if isinstance(node, LogWildcard):
        pattern = _normalize_kql_escapes(node.pattern)
        if node.attribute:
            attr = fm.get(node.attribute, node.attribute)
            return f"{attr}: {pattern}"
        return pattern

    if isinstance(node, LogBoolOp):
        parts = [p for p in (log_ast_to_kql(c, fm) for c in node.children) if p]
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        joiner = f" {node.op} "
        joined = joiner.join(f"({p})" if " " in p else p for p in parts)
        return _balance_parens(joined)

    if isinstance(node, LogNot):
        inner = log_ast_to_kql(node.child, fm)
        result = f"NOT ({inner})" if " " in inner else f"NOT {inner}"
        return _balance_parens(result)

    return str(node)


def _balance_parens(kql: str) -> str:
    """Ensure parentheses are balanced by stripping excess or appending missing."""
    opens = kql.count("(")
    closes = kql.count(")")
    if opens > closes:
        kql = kql + ")" * (opens - closes)
    elif closes > opens:
        kql = kql.lstrip(")")
        opens = kql.count("(")
        closes = kql.count(")")
        if closes > opens:
            kql = "(" * (closes - opens) + kql
    return kql


class _LogFieldProfileLike(Protocol):
    def map_tag(self, dd_tag: str, context: str = "") -> str: ...

    def field_capability(self, field_name: str, context: str = "") -> Any | None: ...


LogFieldMapping: TypeAlias = Mapping[str, str] | _LogFieldProfileLike | None


def log_ast_to_esql_where(node: Any, field_map: LogFieldMapping = None) -> str:
    """Convert a log search AST to an ES|QL WHERE clause (without the WHERE keyword).

    Uses direct ES|QL predicates for attribute filters and KQL() for free-text.
    """
    if node is None:
        return ""

    if isinstance(node, LogTerm):
        val = node.value
        if _TEMPLATE_VAR_RE.search(val):
            return ""
        if node.quoted:
            return f'message LIKE "*{_esql_escape(val)}*"'
        return f'message LIKE "*{_esql_escape(val)}*"'

    if isinstance(node, LogAttributeFilter):
        field_name, capability = _resolve_esql_field(node.attribute, field_map)
        field = _esql_identifier(field_name)
        if _TEMPLATE_VAR_RE.search(node.value):
            return ""
        if node.value.startswith("(") and node.value.endswith(")") and re.search(r"\bOR\b", node.value, re.IGNORECASE):
            inner = node.value[1:-1]
            options = [part.strip() for part in re.split(r"\bOR\b", inner, flags=re.IGNORECASE) if part.strip()]
            clauses = [_render_attr_predicate(field, value, node.negated, capability) for value in options]
            clauses = [clause for clause in clauses if clause]
            if not clauses:
                return ""
            joiner = " AND " if node.negated else " OR "
            return "(" + joiner.join(clauses) + ")"
        return _render_attr_predicate(field, node.value, node.negated, capability)

    if isinstance(node, LogRange):
        field_name, capability = _resolve_esql_field(node.attribute, field_map)
        field = _esql_identifier(field_name)
        low_op = ">=" if node.low_inclusive else ">"
        high_op = "<=" if node.high_inclusive else "<"
        low = _render_attr_literal(node.low, capability)
        high = _render_attr_literal(node.high, capability)
        return f"{field} {low_op} {low} AND {field} {high_op} {high}"

    if isinstance(node, LogWildcard):
        if node.attribute:
            field_name, _capability = _resolve_esql_field(node.attribute, field_map)
            field = _esql_identifier(field_name)
            if _TEMPLATE_VAR_RE.search(node.pattern):
                return ""
            return f'{field} LIKE "{_esql_escape(node.pattern)}"'
        if _TEMPLATE_VAR_RE.search(node.pattern):
            return ""
        return f'message LIKE "{_esql_escape(node.pattern)}"'

    if isinstance(node, LogBoolOp):
        parts = [log_ast_to_esql_where(c, field_map) for c in node.children if c]
        parts = [p for p in parts if p]
        if not parts:
            return ""
        joiner = f" {node.op} "
        return joiner.join(f"({p})" if " AND " in p or " OR " in p else p for p in parts)

    if isinstance(node, LogNot):
        inner = log_ast_to_esql_where(node.child, field_map)
        if not inner:
            return ""
        return f"NOT ({inner})"

    return ""


def _esql_escape(val: str) -> str:
    return val.replace("\\", "\\\\").replace('"', '\\"')


def _esql_identifier(field_name: str) -> str:
    parts = []
    for part in field_name.split("."):
        if _SAFE_IDENTIFIER_RE.match(part):
            parts.append(part)
        else:
            parts.append(f"`{part.replace('`', '``')}`")
    return ".".join(parts)


def _resolve_esql_field(raw_field: str, field_map: LogFieldMapping) -> tuple[str, Any | None]:
    if field_map is None:
        return raw_field, None
    if isinstance(field_map, dict):
        return field_map.get(raw_field, raw_field), None
    profile = cast(_LogFieldProfileLike, field_map)
    mapped = profile.map_tag(raw_field, context="log")
    capability = profile.field_capability(mapped, context="log")
    return mapped, capability


def _render_attr_literal(value: str, field_capability: Any | None = None) -> str:
    if field_capability is not None:
        if is_numeric_field(field_capability):
            return value
        if is_string_field(field_capability):
            return f'"{_esql_escape(value)}"'
    if _NUMERIC_RE.fullmatch(value):
        return value
    return f'"{_esql_escape(value)}"'


def _render_attr_predicate(field: str, value: str, negated: bool, field_capability: Any | None = None) -> str:
    value = value.strip()
    if value.startswith("(") and value.endswith(")") and not re.search(r"\bOR\b", value, re.IGNORECASE):
        value = value[1:-1].strip()
    if _TEMPLATE_VAR_RE.search(value):
        pattern = _template_value_to_like_pattern(value)
        if pattern in ("", "*") and not negated:
            return ""
        like_op = "NOT LIKE" if negated else "LIKE"
        return f'{field} {like_op} "{pattern}"'
    if "*" in value or "?" in value:
        like_op = "NOT LIKE" if negated else "LIKE"
        return f'{field} {like_op} "{_esql_escape(value)}"'
    comp = _COMPARISON_RE.fullmatch(value)
    if comp:
        op, number = comp.groups()
        rhs = _render_attr_literal(number, field_capability)
        if negated:
            return f"NOT ({field} {op} {rhs})"
        return f"{field} {op} {rhs}"
    op = "!=" if negated else "=="
    return f"{field} {op} {_render_attr_literal(value, field_capability)}"


def _template_value_to_like_pattern(value: str) -> str:
    pattern = _TEMPLATE_VAR_RE.sub("*", value)
    return _esql_escape(pattern)
