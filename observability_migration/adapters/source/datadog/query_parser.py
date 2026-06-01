# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Parser for Datadog metric query strings and formula expressions.

Metric query format:
    <space_agg>:<metric>{<scope>} [by {<tags>}] [.function(args)]*

Examples:
    avg:system.cpu.user{*}
    sum:trace.flask.request.hits{service:web} by {resource_name}
    avg:system.disk.free{host:web*,!env:staging} by {host}.rollup(avg, 60)

Formula format:
    Arithmetic expressions with function calls over named query references.
    Example: per_second(query1) / query2 * 100
"""

from __future__ import annotations

import re
from typing import Any

from .models import (
    FormulaBinOp,
    FormulaExpression,
    FormulaFuncCall,
    FormulaNumber,
    FormulaRef,
    FormulaString,
    FormulaUnary,
    FunctionCall,
    MetricQuery,
    ScopeBoolOp,
    TagFilter,
)
from .parser_results import (
    ParserDiagnostic,
    ParserDiagnosticCode,
    ParserResult,
)


class ParseError(Exception):
    pass


# ---------------------------------------------------------------------------
# Metric query parser
# ---------------------------------------------------------------------------

VALID_AGGREGATORS = {"avg", "sum", "min", "max", "count", "last", "p50", "p75", "p90", "p95", "p99"}

_TEMPLATE_VAR_RE = re.compile(r"\$\w+(?:\.\w+)*")


def parse_metric_query(raw: str) -> MetricQuery:
    """Parse a Datadog metric query string into a MetricQuery model.

    Handles template variables ($var), wildcards, negated tags, function chains,
    and as_count/as_rate modifiers.
    """
    if not raw or not isinstance(raw, str):
        raise ParseError("empty metric query")
    raw = raw.strip()
    if not raw:
        raise ParseError("empty metric query")

    colon_pos = _find_aggregator_colon(raw)
    if colon_pos < 0:
        raise ParseError(f"no aggregator colon found in: {raw}")

    space_agg = raw[:colon_pos].strip().lower()
    if space_agg not in VALID_AGGREGATORS:
        raise ParseError(f"unsupported aggregator '{space_agg}'; valid: {', '.join(sorted(VALID_AGGREGATORS))}")
    rest = raw[colon_pos + 1:].strip()

    brace_pos = rest.find("{")
    if brace_pos < 0:
        return MetricQuery(
            raw=raw, space_agg=space_agg, metric=rest.strip(),
        )

    metric = rest[:brace_pos].strip()
    rest = rest[brace_pos:]

    close_brace = _find_matching_brace(rest, 0)
    if close_brace < 0:
        raise ParseError(f"unmatched {{ in scope: {rest}")

    scope_str = rest[1:close_brace].strip()
    rest = rest[close_brace + 1:].strip()

    scope = _parse_scope(scope_str)

    group_by: list[str] = []
    by_match = re.match(r"by\s*\{", rest, re.IGNORECASE)
    if by_match:
        rest = rest[by_match.start():]
        gb_brace = rest.index("{")
        gb_close = _find_matching_brace(rest, gb_brace)
        if gb_close < 0:
            raise ParseError(f"unmatched {{ in group by: {rest}")
        gb_str = rest[gb_brace + 1:gb_close].strip()
        group_by = [t.strip() for t in gb_str.split(",") if t.strip()]
        rest = rest[gb_close + 1:].strip()

    functions, as_rate, as_count, remainder = _parse_function_chain(rest)
    if remainder.strip():
        raise ParseError(f"unexpected trailing tokens in metric query: {remainder}")

    return MetricQuery(
        raw=raw,
        space_agg=space_agg,
        metric=metric,
        scope=scope,
        group_by=group_by,
        functions=functions,
        as_rate=as_rate,
        as_count=as_count,
    )


def parse_metric_query_result(raw: str) -> ParserResult:
    """Parse metric query with structured diagnostics instead of exceptions."""
    try:
        query = parse_metric_query(raw)
    except ParseError as exc:
        message = str(exc)
        code = (
            ParserDiagnosticCode.METRIC_TRAILING_TOKENS
            if "trailing tokens" in message.lower()
            else ParserDiagnosticCode.METRIC_PARSE_ERROR
        )
        return ParserResult(
            value=None,
            diagnostics=[ParserDiagnostic(code=code.value, message=message)],
            degraded=True,
            lossless=False,
        )
    return ParserResult(value=query)


def _find_aggregator_colon(text: str) -> int:
    """Find the colon that separates aggregator from metric name.

    Must distinguish from colons inside braces (scope filters like host:foo).
    The aggregator colon is always the first colon before any opening brace.
    """
    brace_depth = 0
    paren_depth = 0
    for i, ch in enumerate(text):
        if ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
        elif ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
        elif ch == ":" and brace_depth == 0 and paren_depth == 0:
            return i
    return -1


def _find_matching_brace(text: str, start: int) -> int:
    """Find the matching closing brace for an opening brace at `start`."""
    if start >= len(text) or text[start] != "{":
        return -1
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _parse_scope(scope_str: str) -> list[Any]:
    """Parse the contents of a scope: `host:web01,!env:staging,role:*`."""
    if not scope_str or scope_str == "*":
        return []

    if re.search(r"\bAND\b|\bOR\b", scope_str, re.IGNORECASE):
        return _parse_boolean_scope(scope_str)

    filters: list[Any] = []
    for part in _split_scope(scope_str):
        part = part.strip()
        if not part or part == "*":
            continue
        filters.append(_parse_single_filter(part))
    return filters


def _split_scope(scope_str: str) -> list[str]:
    """Split scope on commas, respecting quoted strings and parentheses.

    Parentheses must be honored so a Datadog ``key IN (a, b, c)`` list is not
    split on its internal commas (which would silently drop the filter).
    """
    parts: list[str] = []
    current: list[str] = []
    in_quote = False
    quote_char = ""
    depth = 0
    for ch in scope_str:
        if ch in ('"', "'") and not in_quote:
            in_quote = True
            quote_char = ch
            current.append(ch)
        elif ch == quote_char and in_quote:
            in_quote = False
            current.append(ch)
        elif not in_quote and ch == "(":
            depth += 1
            current.append(ch)
        elif not in_quote and ch == ")":
            depth = max(depth - 1, 0)
            current.append(ch)
        elif ch == "," and not in_quote and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def _parse_boolean_scope(scope_str: str) -> list[Any]:
    filters: list[Any] = []
    for part in _split_on_keyword(scope_str, "AND"):
        part = part.strip().strip(",").strip()
        if not part or part == "*":
            continue
        if part.startswith("(") and part.endswith(")"):
            part = part[1:-1].strip()
        if not part:
            continue

        if re.search(r"\bOR\b", part, re.IGNORECASE):
            options = [p.strip() for p in _split_on_keyword(part, "OR") if p.strip()]
            parsed = []
            for option in options:
                if (
                    ":" not in option
                    and not option.startswith(("!", "-"))
                    and not _TEMPLATE_VAR_RE.fullmatch(option)
                ):
                    continue
                parsed.append(_parse_single_filter(option))
            if parsed and all(
                item.key == parsed[0].key and item.negated == parsed[0].negated
                for item in parsed
            ):
                filters.append(
                    TagFilter(
                        key=parsed[0].key,
                        value="|".join(item.value for item in parsed),
                        negated=parsed[0].negated,
                    )
                )
            elif parsed:
                filters.append(ScopeBoolOp(op="OR", children=parsed))
            continue

        filters.append(_parse_single_filter(part))
    return filters


def _split_on_keyword(text: str, keyword: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_quote = False
    quote_char = ""
    i = 0
    marker = keyword.upper()
    marker_len = len(marker)
    text_upper = text.upper()

    while i < len(text):
        ch = text[i]
        if ch in ('"', "'") and not in_quote:
            in_quote = True
            quote_char = ch
            current.append(ch)
            i += 1
            continue
        if in_quote and ch == quote_char:
            in_quote = False
            current.append(ch)
            i += 1
            continue
        if not in_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(depth - 1, 0)
            if depth == 0 and text_upper[i:i + marker_len] == marker:
                prev = text[i - 1] if i > 0 else ""
                nxt = text[i + marker_len] if i + marker_len < len(text) else ""
                prev_ok = not prev or not (prev.isalnum() or prev == "_")
                next_ok = not nxt or not (nxt.isalnum() or nxt == "_")
                if prev_ok and next_ok:
                    parts.append("".join(current))
                    current = []
                    i += marker_len
                    continue
        current.append(ch)
        i += 1

    if current:
        parts.append("".join(current))
    return parts


_IN_LIST_RE = re.compile(
    r"^(?P<key>[^:!\s][^:]*?)\s+(?P<op>NOT\s+IN|IN)\s*\((?P<members>[^)]*)\)\s*$",
    re.IGNORECASE,
)


def _parse_single_filter(part: str) -> TagFilter:
    in_match = _IN_LIST_RE.match(part.strip())
    if in_match:
        members = [
            m.strip().strip("'\"")
            for m in in_match.group("members").split(",")
            if m.strip()
        ]
        return TagFilter(
            key=in_match.group("key").strip(),
            value="|".join(members),
            negated=in_match.group("op").upper().startswith("NOT"),
            is_in_list=True,
        )

    negated = False
    if part.startswith("!") or part.startswith("-"):
        negated = True
        part = part[1:]

    colon_pos = part.find(":")
    if colon_pos < 0:
        return TagFilter(key=part, value="*", negated=negated)

    key = part[:colon_pos].strip()
    value = part[colon_pos + 1:].strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1]

    return TagFilter(key=key, value=value, negated=negated)


def _parse_function_chain(text: str) -> tuple[list[FunctionCall], bool, bool, str]:
    """Parse `.rollup(avg, 60).fill(zero).as_count()` chains."""
    functions: list[FunctionCall] = []
    as_rate = False
    as_count = False
    rest = text.strip()

    while rest.startswith("."):
        rest = rest[1:]
        match = re.match(r"(\w+)\s*\(", rest)
        if not match:
            break

        fname = match.group(1)
        rest = rest[match.end():]

        paren_close = _find_matching_paren(rest)
        if paren_close < 0:
            raise ParseError(f"unclosed parenthesis in function call '.{fname}(...'")


        args_str = rest[:paren_close].strip()
        rest = rest[paren_close + 1:].strip()

        if fname == "as_rate":
            as_rate = True
            continue
        if fname == "as_count":
            as_count = True
            continue

        args = _parse_function_args(args_str)
        functions.append(FunctionCall(name=fname, args=args))

    return functions, as_rate, as_count, rest


def _find_matching_paren(text: str) -> int:
    """Find the closing paren matching an already-consumed opening paren."""
    depth = 1
    in_quote = False
    quote_char = ""
    for i, ch in enumerate(text):
        if ch in ('"', "'") and not in_quote:
            in_quote = True
            quote_char = ch
        elif ch == quote_char and in_quote:
            in_quote = False
        elif not in_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
    return -1


def _parse_function_args(args_str: str) -> list[Any]:
    if not args_str:
        return []
    parts = _split_args(args_str)
    result: list[Any] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if (p.startswith('"') and p.endswith('"')) or (p.startswith("'") and p.endswith("'")):
            result.append(p[1:-1])
        else:
            try:
                if "." in p:
                    result.append(float(p))
                else:
                    result.append(int(p))
            except ValueError:
                result.append(p)
    return result


def _split_args(text: str) -> list[str]:
    """Split function arguments on commas, respecting parens, braces, and quotes."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    brace_depth = 0
    in_quote = False
    quote_char = ""
    for ch in text:
        if ch in ('"', "'") and not in_quote:
            in_quote = True
            quote_char = ch
            current.append(ch)
        elif ch == quote_char and in_quote:
            in_quote = False
            current.append(ch)
        elif not in_quote:
            if ch == "(":
                depth += 1
                current.append(ch)
            elif ch == ")":
                depth -= 1
                current.append(ch)
            elif ch == "{":
                brace_depth += 1
                current.append(ch)
            elif ch == "}":
                brace_depth -= 1
                current.append(ch)
            elif ch == "," and depth == 0 and brace_depth == 0:
                parts.append("".join(current))
                current = []
            else:
                current.append(ch)
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


