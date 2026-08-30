"""
Dedicated test fixture for autofix.py — covers all 5 rule ids it claims to
be able to fix, one call per rule, so a before/after diff can be checked
against exactly what should (and shouldn't) change. bot.py already covers
the "does detection still work on the untouched issues" side; this file is
about the fixer itself.
"""

import anthropic

client = anthropic.Anthropic()


def fixable_sampling_params():
    return client.messages.create(
        model="claude-opus-5-20260501",
        max_tokens=100,
        temperature=0.9,
        top_p=0.8,
        messages=[{"role": "user", "content": "hi"}],
    )


def fixable_fast_mode():
    return client.messages.create(
        model="claude-opus-4-7-20260210",
        max_tokens=100,
        speed="fast",
        messages=[{"role": "user", "content": "hi"}],
    )


def fixable_thinking_disabled_at_xhigh():
    return client.messages.create(
        model="claude-opus-5-20260501",
        max_tokens=100,
        effort="xhigh",
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": "hi"}],
    )


def fixable_beta_files():
    return client.beta.files.list()


def fixable_beta_skills():
    return client.beta.skills.create(display_title="x", files=[])


def fixable_memory_header():
    return client.beta.memory.list(betas=["managed-agents-2026-04-01"])


# Should NOT be touched by autofix.py — these need a human judgment call.
def not_fixable_model_deprecated():
    return client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
    )


def not_fixable_manual_thinking_budget():
    return client.messages.create(
        model="claude-opus-5-20260501",
        max_tokens=100,
        thinking={"type": "enabled", "budget_tokens": 4000},
        messages=[{"role": "user", "content": "hi"}],
    )
