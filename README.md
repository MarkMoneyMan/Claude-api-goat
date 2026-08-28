# claude-api-guard (prototype)

A proof-of-concept "self-maintaining API" tool: scans a codebase for usage
of the Claude/Anthropic API and flags code that's broken (or about to
break) because of a known, dated API change.

## Two versions in this repo

- **`scan.py`** — v1, regex over the whole file. Fast to build, but tested
  against 5 real public repos (anthropic-cookbook, anthropic-sdk-python,
  llm, aider, OpenHands, litellm) and produced **1,576 findings total**,
  the vast majority false positives: comments, docstrings, string
  literals, and — in multi-provider codebases like litellm — other
  vendors' API calls that happened to share a method name with Anthropic's.

- **`ast_scan.py`** — v2, walks the real Python syntax tree instead of
  matching text. Only looks at actual `client.messages.create(...)` /
  `client.completions.create(...)` call sites, reads `model=` from *that
  same call*, and checks the client isn't actually OpenAI's SDK (which
  has a same-named legacy `completions.create` method). Same 6 repos:
  **17 findings total** — a ~99% drop in noise, and every remaining
  finding checks out on manual inspection.

## Rules

`rules.py` holds the current rule set, hand-extracted from Anthropic's
live release notes (as of 2026-08-27). `extract_rules.py` is the designed
(but not yet automated) path to generating these from the changelog via
an LLM call instead of by hand — needs an `ANTHROPIC_API_KEY` to run.

## Known limitations (be honest about these before building further)

- Only catches calls written in a fairly direct style. Code that builds
  the call via `**kwargs` splatting or heavy indirection won't be seen
  (confirmed: this is why `aider` shows 0 findings — it talks to Claude
  through `litellm`, not the Anthropic SDK directly, so there's nothing
  for this tool to see there yet).
- Python only. No JS/TS/Go support.
- Rules are still hand-maintained, not yet auto-generated on a schedule.
- No auto-fix / PR generation yet — detection only.

## Validated against

Real code: `/Users/markus/Desktop/oddsscanner` (clean — SDK pinned to
0.28.0, pre-v1.0, so the SDK-v1.0 rules don't apply yet; no other issues
found). Plus the 6 public repos above for false-positive testing.

## Next steps, roughly in priority order

1. Handle `**kwargs`-style calls (would unlock testing against litellm's
   own Anthropic provider code, and tools like aider that route through it).
2. Multi-language support (start with JS/TS via a tree-sitter parser).
3. Automate `extract_rules.py` on a schedule instead of running by hand.
4. Auto-fix: generate the actual code patch and open a PR, once detection
   has been trusted on more real-world code than just one project.
