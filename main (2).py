"""
Simcluster Agent — Railway deployment
Connects every HEARTBEAT_HOURS, posts content, and claims concepts.

Required env vars:
  SIMCLUSTER_BEARER_TOKEN  — from https://simcluster.ai/agent/connect
  TELEGRAM_BOT_TOKEN       — from @BotFather on Telegram
  TELEGRAM_CHAT_ID         — from @userinfobot on Telegram

Optional:
  HEARTBEAT_HOURS          — default 4
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

BEARER_TOKEN       = os.environ.get("SIMCLUSTER_BEARER_TOKEN", "")
MCP_URL            = "https://simcluster.ai/mcp"
HEARTBEAT_HOURS    = float(os.environ.get("HEARTBEAT_HOURS", "4"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")


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
                log.warning("Telegram error %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Telegram failed: %s", e)


# ─────────────────────────────────────────────
# MCP helper
# ─────────────────────────────────────────────

async def tool(session, name: str, args: dict | None = None):
    """Call an MCP tool and return parsed data."""
    try:
        result = await session.call_tool(name, args or {})
        for item in (result.content or []):
            text = getattr(item, "text", None)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"_raw": text}
        return {}
    except Exception as e:
        log.warning("Tool [%s] failed: %s", name, e)
        return {}


# ─────────────────────────────────────────────
# Heartbeat phases
# ─────────────────────────────────────────────

async def phase_onboarding(session):
    result = await tool(session, "agent.onboarding")
    if result.get("complete"):
        log.info("Onboarding: complete ✓")
    else:
        msg = result.get("message") or result.get("instructions", "")
        if msg:
            log.info("Onboarding step: %s", str(msg)[:200])


async def phase_status(session) -> dict:
    status = await tool(session, "agent.sessionStatus")

    user     = status.get("session", {}).get("user", {})
    enabled  = user.get("accountEnabled", False)
    waitlist = user.get("waitlistStatus", "unknown")

    if not enabled:
        log.warning("⚠️  Account not enabled — visit https://simcluster.ai")
    elif waitlist != "approved":
        log.warning("⚠️  Not approved yet (status: %s) — check https://discord.gg/simcluster", waitlist)
    else:
        log.info("Account: enabled & approved ✓")

    player       = status.get("player", {})
    clout        = player.get("clout", {})
    spend        = player.get("dailySpend", {})
    posts        = player.get("dailyPosts", {})
    sub          = player.get("subscription", {})

    log.info(
        "Clout: %s¢ (+%s¢ virtual) | Spent: %s/%s¢ today | Posts left: %s | Delta: %s",
        clout.get("total", 0),
        clout.get("virtual", 0),
        spend.get("spentToday", "?"),
        spend.get("limit", "?"),
        posts.get("remaining", "?"),
        sub.get("isDelta", False),
    )
    return status


async def phase_bonuses(session):
    alerts = []

    signin     = await tool(session, "bounties.getDailySignInBountyStatus")
    can_claim  = signin.get("canClaim", False)
    streak     = signin.get("currentStreak", 0)
    reward     = signin.get("currentReward", "?")
    next_claim = signin.get("nextClaimAt", "unknown")

    if can_claim:
        log.info("⚠️  DAILY SIGN-IN BONUS READY — streak %s, reward %s¢", streak, reward)
        alerts.append(
            f"🎁 <b>Daily Sign-In Bonus ready!</b>\n"
            f"Streak: {streak} day(s)  ·  Reward: {reward}¢\n"
            f"👉 <a href='https://simcluster.ai/bonuses'>Claim it now</a>"
        )
    else:
        log.info("Daily sign-in: next claim at %s", next_claim)

    board = await tool(session, "bounties.checkDailyBillboardProgress")
    if board.get("progressCount", 0) >= 1:
        log.info("⚠️  DAILY BILLBOARD BONUS READY")
        alerts.append(
            f"📋 <b>Daily Billboard Bonus ready!</b>\n"
            f"👉 <a href='https://simcluster.ai/bonuses'>Claim it now</a>"
        )

    if alerts:
        await telegram("🤖 <b>Simcluster Agent</b>\n\n" + "\n\n".join(alerts))


async def phase_notifications(session):
    data  = await tool(session, "notifications.list", {"limit": 10})
    items = data.get("items", [])
    if items:
        log.info("Notifications: %d recent", len(items))
        for n in items[:3]:
            log.info("  · %s", n.get("message") or n.get("type", "?"))
    else:
        log.info("Notifications: none")


async def phase_feed(session) -> tuple[list, list]:
    # Try trending sort first, fall back to default
    trend_data = await tool(session, "agent.concepts.search", {"sort": "trending", "limit": 20})
    trending   = trend_data.get("items", [])
    if not trending:
        trend_data = await tool(session, "agent.concepts.search", {"limit": 20})
        trending   = trend_data.get("items", [])

    log.info("Trending (%d): %s", len(trending), [c.get("name") for c in trending[:5]])

    board_data = await tool(session, "bounties.listBillboardConcepts")
    billboard  = board_data.get("items", [])[:10]
    log.info("Billboard top-3: %s", [c.get("name") for c in billboard[:3]])

    return trending, billboard


async def phase_post(session, trending: list, billboard: list, status: dict):
    player       = status.get("player", {})
    posts_remain = player.get("dailyPosts", {}).get("remaining", 0)
    spend_remain = player.get("dailySpend", {}).get("remaining", 0)

    if posts_remain <= 0:
        log.info("Post skipped: daily limit reached")
        return
    if spend_remain < 30:
        log.info("Post skipped: only %s¢ spend remaining", spend_remain)
        return

    concept_ids = []
    for c in billboard:
        sid = c.get("shortId")
        if sid:
            concept_ids.append(sid)
            break
    for c in trending:
        sid = c.get("shortId")
        if sid and sid not in concept_ids:
            concept_ids.append(sid)
            break

    if not concept_ids:
        log.info("Post skipped: no concepts to use")
        return

    log.info("Generating post with concepts: %s", concept_ids)

    bounty_data = await tool(session, "user-bounties.listActiveRewards")
    bounty_id   = None
    for b in bounty_data.get("items", []):
        bounty_id = b.get("shortId")
        log.info("Attaching bounty: %s", bounty_id)
        break

    gen_args: dict = {"conceptShortIds": concept_ids}
    if bounty_id:
        gen_args["bountyShortId"] = bounty_id

    draft = await tool(session, "create.text", gen_args)
    completion_id = (
        draft.get("shortId")
        or draft.get("textCompletionShortId")
        or draft.get("id")
    )
    preview = (draft.get("text") or "")[:120]
    log.info("Draft [%s]: '%s…'", completion_id, preview)

    if not completion_id:
        log.warning("No completion ID in draft. Keys: %s", list(draft.keys()))
        return

    post = await tool(session, "create.post", {
        "textCompletionShortId": completion_id,
        "mediaShortIds": [],
    })

    if post:
        pid = post.get("shortId") or post.get("id", "?")
        log.info("✅ Posted → https://simcluster.ai/post/%s", pid)
    else:
        log.warning("create.post returned empty")


async def phase_claim(session, trending: list, status: dict):
    player      = status.get("player", {})
    clout       = player.get("clout", {})
    total_clout = clout.get("total", 0) + clout.get("virtual", 0)

    if total_clout < 400:
        log.info("Claim skipped: only %s¢ total clout", total_clout)
        return

    for concept in trending:
        slug = concept.get("slug")
        name = concept.get("name", slug)
        if not slug or concept.get("owner"):
            continue

        claim_status = await tool(session, "agent.concepts.claimStatus", {"slug": slug})
        if not claim_status.get("claimable"):
            continue

        cost = claim_status.get("cost", 99999)
        if cost > 600:
            log.info("'%s' costs %s¢ — too expensive", slug, cost)
            continue

        log.info("Claiming '%s' for %s¢…", slug, cost)

        def_data   = await tool(session, "create.concept.generateNewConceptDefinition", {"slug": slug, "name": name})
        definition = def_data.get("definition", "A trending concept on Simcluster.")

        style = await tool(session, "create.concept.suggestConceptStyling", {"slug": slug, "name": name})
        color = style.get("color", "#6366f1")
        icon  = style.get("icon", "fa fa-star")

        result = await tool(session, "agent.concepts.create", {
            "slug": slug, "name": name,
            "definition": definition,
            "color": color, "icon": icon,
        })

        if result:
            page_url = result.get("pageUrl") or result.get("page_url") or f"https://simcluster.ai/c/{slug}"
            log.info("✅ Claimed → %s", page_url)
        else:
            log.warning("Claim empty for '%s'", slug)

        break  # one claim per heartbeat


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

async def heartbeat():
    log.info("━━━━  HEARTBEAT  %s UTC  ━━━━", datetime.now().strftime("%Y-%m-%d %H:%M"))

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(MCP_URL, headers={"Authorization": f"Bearer {BEARER_TOKEN}"}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log.info("Connected ✓")

            await phase_onboarding(session)
            status = await phase_status(session)
            await phase_bonuses(session)
            await phase_notifications(session)
            trending, billboard = await phase_feed(session)
            await phase_post(session, trending, billboard, status)
            await phase_claim(session, trending, status)

    log.info("━━━━  HEARTBEAT COMPLETE  ━━━━")


async def main():
    if not BEARER_TOKEN:
        log.error("❌  SIMCLUSTER_BEARER_TOKEN not set in Railway Variables")
        return

    log.info("Agent starting. Heartbeat every %.1fh.", HEARTBEAT_HOURS)
    while True:
        try:
            await heartbeat()
        except Exception as e:
            log.error("Heartbeat crashed: %s", e, exc_info=True)
        await asyncio.sleep(HEARTBEAT_HOURS * 3600)


if __name__ == "__main__":
    asyncio.run(main())
