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
  (be conservative — prefer missing an edge case over a regex so broad it matches unrelated code). \
  IMPORTANT: this field is a JSON string, so every literal backslash in the regex must be written \
  as TWO backslashes in your output — a regex word boundary \\b must appear in the JSON as \\\\b, \
  and a literal dot \\. must appear as \\\\. . A single backslash makes your entire response invalid JSON.
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


# Backslashes we trust the model to have meant as real JSON escapes, so
# _repair_stray_backslashes leaves them alone. Deliberately excludes "b":
# see that function's docstring for why \b specifically can't be trusted
# even though it's technically valid JSON.
_TRUSTED_JSON_ESCAPES = set('"\\/fnrtu')


def _repair_stray_backslashes(s):
    """Escape every backslash that isn't part of a JSON escape this project
    can actually trust, so a regex written straight into the "pattern" field
    survives JSON round-tripping instead of getting silently mangled or
    rejected outright.

    Real bug, found on the first live GitHub Actions run of this pipeline
    against genuinely new content (not the hand-reviewed batch that seeded
    rules.py): asked for a "pattern" field containing a regex, the model
    wrote a literal single backslash (e.g. the JSON text \\. for a regex
    literal dot) instead of the two backslashes valid JSON requires to
    represent one literal backslash character. json.loads rightly rejected
    it outright: "Invalid \\escape".

    That crash is the GOOD outcome. The dangerous version of the same
    mistake is \\b: unlike \\., \\b IS valid JSON — but JSON's \\b means
    "backspace control character" (0x08), while a regex's \\b means "word
    boundary" — two unrelated meanings that happen to share a spelling.
    json.loads(r'"client\\b"') parses without complaint and silently hands
    back a pattern field containing a literal backspace character instead
    of the word-boundary regex it was supposed to be — a rule that looks
    fine, ships fine, and then never matches anything, with nothing in any
    log to say why. Confirmed by constructing this case directly (not by
    guessing): `json.loads('"client\\\\b"')` returns `'client\\x08'`, and
    `re.search('client\\x08', 'client code')` returns no match — silent,
    not loud. So \\b is excluded from the trusted set here even though the
    JSON spec allows it: nothing in this project's data (a regex, or a
    plain-English title/detail/fix string) legitimately needs a literal
    backspace character, and the failure mode of guessing wrong the other
    way (treating an actually-intended \\b backspace as a literal
    backslash-b) is a corrupted regex character class at worst — visible on
    inspection — not a rule that silently never fires.
    """
    out = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in _TRUSTED_JSON_ESCAPES:
                # A real trusted pair (e.g. \\ or \n) — consume BOTH chars
                # here. Bug caught in testing: advancing by only 1 left the
                # second backslash of an already-correct \\ pair to be
                # re-examined on its own next iteration, where it looked
                # like a fresh stray backslash and got a third one glued on
                # (\\b -> \\\b, silently corrupting an already-valid escape
                # that never needed touching).
                out.append(ch)
                out.append(nxt)
                i += 2
                continue
            out.append("\\\\")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def extract(changelog_text, model="claude-sonnet-4-6"):
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        messages=[
            {"role": "user", "content": EXTRACTION_PROMPT.format(changelog_text=changelog_text)}
        ],
    )
    raw = response.content[0].text
    if response.stop_reason == "max_tokens":
        raise ValueError(
            "Model output was cut off at max_tokens before finishing the JSON array. "
            "The changelog input is too large for one call — split it into smaller "
            "chunks (e.g. one call per month) rather than raising max_tokens forever."
        )
    # Expect a JSON array in the response; be lenient about surrounding prose.
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON array found in model output:\n{raw}")
    candidate = raw[start : end + 1]

    # Repair unconditionally, before the first parse attempt — not just as
    # a fallback when parsing fails. The \b case above parses "successfully"
    # on the raw text, so a repair-on-JSONDecodeError-only design would
    # never even run for that one, and would silently ship a broken rule.
    repaired = _repair_stray_backslashes(candidate)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError as e:
        # Repair didn't make it valid JSON either — surface the actual bad
        # output (truncated) instead of just a json/decoder.py stack frame,
        # so whoever's debugging this doesn't have to reproduce the call to
        # see what the model actually said.
        raise ValueError(
            f"Model output wasn't valid JSON, even after repairing suspect "
            f"backslash escapes ({e}). First 2000 chars of the JSON "
            f"candidate:\n{candidate[:2000]}"
        ) from e


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
