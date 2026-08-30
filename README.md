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
  matching text. Started with hand-coded checks for the 9 hand-written
  rules only; as of 2026-08-27 it also runs a generic engine
  (`generic_scan()`) that turns any rule from `rules.py` — including ones
  `extract_rules.py` generates automatically — into an AST-level check,
  without hand-coding logic per rule. See "Rules" and step 2 below for how
  that engine earned its noise budget the hard way.

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

   **Known gap at the time, stated plainly:** these 14 auto-extracted
   rules used the v1 `rules.py` schema (regex `pattern` field) that
   `scan.py` reads — `ast_scan.py` (the good, low-noise v2 scanner) didn't
   read `rules.py` at all; every check in it was hand-coded per rule type.
   Closed in step 3 below.
3. ~~Teach `ast_scan.py` to consume auto-extracted rules~~ — **done, and
   it broke on the first real run, which is exactly why "run it for
   real" beats "looks right on paper."** Added `generic_scan()`: for each
   of the 8 new (non-hand-coded) rules, it regex-matches the rule's
   `pattern` against the unparsed source of individual real AST nodes
   (`Call`, `Import`, `Assign`, ...) — never the whole file, so it
   structurally can't match a comment or a docstring the way v1 did. First
   run against the same 6 repos: **litellm alone produced 1,720
   findings**, almost all from one rule (`python-sdk-v1-httpx-to-httpx2`,
   1,604 hits) and a second (`python-sdk-v1-async-with-raw-response`, 114
   hits). Both were the *same class* of bug as the very first
   `chat.completions.create` collision, just recurring at the pattern
   level instead of the file-context level:
   - `httpx` is a generic HTTP library. litellm imports it ~40 times for
     its own multi-provider handling and — confirmed by grep — never
     imports the actual `anthropic` package in any of them. The pattern
     alone can't tell "this httpx client feeds the Anthropic SDK" apart
     from "this httpx client does literally anything else."
   - `.with_raw_response` isn't Anthropic-specific either — it's a shared
     naming convention across every Stainless-generated SDK, and
     OpenAI's is one too. litellm's Azure/OpenAI calls
     (`azure_client.chat.completions.with_raw_response.create(...)`)
     matched it directly. Separately, the real breaking change only
     affects the *async* client, and the pattern had no async awareness
     at all — its single false-positive hit in `anthropic-sdk-python`
     itself was a **sync** test correctly calling `response.parse()` with
     no `await`, not broken code.

   Fix, in both cases: not a wider or narrower regex, but one real
   structural precondition per rule (`GENERIC_EXTRA_CONDITIONS` in
   `ast_scan.py`) — "this file actually imports `anthropic`" (checked
   correctly for *absolute* imports only; a second bug surfaced here too,
   since litellm's own `from ...anthropic.chat.transformation import X`
   is a *relative* import of its own same-named submodule and initially
   tripped the naive version of this check) and "this call site sits
   inside an `async def`." After both fixes, same 6 repos:
   **litellm 1,720 → 2, aider 11 → 0, anthropic-cookbook's 36 remaining
   findings all check out on inspection** (real `client.beta.files` /
   `client.beta.skills` calls that will genuinely need updating). One
   repo didn't clean up: `anthropic-sdk-python` still shows ~1,478,
   because it's not a fair test bed for these particular rules — it *is*
   the SDK, so its own source and test suite naturally define and
   exercise the exact strings these rules look for (e.g. the one
   `memory-list` hit inspected was the SDK's own source *defining* the
   `MANAGED_AGENTS_BETA` constant). That's a limitation of the test setup,
   not a scanner bug — but it's honest to say the generic engine has only
   been proven clean against real *downstream consumer* code, not against
   a library that mirrors its own rules back at itself.
4. Multi-language support (start with JS/TS via a tree-sitter parser).
5. Auto-fix: generate the actual code patch and open a PR, once detection
   has been trusted on more real-world code than just one project.
