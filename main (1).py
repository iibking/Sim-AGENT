"""
Simcluster Agent — Railway deployment
Runs a heartbeat loop, posts content, and claims concepts automatically.
Set SIMCLUSTER_BEARER_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID as
environment variables in Railway.
"""

import asyncio
import os
import json
import logging
from datetime import datetime
import httpx

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

BEARER_TOKEN = os.environ.get("SIMCLUSTER_BEARER_TOKEN", "")
MCP_URL = "https://simcluster.ai/mcp"
HEARTBEAT_HOURS = float(os.environ.get("HEARTBEAT_HOURS", "4"))  # default every 4 h

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

async def telegram(message: str):
    """Send a message to the configured Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            })
            if resp.status_code != 200:
                log.warning("Telegram error: %s", resp.text)
            else:
                log.info("Telegram message sent ✓")
    except Exception as exc:
        log.warning("Telegram send failed: %s", exc)


# ---------------------------------------------------------------------------
# MCP helpers
# ---------------------------------------------------------------------------

async def call_tool(session, name: str, args: dict | None = None):
    """Call an MCP tool, return parsed JSON or raw text."""
    try:
        result = await session.call_tool(name, args or {})
        log.debug("Raw result from %s: %s", name, result)
        for item in result.content or []:
            log.debug("Content item type=%s text=%s", getattr(item, "type", "?"), getattr(item, "text", None))
            text = getattr(item, "text", None)
            if text:
                try:
                    parsed = json.loads(text)
                    log.debug("Parsed JSON from %s: %s", name, str(parsed)[:300])
                    return parsed
                except json.JSONDecodeError:
                    log.debug("Non-JSON from %s: %s", name, text[:200])
                    return text
        log.debug("No content returned from %s", name)
        return None
    except Exception as exc:
        log.warning("Tool %s failed: %s", name, exc, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Heartbeat phases
# ---------------------------------------------------------------------------

async def phase_status(session) -> dict:
    """Fetch session status and log a summary."""
    status = await call_tool(session, "agent.sessionStatus") or {}
    player = status.get("player", {})
    clout = player.get("clout", {})
    spend = player.get("dailySpend", {})
    posts = player.get("dailyPosts", {})
    sub = player.get("subscription", {})

    log.info(
        "Status — clout: %s¢ (virtual: %s¢) | spent: %s/%s¢ today | posts: %s/5 | delta: %s",
        clout.get("total", "?"),
        clout.get("virtual", 0),
        spend.get("spentToday", "?"),
        spend.get("limit", "?"),
        posts.get("used", "?"),
        sub.get("isDelta", False),
    )
    return status


async def phase_daily_bonuses(session):
    """Check daily bonus readiness and notify via Telegram if ready."""
    alerts = []

    signin = await call_tool(session, "bounties.getDailySignInBountyStatus") or {}
    if signin.get("canClaim"):
        streak = signin.get("currentStreak", 0)
        reward = signin.get("currentReward", "?")
        log.info("⚠️  DAILY SIGN-IN BONUS READY  (streak %s) → https://simcluster.ai/bonuses", streak)
        alerts.append(
            f"🎁 <b>Daily Sign-In Bonus ready!</b>\n"
            f"Streak: {streak} day(s) · Reward: {reward}¢\n"
            f"👉 <a href=\'https://simcluster.ai/bonuses\'>Claim it now</a>"
        )
    else:
        nxt = signin.get("nextClaimAt", "?")
        log.info("Daily sign-in: next claim at %s", nxt)

    billboard = await call_tool(session, "bounties.checkDailyBillboardProgress") or {}
    if billboard.get("progressCount", 0) >= 1:
        log.info("⚠️  DAILY BILLBOARD BONUS READY → https://simcluster.ai/bonuses")
        alerts.append(
            f"📋 <b>Daily Billboard Bonus ready!</b>\n"
            f"👉 <a href=\'https://simcluster.ai/bonuses\'>Claim it now</a>"
        )

    if alerts:
        await telegram("🤖 <b>Simcluster Agent</b>\n\n" + "\n\n".join(alerts))


async def phase_notifications(session):
    """Skim notifications."""
    data = await call_tool(session, "notifications.list", {"limit": 20}) or {}
    items = data.get("items", []) if isinstance(data, dict) else []
    if items:
        log.info("Notifications: %d unread", len(items))
        for n in items[:3]:
            log.info("  · %s", n.get("message") or n.get("type", "?"))


async def phase_trending(session) -> tuple[list, list]:
    """Fetch trending concepts and billboard concepts."""
    trending_data = await call_tool(session, "agent.concepts.search", {"sort": "trending", "limit": 15}) or {}
    trending = trending_data.get("items", []) if isinstance(trending_data, dict) else []
    log.info("Trending concepts: %s", [c.get("name") for c in trending[:5]])

    board_data = await call_tool(session, "bounties.listBillboardConcepts") or {}
    billboard = board_data.get("items", [])[:10] if isinstance(board_data, dict) else []
    log.info("Billboard top-3: %s", [c.get("name") for c in billboard[:3]])

    return trending, billboard


async def phase_post(session, trending: list, billboard: list, status: dict):
    """Generate and publish one post if budget allows."""
    player = status.get("player", {})
    posts_left = player.get("dailyPosts", {}).get("remaining", 0)
    clout_left = player.get("dailySpend", {}).get("remaining", 0)

    if posts_left <= 0:
        log.info("Post skipped: daily post limit reached.")
        return
    if clout_left < 30:
        log.info("Post skipped: only %s¢ remaining today.", clout_left)
        return

    # Pick concepts: prefer one billboard concept (for daily bonus) + one trending
    concept_ids = []
    if billboard:
        concept_ids.append(billboard[0]["shortId"])
    for tc in trending:
        sid = tc.get("shortId")
        if sid and sid not in concept_ids:
            concept_ids.append(sid)
            break

    if not concept_ids:
        log.info("Post skipped: no concepts found.")
        return

    chosen = concept_ids[:2]
    log.info("Generating post with concepts: %s", chosen)

    # Check for any active bounty we can claim
    bounties_data = await call_tool(session, "user-bounties.listActiveRewards") or {}
    bounty_id = None
    for b in (bounties_data.get("items", []) if isinstance(bounties_data, dict) else []):
        bounty_id = b.get("shortId")
        log.info("Using bounty %s", bounty_id)
        break

    # Generate text draft
    gen_args: dict = {"conceptShortIds": chosen}
    if bounty_id:
        gen_args["bountyShortId"] = bounty_id

    draft = await call_tool(session, "create.text", gen_args)
    if not draft or not isinstance(draft, dict):
        log.warning("Text generation returned nothing.")
        return

    completion_id = draft.get("shortId") or draft.get("textCompletionShortId")
    preview = (draft.get("text") or "")[:120]
    log.info("Draft: '%s…'", preview)

    if not completion_id:
        log.warning("No completion ID in draft, cannot post.")
        return

    # Publish
    post = await call_tool(session, "create.post", {
        "textCompletionShortId": completion_id,
        "mediaShortIds": [],
    })
    if post and isinstance(post, dict):
        pid = post.get("shortId", "?")
        log.info("✅ Posted → https://simcluster.ai/post/%s", pid)
    else:
        log.warning("Post call returned: %s", post)


async def phase_claim(session, trending: list, status: dict):
    """Opportunistically claim one affordable trending concept."""
    total_clout = status.get("player", {}).get("clout", {}).get("total", 0)
    if total_clout < 400:
        log.info("Claim skipped: only %s¢ total clout.", total_clout)
        return

    for concept in trending:
        slug = concept.get("slug")
        if not slug:
            continue
        # Skip already-owned concepts
        if concept.get("owner"):
            continue

        status_data = await call_tool(session, "agent.concepts.claimStatus", {"slug": slug})
        if not status_data or not isinstance(status_data, dict):
            continue

        if not status_data.get("claimable"):
            continue

        cost = status_data.get("cost", 99999)
        if cost > 500:
            log.info("Concept '%s' costs %s¢ — too expensive, skipping.", slug, cost)
            continue

        log.info("Claiming '%s' for %s¢…", slug, cost)

        # Auto-generate definition + styling
        def_data = await call_tool(session, "create.concept.generateNewConceptDefinition", {
            "slug": slug, "name": concept.get("name", slug)
        }) or {}
        definition = def_data.get("definition", f"A trending concept on Simcluster.") if isinstance(def_data, dict) else f"A trending concept on Simcluster."

        style = await call_tool(session, "create.concept.suggestConceptStyling", {
            "slug": slug, "name": concept.get("name", slug)
        }) or {}
        color = style.get("color", "#6366f1") if isinstance(style, dict) else "#6366f1"
        icon = style.get("icon", "fa fa-star") if isinstance(style, dict) else "fa fa-star"

        result = await call_tool(session, "agent.concepts.create", {
            "slug": slug,
            "name": concept.get("name", slug),
            "definition": definition,
            "color": color,
            "icon": icon,
        })

        if result:
            page_url = result.get("pageUrl", f"https://simcluster.ai/c/{slug}") if isinstance(result, dict) else f"https://simcluster.ai/c/{slug}"
            log.info("✅ Claimed concept → %s", page_url)
        else:
            log.warning("Claim failed for '%s'.", slug)

        break  # one claim per heartbeat


# ---------------------------------------------------------------------------
# Main heartbeat
# ---------------------------------------------------------------------------

async def heartbeat():
    """Run one full heartbeat cycle."""
    log.info("━━━━  HEARTBEAT  %s  ━━━━", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {BEARER_TOKEN}"}

    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log.info("Connected to Simcluster MCP ✓")

            status = await phase_status(session)
            await phase_daily_bonuses(session)
            await phase_notifications(session)
            trending, billboard = await phase_trending(session)
            await phase_post(session, trending, billboard, status)
            await phase_claim(session, trending, status)

    log.info("━━━━  HEARTBEAT COMPLETE  ━━━━")


async def main():
    if not BEARER_TOKEN:
        log.error("❌  SIMCLUSTER_BEARER_TOKEN is not set. Add it in Railway → Variables.")
        return

    log.info("Simcluster agent starting. Heartbeat every %.1f hour(s).", HEARTBEAT_HOURS)

    while True:
        try:
            await heartbeat()
        except Exception as exc:
            log.error("Heartbeat crashed: %s", exc, exc_info=True)

        sleep_secs = HEARTBEAT_HOURS * 3600
        log.info("Sleeping %.0f seconds until next heartbeat…", sleep_secs)
        await asyncio.sleep(sleep_secs)


if __name__ == "__main__":
    asyncio.run(main())
