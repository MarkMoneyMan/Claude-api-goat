/**
 * rules_js.js — the JS/TS-applicable subset of rules.py's rule set.
 *
 * Deliberately NOT a straight port of rules.py. Went through the same raw
 * changelog text (pipeline_runs/2026-08-27_changelog_input.txt +
 * the fuller fetch it was trimmed from) looking specifically for what's
 * confirmed to affect the TypeScript SDK, rather than assuming every
 * Python-flagged change applies equally.
 *
 * Two rule groups, based on what the changelog actually says:
 *
 * 1. API/request-level changes (the model or endpoint behaves this way no
 *    matter which language calls it): model deprecations/retirements,
 *    Opus 4.7 fast-mode removal, Opus 5 effort+thinking rejection,
 *    assistant-prefill removal, manual thinking-budget phase-out,
 *    experimental endpoint retirement. Included.
 *
 * 2. Changes the changelog explicitly names as landing in "Python SDK X,
 *    TypeScript SDK Y, ..." together: the beta.files/beta.skills header
 *    change (confirmed: TypeScript SDK 0.122.0) and the memory-list
 *    managed-agents header change (confirmed: TypeScript SDK 0.110.0).
 *    Included.
 *
 * Explicitly EXCLUDED, and why: sdk-v1-sampling-params-removed,
 * sdk-v1-text-completions-removed, python-sdk-v1-httpx-to-httpx2,
 * python-sdk-v1-compaction-control-removed,
 * python-sdk-v1-async-with-raw-response,
 * python-sdk-v1-bedrock-no-default-region, python-sdk-v1-min-python-version.
 * Every one of these is named "Python SDK v1.0 ..." in its own title/detail
 * in rules.py, because that's literally what the changelog said — a
 * packaging/implementation detail of the Python client library, not a
 * cross-language API behavior. There's no changelog evidence the
 * TypeScript SDK removed temperature/top_p/top_k from its method
 * signatures, swapped an HTTP library, or any of the rest. Rather than
 * guess by analogy, this is left an open question — see README.
 */

