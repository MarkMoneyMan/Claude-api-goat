#!/usr/bin/env python3
"""
ast_scan.py — v2 of the scanner, rebuilt around real code understanding
instead of regex-over-the-whole-file.

Why this exists: testing scan.py (v1, regex-based) against 5 real public
repos found 1,423 "findings" in litellm alone, and spot-checks showed most
were comments, docstrings, and unrelated string literals — not live code
at risk. This version walks the actual Python syntax tree, so it only
ever looks at real `client.messages.create(...)` call sites, and reads
the `model=` argument from *that same call* instead of guessing from
whatever model name happens to appear anywhere in the file.

Trade-off, stated plainly: this only catches calls written in a fairly
direct style (`client.messages.create(model=..., ...)`). Code that builds
the call through heavy indirection (kwargs assembled in a dict and
splatted in with `**kwargs`, or config-driven wrappers) won't be seen.
That's a real, known limitation — better to under-report than to keep
drowning real findings in noise.
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

from rules import RULES

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build"}

# Rule ids that already have bespoke, more-precise logic below (var_dict_index
# resolution, thinking+effort combo checks, etc.) — running the generic
# engine on these too would just produce duplicate or less-precise findings.
# Also excludes model-tentative-retirement-sonnet45: that's an informational
# "no action needed" note, not a broken-code finding, so it doesn't belong
# in a scanner whose job is flagging things that ARE breaking.
GENERIC_RULE_EXCLUDE_IDS = {
    "sdk-v1-sampling-params-removed",
    "sdk-v1-text-completions-removed",
    "manual-thinking-budget",
    "opus5-effort-xhigh-thinking-disabled",
    "assistant-prefill-removed",
    "experimental-endpoint-retiring",
    "model-deprecated-sonnet4-opus4",
    "model-retired-opus-4-1",
    "fast-mode-removed-opus-4-7",
    "model-tentative-retirement-sonnet45",
}

# Node types whose *own* unparsed text becomes a match candidate for the
# generic engine. Deliberately narrow: these are real, executable pieces of
# code, never a comment (AST never sees comments at all) and never an
# unrelated docstring (a bare Expr(Constant) node isn't in this list, so a
# module/function docstring is never a candidate on its own). Restricting to
# single nodes rather than "the whole file" is what keeps this from
# regressing to v1's noise — a match has to live inside a real call,
# import, or assignment, not just appear anywhere in the source text.
GENERIC_CANDIDATE_TYPES = (ast.Call, ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign)

# Models known to reject non-default temperature/top_p/top_k as of SDK v1.0 —
# kept for reference/labelling only; the SDK-v1.0 rule itself applies
# regardless of model (see rules.py history for why that distinction matters).
DEPRECATED_4_PATTERN = None  # handled via string match on the literal, see below


def literal_str(node):
    """Return the string value of a node if it's a plain string literal, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def get_call_chain_name(node):
    """For a Call node, reconstruct a dotted name like 'client.messages.create'."""
    parts = []
    cur = node.func
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _dict_literal_to_map(dict_node):
    """Turn an ast.Dict literal into {literal_str_key: value_node}, skipping
    any key that isn't a plain string literal (e.g. a computed key)."""
    out = {}
    for k, v in zip(dict_node.keys, dict_node.values):
        key = literal_str(k)
        if key is not None:
            out[key] = v
    return out


def build_var_dict_index(tree):
    """Module-wide index of {var_name: dict_of_literal_kwargs} for every
    top-level-ish assignment `var_name = {...}` where the RHS is a dict
    literal. Deliberately simple (no scoping, no control flow, last
    assignment wins) — good enough to catch the common
    `kwargs = {...}; client.messages.create(**kwargs)` pattern without
    pretending to be a real data-flow analysis."""
    index = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    index[target.id] = _dict_literal_to_map(node.value)
    return index


def get_kwargs(call_node, var_dict_index=None):
    """Return {kwarg_name: ast_node} for a call's keyword arguments.

    Handles two shapes: direct keywords (`model="x"`) and a single
    `**some_dict` splat where `some_dict` was assigned a literal dict
    earlier in the file (`kwargs = {"model": "x"}; f(**kwargs)`) — the
    latter is a heuristic (see build_var_dict_index), not a guarantee.
    """
    kwargs = {kw.arg: kw.value for kw in call_node.keywords if kw.arg is not None}
    if var_dict_index:
        for kw in call_node.keywords:
            if kw.arg is None and isinstance(kw.value, ast.Name):
                resolved = var_dict_index.get(kw.value.id)
                if resolved:
                    # Direct keywords win over a splatted dict, matching Python's own rule.
                    kwargs = {**resolved, **kwargs}
    return kwargs


