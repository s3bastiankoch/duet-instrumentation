from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re


class InstrumentationError(RuntimeError):
    """Raised when we cannot apply instrumentation safely."""


MARKER_COMMENT_PREFIX = "// otel-agent: perf-marker"


@dataclass(frozen=True)
class InsertPlan:
    path: str
    insert_after: str
    occurrence: int
    label: str
    reason: str
    # Optional: LLM-provided anchor for endSpan placement (insert BEFORE this line).
    # If None, auto-detect by finding the closing brace of the surrounding block.
    end_before: str | None = None
    end_occurrence: int = 1
    # Extended context for richer span metadata (OTel semantic conventions).
    function_name: str | None = None  # maps to OTel `code.function`
    operation_type: str | None = None  # e.g., "database", "http", "computation"
    attributes: dict[str, str] | None = None  # additional custom attributes


def _find_nth(haystack: str, needle: str, n: int) -> int:
    if n <= 0:
        n = 1
    start = -1
    for _ in range(n):
        start = haystack.find(needle, start + 1)
        if start == -1:
            return -1
    return start


def _find_nth_ws_insensitive(haystack: str, needle: str, n: int) -> int:
    """
    Find the nth occurrence of `needle` in `haystack`, but treat whitespace differences
    (spaces/tabs/newlines and runs thereof) as equivalent.
    Returns the start index of the match in the original haystack, or -1.
    """
    if n <= 0:
        n = 1
    # Split the needle into non-whitespace tokens; join them with a flexible whitespace matcher.
    tokens = re.split(r"\s+", needle.strip())
    if not tokens or tokens == [""]:
        return -1
    pattern = r"\s+".join(re.escape(t) for t in tokens)
    # Allow indentation differences at the beginning of the snippet as well.
    # (This is naturally handled if the first token is after indentation; still, \s* is cheap.)
    pattern = r"\s*" + pattern
    matches = list(re.finditer(pattern, haystack, flags=re.MULTILINE | re.DOTALL))
    if len(matches) < n:
        return -1
    return matches[n - 1].start()


def _indent_of_line(s: str, idx: int) -> str:
    line_start = s.rfind("\n", 0, idx) + 1
    line = s[line_start : s.find("\n", line_start) if "\n" in s[line_start:] else len(s)]
    return line[: len(line) - len(line.lstrip(" \t"))]


def _span_name(path: str, label: str) -> str:
    """
    Create a stable, short span name to avoid collisions within a request.
    """

    h = hashlib.sha1(f"{path}\n{label}".encode("utf-8")).hexdigest()[:8]
    safe_label = label.replace('"', "'").replace("\n", " ").strip()
    if not safe_label:
        safe_label = "perf-marker"
    return f"otel-agent:{safe_label}:{h}"


