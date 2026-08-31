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

- **`js_scanner/`** — JS/TS sibling, added 2026-08-27. Node + `@babel/parser`
  instead of Python's `ast` module (simpler than getting a tree-sitter
  grammar built in this environment; same "walk the real syntax tree"
  idea). See "JS/TS support" below.

## Rules

`rules.py` holds the current rule set: 10 hand-extracted from Anthropic's
live release notes (as of 2026-08-27), plus whatever `sync_rules.py` has
appended automatically since. `extract_rules.py` is the LLM-extraction
step itself (changelog text in, structured rules out); `sync_rules.py`
is what actually runs it unattended — see "Rule sync" below. Both need
an `ANTHROPIC_API_KEY` to run for real.

## Known limitations (be honest about these before building further)

- Only catches calls written in a fairly direct style. Code that builds
  the call via `**kwargs` splatting or heavy indirection won't be seen
  (confirmed: this is why `aider` shows 0 findings — it talks to Claude
  through `litellm`, not the Anthropic SDK directly, so there's nothing
  for this tool to see there yet).
- Structurally blind to raw-HTTP integrations (a hand-built JSON body
  POSTed straight to `api.anthropic.com`, no official SDK in the call
  path at all) — every rule matches an SDK call *shape*, so there's
  nothing for the AST walk to find. Confirmed twice now, not just
  theorized: litellm's own Anthropic integration (HTTP-level, not
  SDK-level) and a legacy Node.js prototype inside `oddsscanner`
  (`server.js`) both produce 0 findings for this reason, not because
  they're actually safe. A future rule format that also matches literal
  header/param strings in a raw request body could close part of this,
  but nothing like that exists yet.
- Python only for detection; JS/TS added as a first pass (see below), no
  Go/other-language support.
- Rule *sync* runs on a schedule now (`sync_rules.py` +
  `update-rules.yml`), but every extracted rule still goes through a PR a
  human reviews before it's live — deliberately not fully unattended.

## Validated against

Real code: `/Users/markus/Desktop/oddsscanner`, re-run directly (not
through CI — that repo has no `.git` yet) against the full current rule
set, Python and JS both, once the rule sync work above made the rule
count grow well past the original 6.

- **`app.py` (the live backend — `start.sh`/`start.bat` both run this,
  port 5000):** clean. Confirmed by reading the actual call site, not
  just trusting the scanner: `anthropic.Anthropic(...)`,
  `client.messages.create(model="claude-sonnet-4-6", max_tokens=...,
  system=[...], messages=[...])` — no temperature/top_p/top_k, no
  `.with_raw_response`, no beta headers, no `AnthropicBedrock`. SDK is
  pinned to `anthropic==0.28.0`, well below v1.0, so the SDK-v1.0 rules
  correctly don't fire yet — this is a true negative, not a blind spot.
- **`server.js` + `index.html` (an older Node.js prototype, both dated
  well before `app.py` and `static/index.html`, and not what
  `start.sh`/`start.bat` actually launch):** also 0 findings, but for a
  reason worth stating plainly rather than taking credit for: this code
  never calls the Anthropic SDK at all. It hand-builds a JSON body and
  POSTs it to `https://api.anthropic.com/v1/messages` with Node's raw
  `https` module. Every rule in `rules_js.js` matches SDK call *shapes*
  (`.messages.create(...)`, `.beta.files`, ...), so there is structurally
  nothing here for it to match — the same class of blind spot already
  documented for litellm's own Anthropic integration, now confirmed in a
  second, real, personally-used codebase rather than just a public one.
  Concretely: this dead path has a hardcoded, dated model snapshot
  (`claude-sonnet-4-20250514`) that a raw-HTTP-aware rule set would
  reasonably flag someday — worth knowing about even though it's not
  live traffic today.

