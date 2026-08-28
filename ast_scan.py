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
import sys
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build"}

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


def get_kwargs(call_node):
    """Return {kwarg_name: ast_node} for keyword arguments with a literal name."""
    return {kw.arg: kw.value for kw in call_node.keywords if kw.arg is not None}


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


def scan_source(path, text):
    findings = []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return findings  # not valid Python (or Python 2, or a template) — skip quietly

    # --- Messages API calls: messages.create(...) ---
    for call, chain in find_calls(tree, ["messages.create"]):
        kwargs = get_kwargs(call)
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
        kwargs = get_kwargs(call)
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

    return findings


def _finding(path, line, code, rule_id, severity, deadline, title, detail, fix):
    return {
        "file": str(path), "line": line, "code": code, "rule_id": rule_id,
        "severity": severity, "deadline": deadline, "title": title,
        "detail": detail, "fix": fix,
    }


def scan_dir(root):
    root = Path(root)
    all_findings = []
    for path in root.rglob("*.py"):
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
