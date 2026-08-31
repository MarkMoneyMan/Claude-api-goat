"""
Same idea as bot.py, but for OpenAI — a small app calling the OpenAI API
with a mix of a legacy pre-v1.0 call pattern, a custom httpx client that
needs the httpx2 migration, and one clean, unaffected call, so a scan
against this file has to find exactly the two real issues and nothing
else.
"""

import httpx
import openai
from openai import OpenAI

client = OpenAI(http_client=httpx.Client(timeout=30.0))  # <- httpx, needs httpx2


def get_completion(prompt):
    # Legacy pre-v1.0 module-level call style — still shows up in old
    # tutorials/scripts people copy-paste from.
    openai.api_key = "sk-..."
    return openai.ChatCompletion.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
    )


def get_completion_current(prompt):
    # The actually-current call shape — should NOT be flagged by anything.
    return client.chat.completions.create(
        model="gpt-5.6-sol",
        messages=[{"role": "user", "content": prompt}],
    )
