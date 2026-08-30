#!/usr/bin/env python3
"""
autofix.py — the "and eventually auto-fix it" half of self-maintaining
APIs. Generates real source patches, not suggestions in a report.

Deliberately covers a SMALL, mechanical subset of rules.py's rules —
5 of the ~19 total. Each one is safe to automate for a specific, stated
reason: the fix is a pure deletion or a 1:1 string swap with no judgment
call involved (no "which model should this migrate to", no "how should
this system prompt be phrased", no "which effort level is right here").
Everything else stays detection-only. Guessing a plausible-looking but
wrong fix is worse than not fixing it — a wrong regex was already the
whole first act of this project (scan.py); a wrong auto-fix rewrites
someone's actual code.

AUTOFIXABLE_RULE_IDS, each with why it's safe:
  - sdk-v1-sampling-params-removed: delete temperature=/top_p=/top_k= —
    the parameter is simply rejected, there's no "correct value" to move
    to, dropping it restores default (and now only legal) behavior.
  - fast-mode-removed-opus-4-7: delete speed="fast" — same shape, the
    parameter value itself is what's rejected.
  - opus5-effort-xhigh-thinking-disabled: delete the thinking={"type":
    "disabled"} kwarg on a qualifying call — falls back to the model's
    default thinking behavior, which is exactly what "drop the explicit
    override" means.
  - beta-files-skills-sdk-shape-change: client.beta.files -> client.files,
    client.beta.skills -> client.skills. A pure attribute-chain rename;
    confirmed identical behavior for callers not depending on the old beta
    response shape.
  - memory-list-managed-agents-header-behavior-change: the literal string
    "managed-agents-2026-04-01" -> "agent-memory-2026-07-22". Confirmed in
    the changelog as a direct 1:1 header replacement, not a shape change.

NOT auto-fixed, and why: manual-thinking-budget and
assistant-prefill-removed need a real choice (which effort level; how to
phrase a system-prompt instruction) that only whoever wrote the original
prompt can make well. model-deprecated-sonnet4-opus4 and
model-retired-opus-4-1 need a choice of which current model to migrate to.
sdk-v1-text-completions-removed is a different API shape, not a parameter
tweak. computer-use-toolset-new-shape and experimental-endpoint-retiring
are explicitly documented as request-shape changes, not string swaps —
renaming the string alone would look fixed while still being broken.

Usage:
    python autofix.py path/to/file.py            # show a unified diff
    python autofix.py path/to/file.py --write     # apply in place
    python autofix.py path/to/project --write     # whole directory
"""

import argparse
import ast
import difflib
import sys
from pathlib import Path

from ast_scan import literal_str, get_call_chain_name, get_kwargs, build_var_dict_index, find_calls

AUTOFIXABLE_RULE_IDS = {
    "sdk-v1-sampling-params-removed",
    "fast-mode-removed-opus-4-7",
    "opus5-effort-xhigh-thinking-disabled",
    "beta-files-skills-sdk-shape-change",
    "memory-list-managed-agents-header-behavior-change",
}

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build"}


class Edit:
    __slots__ = ("start", "end", "replacement", "reason", "rule_id")

    def __init__(self, start, end, replacement, reason, rule_id):
        self.start = start
        self.end = end
        self.replacement = replacement
        self.reason = reason
        self.rule_id = rule_id


def build_line_offsets(text):
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def pos(line_offsets, lineno, col_offset):
    return line_offsets[lineno - 1] + col_offset


