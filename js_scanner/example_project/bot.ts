// TS sibling of ../../example_project/bot.py — same intentional issues,
// translated to the TypeScript SDK's call shape, to sanity-check ast_scan.js
// before pointing it at real repos.

import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic();

async function getBettingTip(matchSummary: string) {
  const response = await client.messages.create({
    model: "claude-opus-5-20260501",
    max_tokens: 1024,
    messages: [
      { role: "user", content: `Give a betting tip for: ${matchSummary}` },
      { role: "assistant", content: "Based on the data, I'd lean towards" }, // <- prefill removed
    ],
  });
  return response.content[0];
}

async function getDeepAnalysis(matchSummary: string) {
  const response = await client.messages.create({
    model: "claude-opus-5-20260501",
    max_tokens: 2048,
    thinking: { type: "enabled", budget_tokens: 4000 }, // <- manual thinking budget, needs migration
    messages: [{ role: "user", content: `Deeply analyze: ${matchSummary}` }],
  });
  return response.content[0];
}

async function getSummaryOldModel(text: string) {
  // Still pinned to an old, deprecated snapshot
  const response = await client.messages.create({
    model: "claude-sonnet-4-20250514",
    max_tokens: 512,
    messages: [{ role: "user", content: text }],
  });
  return response.content[0];
}

async function listSkills() {
  // Uses the beta namespace that's about to change shape
  return client.beta.skills.list();
}
