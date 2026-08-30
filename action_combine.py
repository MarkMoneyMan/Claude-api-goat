#!/usr/bin/env python3
"""
action_combine.py — glue between action.yml's steps and a CI job's pass/
fail state. Merges whichever *_findings.json files actually exist (the
JS/TS one is conditional), writes a combined report, sets this composite
action's outputs via $GITHUB_OUTPUT, and exits non-zero only when a
finding meets or exceeds --fail-on — so "detected, but only LOW severity"
doesn't fail a build by default the way "detected, HIGH severity" should.

Not itself part of the scanning logic (that's ast_scan.py / ast_scan.js) —
just the small amount of CI plumbing a composite action needs, kept as its
own file rather than folded into ast_scan.py so the actual scanner stays
usable standalone with no GitHub Actions concepts leaking into it.
"""

import argparse
import json
import os
import sys
from pathlib import Path

SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
SEVERITY_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "none": 0}


def load_findings(paths):
    all_findings = []
    for p in paths:
        if not p:
            continue
        path = Path(p)
        if not path.exists():
            continue  # step that would have produced this didn't run — not an error
        all_findings.extend(json.loads(path.read_text()))
    return all_findings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="*")
    parser.add_argument("--fail-on", default="HIGH", choices=["HIGH", "MEDIUM", "LOW", "none"])
    args = parser.parse_args()

    findings = load_findings(args.files)
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["file"], f["line"]))

    out_path = Path("claude-api-guard-findings.json")
    out_path.write_text(json.dumps(findings, indent=2))

    if findings:
        print(f"claude-api-guard: {len(findings)} finding(s):\n")
        for f in findings:
            print(f"[{f['severity']}] {f['file']}:{f['line']}  {f['title']}")
    else:
        print("claude-api-guard: no known Claude API breaking changes detected.")

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as fh:
            fh.write(f"findings-count={len(findings)}\n")
            fh.write(f"findings-json={out_path}\n")

    fail_threshold = SEVERITY_RANK[args.fail_on]
    if fail_threshold == 0:
        sys.exit(0)
    should_fail = any(SEVERITY_RANK.get(f["severity"], 0) >= fail_threshold for f in findings)
    sys.exit(1 if should_fail else 0)


if __name__ == "__main__":
    main()
