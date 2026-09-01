#!/usr/bin/env python3
"""
sync_rules.py — the piece that actually closes the loop on "self-maintaining
API": run this on a schedule (see .github/workflows/update-rules.yml) and it

  1. fetches a provider's live changelog/release notes,
  2. finds only the entries newer than the last time this ran *for that
     provider*,
  3. hands just that new text to extract_rules.py's extract() (the same LLM
     extraction used for the one-off manual run that seeded rules.py and
     rules_openai.py),
  4. skips any extracted rule whose id already exists (defense against the
     same change getting re-described slightly differently on a re-run),
  5. appends whatever's left to that provider's rules file as a new dated
     block, and
  6. advances that provider's "synced through" date — even when nothing new
     rule-worthy was found, so next run doesn't re-fetch and re-pay for the
     same window.

Multi-provider since 2026-08-31 (added alongside rules_openai.py). Each
provider is one entry in PROVIDERS below — its own changelog URL, its own
section parser (Anthropic's release notes and OpenAI's CHANGELOG.md have
genuinely different formats, not just different URLs), its own rules file,
and its own key in pipeline_runs/last_synced.json. Adding a third provider
later means adding one more PROVIDERS entry and, almost certainly, one more
parser function — changelog formats aren't standardized across the
industry, so a parser per provider is the honest shape, not a shortcut.

Nothing here commits or opens a PR — that's the calling workflow's job
(mirrors autofix.py: this script only touches files on disk, and
peter-evans/create-pull-request notices the diff and does the rest, or
no-ops cleanly if there isn't one).

Usage:
    python sync_rules.py [--provider anthropic|openai] [--dry-run]

    Defaults to --provider anthropic for backward compatibility with
    existing invocations (update-rules.yml is being updated to call this
    once per provider explicitly, but a bare `python sync_rules.py` keeps
    doing exactly what it always did).

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
STATE_PATH = REPO_ROOT / "pipeline_runs" / "last_synced.json"

# "### August 27, 2026" -> captures "August 27, 2026"
ANTHROPIC_DATE_HEADER_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)

# "## [3.6.0](.../compare/v3.5.0...v3.6.0) (2026-08-27)" (recent entries) or
# "## 2.0.0 (2025-09-30)" (older entries, no brackets/link) — both forms
# confirmed against the real file's full ~344-version history, not guessed.
OPENAI_VERSION_HEADER_RE = re.compile(
    r"^##\s+\[?([\d.]+)\]?(?:\([^)]*\))?\s*\((\d{4}-\d{2}-\d{2})\)\s*$", re.MULTILINE
)


def fetch_url(url):
    """Plain GET, no auth — every source here is a public page."""
    req = urllib.request.Request(url, headers={"User-Agent": "claude-api-guard/sync_rules.py"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def parse_dated_sections_anthropic(markdown_text):
    """Split Anthropic's release-notes markdown into (date, label,
    section_text) tuples, one per "### <Month> <Day>, <Year>" header,
    newest-first as published. Section text includes the header line
    through (not including) the next header."""
    headers = list(ANTHROPIC_DATE_HEADER_RE.finditer(markdown_text))
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


def parse_dated_sections_openai(markdown_text):
    """Split openai-python's CHANGELOG.md into (date, label, section_text)
    tuples, one per version header — but keep ONLY sections containing an
    explicit "### ⚠ BREAKING CHANGES" marker.

    This is a deliberate difference from the Anthropic parser, not an
    oversight: Anthropic's release notes are unstructured prose with no
    reliable breaking/non-breaking signal beyond what the model infers, so
    every new section has to go to it. OpenAI's changelog already tells us
    which versions are breaking (checked against the real file: 344
    version headers total, only 2 ever marked breaking). Sending the other
    ~99% — Features, Bug Fixes, Chores, dependency bumps — to the model on
    every run would be pure wasted cost with zero chance of a real
    finding, so this filters at parse time instead of relying on the
    model to correctly say "nothing breaking here" 342 times.
    """
    headers = list(OPENAI_VERSION_HEADER_RE.finditer(markdown_text))
    sections = []
    for i, m in enumerate(headers):
        version, date_str = m.group(1), m.group(2)
        try:
            date = datetime.date.fromisoformat(date_str)
        except ValueError:
            continue
        start = m.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(markdown_text)
        section_text = markdown_text[start:end].rstrip()
        if "BREAKING CHANGE" not in section_text:
            continue
        sections.append((date, f"v{version} ({date_str})", section_text))
    return sections


PROVIDERS = {
    "anthropic": {
        "changelog_url": "https://platform.claude.com/docs/en/release-notes/overview.md",
        "parse_sections": parse_dated_sections_anthropic,
        "rules_path": REPO_ROOT / "rules.py",
        "rules_var": "RULES",
        "auto_var_prefix": "RULES_AUTO_",
        "provider_label": "Anthropic's Claude API",
    },
    "openai": {
        "changelog_url": "https://raw.githubusercontent.com/openai/openai-python/main/CHANGELOG.md",
        "parse_sections": parse_dated_sections_openai,
        "rules_path": REPO_ROOT / "rules_openai.py",
        "rules_var": "RULES_OPENAI",
        "auto_var_prefix": "RULES_OPENAI_AUTO_",
        "provider_label": "OpenAI's API",
    },
}


def load_state():
    if not STATE_PATH.exists():
        return {}
    data = json.loads(STATE_PATH.read_text())
    # Backward compatibility: the file used to be flat
    # ({"last_synced_date": "..."}) and implicitly meant Anthropic, from
    # before a second provider existed. Migrate that shape in memory (not
    # written back until save_last_synced_date actually runs) so an old
    # checkout doesn't lose its Anthropic sync history.
    if "last_synced_date" in data:
        data = {"anthropic": {"last_synced_date": data["last_synced_date"]}}
    return data


def load_last_synced_date(provider):
    state = load_state()
    entry = state.get(provider)
    if not entry:
        return None
    return datetime.date.fromisoformat(entry["last_synced_date"])


def save_last_synced_date(provider, date):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = load_state()
    state[provider] = {"last_synced_date": date.isoformat()}
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def load_existing_rule_ids(provider_cfg):
    # Import the rules module rather than re-parsing it by hand — it's a
    # plain Python file with no side effects beyond building its rules
    # list, so this is safe and avoids maintaining a second parser for the
    # same file format.
    sys.path.insert(0, str(REPO_ROOT))
    module_name = provider_cfg["rules_path"].stem
    module = __import__(module_name)
    return {r["id"] for r in getattr(module, provider_cfg["rules_var"])}


def format_rule_literal(rule, provider):
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
    if provider != "anthropic":
        # rules.py's entries predate the "provider" field and default to
        # "anthropic" in ast_scan.py when absent — see that module. Every
        # non-Anthropic provider's rules (auto-extracted or hand-written)
        # need it explicit, same as rules_openai.py's hand-written ones.
        lines.append(f"        'provider': {provider!r},")
    lines.append("    },")
    return "\n".join(lines)


def append_rules_block(new_rules, through_date, provider, provider_cfg):
    tag = through_date.strftime("%Y%m%d")
    var_name = f"{provider_cfg['auto_var_prefix']}{tag}"
    body = "\n".join(format_rule_literal(r, provider) for r in new_rules)
    block = (
        f"\n\n# --- Auto-extracted by sync_rules.py from release notes through "
        f"{through_date.isoformat()} ---\n"
        f"{var_name} = [\n{body}\n]\n\n"
        f"{provider_cfg['rules_var']} = {provider_cfg['rules_var']} + {var_name}\n"
    )
    with open(provider_cfg["rules_path"], "a") as fh:
        fh.write(block)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provider",
        choices=sorted(PROVIDERS),
        default="anthropic",
        help="Which provider's changelog to sync (default: anthropic, for backward compatibility).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and report what would change, but write nothing and don't call the API.",
    )
    args = parser.parse_args()
    provider = args.provider
    cfg = PROVIDERS[provider]

    print(f"[{provider}] Fetching {cfg['changelog_url']} ...")
    markdown_text = fetch_url(cfg["changelog_url"])
    sections = cfg["parse_sections"](markdown_text)
    if not sections:
        # For openai this is ambiguous by design (0 breaking sections is a
        # perfectly normal outcome, not necessarily a parser break) unless
        # this is also the very first run ever (no prior state at all) —
        # keep the "format may have changed" failure mode for Anthropic,
        # where an empty result really has always meant something's wrong.
        if provider == "anthropic":
            print("No dated sections found in the fetched page — format may have changed. Nothing done.")
            sys.exit(1)
        print("No breaking-change sections found (openai-python's changelog explicitly marks these, "
              "so an empty result here is a normal outcome, not necessarily a parse failure).")

    last_synced = load_last_synced_date(provider)
    new_sections = [s for s in sections if last_synced is None or s[0] > last_synced]
    latest_date = max((s[0] for s in sections), default=last_synced)

    gh_output = os.environ.get("GITHUB_OUTPUT")

    def write_gh_output(new_rules_count, new_sections_count):
        if gh_output:
            with open(gh_output, "a") as fh:
                fh.write(f"{provider}-new-rules-count={new_rules_count}\n")
                fh.write(f"{provider}-new-sections-count={new_sections_count}\n")

    if not new_sections:
        print(f"[{provider}] Nothing newer than {last_synced} — changelog unchanged since last sync. Nothing done.")
        write_gh_output(0, 0)
        sys.exit(0)

    print(f"[{provider}] {len(new_sections)} new dated section(s) since {last_synced} (through {latest_date}).")
    for date, label, _ in sorted(new_sections, key=lambda s: s[0]):
        print(f"  - {label}")

    if args.dry_run:
        print("\n--dry-run: not calling the API, not writing anything.")
        sys.exit(0)

    if "ANTHROPIC_API_KEY" not in os.environ:
        print(f"[{provider}] New content found, but ANTHROPIC_API_KEY isn't set — can't extract rules from it.", file=sys.stderr)
        sys.exit(1)

    chunk_text = "\n\n".join(s[2] for s in sorted(new_sections, key=lambda s: s[0]))
    from extract_rules import extract  # local import: only needed (and only importable) for a real run

    print(f"\n[{provider}] Calling the model to extract structured rules from the new text...")
    candidates = extract(chunk_text, provider_label=cfg["provider_label"])
    for rule in candidates:
        rule.setdefault("provider", provider)

    existing_ids = load_existing_rule_ids(cfg)
    kept = [r for r in candidates if r["id"] not in existing_ids]
    skipped = [r["id"] for r in candidates if r["id"] in existing_ids]

    if skipped:
        print(f"[{provider}] Skipped {len(skipped)} extracted rule(s) with an id that already exists: {skipped}")

    if kept:
        append_rules_block(kept, latest_date, provider, cfg)
        print(f"[{provider}] Appended {len(kept)} new rule(s) to {cfg['rules_path'].name}:")
        for r in kept:
            print(f"  [{r['severity']}] {r['id']} — {r['title']}")
    else:
        print(f"[{provider}] No new (non-duplicate) breaking-change rules found in the new text.")

    save_last_synced_date(provider, latest_date)
    print(f"\n[{provider}] Advanced last_synced_date to {latest_date.isoformat()}.")

    write_gh_output(len(kept), len(new_sections))


if __name__ == "__main__":
    main()
