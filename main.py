"""
Simcluster AI Agent — Railway deployment
Uses the Anthropic SDK + Simcluster MCP to autonomously play Simcluster.

Required env vars:
  ANTHROPIC_API_KEY        — from console.anthropic.com
  SIMCLUSTER_BEARER_TOKEN  — from simcluster.ai/agent/connect
  TELEGRAM_BOT_TOKEN       — from @BotFather (optional)
  TELEGRAM_CHAT_ID         — from @userinfobot (optional)

Optional:
  HEARTBEAT_HOURS          — how often to run (default: 4)
"""

import asyncio
import os
import logging
from datetime import datetime
import httpx
import anthropic

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

1. Run agent.onboarding first.
2. Check agent.sessionStatus — read your clout balance, daily spend remaining, posts remaining, and account status.
3. Check bounties.getDailySignInBountyStatus — if the daily sign-in bonus is ready, note it prominently.
4. Check bounties.checkDailyBillboardProgress — if the billboard bonus is ready, note it prominently.
5. Check notifications.list for anything important.
6. Search agent.concepts.search with sort=trending to find trending concepts.
7. Check bounties.listBillboardConcepts to see the current billboard top 10.
8. If you have posts remaining and enough spend budget (at least 30¢):
   - Generate a post using create.text with 1-2 concept IDs (prefer a billboard concept + a trending one).
   - Publish it with create.post using the textCompletionShortId and mediaShortIds=[].
9. If total clout (regular + virtual) is above 400¢, look for an unclaimed trending concept under 600¢ and claim it:
   - Generate definition with create.concept.generateNewConceptDefinition
   - Get styling with create.concept.suggestConceptStyling
   - Claim with agent.concepts.create
10. Write a short summary with: clout balance, bonus status, what you posted (with link), what concept you claimed (with link), interesting trends.

Be concise and action-oriented. Use real numbers. Never make up data.
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
# Agent
# ─────────────────────────────────────────────

def run_agent() -> str:
    """Ask Claude to play Simcluster. Returns Claude's summary."""
    log.info("Calling Anthropic API with Simcluster MCP...")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    response = client.beta.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"It is {datetime.now().strftime('%A, %Y-%m-%d %H:%M UTC')}. Please run your Simcluster session now."
            }
        ],
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": "simcluster",
            }
        ],
        mcp_servers=[
            {
                "type": "url",
                "url": "https://simcluster.ai/mcp",
                "name": "simcluster",
                "authorization_token": SIMCLUSTER_BEARER_TOKEN,
            }
        ],
        betas=["mcp-client-2025-11-20"],
    )

    # Log tool calls
    tool_calls = [b.name for b in response.content if hasattr(b, "name") and b.type == "mcp_tool_use"]
    if tool_calls:
        log.info("Tools used: %s", ", ".join(tool_calls))

    # Extract text summary
    summary = " ".join(
        b.text for b in response.content if hasattr(b, "text") and b.type == "text"
    ).strip()

    return summary or "(no summary returned)"


# ─────────────────────────────────────────────
# Heartbeat
# ─────────────────────────────────────────────

async def heartbeat():
    log.info("━━━━  HEARTBEAT  %s UTC  ━━━━", datetime.now().strftime("%Y-%m-%d %H:%M"))

    summary = await asyncio.to_thread(run_agent)

    log.info("Agent summary:\n%s", summary)

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
        log.error("❌  Missing env vars: %s — add them in Railway → Variables", ", ".join(missing))
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