Plus the 6 public repos above for false-positive testing.

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

   **Update, found building the JS/TS scanner below:** that ~1,478 number
   was itself inflated by a real bug, not just the self-referential-repo
   problem — `generic_scan`'s candidate node types overlap (a `Call` is a
   child of the `Assign` that captures its result, e.g. `client =
   AnthropicBedrock(...)`), so the same real match got reported twice, once
   per node. Confirmed: 427 of the 1,478 were exact-duplicate
   `(file, line, rule_id)` triples. Fixed with a `dedupe_findings()` pass
   that also prefers the more informative duplicate (a model-scoped rule
   can only confirm the model on the `Call` node itself, never on the
   wrapping `Assign` — naive dedup could keep the less-informative
   "unconfirmed" copy). Real count for `anthropic-sdk-python`: **1,051**,
   still mostly the self-referential-repo effect, not noise.
4. ~~Multi-language support (start with JS/TS)~~ — **done as a first pass**,
   see "JS/TS support" below.
5. ~~Auto-fix: generate the actual code patch~~ — **done for a small,
   deliberately mechanical subset.** See "Auto-fix" below. ("...and open a
   PR" is now just the `git`/`gh` mechanics on top of a real patch — not
   attempted against a real third-party repo without being asked to.)
6. ~~Package as a CI Action~~ — **done**, and it found a real bug on its
   first real Actions run. See "CI / GitHub Action" below.
