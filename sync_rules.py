#!/usr/bin/env python3
"""
sync_rules.py — the piece that actually closes the loop on "self-maintaining
API": run this on a schedule (see .github/workflows/update-rules.yml) and it

  1. fetches Anthropic's live release notes,
  2. finds only the entries newer than the last time this ran,
  3. hands just that new text to extract_rules.py's extract() (the same LLM
     extraction used for the one-off manual run that seeded rules.py),
  4. skips any extracted rule whose id already exists (defense against the
     same change getting re-described slightly differently on a re-run),
  5. appends whatever's left to rules.py as a new dated block, and
  6. advances the "synced through" date — even when nothing new rule-worthy
     was found, so next run doesn't re-fetch and re-pay for the same window.

Nothing here commits or opens a PR — that's the calling workflow's job
(mirrors autofix.py: this script only touches files on disk, and
peter-evans/create-pull-request notices the diff and does the rest, or
no-ops cleanly if there isn't one).

Usage:
    python sync_rules.py [--dry-run]

Requires: pip install anthropic
Requires: ANTHROPIC_API_KEY environment variable (unless --dry-run)
"""

import argparse
import datetime
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
CHANGELOG_URL = "https://platform.claude.com/docs/en/release-notes/overview.md"
STATE_PATH = REPO_ROOT / "pipeline_runs" / "last_synced.json"
RULES_PATH = REPO_ROOT / "rules.py"

# "### August 27, 2026" -> captures "August 27, 2026"
DATE_HEADER_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)


def fetch_changelog_markdown(url=CHANGELOG_URL):
    """Plain GET, no auth — this is a public docs page. Appending .md to a
    platform.claude.com/docs/... URL returns raw markdown instead of the
    rendered HTML page, which is much easier to parse reliably than
    scraping rendered HTML (found by trying it, not documented anywhere)."""
    req = urllib.request.Request(url, headers={"User-Agent": "claude-api-guard/sync_rules.py"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def parse_dated_sections(markdown_text):
    """Split the changelog markdown into (date, label, section_text) tuples,
    one per "### <Month> <Day>, <Year>" header, newest-first as published.
    Section text includes the header line through (not including) the next
    header."""
    headers = list(DATE_HEADER_RE.finditer(markdown_text))
    sections = []
    for i, m in enumerate(headers):
        label = m.group(1).strip()
        # Older entries use ordinal day suffixes ("April 9th, 2025",
        # "March 31st, 2025") that strptime can't parse; recent ones don't
        # ("August 27, 2026") — strip the suffix unconditionally so both
        # forms work, found by testing against the real page's full history
        # rather than just the newest (unsuffixed) entries.
        clean_label = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", label)
        try:
            date = datetime.datetime.strptime(clean_label, "%B %d, %Y").date()
        except ValueError:
            continue  # not a real date header (shouldn't happen, but don't crash the pipeline over it)
        start = m.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(markdown_text)
        sections.append((date, label, markdown_text[start:end].rstrip()))
    return sections


def load_last_synced_date():
    if not STATE_PATH.exists():
        return None
    data = json.loads(STATE_PATH.read_text())
    return datetime.date.fromisoformat(data["last_synced_date"])


def save_last_synced_date(date):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"last_synced_date": date.isoformat()}, indent=2) + "\n")


def load_existing_rule_ids():
    # Import rules.py as a module rather than re-parsing it by hand — it's
    # a plain Python file with no side effects beyond building the RULES
    # list, so this is safe and avoids maintaining a second parser for the
    # same file format.
    sys.path.insert(0, str(REPO_ROOT))
    import rules  # noqa: E402 (path insert must happen first)

    return {r["id"] for r in rules.RULES}


def format_rule_literal(rule):
    def pyval(key, val):
        if val is None:
            return "None"
        if key == "pattern" and isinstance(val, str) and '"' not in val and not val.endswith("\\"):
            return f'r"{val}"'
        return repr(val)

    order = ["id", "pattern", "applies_if_model", "severity", "deadline", "title", "detail", "fix"]
    lines = ["    {"]
    for key in order:
        lines.append(f"        {key!r}: {pyval(key, rule.get(key))},")
    lines.append("    },")
    return "\n".join(lines)


def append_rules_block(new_rules, through_date):
    tag = through_date.strftime("%Y%m%d")
    var_name = f"RULES_AUTO_{tag}"
    body = "\n".join(format_rule_literal(r) for r in new_rules)
    block = (
        f"\n\n# --- Auto-extracted by sync_rules.py from release notes through "
        f"{through_date.isoformat()} ---\n"
        f"{var_name} = [\n{body}\n]\n\n"
        f"RULES = RULES + {var_name}\n"
    )
    with open(RULES_PATH, "a") as fh:
        fh.write(block)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and report what would change, but write nothing and don't call the API.",
    )
    args = parser.parse_args()

    print(f"Fetching {CHANGELOG_URL} ...")
    markdown_text = fetch_changelog_markdown()
    sections = parse_dated_sections(markdown_text)
    if not sections:
        print("No dated sections found in the fetched page — format may have changed. Nothing done.")
        sys.exit(1)

    last_synced = load_last_synced_date()
    new_sections = [s for s in sections if last_synced is None or s[0] > last_synced]
    latest_date = max(s[0] for s in sections)

    gh_output = os.environ.get("GITHUB_OUTPUT")

    if not new_sections:
        print(f"Nothing newer than {last_synced} — changelog unchanged since last sync. Nothing done.")
        if gh_output:
            with open(gh_output, "a") as fh:
                fh.write("new-rules-count=0\n")
                fh.write("new-sections-count=0\n")
        sys.exit(0)

    print(f"{len(new_sections)} new dated section(s) since {last_synced} (through {latest_date}).")
    for date, label, _ in sorted(new_sections, key=lambda s: s[0]):
        print(f"  - {label}")

    if args.dry_run:
        print("\n--dry-run: not calling the API, not writing anything.")
        sys.exit(0)

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("New content found, but ANTHROPIC_API_KEY isn't set — can't extract rules from it.", file=sys.stderr)
        sys.exit(1)

    chunk_text = "\n\n".join(s[2] for s in sorted(new_sections, key=lambda s: s[0]))
    from extract_rules import extract  # local import: only needed (and only importable) for a real run

    print("\nCalling the model to extract structured rules from the new text...")
    candidates = extract(chunk_text)

    existing_ids = load_existing_rule_ids()
    kept = [r for r in candidates if r["id"] not in existing_ids]
    skipped = [r["id"] for r in candidates if r["id"] in existing_ids]

    if skipped:
        print(f"Skipped {len(skipped)} extracted rule(s) with an id that already exists: {skipped}")

    if kept:
        append_rules_block(kept, latest_date)
        print(f"Appended {len(kept)} new rule(s) to {RULES_PATH.name}:")
        for r in kept:
            print(f"  [{r['severity']}] {r['id']} — {r['title']}")
    else:
        print("No new (non-duplicate) breaking-change rules found in the new text.")

    save_last_synced_date(latest_date)
    print(f"\nAdvanced last_synced_date to {latest_date.isoformat()}.")

    if gh_output:
        with open(gh_output, "a") as fh:
            fh.write(f"new-rules-count={len(kept)}\n")
            fh.write(f"new-sections-count={len(new_sections)}\n")


if __name__ == "__main__":
    main()
