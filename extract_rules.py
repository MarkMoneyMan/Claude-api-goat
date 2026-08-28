#!/usr/bin/env python3
"""
extract_rules.py — the "self-maintaining" part of self-maintaining APIs.

This is the piece that turns raw changelog text into structured rules
*automatically*, instead of a human reading the release notes and hand-
writing regex (which is what rules.py currently is, and which doesn't
scale — I did that extraction by hand this one time, but a real product
needs this to run itself, e.g. once a day via cron/GitHub Action).

Requires: pip install anthropic
Requires: ANTHROPIC_API_KEY environment variable

Usage:
    python extract_rules.py changelog.txt >> new_rules.json
"""

import json
import os
import sys

import anthropic

EXTRACTION_PROMPT = """You are reviewing a page of raw API changelog/release-note text \
from an API provider (in this case, Anthropic's Claude API). Your job is to find every \
entry that describes a BREAKING change — something that would make existing client code \
stop working, start erroring, or silently misbehave. Ignore purely additive features \
(new endpoints, new optional parameters, new models with no removed behavior).

For every breaking change you find, output one JSON object with these fields:
- "id": a short kebab-case identifier
- "pattern": a Python regex that would match the affected code pattern in a real codebase \
  (be conservative — prefer missing an edge case over a regex so broad it matches unrelated code)
- "applies_if_model": a list of affected model name fragments (e.g. ["opus-5"]), \
  or null if it applies regardless of model
- "severity": "HIGH" | "MEDIUM" | "LOW"
- "deadline": the date the change takes effect, as stated in the text (or "already active")
- "title": one-line human summary
- "detail": 1-3 sentences of context, in your own words, staying strictly grounded in \
  what the text actually says — never infer a cause the text doesn't state
- "fix": one sentence of concrete migration advice

Output a JSON array. If there are no breaking changes in the text, output an empty array.
Do not invent changes that aren't in the text. If the text is ambiguous about whether \
something is breaking, err on the side of leaving it out rather than guessing.

--- CHANGELOG TEXT ---
{changelog_text}
--- END CHANGELOG TEXT ---
"""


def extract(changelog_text, model="claude-sonnet-4-6"):
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[
            {"role": "user", "content": EXTRACTION_PROMPT.format(changelog_text=changelog_text)}
        ],
    )
    raw = response.content[0].text
    # Expect a JSON array in the response; be lenient about surrounding prose.
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON array found in model output:\n{raw}")
    return json.loads(raw[start : end + 1])


def main():
    if len(sys.argv) != 2:
        print("Usage: python extract_rules.py changelog.txt", file=sys.stderr)
        sys.exit(1)

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("Set ANTHROPIC_API_KEY to run this for real.", file=sys.stderr)
        sys.exit(1)

    changelog_text = open(sys.argv[1]).read()
    rules = extract(changelog_text)
    print(json.dumps(rules, indent=2))


if __name__ == "__main__":
    main()