7. ~~Automate the rule-extraction step end-to-end (not just "ran once by
   hand")~~ — **done.** `sync_rules.py` + `.github/workflows/update-rules.yml`
   run this on a schedule now instead of a human copy-pasting changelog
   text into a file. See "Rule sync" below.

## Auto-fix

`autofix.py` generates real source patches — not suggestions in a report —
for **5 of the ~19 rules**, chosen because the fix is a pure deletion or a
1:1 string swap with no judgment call attached (no "which model should
this migrate to," no "how should this system-prompt instruction be
phrased," no "which effort level is right here"). Everything else stays
detection-only on purpose: a wrong regex was already the first act of this
project (`scan.py`); a wrong auto-fix rewrites someone's actual code, which
is a worse failure than not fixing it. See the module docstring in
`autofix.py` for the full list and the reasoning per rule, fixed and
not-fixed alike.

Every patch goes through one hard gate before it's ever written: the
patched file must still parse (`ast.parse`) or the patch is refused and
logged, never applied. That gate mattered for real, immediately — first
run against a real repo (a copy of `anthropic-cookbook`) hit a genuine bug
in the edit engine: deleting the *last* keyword argument in a call only
scanned backward through same-line whitespace looking for the separating
comma, so when the previous argument was on an earlier line (the common
one-arg-per-line style), it never found that comma and left the deleted
argument's own trailing comma orphaned on its own line — invalid syntax.
The parse gate caught it before anything was written; the practical effect
was just a silently-skipped fix, not corrupted code. Fixed by mirroring
the already-correct forward-scanning logic (cross one newline + its
indentation, not just spaces/tabs on the same line) and re-verified.

**Validated:** `example_project/autofix_test.py` has one call per
auto-fixable rule plus two calls that must NOT be touched (a deprecated
model string, a manual thinking budget) — confirmed after fixing: the 5
fixable ones are gone, the 2 judgment-call ones are untouched, the file
still parses. Then for real: ran `--write` against a full copy of
`anthropic-cookbook`. Result: **20 edits across 9 real files**, every
patched file still parses, and rescanning afterward shows only the 4
`assistant-prefill-removed` findings left — exactly the ones this tool
was never supposed to touch. Known gap: the fixer only edits a direct
keyword argument on the call site itself, not one assembled in a `**kwargs`
dict elsewhere (the same splat-resolution limitation `ast_scan.py`'s
*detection* side already handles for reading, but hasn't been extended to
for writing) — one real `temperature=` finding in cookbook was left
un-autofixed for exactly this reason, correctly, rather than attempting an
edit somewhere else in the file it wasn't confident about.

Also fixed along the way: `ast_scan.py` and `scan.py` silently reported
"no findings" when pointed at a single file instead of a directory
(`Path(file).rglob("*.py")` returns an empty iterator, not an error) — a
false "all clear" is the exact failure mode this whole project exists to
prevent, so worth fixing the moment building/testing `autofix.py` on a
single file actually hit it.

## JS/TS support

`js_scanner/ast_scan.js` — same "walk the real tree, match node-by-node,
never the whole file" idea as `ast_scan.py`, ported to JS/TS. Built the
generic engine directly from the start this time (no separate hand-coded
phase first) — there was no reason to relearn the lesson from the Python
side about testing against real repos before trusting a rule set.

**Rule set is deliberately smaller than Python's.** Went back through the
same raw changelog text looking specifically for what's confirmed to touch
the TypeScript SDK, rather than assuming every Python-flagged change
applies by analogy. Included: API/request-level changes that don't care
which language calls them (model deprecations, Opus 4.7 fast-mode removal,
Opus 5 effort+thinking rejection, assistant-prefill removal, experimental
endpoint retirement), plus the two changes the changelog explicitly names
"Python SDK X, TypeScript SDK Y, ...": the `beta.files`/`beta.skills`
shape change and the memory-list header change. Excluded: every rule whose
own title says "Python SDK v1.0" (httpx→httpx2, `compaction_control`,
async `.with_raw_response`, Bedrock's default region, the Python 3.10
floor) — those are Python-package-internal, and there's no changelog
evidence the TypeScript SDK did the same thing. Left as an open question
rather than guessed.

**First live run found 4 real bugs, same pattern as every other "test it
for real" pass in this project:**

1. A crash, not just noise: `@babel/traverse`'s scope-crawling threw an
   uncaught error on one real file in `vercel/ai` (a valid-but-unusual TS
   type/value naming collision) and killed the *entire* batch scan, losing
   every finding already collected. Fixed with a per-file try/catch, same
   principle as `ast.parse`'s `SyntaxError` being caught per-file in
   Python, just a different failure mode (traverse-time, not parse-time).
2. The same cross-node duplicate-finding bug described in step 3 above —
   found in Python first, then confirmed live here too by literally
   translating the same fix and watching it matter immediately.
3. `assistant-prefill-removed` regex-matched **1,619 times** in
   `vercel/ai` alone: `role: "assistant"` near `content:` is the shape of
   *any* code representing an assistant chat message at all (rendering
   history, type defs, test fixtures), not specifically "the last message
   of an outgoing request." Fixed by pulling this one rule out of the
   generic engine entirely and porting the precise version of the check
   from `ast_scan.py`'s `extract_messages_prefill()` — only the literal
   last element of an actual `messages.create()` call's `messages` array
   counts.
4. Two variations on "a candidate node's span can be bigger than it
   looks": a JS test-framework call like
   `describe('X', () => { ...whole rest of the file... })` is itself one
   `CallExpression`, so anything anywhere in that block counted as a
   "match" on the outer call; a large `expect(x).toMatchObject({ ...huge
   mock... })` has the same problem without being a callback. Fixed the
   first with a structural check (skip a call whose argument is a function
   with a real body — traversal still walks into it, so a real Anthropic
   call nested inside still gets checked on its own node) and the second
   with a blunter 2000-character snippet cap, documented as a safety valve
   rather than a precise fix.

**Net result**, tested against `anthropic-sdk-typescript` (the SDK's own
repo — same self-referential-test-bed caveat as the Python side applies)
and `vercel/ai` (a real, large downstream consumer): `vercel/ai` went
1,734 → 67 findings across the 4 fixes above, and the 67 remaining check
out on inspection (real references to `computer_20251124`, a real
deprecated-model-string literal, etc. — see git history for the exact
before/after JSON if you want to see the noise that got cut). Only tested
against 2 real repos so far, not 6 like the Python side — this is
explicitly a first pass, not yet hardened to the same degree.

## Rule sync

`sync_rules.py` is what actually makes this project "self-maintaining"
instead of "a scanner someone has to remember to update by hand." It:

1. fetches `https://platform.claude.com/docs/en/release-notes/overview.md`
   — appending `.md` to a `platform.claude.com/docs/...` URL returns raw
   markdown instead of the rendered page, found by trying it, not
   documented anywhere, and much easier to parse reliably than scraping
   HTML;
2. splits it into dated sections and keeps only the ones newer than
   `pipeline_runs/last_synced.json`'s stored date, so a weekly run doesn't
   re-fetch and re-pay for the same 2+ years of history every time;
3. hands just the new text to `extract_rules.py`'s `extract()` — the same
   extraction used for the one-off manual run that seeded `rules.py`;
4. drops any extracted rule whose `id` already exists in `rules.py`
   (defense against the same change getting described slightly
   differently on a re-run);
5. appends whatever's left as a new dated block (`RULES_AUTO_<date> = [...]`
   `RULES = RULES + RULES_AUTO_<date>`), and advances the synced-through
   date regardless of whether anything new was found, so a week with only
   additive (non-breaking) changes doesn't get re-processed forever.

`.github/workflows/update-rules.yml` runs it weekly (Mondays) and on
`workflow_dispatch`, then hands off to `peter-evans/create-pull-request`
— same no-commit-if-nothing-changed pattern as `autofix-weekly.yml`, on
a fixed branch name so a run before last week's PR merges updates that
PR instead of opening a duplicate. **Needs a repo secret,
`ANTHROPIC_API_KEY`, that hasn't been added to the real repo yet** — the
workflow fails loudly rather than silently skipping if it's missing.

**What's tested and how, stated plainly:** this cloud environment's own
network egress blocks `platform.claude.com` directly (confirmed — a plain
`curl` and `urllib.request` both get rejected by the sandbox's proxy, an
environment restriction, not a bug in the fetch code), so the actual
`fetch_changelog_markdown()` HTTP call hasn't run inside this box. It has
been tested with the *real* page content, though: `WebFetch` (which goes
through a different path) pulled the live `.md` page directly, and that
real output — all 135 dated sections back to May 2024 — was fed through
the parser and dedupe/merge logic directly. That's how a real bug got
caught before this ever ran unattended: older entries use ordinal day
suffixes ("`April 9th, 2025`", "`March 31st, 2025`") that `strptime`
can't parse, while recent ones don't ("`August 27, 2026`") — the first
version silently dropped every suffixed section instead of erroring,
which would have been a **quiet under-processing bug**, not a crash (the
exact failure shape this whole project tries to catch in *other* code).
Fixed by stripping the suffix before parsing; re-tested against the same
135 sections, all parse correctly now. Separately verified end-to-end
with synthetic candidate rules (bypassing the real API call): dedup
correctly skips a rule whose id already exists, keeps a genuinely new
one, appends a block that keeps `rules.py` parsing as valid Python, and
the newly appended rule is immediately usable by `ast_scan.py` — it
found the synthetic rule's trigger pattern in a test fixture, same as any
hand-written rule would. **Update:** ran for real on GitHub Actions
(`update-rules.yml` run #1, `workflow_dispatch`, after the
`ANTHROPIC_API_KEY` secret was added) — succeeded in 14s. That confirms
the actual `fetch_changelog_markdown()` HTTP call works from a real
runner (this sandbox's own egress blocks it, so it had only ever been
exercised with a pre-fetched copy of the page before this), and that the
secret is read correctly. The 14s runtime is itself informative: too
fast to have called the model, consistent with hitting the "nothing new
since 2026-08-27" fast path and exiting before ever importing
`extract_rules`. Confirmed on GitHub afterward: no PR was opened — the
"nothing changed, don't bother `create-pull-request`" path behaves
correctly for real, not just in the code reading right. What's still
genuinely untested: the extraction call itself firing for real, which
needs an actual new changelog entry to show up — that'll happen on its
own whenever Anthropic next publishes one and the Monday schedule (or a
manual run) picks it up.

## CI / GitHub Action

`action.yml` packages the Python (and optionally JS/TS) scanner as a
composite GitHub Action, so a project can get checked on every PR instead
of someone running `ast_scan.py` by hand and remembering to. Two pieces:

- **`action.yml` + `action_combine.py`** — the action itself. Runs
  `ast_scan.py` (and `js_scanner/ast_scan.js` if `scan-js: true`), merges
  whatever findings files actually exist, and fails the job only at or
  above a configurable `fail-on` severity (default `HIGH`) — a MEDIUM/LOW
  heads-up shouldn't block a merge the way a HIGH one should.
- **`.github/workflows/self-check.yml`** — dogfoods the action against
  *this* repo on every push: one job asserts the severity gate correctly
  **fails** against `example_project/` (which has known HIGH findings by
  design), the other asserts it correctly **passes** against a dedicated
  known-clean fixture, `ci_fixtures/known_clean.py`. Both are assertions
  about the action's own correctness, not about this repo's code health.

  That second job originally pointed at `rules.py` itself, on the
  reasoning "it doesn't call the Anthropic API, so it should be clean."
  The first real run on GitHub Actions (run #1, commit `fedb0e7`) came
  back red. Reproduced locally with `python3 ast_scan.py rules.py`: 7
  findings, several HIGH. The reasoning was wrong — "doesn't call the
  API" and "contains no matching text" aren't the same property, and
  `rules.py`'s entire job is to store the literal trigger strings (like
  `client.beta.files`, `managed-agents-2026-04-01`) as rule data, so the
  generic engine's `ast.Assign` matching legitimately finds them there.
  Fixed by pointing the job at a small, deliberately unrelated fixture
  file instead of reusing a file whose actual purpose guarantees it can
  never be "clean." Caught by getting a real Actions run — this is
  exactly the class of bug local YAML validation and the unit-tested
  Python logic couldn't have found (see below).
- **`examples/consumer-workflows/`** — two templates (`check-on-pr.yml`,
  a weekly `autofix-weekly.yml` that opens a PR via the well-established
  `peter-evans/create-pull-request` action when `autofix.py` finds
  something to fix) showing how a *downstream* project would wire this
  in. Now point at the real `MarkMoneyMan/Claude-api-goat@master`
  instead of a placeholder — see "Publishing" below for the access
  caveats that come with that repo being private.

**What's validated and what isn't, stated plainly:** all 4 YAML files
parse as valid YAML, and the Python logic each step actually calls
(`ast_scan.py`'s exit code, `action_combine.py`'s severity gate and
`$GITHUB_OUTPUT` writing) was tested directly and behaves correctly across
all 3 cases that matter — findings at/above threshold, findings below
`fail-on`, and no findings. Local testing stopped there: `nektos/act` (a
local Actions runner) installed fine but needs a Docker daemon to spin up
runner containers, and this environment doesn't have one running
(`docker info` confirms no daemon, not just a missing CLI).

That gap got closed for real once the repo was published (see
"Publishing" below): `self-check.yml` ran on actual GitHub Actions and
immediately found a real bug — the `rules.py`-as-known-clean-fixture
mistake described above — that no amount of local YAML validation or
unit-tested Python logic could have surfaced, because the bug wasn't in
the YAML wiring or the scanner logic, it was in a *test's assumption*
about its own fixture. After swapping in `ci_fixtures/known_clean.py`
and re-pushing, run #2 (commit `7a97f7f`) went green on both jobs —
confirmed end-to-end on real GitHub Actions, not just locally. That's
the whole point of dogfooding this against a real remote instead of
stopping at "the YAML looks right": the bug this section describes only
existed to find because a real run happened.

## Publishing

Published to a real (private) GitHub repository:
`github.com/MarkMoneyMan/Claude-api-goat`. Getting there needed two
rounds of Personal Access Token permission fixes — GitHub refuses to let
a token without "Workflows" scope push changes to `.github/workflows/*`,
even if it already has "Contents: Read and write" — which isn't obvious
until the push is rejected with that exact error.

Both consumer-workflow templates now point at the real
`MarkMoneyMan/Claude-api-goat@master` instead of the old
`YOUR-GITHUB-USERNAME` placeholder, but "private" isn't free to work
around — two different mechanisms are involved, and they were kept
separate deliberately rather than papered over:

- `check-on-pr.yml`'s `uses: MarkMoneyMan/Claude-api-goat@master` (an
  *action reference*) works for a same-account repo like OddsScanner with
  no extra setup — GitHub's repo Settings → Actions → General → "Access"
  on Claude-api-goat covers this case, and same-account repos get it by
  default.
- `autofix-weekly.yml`'s `actions/checkout` step with
  `repository: MarkMoneyMan/Claude-api-goat` (*cloning a second repo's
  contents*, to get `autofix.py` itself) is a different mechanism — the
  default `GITHUB_TOKEN` a workflow run gets is scoped only to the repo
  it's running in, same-account or not. That step needs a `token:` input
  pointing at a PAT (read-only "Contents" scope on Claude-api-goat is
  enough) stored as a secret in the *downstream* repo. Not yet set up in
  OddsScanner — that's the actual remaining step now, not the placeholder
  swap.
