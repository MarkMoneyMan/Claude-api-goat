#!/usr/bin/env python3
"""
claude-api-guard — a tiny proof-of-concept "self-maintaining API" scanner.

Walks a codebase, finds usage of the Anthropic/Claude API, and flags any
code that's about to break (or is already broken) because of a known,
dated API change. Also checks requirements.txt to see whether the
installed SDK version is even new enough for the SDK-version-specific
rules to apply yet.

Usage:
    python scan.py /path/to/project
    python scan.py /path/to/project --json report.json
"""

import argparse
import json
import re
import sys
from pathlib import Path

from rules import RULES

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build"}

MODEL_ID_PATTERN = re.compile(r"claude-(opus|sonnet|haiku)-([\d.-]+)")

PINNED_VERSION_PATTERN = re.compile(r"^anthropic\s*(==|>=|~=)\s*([\d.]+)", re.MULTILINE)
CURRENT_SDK_MAJOR = 1  # anthropic Python SDK v1.0 shipped 2026-08-20


def check_sdk_version(root):
    """Look for a pinned `anthropic` version in requirements.txt and flag if it's stale.

    This matters because several rules (e.g. sdk-v1-sampling-params-removed) only
    apply once you're actually on SDK v1.0+ — scanning code patterns alone can't
    tell you that, you have to check what's actually installed/pinned.
    """
    req_file = Path(root) / "requirements.txt"
    if not req_file.exists():
        return None
    match = PINNED_VERSION_PATTERN.search(req_file.read_text())
    if not match:
        return None
    pinned = match.group(2)
    major = int(pinned.split(".")[0])
    return {
        "file": str(req_file),
        "pinned_version": pinned,
        "is_pre_v1": major < CURRENT_SDK_MAJOR,
    }


def find_model_context(text):
    """Return the set of Claude model families referenced in this file."""
    found = set()
    for m in MODEL_ID_PATTERN.finditer(text):
        family, version = m.group(1), m.group(2)
        found.add(f"{family}-{version}")
    return found


def scan_file(path):
    text = path.read_text(errors="ignore")
    lines = text.splitlines()
    model_context = find_model_context(text)

    findings = []
    for rule in RULES:
        applies = rule["applies_if_model"]
        if applies is not None:
            # Substring match, not exact equality: a detected model like
            # "opus-5-20260501" should still match a rule scoped to "opus-5".
            # (Bug found live: the first version used exact set intersection,
            # which silently stopped matching once model detection got more
            # precise about full snapshot strings. Exactly the kind of subtle
            # miss a "trust me, it's clean" tool must not make.)
            if not any(a in mc for mc in model_context for a in applies):
                continue
        pattern = re.compile(rule["pattern"])
        for i, line in enumerate(lines, start=1):
            if pattern.search(line):
                findings.append(
                    {
                        "file": str(path),
                        "line": i,
                        "code": line.strip(),
                        "rule_id": rule["id"],
                        "severity": rule["severity"],
                        "deadline": rule["deadline"],
                        "title": rule["title"],
                        "detail": rule["detail"],
                        "fix": rule["fix"],
                    }
                )
    return findings


def scan_dir(root):
    root = Path(root)
    all_findings = []
    for path in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        all_findings.extend(scan_file(path))
    return all_findings


SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def print_report(findings):
    if not findings:
        print("No known Claude API breaking changes detected. Nice.")
        return

    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["file"], f["line"]))

    print(f"Found {len(findings)} affected line(s):\n")
    for f in findings:
        print(f"[{f['severity']}] {f['file']}:{f['line']}  ({f['deadline']})")
        print(f"  {f['title']}")
        print(f"  > {f['code']}")
        print(f"  Fix: {f['fix']}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Scan a codebase for known Claude API breaking changes.")
    parser.add_argument("path", help="Path to the project to scan")
    parser.add_argument("--json", metavar="FILE", help="Also write findings as JSON to this file")
    args = parser.parse_args()

    sdk_info = check_sdk_version(args.path)
    if sdk_info:
        if sdk_info["is_pre_v1"]:
            print(
                f"Note: {sdk_info['file']} pins anthropic=={sdk_info['pinned_version']} "
                f"(pre-v1.0). The v1.0-specific rules below don't apply to your installed "
                f"version yet — but will the moment you upgrade.\n"
            )
        else:
            print(f"anthropic SDK pin looks current ({sdk_info['pinned_version']}).\n")

    findings = scan_dir(args.path)
    print_report(findings)

    if args.json:
        Path(args.json).write_text(json.dumps(findings, indent=2))
        print(f"Wrote {len(findings)} finding(s) to {args.json}")

    sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()