def extract_messages_prefill(messages_node):
    """If the `messages=` argument is a literal list ending in an assistant
    message, return that dict's line number. Else None."""
    if not isinstance(messages_node, ast.List):
        return None
    if not messages_node.elts:
        return None
    last = messages_node.elts[-1]
    if not isinstance(last, ast.Dict):
        return None
    for k, v in zip(last.keys, last.values):
        if literal_str(k) == "role" and literal_str(v) == "assistant":
            return last.lineno
    return None


def find_calls(tree, chain_suffixes):
    """Yield Call nodes whose reconstructed dotted name ends with one of chain_suffixes."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            chain = get_call_chain_name(node)
            if any(chain.endswith(suffix) for suffix in chain_suffixes):
                yield node, chain


def node_model_literal(node):
    """Best-effort: if this node is a Call with a literal model= kwarg, return it."""
    if isinstance(node, ast.Call):
        for kw in node.keywords:
            if kw.arg == "model":
                return literal_str(kw.value)
    return None


def file_references_anthropic(tree):
    """True if this file imports the `anthropic` package anywhere.

    This is a precondition, not a parameter-guessing heuristic — it's a
    different kind of check from the "model context anywhere in the file"
    mistake that made v1 noisy. That mistake was inferring *which specific
    value* (which model) applies to a match found elsewhere. This is only
    asking whether the file could possibly contain Anthropic SDK code at
    all. If it never imports `anthropic`, nothing in it can be Anthropic
    SDK code breaking — full stop, not a guess.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "anthropic" or a.name.startswith("anthropic.") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            # level == 0 means an absolute import. A relative import like
            # `from ...anthropic.chat.transformation import X` (level=3) can
            # have a module string that also starts with "anthropic." but
            # names a same-package submodule, not the real PyPI package.
            # Bug found live: this exact pattern in litellm (its own
            # internal `litellm.types.llms.anthropic` and relative
            # `...anthropic.*` submodules) tripped the naive version of
            # this check.
            if node.level == 0 and node.module and (node.module == "anthropic" or node.module.startswith("anthropic.")):
                return True
    return False


def build_async_context_map(tree):
    """Map id(node) -> True if node sits inside an `async def` function body
    (nearest enclosing FunctionDef/AsyncFunctionDef wins; a sync function
    nested inside an async one is correctly sync). No parent pointers in
    the stdlib ast module, so this is a one-time top-down walk instead."""
    ctx = {}

    def walk(node, in_async):
        ctx[id(node)] = in_async
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AsyncFunctionDef):
                walk(child, True)
            elif isinstance(child, ast.FunctionDef):
                walk(child, False)
            else:
                walk(child, in_async)

    walk(tree, False)
    return ctx


# Rules whose pattern alone is too broad to trust (proven live: see README —
# python-sdk-v1-httpx-to-httpx2 hit 1,604 lines in litellm, almost entirely
# generic HTTP-client code with no relation to the Anthropic SDK). Rather
# than trust the auto-extracted pattern on its own, these get one extra,
# narrow structural condition. Still driven by the auto-extracted title/
# severity/detail/fix — only the *gating* is hand-added, and each reason is
# a precondition (not a guessed parameter), same spirit as
# file_references_anthropic() above.
GENERIC_EXTRA_CONDITIONS = {
    # The breaking change is specifically about the ASYNC client's
    # .with_raw_response. Confirmed live: the only anthropic-sdk-python hit
    # for this rule was a *sync* test function correctly calling
    # `response.parse()` with no await at all — not broken, just matched by
    # a pattern with no async awareness. Second bug found live, same rule:
    # `.with_raw_response` is not an Anthropic-specific method name at all —
    # it's a shared convention across every Stainless-generated SDK
    # (Anthropic's and OpenAI's both are). litellm's Azure/OpenAI client
    # calls (`azure_client.chat.completions.with_raw_response.create(...)`)
    # matched even after the async fix, because they're genuinely async —
    # just not Anthropic. Needs both conditions at once.
    "python-sdk-v1-async-with-raw-response": lambda node, snippet, tree_ctx: (
        tree_ctx["async_map"].get(id(node), False) and tree_ctx["references_anthropic"]
    ),
    # httpx is a general-purpose HTTP library with no inherent connection to
    # the Anthropic SDK; only meaningful in a file that actually imports
    # anthropic. Confirmed live: litellm imports httpx in ~40 files for its
    # own multi-provider HTTP handling and doesn't import `anthropic` (the
    # package) in any of them — same root cause as the kwargs-handling
    # dead-end documented elsewhere in this README.
    "python-sdk-v1-httpx-to-httpx2": lambda node, snippet, tree_ctx: tree_ctx["references_anthropic"],
}