const RULES_JS = [
  {
    id: "model-retired-opus-4-1",
    pattern: /claude-opus-4-1-20250805/,
    appliesIfModel: null,
    severity: "HIGH",
    deadline: "2026-08-05 (already retired)",
    title: "Claude Opus 4.1 fully retired — this call will now error",
    detail: "Confirmed in the 2026-08-05 release notes: every request to claude-opus-4-1-20250805 now returns an error. API-level, applies regardless of SDK/language.",
    fix: "Upgrade to Claude Opus 5.",
  },
  {
    id: "model-deprecated-sonnet4-opus4",
    pattern: /claude-(sonnet|opus)-4-\d{8}/,
    appliesIfModel: null,
    severity: "HIGH",
    deadline: "deprecated — migrate now",
    title: "Pinned to a deprecated Claude 4 model snapshot",
    detail: "Claude Sonnet 4 / Opus 4 (dated snapshots) are deprecated in favor of the 4.5/5 line.",
    fix: "Update the model string to a current Sonnet/Opus 5 snapshot.",
  },
  {
    id: "fast-mode-removed-opus-4-7",
    pattern: /claude-opus-4-7[\s\S]{0,300}speed\s*:\s*['"]fast['"]/,
    appliesIfModel: null,
    severity: "MEDIUM",
    deadline: "2026-07-24 (Claude Opus 5 launch)",
    title: "Fast mode removed for Claude Opus 4.7 — now errors instead of falling back",
    detail: "Requests to claude-opus-4-7 with speed: \"fast\" now return an error instead of silently falling back to standard speed. Request-body-level, applies regardless of SDK/language.",
    fix: "Migrate to Claude Opus 5 or Opus 4.8 to keep using fast mode.",
  },
  {
    id: "opus5-effort-xhigh-thinking-disabled",
    pattern: /type\s*:\s*['"]disabled['"][\s\S]{0,200}effort\s*:\s*['"](xhigh|max)['"]|effort\s*:\s*['"](xhigh|max)['"][\s\S]{0,200}type\s*:\s*['"]disabled['"]/,
    appliesIfModel: ["opus-5"],
    severity: "MEDIUM",
    deadline: "2026-07-24 (Claude Opus 5 launch)",
    title: "Disabling thinking at xhigh/max effort is rejected on Opus 5",
    detail: "thinking: {type: \"disabled\"} combined with effort xhigh/max returns a 400 error on Opus 5.",
    fix: "Drop the explicit thinking disabled, or lower the effort level.",
  },
  {
    id: "manual-thinking-budget",
    pattern: /thinking\s*:\s*\{[^}]*budget_tokens/,
    appliesIfModel: ["opus-4-7", "opus-5", "sonnet-5"],
    severity: "HIGH",
    deadline: "already active",
    title: "Manual thinking budget rejected — needs adaptive thinking",
    detail: "Fixed budget_tokens is being phased out in favor of effort levels.",
    fix: 'Replace budget_tokens with an `effort` level (e.g. "high", "xhigh").',
  },
  {
    id: "assistant-prefill-removed",
    pattern: /role\s*:\s*['"]assistant['"][\s\S]{0,60}content\s*:/,
    appliesIfModel: ["opus-4-7", "opus-5", "sonnet-5"],
    severity: "MEDIUM",
    deadline: "already active",
    title: "Assistant message prefill removed on recent models",
    detail: "Pre-filling the start of the assistant's reply is no longer supported on newer models. Note: this pattern can't confirm the assistant message was the *last* one in the array (a real limitation, inherited from the same tradeoff in the original hand-written Python regex) — flag, don't assume.",
    fix: "Move the prefilled text into the system prompt instead.",
  },
  {
    id: "experimental-endpoint-retiring",
    pattern: /\/v1\/experimental\/(generate_prompt|improve_prompt|templatize_prompt)/,
    appliesIfModel: null,
    severity: "HIGH",
    deadline: "2026-08-17 (already passed)",
    title: "Experimental prompt endpoint retired",
    detail: "/v1/experimental/{generate,improve,templatize}_prompt was shut down 2026-08-17.",
    fix: "Migrate to the stable prompt-engineering workflow in the Console.",
  },
  {
    id: "beta-files-skills-sdk-shape-change",
    pattern: /client\.beta\.(files|skills)\b/,
    appliesIfModel: null,
    severity: "HIGH",
    deadline: "2026-08-27",
    title: "client.beta.files / client.beta.skills no longer send beta headers, return new shapes",
    detail: "Confirmed for TypeScript SDK 0.122.0: the beta namespace now behaves like the non-beta one. client.beta.skills.delete() now deletes all versions of a Skill, and BetaSkill is renamed BetaContainerSkill.",
    fix: "Migrate to client.files / client.skills directly; update BetaSkill references to BetaContainerSkill.",
  },
  {
    id: "memory-list-managed-agents-header-behavior-change",
    pattern: /managed-agents-2026-04-01/,
    appliesIfModel: null,
    severity: "MEDIUM",
    deadline: "2026-07-22",
    title: "managed-agents-2026-04-01 header adopts new memory listing behavior",
    detail: "Confirmed for TypeScript SDK 0.110.0: this header now returns results in stable server-defined order, restricts depth to 0/1/omitted, and requires path_prefix to end with '/'.",
    fix: "Switch to the agent-memory-2026-07-22 beta header and adjust order_by/depth/path_prefix usage.",
  },
  {
    id: "computer-use-toolset-new-shape",
    pattern: /computer_20251124/,
    appliesIfModel: null,
    severity: "MEDIUM",
    deadline: "2026-08-19",
    title: "computer_toolset_20260801 (GA) changes request shape vs. beta computer_20251124",
    detail: "The computer use tool is now GA as computer_toolset_20260801, changing request shape and tool handling vs. the beta computer_20251124.",
    fix: "Follow the migration guide before switching from computer_20251124 to computer_toolset_20260801.",
  },
];

module.exports = { RULES_JS };