# ---------------------------------------------------------------------------
# Legacy query string parser
# ---------------------------------------------------------------------------

_FORMULA_FUNC_RE = re.compile(
    r"^(timeshift|top|anomalies|forecast|outliers|"
    r"abs|log2|log10|cumsum|integral|"
    r"per_second|per_minute|per_hour|"
    r"diff|derivative|monotonic_diff|"
    r"ewma_\d+|median_\d+|"
    r"moving_rollup|autosmooth|robust_trend|trend_line|piecewise_constant|"
    r"clamp_min|clamp_max|cutoff_min|cutoff_max|"
    r"default_zero|exclude_null|"
    r"count_not_null|count_nonzero)\s*\(",
    re.IGNORECASE,
)


def parse_legacy_query(raw: str) -> tuple[MetricQuery | None, list[FunctionCall]]:
    """Parse a legacy `q` string that may wrap a metric query in formula-level functions.

    Returns (inner_metric_query, outer_functions).
    If the string contains no recognizable metric query, returns (None, []).
    """
    raw = raw.strip()
    outer_fns: list[FunctionCall] = []
    inner = raw

    while True:
        m = _FORMULA_FUNC_RE.match(inner)
        if not m:
            break
        fname = m.group(1)
        inner = inner[m.end():]
        close = _find_matching_paren(inner)
        if close < 0:
            break
        body = inner[:close]
        inner_parts = _split_args(body)
        if not inner_parts:
            break
        inner = inner_parts[0].strip()
        extra_args = [_coerce_arg(a.strip()) for a in inner_parts[1:]]
        outer_fns.append(FunctionCall(name=fname, args=extra_args))

    try:
        mq = parse_metric_query(inner)
        return mq, outer_fns
    except ParseError:
        return None, []


