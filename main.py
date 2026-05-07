"""
Simcluster Agent — Railway deployment
Fixes: skill hash headers, follow/unfollow with 48h tracking, Telegram alerts.

Required env vars:
  GEMINI_API_KEY           — from aistudio.google.com (free)
  SIMCLUSTER_BEARER_TOKEN  — from simcluster.ai/agent/connect
  TELEGRAM_BOT_TOKEN       — from @BotFather (optional)
  TELEGRAM_CHAT_ID         — from @userinfobot (optional)

Optional:
  HEARTBEAT_HOURS          — default 4
"""

import asyncio
import os
import json
import re
import hashlib
import logging
from datetime import datetime, timezone
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
FOLLOWS_FILE            = "/tmp/simcluster_follows.json"


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
# Skill hash — required for protected MCP tools
# ─────────────────────────────────────────────

async def get_skill_headers() -> dict:
    """
    Fetch skill.md, compute SHA-256, extract ack phrase.
    These headers are required by Simcluster for protected MCP tools.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get("https://simcluster.ai/skill.md")
            r.raise_for_status()
            content = r.text

        skill_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Extract ack phrase: remember "phrase-here"
        match = re.search(r'remember\s+"([^"]+)"', content)
        ack = match.group(1) if match else ""

        log.info("Skill hash: %s…", skill_hash[:20])
        log.info("Skill ack: %s", ack)

        return {
            "X-Simcluster-Skill-Hash": skill_hash,
            "X-Simcluster-Skill-Ack": ack,
        }
    except Exception as e:
        log.error("Could not fetch skill.md: %s", e)
        return {}


# ─────────────────────────────────────────────
# MCP helper
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
        log.info("RAW [%s]: (empty)", name)
        return {}
    except Exception as e:
        log.warning("Tool [%s] failed: %s", name, e)
        return {}


def extract(data: dict, *paths):
    """Try multiple dot-path keys, return first match."""
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
# Follow tracking (persisted to /tmp)
# ─────────────────────────────────────────────

def load_follows() -> dict:
    """Load follow tracking. Schema: {username: {followedAt, charShortId, followedBack}}"""
    try:
        with open(FOLLOWS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_follows(data: dict):
    try:
        with open(FOLLOWS_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.warning("Could not save follows: %s", e)


# ─────────────────────────────────────────────
# Gemini text generation (free tier)
# ─────────────────────────────────────────────

async def gemini_generate(concept_names: list) -> str:
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
# Heartbeat phases
# ─────────────────────────────────────────────

async def phase_status(session) -> dict:
    status = await call_tool(session, "agent.sessionStatus")
    log.info("Status keys: %s", list(status.keys()) if isinstance(status, dict) else type(status))

    user = status.get("session", {}).get("user", {})
    enabled  = user.get("accountEnabled", False)
    waitlist = user.get("waitlistStatus", "unknown")

    if not enabled:
        log.warning("⚠️  accountEnabled=false — visit simcluster.ai to complete account setup")
    else:
        log.info("Account: enabled ✓ | waitlist: %s", waitlist)

    player = status.get("player", {})
    clout        = player.get("clout", {})
    spend        = player.get("dailySpend", {})
    posts        = player.get("dailyPosts", {})

    clout_total   = clout.get("total", 0) + clout.get("virtual", 0)
    spend_remain  = spend.get("remaining", 0)
    posts_remain  = posts.get("remaining", 0)

    log.info("Clout: %s¢ | Posts left: %s | Spend left: %s¢",
             clout_total, posts_remain, spend_remain)
    return status


def bonus_is_claimable(data: dict) -> bool:
    """Check if a bonus can be claimed based on nextClaimLockedUntil."""
    locked = data.get("nextClaimLockedUntil")
    if not locked:
        return data.get("canClaim", False)
    try:
        locked_dt = datetime.fromisoformat(locked.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) > locked_dt
    except Exception:
        return data.get("canClaim", False)


async def phase_bonuses(session) -> list:
    alerts = []

    signin = await call_tool(session, "bounties.getDailySignInBountyStatus")
    streak = signin.get("streakLength", signin.get("currentStreak", 0))
    if bonus_is_claimable(signin):
        log.info("⚠️  SIGN-IN BONUS READY — streak %s", streak)
        alerts.append(
            f"🎁 <b>Sign-In Bonus ready!</b>\nStreak: {streak} day(s)\n"
            f"👉 <a href='https://simcluster.ai/bonuses'>Claim now</a>"
        )
    else:
        nxt = signin.get("nextClaimLockedUntil", "?")
        log.info("Sign-in bonus: next at %s | streak %s", nxt, streak)

    board = await call_tool(session, "bounties.checkDailyBillboardProgress")
    progress = board.get("extras", {}).get("progressCount", board.get("progressCount", 0))
    if progress >= 1 and bonus_is_claimable(board):
        log.info("⚠️  BILLBOARD BONUS READY")
        alerts.append(
            "📋 <b>Billboard Bonus ready!</b>\n"
            "👉 <a href='https://simcluster.ai/bonuses'>Claim now</a>"
        )
    else:
        log.info("Billboard bonus: progress %s | next at %s", progress, board.get("nextClaimLockedUntil", "?"))

    return alerts


async def phase_notifications(session):
    data  = await call_tool(session, "notifications.list", {"limit": 10})
    items = data.get("items", [])
    log.info("Notifications: %d", len(items))


async def phase_feed(session) -> tuple:
    # concepts.definition.trending doesn't need a query param
    trend = await call_tool(session, "concepts.definition.trending", {"limit": 15})
    # response may be a list directly or wrapped in items
    trending = (trend if isinstance(trend, list) else trend.get("items", []))
    log.info("Trending (%d): %s", len(trending), [c.get("name") for c in trending[:5]])

    board = await call_tool(session, "bounties.listBillboardConcepts")
    # billboard returns a list directly (not wrapped in {"items": [...]})
    billboard = (board if isinstance(board, list) else board.get("items", []))[:10]
    log.info("Billboard top-3: %s", [c.get("name") for c in billboard[:3]])

    return trending, billboard


async def phase_post(session, trending, billboard, status) -> str | None:
    player       = status.get("player", {})
    posts_remain = player.get("dailyPosts", {}).get("remaining", 0)
    spend_remain = player.get("dailySpend", {}).get("remaining", 0)

    if posts_remain <= 0:
        log.info("Post skipped: daily limit reached")
        return None
    if spend_remain < 30:
        log.info("Post skipped: only %s¢ remaining", spend_remain)
        return None

    chosen = []
    for c in billboard[:1] + trending[:2]:
        sid = c.get("shortId")
        if sid and sid not in chosen:
            chosen.append(sid)
        if len(chosen) == 2:
            break

    if not chosen:
        log.info("Post skipped: no concepts available")
        return None

    draft = await call_tool(session, "create.text", {"conceptShortIds": chosen})
    completion_id = (
        draft.get("shortId") or draft.get("textCompletionShortId") or draft.get("id")
    )
    log.info("Draft completion_id: %s | keys: %s", completion_id, list(draft.keys()) if isinstance(draft, dict) else type(draft))

    if not completion_id:
        return None

    post = await call_tool(session, "create.post", {
        "textCompletionShortId": completion_id,
        "mediaShortIds": [],
    })
    pid = post.get("shortId") or post.get("id")
    if pid:
        url = f"https://simcluster.ai/post/{pid}"
        log.info("✅ Posted → %s", url)
        return url
    return None


async def phase_claim(session, trending, status) -> str | None:
    player      = status.get("player", {})
    clout       = player.get("clout", {})
    total_clout = clout.get("total", 0) + clout.get("virtual", 0)

    if total_clout < 400:
        log.info("Claim skipped: only %s¢", total_clout)
        return None

    for concept in trending:
        slug = concept.get("slug")
        name = concept.get("name", slug)
        if not slug or concept.get("owner"):
            continue
        cs = await call_tool(session, "agent.concepts.claimStatus", {"slug": slug})
        if not cs.get("claimable") or cs.get("cost", 99999) > 600:
            continue
        log.info("Claiming '%s' for %s¢", slug, cs.get("cost"))
        def_data = await call_tool(session,
            "create.concept.generateNewConceptDefinition", {"slug": slug, "name": name})
        definition = def_data.get("definition", "A trending concept on Simcluster.")
        style = await call_tool(session,
            "create.concept.suggestConceptStyling", {"slug": slug, "name": name})
        result = await call_tool(session, "agent.concepts.create", {
            "slug": slug, "name": name,
            "definition": definition,
            "color": style.get("color", "#6366f1"),
            "icon":  style.get("icon",  "fa fa-star"),
        })
        if result:
            url = result.get("pageUrl") or f"https://simcluster.ai/c/{slug}"
            log.info("✅ Claimed → %s", url)
            return url
        break
    return None


async def phase_follows(session) -> tuple:
    """
    Follow recommended active accounts.
    Unfollow anyone who hasn't followed back within 48 hours.
    Returns (newly_followed, unfollowed_list).
    """
    follows = load_follows()
    now_ts  = datetime.now(timezone.utc).timestamp()

    # ── 1. Get current followers so we can detect follow-backs ──
    followers_data = await call_tool(session, "me.char.getFollowers")
    if isinstance(followers_data, list):
        follower_items = followers_data
        log.info("Followers: %d (list)", len(follower_items))
    else:
        log.info("Followers raw keys: %s", list(followers_data.keys()))
        follower_items = followers_data.get("items", []) or followers_data.get("characters", []) or []
    follower_usernames = {
        f.get("username") or f.get("handle") or f.get("char", {}).get("username", "")
        for f in follower_items
    }
    log.info("Current followers (%d): %s", len(follower_usernames), list(follower_usernames)[:5])

    # Update followedBack status
    for username in list(follows.keys()):
        if username in follower_usernames:
            follows[username]["followedBack"] = True

    # ── 2. Unfollow those who didn't follow back in 48 h ──
    unfollowed = []
    for username, data in list(follows.items()):
        hours = (now_ts - data.get("followedAt", now_ts)) / 3600
        if not data.get("followedBack") and hours >= 48:
            char_id = data.get("charShortId")
            if char_id:
                result = await call_tool(session, "me.char.toggleFollow",
                                         {"charShortId": char_id})
                log.info("Unfollowed %s after %.0fh (no follow-back): %s",
                         username, hours, result)
                unfollowed.append(username)
                del follows[username]

    # ── 3. Follow recommended accounts (up to 5 per heartbeat) ──
    recs = await call_tool(session, "me.char.getRecommendedFollows")
    if isinstance(recs, list):
        rec_items = recs
        log.info("Recommended follows: %d (list)", len(rec_items))
    else:
        log.info("Recommended follows raw keys: %s", list(recs.keys()))
        rec_items = recs.get("items", []) or recs.get("characters", []) or []

    newly_followed = []
    for account in rec_items[:5]:
        # Try multiple possible field names
        username   = (account.get("username") or account.get("handle")
                      or account.get("char", {}).get("username", ""))
        char_id    = (account.get("charShortId") or account.get("shortId")
                      or account.get("char", {}).get("shortId", ""))

        if not username or not char_id:
            log.info("Skipping rec follow — missing username/charId: %s", account)
            continue
        if username in follows:
            log.info("Already following %s, skipping", username)
            continue

        result = await call_tool(session, "me.char.toggleFollow", {"charShortId": char_id})
        log.info("Followed %s: %s", username, result)

        follows[username] = {
            "followedAt":   now_ts,
            "charShortId":  char_id,
            "followedBack": username in follower_usernames,
        }
        newly_followed.append(username)

    save_follows(follows)
    log.info("Follows: +%d new, -%d unfollowed", len(newly_followed), len(unfollowed))
    return newly_followed, unfollowed


# ─────────────────────────────────────────────
# Main heartbeat
# ─────────────────────────────────────────────

async def heartbeat():
    log.info("━━━━  HEARTBEAT  %s UTC  ━━━━", datetime.now().strftime("%Y-%m-%d %H:%M"))

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    # Fetch skill.md hash — required for protected tools
    skill_headers = await get_skill_headers()

    headers = {
        "Authorization": f"Bearer {SIMCLUSTER_BEARER_TOKEN}",
        **skill_headers,
    }

    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log.info("MCP connected ✓")

            status            = await phase_status(session)
            bonus_alerts      = await phase_bonuses(session)
            await phase_notifications(session)
            trending, billboard = await phase_feed(session)
            posted_url        = await phase_post(session, trending, billboard, status)
            claimed_url       = await phase_claim(session, trending, status)
            newly_followed, unfollowed = await phase_follows(session)

    # ── Telegram summary ──
    player       = status.get("player", {})
    clout_total  = player.get("clout", {}).get("total", 0)
    posts_remain = player.get("dailyPosts", {}).get("remaining", 0)

    parts = [
        f"🤖 <b>Simcluster Agent — {datetime.now().strftime('%b %d, %H:%M')}</b>",
        f"💰 Clout: <b>{clout_total}¢</b> | Posts left: {posts_remain}",
    ]
    if posted_url:
        parts.append(f"✅ Posted → <a href='{posted_url}'>view post</a>")
    else:
        parts.append("⏭️ No post this heartbeat")
    if claimed_url:
        parts.append(f"🏷️ Claimed → <a href='{claimed_url}'>view concept</a>")
    if newly_followed:
        parts.append(f"👥 Followed: {', '.join(newly_followed)}")
    if unfollowed:
        parts.append(f"🚫 Unfollowed (no follow-back in 48h): {', '.join(unfollowed)}")
    if bonus_alerts:
        parts.append("⚠️ <b>Bonuses ready at simcluster.ai/bonuses!</b>")

    await telegram("\n".join(parts))

    if bonus_alerts:
        await telegram("🤖 <b>Simcluster Agent</b>\n\n" + "\n\n".join(bonus_alerts))

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
        log.error("❌  Missing env vars: %s", ", ".join(missing))
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