def delete_keyword_edit(text, line_offsets, call_node, kw, reason, rule_id):
    """Delete one keyword argument, eating the comma that separated it from
    its neighbor so the result is syntactically clean, not just gap-shaped.

    Heuristic, not a full formatter: if this isn't the last argument, eat
    forward through one comma + the whitespace/newline run after it; if it
    is the last argument, eat backward through the whitespace before it and
    the preceding comma. Good enough for the common single-line and
    one-arg-per-line call shapes; the real check on whether this held is
    "does the file still parse and does the finding disappear on rescan",
    not the heuristic's own logic — see main()'s --write path.
    """
    start = pos(line_offsets, kw.lineno, kw.col_offset)
    end = pos(line_offsets, kw.end_lineno, kw.end_col_offset)

    idx = call_node.keywords.index(kw)
    if idx < len(call_node.keywords) - 1:
        j = end
        while j < len(text) and text[j] in " \t":
            j += 1
        if j < len(text) and text[j] == ",":
            j += 1
            while j < len(text) and text[j] in " \t":
                j += 1
            if j < len(text) and text[j] == "\n":
                j += 1
                while j < len(text) and text[j] in " \t":
                    j += 1
        end = j
    else:
        # Bug found live against a real repo (anthropic-cookbook): this only
        # skipped spaces/tabs backward, so when the preceding keyword was on
        # an earlier line (the common one-kwarg-per-line style), the scan
        # stopped right at the newline instead of crossing it — so it never
        # found the comma it was looking for and left the parameter's own
        # trailing comma orphaned on its own line, producing invalid syntax
        # (caught by the parse-before-write check below, so nothing corrupt
        # was ever written — but the fix was silently skipped). Now mirrors
        # the forward branch above: cross one newline (and any indentation
        # before it), not just same-line whitespace.
        i = start
        while i > 0 and text[i - 1] in " \t":
            i -= 1
        if i > 0 and text[i - 1] == "\n":
            i -= 1
            while i > 0 and text[i - 1] in " \t":
                i -= 1
        if i > 0 and text[i - 1] == ",":
            i -= 1
        start = i

    return Edit(start, end, "", reason, rule_id)


def rename_attr_edit(text, line_offsets, attr_node, reason, rule_id):
    """Delete the `.beta` segment out of `client.beta.files` -> `client.files`.

    An ast.Attribute's own span covers the *whole* `value.attr` expression
    (e.g. all of `client.beta`), not just the `.beta` part — so the text to
    remove is [end of attr_node.value, end of attr_node].
    """
    start = pos(line_offsets, attr_node.value.end_lineno, attr_node.value.end_col_offset)
    end = pos(line_offsets, attr_node.end_lineno, attr_node.end_col_offset)
    return Edit(start, end, "", reason, rule_id)


def replace_string_edit(text, line_offsets, str_node, new_value, reason, rule_id):
    start = pos(line_offsets, str_node.lineno, str_node.col_offset)
    end = pos(line_offsets, str_node.end_lineno, str_node.end_col_offset)
    quote = text[start]  # preserve ' vs " from the original
    return Edit(start, end, f"{quote}{new_value}{quote}", reason, rule_id)