def _coerce_arg(val: str) -> Any:
    if val.startswith(("'", '"')) and len(val) >= 2 and val[-1] == val[0]:
        return val[1:-1]
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


# ---------------------------------------------------------------------------
# Formula expression parser (for the `formula` field in modern JSON)
# ---------------------------------------------------------------------------

class _FormulaTokenizer:
    """Tokenize a formula string into a flat list of tokens."""

    _TOKENS = re.compile(
        r"""
        (\d+(?:\.\d+)?)       # number
        |([a-zA-Z_]\w*)       # identifier (query ref or function name)
        |('(?:[^'\\]|\\.)*')  # single-quoted string
        |("(?:[^"\\]|\\.)*")  # double-quoted string
        |([+\-*/])            # operator
        |([(),])              # punctuation
        |\s+                  # whitespace (skip)
        """,
        re.VERBOSE,
    )

    def tokenize(self, text: str) -> list[tuple[str, str]]:
        tokens: list[tuple[str, str]] = []
        pos = 0
        while pos < len(text):
            m = self._TOKENS.match(text, pos)
            if not m:
                raise ParseError(f"unexpected character at position {pos}: {text[pos:]!r}")
            pos = m.end()
            if m.group(1) is not None:
                tokens.append(("NUM", m.group(1)))
            elif m.group(2) is not None:
                tokens.append(("IDENT", m.group(2)))
            elif m.group(3) is not None:
                tokens.append(("STR", m.group(3)[1:-1]))
            elif m.group(4) is not None:
                tokens.append(("STR", m.group(4)[1:-1]))
            elif m.group(5) is not None:
                tokens.append(("OP", m.group(5)))
            elif m.group(6) is not None:
                tokens.append(("PUNCT", m.group(6)))
        return tokens