def generic_scan(tree, path, rules):
    """Run auto-extracted (or hand-written, not-yet-promoted) rules that
    don't have bespoke AST logic, by regex-matching each rule's `pattern`
    against the unparsed text of individual real-code nodes — never the
    whole file, never a comment or unrelated string — instead of hand-
    coding a check per rule the way the block above does.

    This is what actually closes the gap between extract_rules.py (which
    produces regex patterns, because that's a format an LLM can reliably
    emit) and this scanner (which is only trustworthy because it doesn't
    regex the whole file). Trade-off, stated plainly: a rule scoped to a
    specific model (applies_if_model) can only be checked precisely when
    the matching node is itself a Call with a literal model= kwarg — e.g.
    an Import statement has no model context at all. Rather than guess
    from file-wide context (the exact mistake that made v1 noisy), those
    cases are flagged with a lower-confidence note instead of silently
    assumed to apply.

    First real run of this against 6 public repos found two rules whose
    pattern alone was still too broad (see GENERIC_EXTRA_CONDITIONS) — the
    generic engine narrows the honesty gap vs. hand-coding, it doesn't
    close it completely. A pattern this permissive is still a bad rule; the
    fix each time is a real, checkable precondition, not a wider net.
    """
    tree_ctx = {
        "async_map": build_async_context_map(tree),
        "references_anthropic": file_references_anthropic(tree),
    }

    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, GENERIC_CANDIDATE_TYPES):
            continue
        try:
            snippet = ast.unparse(node)
        except Exception:
            continue

        for rule in rules:
            if rule["id"] in GENERIC_RULE_EXCLUDE_IDS:
                continue
            try:
                if not re.search(rule["pattern"], snippet):
                    continue
            except re.error:
                # An auto-extracted regex isn't guaranteed valid — skip that
                # one rule rather than crash the whole scan over it.
                continue

            extra_condition = GENERIC_EXTRA_CONDITIONS.get(rule["id"])
            if extra_condition and not extra_condition(node, snippet, tree_ctx):
                continue

            title = rule["title"]
            applies = rule.get("applies_if_model")
            if applies:
                model = node_model_literal(node)
                if model:
                    if not any(a in model for a in applies):
                        continue
                else:
                    title += " (model-scoped rule, but no literal model= on this exact node — unconfirmed, flagged for manual check)"

            findings.append(_finding(
                path, getattr(node, "lineno", 0), snippet.splitlines()[0][:200],
                rule["id"], rule["severity"], rule["deadline"], title,
                rule["detail"], rule["fix"],
            ))
    return findings