def _find_block_end(ts: str, *, open_brace_idx: int) -> int | None:
    """
    Very lightweight brace matching to find the closing '}' for a block.
    Returns the index of the closing brace character, or None.
    """

    depth = 0
    for i in range(open_brace_idx, len(ts)):
        c = ts[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _is_ident_char(c: str) -> bool:
    return c.isalnum() or c == "_" or c == "$"


def _find_early_exit_line_starts(
    ts: str,
    *,
    block_open_idx: int,
    block_close_idx: int,
) -> list[int]:
    """
    Find line-start indices for return/throw statements within [block_open_idx, block_close_idx),
    excluding ones inside nested function-like blocks (best-effort).

    This is a lightweight scanner (not a full parser). For maximum correctness use AST mode.
    """

    in_line_comment = False
    in_block_comment = False
    in_squote = False
    in_dquote = False
    in_template = False
    escape = False

    pending_function_block = False
    brace_kind_stack: list[bool] = []  # True => this '{' opened a nested function-like block
    func_depth = 0

    out: set[int] = set()
    i = block_open_idx
    end = min(len(ts), block_close_idx)

    def at_line_start(idx: int) -> int:
        return ts.rfind("\n", 0, idx) + 1

    while i < end:
        c = ts[i]

        if in_line_comment:
            if c == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if c == "*" and i + 1 < end and ts[i + 1] == "/":
                in_block_comment = False
                i += 2
            else:
                i += 1
            continue

        if in_squote:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == "'":
                in_squote = False
            i += 1
            continue

        if in_dquote:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_dquote = False
            i += 1
            continue

        if in_template:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == "`":
                in_template = False
            i += 1
            continue

        # Start comments
        if c == "/" and i + 1 < end:
            nxt = ts[i + 1]
            if nxt == "/":
                in_line_comment = True
                i += 2
                continue
            if nxt == "*":
                in_block_comment = True
                i += 2
                continue

        # Start strings
        if c == "'":
            in_squote = True
            i += 1
            continue
        if c == '"':
            in_dquote = True
            i += 1
            continue
        if c == "`":
            in_template = True
            i += 1
            continue

        # Detect function-like starts (best-effort)
        # - "function" keyword
        # - "=> {" arrow function block
        if c == "f" and ts.startswith("function", i):
            prev = ts[i - 1] if i > 0 else " "
            nxt = ts[i + len("function")] if i + len("function") < end else " "
            if not _is_ident_char(prev) and not _is_ident_char(nxt):
                pending_function_block = True
        if c == "=" and i + 1 < end and ts[i + 1] == ">":
            pending_function_block = True

        # Track braces and nested function blocks
        if c == "{":
            brace_kind_stack.append(pending_function_block)
            if pending_function_block:
                func_depth += 1
            pending_function_block = False
        elif c == "}":
            if brace_kind_stack:
                was_func = brace_kind_stack.pop()
                if was_func and func_depth > 0:
                    func_depth -= 1

        # Look for return/throw tokens (skip inside nested functions)
        if func_depth == 0:
            if c in ("r", "t"):
                if ts.startswith("return", i):
                    prev = ts[i - 1] if i > 0 else " "
                    nxt = ts[i + len("return")] if i + len("return") < end else " "
                    if not _is_ident_char(prev) and not _is_ident_char(nxt):
                        out.add(at_line_start(i))
                elif ts.startswith("throw", i):
                    prev = ts[i - 1] if i > 0 else " "
                    nxt = ts[i + len("throw")] if i + len("throw") < end else " "
                    if not _is_ident_char(prev) and not _is_ident_char(nxt):
                        out.add(at_line_start(i))

        i += 1

    return sorted(out)


def _find_early_exit_line_starts_ast(
    ts: str,
    *,
    block_open_idx: int,
    block_close_idx: int,
) -> list[int]:
    """
    AST-based return/throw detection using tree-sitter (experimental).
    Requires `tree_sitter_languages` at runtime.
    """

    try:
        from tree_sitter_languages import get_parser  # type: ignore
    except Exception as e:  # pragma: no cover
        raise InstrumentationError(
            "AST instrumentation requires `tree_sitter_languages`. "
            "Install it and retry (experimental mode)."
        ) from e

    # Build char<->byte mappings for UTF-8
    char_to_byte: list[int] = [0] * (len(ts) + 1)
    b_off = 0
    for i, ch in enumerate(ts):
        char_to_byte[i] = b_off
        b_off += len(ch.encode("utf-8"))
    char_to_byte[len(ts)] = b_off

    byte_to_char: list[int] = [0] * (b_off + 1)
    b_off = 0
    for i, ch in enumerate(ts):
        chb = ch.encode("utf-8")
        for _ in range(len(chb)):
            byte_to_char[b_off] = i
            b_off += 1
    byte_to_char[b_off] = len(ts)

    open_b = char_to_byte[block_open_idx]
    close_b = char_to_byte[block_close_idx]

    # tree_sitter_languages versions differ in which grammars are exposed.
    # Try TypeScript first, then TSX.
    parser = None
    last_err: Exception | None = None
    for lang in ("typescript", "tsx"):
        try:
            parser = get_parser(lang)
            break
        except Exception as e:  # pragma: no cover
            last_err = e
            parser = None

    if parser is None:  # pragma: no cover
        raise InstrumentationError(
            "AST instrumentation could not load a TypeScript grammar from `tree_sitter_languages`. "
            "Tried: 'typescript', 'tsx'. Use --instrument-mode text or install a version that includes TS support."
        ) from last_err
    tree = parser.parse(ts.encode("utf-8"))

    function_like = {
        "function_declaration",
        "function",
        "arrow_function",
        "method_definition",
        "generator_function",
    }

    out: set[int] = set()

    def walk(node) -> None:
        # Range prune
        if node.end_byte < open_b or node.start_byte >= close_b:
            return

        # Skip nested function bodies within the block (best-effort).
        if (
            node.type in function_like
            and node.start_byte >= open_b
            and node.end_byte <= close_b
            and node.start_byte > open_b
        ):
            return

        if node.type in {"return_statement", "throw_statement"}:
            if node.start_byte >= open_b and node.start_byte < close_b:
                char_idx = byte_to_char[node.start_byte]
                ls = ts.rfind("\n", 0, char_idx) + 1
                out.add(ls)

        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return sorted(out)


def _build_span_options(plan: InsertPlan) -> str | None:
    """
    Build a TypeScript options object for startSpan with OTel semantic conventions.
    
    Returns None if no extended context is available, otherwise returns
    a string like: { functionName: "foo", operationType: "database", attributes: {...} }
    """
    parts: list[str] = []
    
    if plan.function_name:
        # Escape quotes in function name
        safe_fn = plan.function_name.replace('"', '\\"')
        parts.append(f'functionName: "{safe_fn}"')
    
    if plan.operation_type:
        safe_op = plan.operation_type.replace('"', '\\"')
        parts.append(f'operationType: "{safe_op}"')
    
    if plan.attributes:
        # Build attributes object
        attr_parts = []
        for k, v in plan.attributes.items():
            safe_k = k.replace('"', '\\"')
            safe_v = v.replace('"', '\\"')
            attr_parts.append(f'"{safe_k}": "{safe_v}"')
        if attr_parts:
            parts.append("attributes: { " + ", ".join(attr_parts) + " }")
    
    if not parts:
        return None
    
    return "{ " + ", ".join(parts) + " }"


def apply_perf_markers(
    ts: str,
    plans: list[InsertPlan],
    *,
    strict: bool = True,
    instrument_mode: str = "text",
) -> tuple[str, int]:
    """
    Insert span-based markers after each anchor occurrence:

    - startSpan(name) near the change point
    - endSpan(name) at the end of the surrounding block (or at LLM-provided anchor)

    Returns (new_content, markers_inserted).
    """

    out = ts
    inserted = 0

    # Apply in order; after each insertion, future indices shift.
    for p in plans:
        span_name = _span_name(p.path, p.label)
        # Prevent duplicates if re-run.
        if f'startSpan("{span_name}")' in out or f"startSpan('{span_name}')" in out:
            continue

        anchor_idx = _find_nth(out, p.insert_after, p.occurrence)
        if anchor_idx == -1:
            if strict:
                raise InstrumentationError(f"Anchor not found for {p.path}: {p.insert_after!r}")
            continue

        after_anchor_idx = anchor_idx + len(p.insert_after)
        # Insert at end of the anchor line.
        line_end = out.find("\n", after_anchor_idx)
        if line_end == -1:
            line_end = len(out)

        indent = _indent_of_line(out, anchor_idx)

        # Build startSpan options if we have extended context.
        start_options = _build_span_options(p)

        # Start span right after the anchor line.
        if start_options:
            start_snippet = (
                f"\n{indent}{MARKER_COMMENT_PREFIX} \"{p.label}\" — {p.reason}\n"
                f'{indent}startSpan("{span_name}", {start_options});\n'
            )
        else:
            start_snippet = (
                f"\n{indent}{MARKER_COMMENT_PREFIX} \"{p.label}\" — {p.reason}\n"
                f'{indent}startSpan("{span_name}");\n'
            )
        out = out[:line_end] + start_snippet + out[line_end:]

        end_snippet = f'{indent}endSpan("{span_name}");\n'

        # Determine where to insert endSpan.
        # Priority: 1) LLM-provided end_before anchor, 2) auto-detect block end.
        if p.end_before:
            # Use LLM-provided anchor: insert endSpan BEFORE the matched line.
            end_anchor_idx = _find_nth(out, p.end_before, p.end_occurrence)
            if end_anchor_idx == -1:
                # Robust fallback: allow whitespace differences (LLMs sometimes reflow snippets).
                end_anchor_idx = _find_nth_ws_insensitive(out, p.end_before, p.end_occurrence)
            if end_anchor_idx != -1:
                # Find the start of the line containing the end anchor.
                end_line_start = out.rfind("\n", 0, end_anchor_idx) + 1
                end_indent = _indent_of_line(out, end_line_start)

                # Also handle early exits between start and end anchors.
                start_insert_end = line_end + len(start_snippet)
                if instrument_mode == "ast":
                    early_exit_starts = _find_early_exit_line_starts_ast(
                        out, block_open_idx=start_insert_end, block_close_idx=end_line_start
                    )
                else:
                    early_exit_starts = _find_early_exit_line_starts(
                        out, block_open_idx=start_insert_end, block_close_idx=end_line_start
                    )
                for ls in reversed(early_exit_starts):
                    if out[max(0, ls - 200) : ls].find(f'endSpan("{span_name}")') != -1:
                        continue
                    exit_indent = _indent_of_line(out, ls)
                    out = out[:ls] + f'{exit_indent}endSpan("{span_name}");\n' + out[ls:]

                # Re-find end anchor after early exit insertions (indices shifted).
                end_anchor_idx = _find_nth(out, p.end_before, p.end_occurrence)
                if end_anchor_idx == -1:
                    end_anchor_idx = _find_nth_ws_insensitive(out, p.end_before, p.end_occurrence)
                if end_anchor_idx != -1:
                    end_line_start = out.rfind("\n", 0, end_anchor_idx) + 1
                    end_indent = _indent_of_line(out, end_line_start)
                    out = out[:end_line_start] + f"{end_indent}{end_snippet}" + out[end_line_start:]
                    inserted += 1
                    continue
            # Fallback to auto-detection if end anchor not found.
            #
            # We keep strictness for the *start* anchor (insert_after), because inserting
            # a span start at the wrong place is dangerous. For the end marker, however,
            # we can safely fall back to block-end auto-detection (or immediate endSpan)
            # without aborting the whole instrumentation run.
            #
            # So even in strict mode, we fall through to auto-detection here.

        # Auto-detect: end span at end of surrounding block (function/loop/etc.).
        # We look for an opening '{' shortly after the anchor; if not found, we end immediately.
        search_start = anchor_idx
        search_end = min(len(out), line_end + len(start_snippet) + 200)
        open_idx = out.find("{", search_start, search_end)

        if open_idx != -1:
            close_idx = _find_block_end(out, open_brace_idx=open_idx)
            if close_idx is not None:
                # Insert endSpan on early exits (return/throw) inside the block.
                close_line_start = out.rfind("\n", 0, close_idx) + 1
                if instrument_mode == "ast":
                    early_exit_starts = _find_early_exit_line_starts_ast(
                        out, block_open_idx=open_idx, block_close_idx=close_line_start
                    )
                else:
                    early_exit_starts = _find_early_exit_line_starts(
                        out, block_open_idx=open_idx, block_close_idx=close_line_start
                    )
                for ls in reversed(early_exit_starts):
                    # Avoid inserting multiple times for the same span on the same line.
                    if out[max(0, ls - 200) : ls].find(f'endSpan("{span_name}")') != -1:
                        continue
                    exit_indent = _indent_of_line(out, ls)
                    out = out[:ls] + f'{exit_indent}endSpan("{span_name}");\n' + out[ls:]

                # Insert before the line that contains the closing brace for a cleaner block end.
                close_indent = _indent_of_line(out, close_line_start)
                out = out[:close_line_start] + f"{close_indent}{end_snippet}" + out[close_line_start:]
            else:
                # Fallback: end right after start
                out = out[: line_end + len(start_snippet)] + f"{indent}{end_snippet}" + out[line_end + len(start_snippet) :]
        else:
            # No obvious block: end right after start
            out = out[: line_end + len(start_snippet)] + f"{indent}{end_snippet}" + out[line_end + len(start_snippet) :]

        inserted += 1

    return out, inserted

