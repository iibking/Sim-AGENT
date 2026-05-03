"""
Simcluster Agent — Railway deployment
Uses mcp package for Simcluster tool calls + Gemini (free) for post generation.

Required env vars:
  GEMINI_API_KEY           — from aistudio.google.com (free, no card needed)
  SIMCLUSTER_BEARER_TOKEN  — from simcluster.ai/agent/connect
  TELEGRAM_BOT_TOKEN       — from @BotFather (optional)
  TELEGRAM_CHAT_ID         — from @userinfobot (optional)

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

GEMINI_API_KEY          = os.environ.get("GEMINI_API_KEY", "")
SIMCLUSTER_BEARER_TOKEN = os.environ.get("SIMCLUSTER_BEARER_TOKEN", "")
TELEGRAM_BOT_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID        = os.environ.get("TELEGRAM_CHAT_ID", "")
HEARTBEAT_HOURS         = float(os.environ.get("HEARTBEAT_HOURS", "4"))
MCP_URL                 = "https://simcluster.ai/mcp"


# ─────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────

async def telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                      "parse_mode": "HTML", "disable_web_page_preview": False},
            )
            log.info("Telegram %s", "sent ✓" if r.status_code == 200 else f"error: {r.text[:100]}")
    except Exception as e:
        log.warning("Telegram failed: %s", e)


# ─────────────────────────────────────────────
# MCP helper — uses mcp package (handles init + SSE)
# ─────────────────────────────────────────────

async def call_tool(session, name: str, args: dict = None):
    """Call an MCP tool, log raw result, return parsed dict."""
    try:
        result = await session.call_tool(name, args or {})
        for item in result.content or []:
            text = getattr(item, "text", None)
            if text:
                log.info("RAW [%s]: %s", name, text[:300])
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"_raw": text}
        log.info("RAW [%s]: (no text content)", name)
        return {}
    except Exception as e:
        log.warning("Tool [%s] failed: %s", name, e)
        return {}


def extract(data: dict, *paths):
    """Try multiple dot-separated key paths, return first match."""
    for path in paths:
        val = data
        for key in path.split("."):
            if not isinstance(val, dict):
                break
            val = val.get(key)
        if val is not None:
            return val
    return None


# ─────────────────────────────────────────────
# Gemini — generate post text (free tier)
# ─────────────────────────────────────────────

async def gemini_generate(concept_names: list) -> str:
    """Use Gemini 2.0 Flash free tier to generate a short post."""
    if not GEMINI_API_KEY or not concept_names:
        return ""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    )
    prompt = (
        f"Write a short, engaging 2-sentence social media post about: {', '.join(concept_names)}. "
        "Be creative and thought-provoking. Plain text only, no hashtags, no emojis."
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 150, "temperature": 0.9},
            })
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        log.warning("Gemini failed: %s", e)
        return ""


# ─────────────────────────────────────────────
# Heartbeat
# ─────────────────────────────────────────────

async def heartbeat():
    log.info("━━━━  HEARTBEAT  %s UTC  ━━━━", datetime.now().strftime("%Y-%m-%d %H:%M"))

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {SIMCLUSTER_BEARER_TOKEN}"}

    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log.info("MCP connected ✓")

            # 1. Onboarding
            await call_tool(session, "agent.onboarding")

            # 2. Session status
            status = await call_tool(session, "agent.sessionStatus")

            # Flexible parsing — log all top-level keys so we can see the structure
            log.info("Status top-level keys: %s", list(status.keys()))

            clout_total = extract(status,
                "clout.total", "player.clout.total", "session.clout.total",
                "user.clout.total", "total"
            ) or 0

            posts_remaining = extract(status,
                "dailyPosts.remaining", "player.dailyPosts.remaining",
                "postsRemaining", "posts.remaining"
            ) or 0

            spend_remaining = extract(status,
                "dailySpend.remaining", "player.dailySpend.remaining",
                "spendRemaining", "spend.remaining"
            ) or 0

            log.info("Clout: %s¢ | Posts left: %s | Spend left: %s¢",
                     clout_total, posts_remaining, spend_remaining)

            # 3. Daily bonuses
            bonus_alerts = []
            signin = await call_tool(session, "bounties.getDailySignInBountyStatus")
            if signin.get("canClaim"):
                streak = signin.get("currentStreak", 0)
                reward = signin.get("currentReward", "?")
                log.info("⚠️  SIGN-IN BONUS READY — streak %s reward %s¢", streak, reward)
                bonus_alerts.append(
                    f"🎁 <b>Sign-In Bonus ready!</b>\nStreak: {streak} · Reward: {reward}¢\n"
                    f"👉 <a href='https://simcluster.ai/bonuses'>Claim now</a>"
                )

            board_bonus = await call_tool(session, "bounties.checkDailyBillboardProgress")
            if board_bonus.get("progressCount", 0) >= 1:
                log.info("⚠️  BILLBOARD BONUS READY")
                bonus_alerts.append(
                    "📋 <b>Billboard Bonus ready!</b>\n"
                    "👉 <a href='https://simcluster.ai/bonuses'>Claim now</a>"
                )

            if bonus_alerts:
                await telegram("🤖 <b>Simcluster Agent</b>\n\n" + "\n\n".join(bonus_alerts))

            # 4. Notifications
            notifs = await call_tool(session, "notifications.list", {"limit": 5})
            notif_items = notifs.get("items", [])
            log.info("Notifications: %d", len(notif_items))

            # 5. Trending + billboard
            trend_data = await call_tool(session, "agent.concepts.search",
                                         {"sort": "trending", "limit": 15})
            trending = trend_data.get("items", [])
            log.info("Trending (%d): %s", len(trending), [c.get("name") for c in trending[:5]])

            board_data = await call_tool(session, "bounties.listBillboardConcepts")
            billboard = board_data.get("items", [])[:10]
            log.info("Billboard top-3: %s", [c.get("name") for c in billboard[:3]])

            # 6. Post
            posted_url = None
            if posts_remaining > 0 and spend_remaining >= 30:
                # Pick concepts: 1 billboard + 1 trending
                chosen = []
                for c in billboard[:1] + trending[:2]:
                    sid = c.get("shortId")
                    if sid and sid not in chosen:
                        chosen.append(sid)
                    if len(chosen) == 2:
                        break

                if chosen:
                    log.info("Generating post with concepts: %s", chosen)

                    # Try Simcluster's own create.text first
                    draft = await call_tool(session, "create.text",
                                           {"conceptShortIds": chosen})
                    completion_id = (
                        draft.get("shortId")
                        or draft.get("textCompletionShortId")
                        or draft.get("id")
                    )
                    log.info("Draft completion_id: %s | keys: %s",
                             completion_id, list(draft.keys()))

                    if completion_id:
                        post = await call_tool(session, "create.post", {
                            "textCompletionShortId": completion_id,
                            "mediaShortIds": [],
                        })
                        pid = post.get("shortId") or post.get("id")
                        if pid:
                            posted_url = f"https://simcluster.ai/post/{pid}"
                            log.info("✅ Posted → %s", posted_url)
                        else:
                            log.info("Post response keys: %s", list(post.keys()))
                else:
                    log.info("No concept IDs available to post")
            else:
                log.info("Skipping post: posts_remaining=%s spend_remaining=%s",
                         posts_remaining, spend_remaining)

            # 7. Claim a concept
            claimed_url = None
            if int(clout_total or 0) >= 400 and trending:
                for concept in trending:
                    slug = concept.get("slug")
                    name = concept.get("name", slug)
                    if not slug or concept.get("owner"):
                        continue
                    cs = await call_tool(session, "agent.concepts.claimStatus", {"slug": slug})
                    if not cs.get("claimable"):
                        continue
                    cost = cs.get("cost", 99999)
                    if cost > 600:
                        continue
                    log.info("Claiming '%s' for %s¢", slug, cost)
                    def_data = await call_tool(session,
                        "create.concept.generateNewConceptDefinition",
                        {"slug": slug, "name": name})
                    definition = def_data.get("definition", "A trending concept on Simcluster.")
                    style = await call_tool(session,
                        "create.concept.suggestConceptStyling",
                        {"slug": slug, "name": name})
                    color = style.get("color", "#6366f1")
                    icon  = style.get("icon", "fa fa-star")
                    result = await call_tool(session, "agent.concepts.create", {
                        "slug": slug, "name": name,
                        "definition": definition, "color": color, "icon": icon,
                    })
                    if result:
                        claimed_url = result.get("pageUrl") or f"https://simcluster.ai/c/{slug}"
                        log.info("✅ Claimed → %s", claimed_url)
                    break

            # 8. Telegram summary
            parts = [f"🤖 <b>Simcluster Agent — {datetime.now().strftime('%b %d, %H:%M')}</b>",
                     f"💰 Clout: <b>{clout_total}¢</b> | Posts left: {posts_remaining}"]
            if posted_url:
                parts.append(f"✅ Posted → <a href='{posted_url}'>view post</a>")
            else:
                parts.append("⏭️ No post this heartbeat")
            if claimed_url:
                parts.append(f"🏷️ Claimed → <a href='{claimed_url}'>view concept</a>")
            if bonus_alerts:
                parts.append("⚠️ <b>Bonuses ready to claim at simcluster.ai/bonuses!</b>")
            await telegram("\n".join(parts))

    log.info("━━━━  HEARTBEAT COMPLETE  ━━━━")


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

async def main():
    missing = []
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if not SIMCLUSTER_BEARER_TOKEN:
        missing.append("SIMCLUSTER_BEARER_TOKEN")
    if missing:
        log.error("❌  Missing env vars: %s — add in Railway → Variables", ", ".join(missing))
        return

    log.info("Simcluster agent starting. Heartbeat every %.1fh.", HEARTBEAT_HOURS)
    while True:
        try:
            await heartbeat()
        except Exception as e:
            log.error("Heartbeat crashed: %s", e, exc_info=True)
            await telegram(f"⚠️ <b>Agent crashed</b>\n\n{str(e)[:300]}")
        await asyncio.sleep(HEARTBEAT_HOURS * 3600)


if __name__ == "__main__":
    asyncio.run(main())
