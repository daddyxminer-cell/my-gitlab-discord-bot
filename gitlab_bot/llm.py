"""
Optional AI judge for borderline projects (OFF unless LLM_API_KEY / OPENAI_API_KEY is set).

The regex/file score decides the clear cases. Only projects close to the threshold are sent here, with
their README, file list and key code snippets. If the call fails for any reason the bot silently keeps the
rule-based verdict -- the AI can never block the bot.
"""
from __future__ import annotations

import json
import logging
import os
import re

import requests

from classify import Repo

log = logging.getLogger("gl-bot")

SYSTEM = (
    "You classify brand-new open-source repositories for a crypto researcher. They want ONLY: "
    "(a) a real new blockchain/cryptocurrency that is mined with Proof-of-Work (own chain, genesis/consensus code or spec), "
    "or (b) a real new blockchain/network project where people must run a node (masternode, validator, incentivised testnet, DePIN node), "
    "at the PRE-TESTNET or TESTNET stage (mainnet already live = not wanted). "
    "NOT wanted: tutorials, 'build a blockchain in Python' demos, student projects, ERC-20/meme tokens, trading/mining bots, "
    "miner dashboards, captcha/anti-bot proof-of-work, password hashing, portfolios, unmodified copies of Bitcoin/Monero/etc, spam. "
    "Answer with JSON only: {\"wanted\": true|false, \"kind\": \"pow\"|\"node\"|\"none\", \"stage\": \"pre-testnet\"|\"testnet\"|\"mainnet\"|\"unknown\", "
    "\"algorithm\": string|null, \"confidence\": 0..1, \"reason\": short string}"
)


def available() -> bool:
    return bool(os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY"))


def _snippet(repo: Repo) -> str:
    parts = [f"name: {repo.full_name}", f"description: {repo.description}", f"topics: {', '.join(repo.topics)}",
             f"rule-based score: {repo.score}  signals: {' '.join(repo.reasons[:30])}",
             "files: " + ", ".join(repo.paths[:120]),
             "README/docs:\n" + (repo.text or "")[:5000]]
    for p, t in list(repo.code.items())[:3]:
        parts.append(f"--- {p} ---\n{t[:1500]}")
    return "\n".join(parts)[:12000]


def judge(repo: Repo) -> dict | None:
    key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    base = (os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("LLM_MODEL") or "gpt-4o-mini"
    try:
        r = requests.post(f"{base}/chat/completions", timeout=60,
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": model, "temperature": 0,
                                "messages": [{"role": "system", "content": SYSTEM},
                                             {"role": "user", "content": _snippet(repo)}]})
        r.raise_for_status()
        txt = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", txt, re.S)
        data = json.loads(m.group(0)) if m else None
        if not isinstance(data, dict) or "wanted" not in data:
            return None
        data["confidence"] = float(data.get("confidence") or 0)
        return data
    except Exception as e:  # noqa: BLE001 - the judge must never break the bot
        log.warning("AI judge unavailable (%s) -- using rule-based verdict", str(e)[:120])
        return None
