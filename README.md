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

1. ~~Handle `**kwargs`-style calls~~ — **done.** Built and validated
   (`example_project/bot.py` has a synthetic test case for it), but it
   changed **zero** findings across the 6 real repos. Turned out litellm
   doesn't call the official `anthropic` SDK at all in its own Anthropic
   integration — it reimplements the API at the HTTP level
   (`litellm/llms/anthropic/...`), so there was never an SDK call site
   there to find. Same root cause explains aider's 0 findings: it talks
   to Claude through litellm, never through `anthropic.Anthropic()`
   directly. Correcting the earlier claim that kwargs-handling would
   "unlock" either of them — it doesn't; that's a structurally different,
   bigger problem (would need to understand each abstraction layer's own
   API, not just the official SDK's).
2. ~~Automate `extract_rules.py`~~ — **ran for real on 2026-08-27**, first
   real (small) cost in this project. Fed it Anthropic's actual release
   notes (last ~8 weeks, `pipeline_runs/2026-08-27_changelog_input.txt`)
   with a real `ANTHROPIC_API_KEY`. Result:
   `pipeline_runs/2026-08-27_extracted_rules.json` — 14 rules extracted
   automatically. It correctly found all 6 breaking changes that had been
   hand-written into `rules.py` earlier (SDK v1.0 sampling params, legacy
   Text Completions removal, Opus 5 xhigh/max thinking error, Opus 4.7
   fast-mode removal, Opus 4.1 retirement, experimental prompt-tools
   retirement) **plus 8 more that hand-extraction had missed**: an
   httpx→httpx2 migration in SDK v1.0, `compaction_control` removal,
   an async `.with_raw_response` behavior change, `AnthropicBedrock`'s
   dropped default AWS region, the Python 3.10 floor, a `client.beta.files`
   /`client.beta.skills` shape change, a Managed Agents header behavior
   change, and a computer-use toolset shape change. One real bug found and
   fixed running this live: the first version hardcoded `max_tokens=4096`,
   which silently truncated the JSON output mid-string on a real-size
   changelog batch and threw a parse error — fixed by raising the limit
   and by making `extract()` raise a clear error on `stop_reason ==
   "max_tokens"` instead of failing on a cryptic JSON error.

   **Known gap, stated plainly:** these 14 auto-extracted rules use the
   v1 `rules.py` schema (regex `pattern` field) that `scan.py` reads —
   `ast_scan.py` (the good, low-noise v2 scanner) does **not** read
   `rules.py` at all; every check in it is still hand-coded per rule
   type. So automated extraction currently feeds the noisier scanner, not
   the one actually worth trusting. Closing that gap — teaching
   `ast_scan.py` to turn a generic extracted rule into an AST-level check
   — is real, non-trivial work and is now the top priority below.
3. Teach `ast_scan.py` to consume auto-extracted rules directly, instead
   of only hand-coded checks (the gap found in step 2).
4. Multi-language support (start with JS/TS via a tree-sitter parser).
5. Auto-fix: generate the actual code patch and open a PR, once detection
   has been trusted on more real-world code than just one project.