def scan_source(path, text):
    findings = []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return findings  # not valid Python (or Python 2, or a template) — skip quietly

    var_dict_index = build_var_dict_index(tree)

    # --- Messages API calls: messages.create(...) ---
    for call, chain in find_calls(tree, ["messages.create"]):
        kwargs = get_kwargs(call, var_dict_index)
        model = literal_str(kwargs.get("model")) if "model" in kwargs else None

        # sdk-v1-sampling-params-removed
        for param in ("temperature", "top_p", "top_k"):
            if param in kwargs:
                findings.append(_finding(
                    path, call.lineno, f"{param}={ast.unparse(kwargs[param])}",
                    "sdk-v1-sampling-params-removed", "HIGH",
                    "2026-08-20 (Python SDK v1.0)",
                    f"{param} removed from Messages methods in Python SDK v1.0",
                    f"Python SDK v1.0 removes temperature/top_p/top_k from Messages "
                    f"methods entirely. Model in this call: {model or 'not a literal, could not check'}.",
                    "Remove the parameter, or pin anthropic<1.0 until you've migrated.",
                ))

        # manual-thinking-budget
        if "thinking" in kwargs and isinstance(kwargs["thinking"], ast.Dict):
            d = kwargs["thinking"]
            keys = [literal_str(k) for k in d.keys]
            if "budget_tokens" in keys:
                findings.append(_finding(
                    path, call.lineno, ast.unparse(kwargs["thinking"]),
                    "manual-thinking-budget", "HIGH", "already active",
                    "Manual thinking budget rejected — needs adaptive thinking",
                    f"Fixed budget_tokens is being phased out in favor of effort levels. Model: {model or '?'}.",
                    'Replace budget_tokens with an `effort` level (e.g. "high", "xhigh").',
                ))
            # opus5-effort-xhigh-thinking-disabled
            if literal_str(next((v for k, v in zip(d.keys, d.values) if literal_str(k) == "type"), None)) == "disabled":
                effort = literal_str(kwargs.get("effort")) if "effort" in kwargs else None
                if effort in ("xhigh", "max") and model and "opus-5" in model:
                    findings.append(_finding(
                        path, call.lineno, ast.unparse(call).splitlines()[0],
                        "opus5-effort-xhigh-thinking-disabled", "MEDIUM",
                        "2026-07-24 (Claude Opus 5 launch)",
                        "Disabling thinking at xhigh/max effort is rejected on Opus 5",
                        "thinking disabled + effort xhigh/max returns a 400 error on Opus 5.",
                        "Drop the explicit thinking=disabled, or lower the effort level.",
                    ))

        # assistant-prefill-removed
        if "messages" in kwargs:
            prefill_line = extract_messages_prefill(kwargs["messages"])
            if prefill_line and model:
                findings.append(_finding(
                    path, prefill_line, "(last message has role=assistant)",
                    "assistant-prefill-removed", "MEDIUM", "already active",
                    "Assistant message prefill removed on recent models",
                    f"Model in this call: {model}. Prefill is no longer supported on newer models.",
                    "Move the prefilled text into the system prompt instead.",
                ))

        # model deprecation checks — only when it's an actual live call, not a
        # constant/test-fixture mention elsewhere in the file.
        if model:
            if model == "claude-opus-4-1-20250805":
                findings.append(_finding(
                    path, call.lineno, f'model="{model}"',
                    "model-retired-opus-4-1", "HIGH", "2026-08-05 (already retired)",
                    "Claude Opus 4.1 fully retired — this call will now error",
                    "Confirmed in the 2026-08-05 release notes: every request now returns an error.",
                    "Upgrade to Claude Opus 5.",
                ))
            elif model.startswith("claude-sonnet-4-2") or model.startswith("claude-opus-4-2"):
                # dated snapshot of the (deprecated) Claude 4 line, e.g. claude-sonnet-4-20250514
                findings.append(_finding(
                    path, call.lineno, f'model="{model}"',
                    "model-deprecated-sonnet4-opus4", "HIGH", "deprecated — migrate now",
                    "Pinned to a deprecated Claude 4 model snapshot",
                    "Claude Sonnet 4 / Opus 4 (dated snapshots) are deprecated in favor of the 4.5/5 line.",
                    "Update the model string to a current Sonnet/Opus 5 snapshot.",
                ))

    # --- Legacy Text Completions API ---
    # NOTE: must NOT match OpenAI-style `client.chat.completions.create(...)` —
    # that's a completely different API (Chat Completions), not Anthropic's
    # legacy Text Completions endpoint. Bug found live: testing against llm
    # and litellm (both multi-provider tools) flagged 40 OpenAI calls as
    # broken Anthropic code, because both chains end in "completions.create".
    for call, chain in find_calls(tree, ["completions.create"]):
        if "chat.completions.create" in chain:
            continue
        # OpenAI also has a legacy client.completions.create(...) endpoint
        # (their old Completions API) — same method chain, different provider.
        # Bug found live: litellm and llm both hit this with real OpenAI model
        # names (gpt-4, gpt-instruct). Only flag when the model literal looks
        # like a Claude model, or there's no model literal to check at all
        # (ambiguous — flagged at lower confidence rather than dropped).
        kwargs = get_kwargs(call, var_dict_index)
        model = literal_str(kwargs.get("model")) if "model" in kwargs else None
        if model and "claude" not in model.lower():
            continue
        confidence_note = "" if model else " (no literal model= found — flagged for manual check, not confirmed Anthropic)"
        findings.append(_finding(
            path, call.lineno, ast.unparse(call).splitlines()[0],
            "sdk-v1-text-completions-removed", "HIGH", "2026-08-20 (Python SDK v1.0)",
            "Legacy Text Completions API removed in Python SDK v1.0" + confidence_note,
            "The old Text Completions surface is gone in v1.0 of the Python SDK.",
            "Migrate to the Messages API (client.messages.create).",
        ))

    # --- Retired experimental endpoints: only inside actual string literals ---
    for node in ast.walk(tree):
        s = literal_str(node)
        if s and "/v1/experimental/" in s:
            for bad in ("generate_prompt", "improve_prompt", "templatize_prompt"):
                if bad in s:
                    findings.append(_finding(
                        path, node.lineno, s, "experimental-endpoint-retiring", "HIGH",
                        "2026-08-17 (already passed)", "Experimental prompt endpoint retired",
                        f"/v1/experimental/{bad} was shut down 2026-08-17.",
                        "Migrate to the stable prompt-engineering workflow in the Console.",
                    ))

    # --- Everything else: rules that came from extract_rules.py and don't
    # (yet) have bespoke logic above. See generic_scan()'s docstring for
    # the trade-off this makes vs. the hand-coded checks. ---
    findings.extend(generic_scan(tree, path, RULES))

    return dedupe_findings(findings)


