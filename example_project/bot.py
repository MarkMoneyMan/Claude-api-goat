"""
Example file simulating a small app that calls the Claude API — this
mimics the kind of code you'd find in a real project (like OddsScanner),
including a couple of patterns that were fine a year ago but break today.
"""

import anthropic

client = anthropic.Anthropic()


def get_betting_tip(match_summary):
    response = client.messages.create(
        model="claude-opus-5-20260501",
        max_tokens=1024,
        temperature=0.7,  # <- non-default sampling param, rejected on Opus 5
        messages=[
            {"role": "user", "content": f"Give a betting tip for: {match_summary}"},
            {"role": "assistant", "content": "Based on the data, I'd lean towards"},  # <- prefill removed
        ],
    )
    return response.content[0].text


def get_deep_analysis(match_summary):
    response = client.messages.create(
        model="claude-opus-5-20260501",
        max_tokens=2048,
        thinking={"type": "enabled", "budget_tokens": 4000},  # <- manual thinking budget, needs migration
        messages=[
            {"role": "user", "content": f"Deeply analyze: {match_summary}"},
        ],
    )
    return response.content[0].text


def get_summary_old_model(text):
    # Still pinned to an old, deprecated snapshot
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=512,
        messages=[{"role": "user", "content": text}],
    )
    return response.content[0].text