class _FormulaParser:
    """Recursive-descent parser for formula expressions."""

    def __init__(self, tokens: list[tuple[str, str]]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> tuple[str, str] | None:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def consume(self) -> tuple[str, str]:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, ttype: str, tval: str | None = None) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise ParseError(f"expected {ttype} {tval!r}, got EOF")
        if tok[0] != ttype or (tval is not None and tok[1] != tval):
            raise ParseError(f"expected {ttype} {tval!r}, got {tok}")
        return self.consume()

    def parse(self) -> Any:
        result = self.expr()
        if self.pos < len(self.tokens):
            raise ParseError(f"unexpected tokens after expression: {self.tokens[self.pos:]}")
        return result

    def expr(self) -> Any:
        left = self.term()
        while True:
            tok = self.peek()
            if not tok or tok[0] != "OP" or tok[1] not in ("+", "-"):
                break
            op = self.consume()[1]
            right = self.term()
            left = FormulaBinOp(op=op, left=left, right=right)
        return left

    def term(self) -> Any:
        left = self.unary()
        while True:
            tok = self.peek()
            if not tok or tok[0] != "OP" or tok[1] not in ("*", "/"):
                break
            op = self.consume()[1]
            right = self.unary()
            left = FormulaBinOp(op=op, left=left, right=right)
        return left

    def unary(self) -> Any:
        if self.peek() and self.peek() == ("OP", "-"):
            self.consume()
            operand = self.atom()
            return FormulaUnary(op="-", operand=operand)
        return self.atom()

    def atom(self) -> Any:
        tok = self.peek()
        if tok is None:
            raise ParseError("unexpected end of formula")

        if tok[0] == "NUM":
            self.consume()
            return FormulaNumber(value=float(tok[1]))

        if tok[0] == "STR":
            self.consume()
            return FormulaString(value=tok[1])

        if tok[0] == "IDENT":
            name = self.consume()[1]
            if self.peek() and self.peek() == ("PUNCT", "("):
                self.consume()
                args: list[Any] = []
                if not (self.peek() and self.peek() == ("PUNCT", ")")):
                    args.append(self.expr())
                    while self.peek() and self.peek() == ("PUNCT", ","):
                        self.consume()
                        args.append(self.expr())
                self.expect("PUNCT", ")")
                return FormulaFuncCall(name=name, args=args)
            return FormulaRef(name=name)

        if tok == ("PUNCT", "("):
            self.consume()
            inner = self.expr()
            self.expect("PUNCT", ")")
            return inner

        raise ParseError(f"unexpected token: {tok}")


def parse_formula(raw: str) -> FormulaExpression:
    """Parse a Datadog formula expression string."""
    raw = raw.strip()
    if not raw:
        return FormulaExpression(raw=raw)

    tokenizer = _FormulaTokenizer()
    tokens = tokenizer.tokenize(raw)
    if not tokens:
        return FormulaExpression(raw=raw)

    parser = _FormulaParser(tokens)
    ast = parser.parse()
    return FormulaExpression(raw=raw, ast=ast)


def parse_formula_result(raw: str) -> ParserResult:
    """Parse formula with structured diagnostics instead of exceptions."""
    try:
        expression = parse_formula(raw)
    except ParseError as exc:
        return ParserResult(
            value=None,
            diagnostics=[
                ParserDiagnostic(
                    code=ParserDiagnosticCode.FORMULA_PARSE_ERROR.value,
                    message=str(exc),
                )
            ],
            degraded=True,
            lossless=False,
        )
    return ParserResult(value=expression)