def dedupe_findings(findings):
    """Collapse findings that are the same real thing reported twice.

    Bug found live building the JS/TS sibling of this scanner (ast_scan.js)
    and then checking whether the same bug existed here too — it did: the
    generic engine's candidate node types overlap (a Call is a child of the
    Assign/AnnAssign that assigns its result, e.g. `client =
    AnthropicBedrock(...)`), so a pattern matching text inside that Call
    also matches when the *same text* is unparsed again as part of the
    surrounding Assign. Checked directly: this was responsible for 427 of
    the 1,478 findings reported for anthropic-sdk-python (~29%) — the real,
    de-duplicated count there is 1,051. Fixing the overlap at the source
    (e.g. skip an Assign whose value is already a matched Call) would need
    parent-tracking this module doesn't have yet; deduping identical
    (file, line, rule_id) results is the honest fix available right now.
    """
    # Prefer the more informative duplicate: when a model-scoped rule
    # matches via two overlapping nodes (e.g. an Assign and the Call it
    # wraps), only the Call node can see the model= kwarg — an Assign node
    # never carries "unconfirmed" gating info of its own. Keeping whichever
    # copy was seen first (typically the outer, less specific node, since
    # ast.walk visits parents before children) would silently downgrade a
    # confirmed match to an "unconfirmed, flagged for manual check" one.
    best = {}
    order = []
    for f in findings:
        key = (f["file"], f["line"], f["rule_id"])
        if key not in best:
            order.append(key)
            best[key] = f
        elif "unconfirmed" in best[key]["title"] and "unconfirmed" not in f["title"]:
            best[key] = f
    return [best[key] for key in order]


def _finding(path, line, code, rule_id, severity, deadline, title, detail, fix):
    return {
        "file": str(path), "line": line, "code": code, "rule_id": rule_id,
        "severity": severity, "deadline": deadline, "title": title,
        "detail": detail, "fix": fix,
    }


def scan_dir(root):
    root = Path(root)
    # Bug found live building autofix.py: `Path(file).rglob("*.py")` on a
    # *file* path silently returns an empty iterator, not an error — so
    # scanning a single file always reported "no findings," even when
    # there were real ones. A false "all clear" is the one failure mode
    # this whole project exists to avoid; worth fixing the moment it
    # actually mattered rather than leaving it as a footgun.
    if root.is_file():
        paths = [root] if root.suffix == ".py" else []
    else:
        paths = root.rglob("*.py")

    all_findings = []
    for path in paths:
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        text = path.read_text(errors="ignore")
        all_findings.extend(scan_source(path, text))
    return all_findings


SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def print_report(findings):
    if not findings:
        print("No known Claude API breaking changes detected in live call sites.")
        return
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["file"], f["line"]))
    print(f"Found {len(findings)} affected call site(s):\n")
    for f in findings:
        print(f"[{f['severity']}] {f['file']}:{f['line']}  ({f['deadline']})")
        print(f"  {f['title']}")
        print(f"  > {f['code']}")
        print(f"  Fix: {f['fix']}")
        print()


def main():
    parser = argparse.ArgumentParser(description="AST-based scan for known Claude API breaking changes.")
    parser.add_argument("path")
    parser.add_argument("--json", metavar="FILE")
    args = parser.parse_args()

    findings = scan_dir(args.path)
    print_report(findings)
    if args.json:
        Path(args.json).write_text(json.dumps(findings, indent=2))
        print(f"Wrote {len(findings)} finding(s) to {args.json}")
    sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()
