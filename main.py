"""
Simcluster AI Agent — Railway deployment
Uses the Anthropic API + Simcluster MCP to autonomously play Simcluster.

Required env vars:
  ANTHROPIC_API_KEY        — from console.anthropic.com
  SIMCLUSTER_BEARER_TOKEN  — from simcluster.ai/agent/connect
  TELEGRAM_BOT_TOKEN       — from @BotFather (optional but recommended)
  TELEGRAM_CHAT_ID         — from @userinfobot (optional but recommended)

Optional:
  HEARTBEAT_HOURS          — how often to run (default: 4)
"""

import asyncio
import os
import json
import logging
from datetime import datetime
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

ANTHROPIC_API_KEY       = os.environ.get("ANTHROPIC_API_KEY", "")
SIMCLUSTER_BEARER_TOKEN = os.environ.get("SIMCLUSTER_BEARER_TOKEN", "")
TELEGRAM_BOT_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID        = os.environ.get("TELEGRAM_CHAT_ID", "")
HEARTBEAT_HOURS         = float(os.environ.get("HEARTBEAT_HOURS", "4"))

SYSTEM_PROMPT = """
You are an autonomous Simcluster agent. Simcluster is a social AI game at simcluster.ai where you earn Clout (¢) by posting content and owning concepts.

On each session, do the following in order:

1. Run agent.onboarding first if you haven't already.
2. Check agent.sessionStatus — read your clout balance, daily spend remaining, posts remaining, and account status.
3. Check bounties.getDailySignInBountyStatus — if the daily sign-in bonus is ready, note it prominently.
4. Check bounties.checkDailyBillboardProgress — if the billboard bonus is ready, note it prominently.
5. Check notifications.list — summarize anything important.
6. Search agent.concepts.search with sort=trending to find the top trending concepts.
7. Check bounties.listBillboardConcepts to see the current billboard top 10.
8. If you have posts remaining and enough spend budget (at least 30¢):
   - Generate a text post using create.text with 1-2 good concept IDs (prefer a billboard concept + a trending one).
   - Publish it with create.post using the textCompletionShortId and mediaShortIds=[].
9. If your total clout (regular + virtual) is above 400¢, look for an unclaimed trending concept under 600¢ and claim it using agent.concepts.create (generate definition with create.concept.generateNewConceptDefinition and styling with create.concept.suggestConceptStyling first).
10. Write a short, punchy summary of everything you did and found. Include:
    - Current clout balance
    - Whether daily bonuses are ready to claim (remind the user to visit simcluster.ai/bonuses)
    - What you posted (with link)
    - What concept you claimed (with link), if any
    - Any interesting notifications or trends

Be concise and action-oriented. Use real numbers from the tools. Never make up data.
""".strip()


# ─────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────

async def telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            })
            if r.status_code == 200:
                log.info("Telegram sent ✓")
            else:
                log.warning("Telegram error: %s", r.text[:200])
    except Exception as e:
        log.warning("Telegram failed: %s", e)


# ─────────────────────────────────────────────
# Anthropic API call with Simcluster MCP
# ─────────────────────────────────────────────

async def run_agent() -> str:
    """Ask Claude to play Simcluster for one session. Returns Claude's summary."""
    log.info("Calling Anthropic API with Simcluster MCP...")

    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": f"It is {datetime.now().strftime('%A, %Y-%m-%d %H:%M UTC')}. Please run your Simcluster session now."
            }
        ],
        "tools": [
            {
                "type": "mcp_toolset",
                "mcp_server_name": "simcluster"
            }
        ],
        "mcp_servers": [
            {
                "type": "url",
                "url": "https://simcluster.ai/mcp",
                "name": "simcluster",
                "authorization_token": SIMCLUSTER_BEARER_TOKEN
            }
        ]
    }

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "mcp-client-2025-11-20",
                "content-type": "application/json",
            },
            json=payload,
        )
        r.raise_for_status()
        data = r.json()

    # Log tool calls
    tool_calls = [b["name"] for b in data.get("content", []) if b.get("type") == "mcp_tool_use"]
    if tool_calls:
        log.info("Tools used: %s", ", ".join(tool_calls))

    # Extract text summary
    summary = " ".join(
        b["text"] for b in data.get("content", []) if b.get("type") == "text"
    ).strip()

    return summary or "(no summary returned)"


# ─────────────────────────────────────────────
# Heartbeat
# ─────────────────────────────────────────────

async def heartbeat():
    log.info("━━━━  HEARTBEAT  %s UTC  ━━━━", datetime.now().strftime("%Y-%m-%d %H:%M"))

    summary = await run_agent()

    log.info("Agent summary:\n%s", summary)

    # Send to Telegram
    msg = f"🤖 <b>Simcluster Agent — {datetime.now().strftime('%b %d, %H:%M')}</b>\n\n{summary}"
    await telegram(msg)

    log.info("━━━━  HEARTBEAT COMPLETE  ━━━━")


async def main():
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not SIMCLUSTER_BEARER_TOKEN:
        missing.append("SIMCLUSTER_BEARER_TOKEN")
    if missing:
        log.error("❌  Missing required env vars: %s", ", ".join(missing))
        log.error("    Add them in Railway → your service → Variables")
        return

    log.info("Simcluster AI agent starting. Heartbeat every %.1fh.", HEARTBEAT_HOURS)

    while True:
        try:
            await heartbeat()
        except Exception as e:
            log.error("Heartbeat crashed: %s", e, exc_info=True)
            await telegram(f"⚠️ <b>Simcluster Agent crashed</b>\n\n{str(e)[:300]}")
        await asyncio.sleep(HEARTBEAT_HOURS * 3600)


if __name__ == "__main__":
    asyncio.run(main())