def collect_edits(text):
    tree = ast.parse(text)
    line_offsets = build_line_offsets(text)
    var_dict_index = build_var_dict_index(tree)
    edits = []

    for call, chain in find_calls(tree, ["messages.create"]):
        kwargs = get_kwargs(call, var_dict_index)
        model = literal_str(kwargs.get("model")) if "model" in kwargs else None

        for param in ("temperature", "top_p", "top_k"):
            kw = next((k for k in call.keywords if k.arg == param), None)
            if kw is not None:
                edits.append(delete_keyword_edit(
                    text, line_offsets, call, kw,
                    f"removed {param}= (rejected by Python SDK v1.0)",
                    "sdk-v1-sampling-params-removed",
                ))

        if "thinking" in kwargs and isinstance(kwargs["thinking"], ast.Dict):
            d = kwargs["thinking"]
            keys = [literal_str(k) for k in d.keys]
            type_val = literal_str(next((v for k, v in zip(d.keys, d.values) if literal_str(k) == "type"), None))
            effort = literal_str(kwargs.get("effort")) if "effort" in kwargs else None
            if type_val == "disabled" and effort in ("xhigh", "max") and model and "opus-5" in model:
                kw = next(k for k in call.keywords if k.arg == "thinking")
                edits.append(delete_keyword_edit(
                    text, line_offsets, call, kw,
                    "removed thinking={\"type\": \"disabled\"} (rejected at xhigh/max effort on Opus 5)",
                    "opus5-effort-xhigh-thinking-disabled",
                ))

        speed_kw = next((k for k in call.keywords if k.arg == "speed"), None)
        if speed_kw is not None and literal_str(speed_kw.value) == "fast" and model and "opus-4-7" in model:
            edits.append(delete_keyword_edit(
                text, line_offsets, call, speed_kw,
                "removed speed=\"fast\" (removed for claude-opus-4-7)",
                "fast-mode-removed-opus-4-7",
            ))

    # client.beta.files / client.beta.skills -> client.files / client.skills
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("files", "skills"):
            inner = node.value
            if isinstance(inner, ast.Attribute) and inner.attr == "beta":
                edits.append(rename_attr_edit(
                    text, line_offsets, inner,
                    f"client.beta.{node.attr} -> client.{node.attr}",
                    "beta-files-skills-sdk-shape-change",
                ))

    # "managed-agents-2026-04-01" -> "agent-memory-2026-07-22"
    for node in ast.walk(tree):
        s = literal_str(node)
        if s == "managed-agents-2026-04-01":
            edits.append(replace_string_edit(
                text, line_offsets, node, "agent-memory-2026-07-22",
                "managed-agents-2026-04-01 -> agent-memory-2026-07-22",
                "memory-list-managed-agents-header-behavior-change",
            ))

    return edits


def apply_edits(text, edits):
    # Apply from the end of the file backward so earlier offsets stay valid.
    edits = sorted(edits, key=lambda e: e.start, reverse=True)
    out = text
    for e in edits:
        out = out[: e.start] + e.replacement + out[e.end :]
    return out


def fix_file(path, write=False):
    text = path.read_text()
    try:
        edits = collect_edits(text)
    except SyntaxError:
        return None  # not valid Python — same "skip quietly" policy as ast_scan.py

    if not edits:
        return None

    patched = apply_edits(text, edits)

    # Verify before ever trusting this patch: it must still parse. A patch
    # that doesn't compile is worse than no patch — refuse it rather than
    # write broken code, even though that means silently dropping a fix
    # opportunity here (logged to stderr, not applied).
    try:
        ast.parse(patched)
    except SyntaxError as e:
        print(f"REFUSED (patch would not parse): {path}: {e}", file=sys.stderr)
        return None

    if write:
        path.write_text(patched)

    return text, patched, edits


def scan_dir(root):
    root = Path(root)
    if root.is_file():
        return [root]
    return [p for p in root.rglob("*.py") if not any(part in SKIP_DIRS for part in p.parts)]


def main():
    parser = argparse.ArgumentParser(description="Generate/apply auto-fixes for the mechanical subset of rules.py.")
    parser.add_argument("path")
    parser.add_argument("--write", action="store_true", help="Apply in place instead of printing a diff")
    args = parser.parse_args()

    total_edits = 0
    total_files = 0
    for path in scan_dir(args.path):
        result = fix_file(path, write=args.write)
        if result is None:
            continue
        original, patched, edits = result
        total_files += 1
        total_edits += len(edits)
        if not args.write:
            diff = difflib.unified_diff(
                original.splitlines(keepends=True),
                patched.splitlines(keepends=True),
                fromfile=str(path), tofile=str(path) + " (patched)",
            )
            sys.stdout.writelines(diff)
        for e in edits:
            print(f"  [{e.rule_id}] {path}: {e.reason}", file=sys.stderr)

    print(f"\n{total_edits} edit(s) across {total_files} file(s)"
          f"{' (written)' if args.write else ' (dry run — pass --write to apply)'}", file=sys.stderr)


if __name__ == "__main__":
    main()
