"""
╔══════════════════════════════════════════════════════════════╗
║           MULCH BOSS — Social Media Auto-Poster              ║
║   Automatically posts photos + AI captions to Facebook       ║
║   and Instagram. Optimized for local service businesses.     ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import json
import time
import uuid
import random
import signal
import atexit
import requests
import anthropic
from pathlib import Path
from datetime import datetime, timezone
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

# ── Business Config ────────────────────────────────────────────────────────────
BUSINESS_NAME    = os.getenv("BUSINESS_NAME",  "SRF Forestry Mulching")
BUSINESS_PHONE   = os.getenv("BUSINESS_PHONE", "555-555-5555")
BUSINESS_CITY    = os.getenv("BUSINESS_CITY",  "Lexington")
BUSINESS_STATE   = os.getenv("BUSINESS_STATE", "TN")
SERVICE_AREA     = os.getenv("SERVICE_AREA",   "Lexington, TN and surrounding areas")

# ── Meta API ───────────────────────────────────────────────────────────────────
FB_PAGE_ID           = os.getenv("FB_PAGE_ID", "")
FB_PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN", "")
IG_ACCOUNT_ID        = os.getenv("IG_ACCOUNT_ID", "")

# ── Claude AI ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ── Bot Settings ───────────────────────────────────────────────────────────────
POST_TO_FACEBOOK  = os.getenv("POST_TO_FACEBOOK",  "true").lower() == "true"
POST_TO_INSTAGRAM = os.getenv("POST_TO_INSTAGRAM", "true").lower() == "true"
POSTS_PER_DAY     = int(os.getenv("POSTS_PER_DAY", "4"))
POST_TIMES        = os.getenv("POST_TIMES", "12:00,17:00,22:30,01:00").split(",")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "mulchboss")

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
MEDIA_DIR   = BASE_DIR / "media"
DATA_DIR    = BASE_DIR / "data"
POST_LOG    = DATA_DIR / "post_log.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

DATA_DIR.mkdir(exist_ok=True)
MEDIA_DIR.mkdir(exist_ok=True)

# Media category folders
CATEGORIES = ["mulching", "cleanup", "before_after", "edging", "landscaping"]
for cat in CATEGORIES:
    (MEDIA_DIR / cat).mkdir(exist_ok=True)

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Mulch Boss")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Serve uploaded media
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")

# ── Claude client ──────────────────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# ── State ──────────────────────────────────────────────────────────────────────
bot_state = {
    "running":       True,
    "posts_today":   0,
    "last_post_time": None,
    "last_post_result": None,
    "total_posts":   0,
    "errors_today":  0,
}


# ══════════════════════════════════════════════════════════════════════════════
#  POST LOG — tracks every post ever made
# ══════════════════════════════════════════════════════════════════════════════

def load_post_log() -> list:
    if POST_LOG.exists():
        try:
            return json.loads(POST_LOG.read_text())
        except Exception:
            return []
    return []

def save_post_log(log: list):
    POST_LOG.write_text(json.dumps(log, indent=2))

def log_post(media_path: str, caption: str, category: str,
             fb_result: dict, ig_result: dict):
    log = load_post_log()
    log.append({
        "id":          str(uuid.uuid4())[:8],
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "media_file":  media_path,
        "category":    category,
        "caption":     caption,
        "facebook":    fb_result,
        "instagram":   ig_result,
        "status":      "posted" if (fb_result.get("success") or ig_result.get("success")) else "failed",
    })
    save_post_log(log)
    bot_state["total_posts"] = len([p for p in log if p["status"] == "posted"])


# ══════════════════════════════════════════════════════════════════════════════
#  SETTINGS — runtime-editable config stored in data/settings.json
# ══════════════════════════════════════════════════════════════════════════════

def load_settings() -> dict:
    defaults = {
        "business_name":   BUSINESS_NAME,
        "phone":           BUSINESS_PHONE,
        "city":            BUSINESS_CITY,
        "state":           BUSINESS_STATE,
        "service_area":    SERVICE_AREA,
        "post_times":      POST_TIMES,
        "posts_per_day":   POSTS_PER_DAY,
        "post_facebook":   POST_TO_FACEBOOK,
        "post_instagram":  POST_TO_INSTAGRAM,
        "caption_tone":    "friendly",   # friendly | professional | bold
        "custom_hashtags": [],
        "cta":             f"Call or text us for a FREE quote: {BUSINESS_PHONE}",
        "auto_post":       True,
    }
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text())
            defaults.update(saved)
        except Exception:
            pass
    return defaults

def save_settings(s: dict):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


# ══════════════════════════════════════════════════════════════════════════════
#  MEDIA PICKER — selects a photo/video that hasn't been used recently
# ══════════════════════════════════════════════════════════════════════════════

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

def get_all_media() -> list:
    """Return list of all media files across all category folders."""
    files = []
    for cat in CATEGORIES:
        folder = MEDIA_DIR / cat
        for f in folder.iterdir():
            if f.suffix.lower() in MEDIA_EXTS:
                files.append({"path": f, "category": cat, "name": f.name})
    return files

def pick_media() -> dict | None:
    """
    Pick a media file, avoiding repeating the last 5 used.
    Prefers before_after category (highest engagement) 40% of the time.
    """
    all_media = get_all_media()
    if not all_media:
        return None

    # Load recently used files
    log = load_post_log()
    recent = [p["media_file"] for p in log[-5:]]

    # Filter out recently used
    fresh = [m for m in all_media if str(m["path"]) not in recent]
    pool  = fresh if fresh else all_media  # if all used, reset pool

    # Weighted: before_after gets extra weight
    before_after = [m for m in pool if m["category"] == "before_after"]
    if before_after and random.random() < 0.40:
        return random.choice(before_after)

    return random.choice(pool)


# ══════════════════════════════════════════════════════════════════════════════
#  CAPTION GENERATOR — AI writes a fresh post every time
# ══════════════════════════════════════════════════════════════════════════════

# ── SEO / Search Visibility Hashtag Strategy ──────────────────────────────────
# Goal: show up when people search for ANY mulching/landscaping service in the area.
# Strategy: use the exact keywords competitors target — same service terms,
# same city terms, same problem-based searches ("mulch near me", etc.)
# This makes the business show up in hashtag searches on FB/IG alongside
# or above competitors who post less frequently.

def build_hashtag_set(settings: dict, category: str) -> str:
    city  = settings.get("city",  "Jackson").replace(" ", "")
    state = settings.get("state", "TN")
    name  = settings.get("business_name", "").replace(" ", "")

    # Core service hashtags — same terms competitors use
    service_tags = [
        "#MulchInstall", "#Mulching", "#MulchingService",
        "#FreshMulch", "#MulchDelivery", "#MulchNearMe",
        "#LandscapingService", "#YardWork", "#LawnCare",
        "#CurbAppeal", "#OutdoorLiving", "#HomeImprovement",
    ]

    # Location hashtags — local search dominance
    location_tags = [
        f"#{city}TN", f"#{city}Landscaping", f"#{city}LawnCare",
        f"#{city}YardWork", f"#{city}{state}", f"#{state}Landscaping",
        f"#{city}HomeServices", f"#{city}Outdoors",
    ]

    # Category-specific tags
    category_tags = {
        "before_after":  ["#BeforeAndAfter", "#Transformation", "#YardGoals", "#OutdoorTransformation"],
        "mulching":      ["#MulchLife", "#Mulch", "#GardenBeds", "#LandscapingIdeas"],
        "cleanup":       ["#YardCleanup", "#SpringCleanup", "#FallCleanup", "#PropertyMaintenance"],
        "edging":        ["#CleanEdges", "#LawnEdging", "#CrispLines", "#ProfessionalLandscaping"],
        "landscaping":   ["#LandscapeDesign", "#GreenThumb", "#PropertyValue", "#BeautifulYard"],
    }.get(category, [])

    # Custom hashtags from settings
    custom = settings.get("custom_hashtags", [])

    # Business name tag (builds brand recognition over time)
    brand_tag = [f"#{name}"] if name else []

    all_tags = service_tags[:6] + location_tags[:4] + category_tags[:3] + brand_tag + custom[:3]
    return " ".join(all_tags[:20])  # Instagram caps at 30, we use 20 for clean look


def generate_caption(category: str, settings: dict, is_video: bool = False) -> str:
    """
    Uses Claude to write a fresh, unique post caption every time.
    Falls back to templates if API key not set or call fails.
    """
    tone = settings.get("caption_tone", "friendly")
    city = settings.get("city", "Jackson")
    state = settings.get("state", "TN")
    phone = settings.get("phone", "555-555-5555")
    cta   = settings.get("cta", f"Call/text for a FREE quote: {phone}")
    name  = settings.get("business_name", "us")

    hashtags = build_hashtag_set(settings, category)

    # Category descriptions for Claude
    category_context = {
        "mulching":      "fresh mulch installation — beds filled, clean edges, fresh color",
        "before_after":  "dramatic before & after yard transformation",
        "cleanup":       "full yard cleanup — debris removed, edges clean, looking sharp",
        "edging":        "crisp lawn edging job — clean lines, professional finish",
        "landscaping":   "landscaping work — property looks great, curb appeal boosted",
    }.get(category, "yard work job")

    if not claude:
        return _fallback_caption(category, settings, hashtags)

    try:
        tone_guide = {
            "friendly":     "warm, friendly, conversational — like a neighbor talking to a neighbor",
            "professional": "professional and confident — emphasize quality and reliability",
            "bold":         "bold and punchy — short sentences, strong energy, hype the results",
        }.get(tone, "friendly")

        prompt = f"""Write a Facebook/Instagram post for a local mulching and landscaping company.

Business: {name}
Location: {city}, {state}
Job shown: {category_context}
{"This is a video post." if is_video else "This is a photo post."}
Tone: {tone_guide}

Rules:
- 2-3 sentences MAX — people scroll fast
- Start with a strong hook about the result or transformation
- Mention {city}, {state} or the local area naturally (not forced)
- End with this exact CTA on its own line: {cta}
- Do NOT include hashtags (added separately)
- Do NOT use emojis in excess — max 2
- Make it sound human, not like a robot wrote it
- Every post must sound different — no two the same

Write ONLY the post text, nothing else."""

        msg = claude.messages.create(
            model="claude-3-haiku-20240307",   # fast + cheap for captions
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        caption_text = msg.content[0].text.strip()
        return f"{caption_text}\n\n{hashtags}"

    except Exception as e:
        print(f"[CAPTION] Claude error: {e} — using template fallback")
        return _fallback_caption(category, settings, hashtags)


def _fallback_caption(category: str, settings: dict, hashtags: str) -> str:
    """Template captions used when Claude is unavailable."""
    city  = settings.get("city", "Jackson")
    state = settings.get("state", "TN")
    phone = settings.get("phone", "555-555-5555")
    name  = settings.get("business_name", "us")

    templates = {
        "mulching": [
            f"Fresh mulch makes all the difference 🌿 Just finished another install in {city}, {state}. Your yard could look this good too. Call/text for a FREE quote: {phone}",
            f"Nothing protects your beds and boosts curb appeal like fresh mulch. Another happy customer in {city}! Call/text us: {phone}",
            f"We just transformed another yard in {city}, {state} with a fresh mulch install. Ready for yours? Call/text: {phone}",
        ],
        "before_after": [
            f"The difference is unreal 👀 Before and after in {city}, {state}. Ready to transform your yard? Call/text: {phone}",
            f"This is why we love what we do. Complete transformation in {city}. Call/text {name} for your FREE quote: {phone}",
            f"Left side vs right side. Before and after says it all. Serving {city} and surrounding areas. Call: {phone}",
        ],
        "cleanup": [
            f"Clean yard, happy homeowner ✅ Just wrapped up a full cleanup in {city}, {state}. Call/text us to get yours on the schedule: {phone}",
            f"From overgrown to spotless. Yard cleanup done right in {city}. Book yours today: {phone}",
            f"We take the work off your hands and leave your yard looking sharp. Serving {city}, {state}. Call/text: {phone}",
        ],
        "edging": [
            f"Clean lines = sharp yard. Just finished edging in {city}, {state}. Details make the difference. Call/text: {phone}",
            f"Crisp edges, professional results. Another satisfied customer in {city}. Call/text for a quote: {phone}",
            f"The secret to a professional-looking yard? Clean edges. Serving {city} and surrounding areas. Call: {phone}",
        ],
        "landscaping": [
            f"Another yard upgraded in {city}, {state} 🌿 Curb appeal is everything. Call/text us for your FREE quote: {phone}",
            f"We take pride in every property we touch. Serving {city}, {state} and surrounding areas. Call/text: {phone}",
            f"Your yard is the first thing people see. Make it count. Serving {city}. Call/text: {phone}",
        ],
    }

    options = templates.get(category, templates["mulching"])
    caption = random.choice(options)
    return f"{caption}\n\n{hashtags}"


# ══════════════════════════════════════════════════════════════════════════════
#  FACEBOOK POSTER
# ══════════════════════════════════════════════════════════════════════════════

def post_to_facebook(media_path: Path, caption: str, is_video: bool) -> dict:
    """Post a photo or video to the Facebook Business Page."""
    if not FB_PAGE_ID or not FB_PAGE_ACCESS_TOKEN:
        return {"success": False, "error": "FB credentials not configured"}
    if not POST_TO_FACEBOOK:
        return {"success": False, "error": "Facebook posting disabled"}

    try:
        if is_video:
            url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/videos"
            with open(media_path, "rb") as f:
                resp = requests.post(url, data={
                    "description":    caption,
                    "access_token":   FB_PAGE_ACCESS_TOKEN,
                }, files={"source": f}, timeout=120)
        else:
            url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/photos"
            with open(media_path, "rb") as f:
                resp = requests.post(url, data={
                    "caption":      caption,
                    "access_token": FB_PAGE_ACCESS_TOKEN,
                }, files={"source": f}, timeout=60)

        data = resp.json()
        if "id" in data:
            print(f"[FACEBOOK] ✅ Posted — ID: {data['id']}")
            return {"success": True, "post_id": data["id"]}
        else:
            print(f"[FACEBOOK] ❌ Error: {data}")
            return {"success": False, "error": str(data)}

    except Exception as e:
        print(f"[FACEBOOK] Exception: {e}")
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
#  INSTAGRAM POSTER
# ══════════════════════════════════════════════════════════════════════════════

def post_to_instagram(media_path: Path, caption: str, is_video: bool) -> dict:
    """
    Post to Instagram Business account via Meta Graph API.
    Instagram requires a 2-step process:
    1. Create a media container (upload the image/video)
    2. Publish the container
    """
    if not IG_ACCOUNT_ID or not FB_PAGE_ACCESS_TOKEN:
        return {"success": False, "error": "Instagram credentials not configured"}
    if not POST_TO_INSTAGRAM:
        return {"success": False, "error": "Instagram posting disabled"}

    # Instagram API requires a PUBLIC URL for the media — not a local file.
    # When deployed on Railway/Render, use the server's own public URL.
    # For now we flag this as needing the public URL.
    # TODO: replace YOUR_RAILWAY_URL with the actual deployed URL
    base_url = os.getenv("PUBLIC_URL", "").rstrip("/")
    if not base_url:
        return {"success": False, "error": "PUBLIC_URL env var not set — needed for Instagram"}

    # Build public URL to the media file
    relative = str(media_path).replace(str(MEDIA_DIR), "").replace("\\", "/")
    media_url = f"{base_url}/media{relative}"

    try:
        # Step 1: Create container
        media_type = "VIDEO" if is_video else "IMAGE"
        container_url = f"https://graph.facebook.com/v19.0/{IG_ACCOUNT_ID}/media"
        payload = {
            "caption":      caption,
            "access_token": FB_PAGE_ACCESS_TOKEN,
        }
        if is_video:
            payload["media_type"] = "REELS"
            payload["video_url"]  = media_url
        else:
            payload["image_url"]  = media_url

        r1 = requests.post(container_url, data=payload, timeout=60)
        d1 = r1.json()

        if "id" not in d1:
            return {"success": False, "error": f"Container error: {d1}"}

        container_id = d1["id"]

        # For videos, wait for processing (up to 2 min)
        if is_video:
            for _ in range(12):
                time.sleep(10)
                status_r = requests.get(
                    f"https://graph.facebook.com/v19.0/{container_id}",
                    params={"fields": "status_code", "access_token": FB_PAGE_ACCESS_TOKEN}
                )
                status = status_r.json().get("status_code", "")
                if status == "FINISHED":
                    break
                if status == "ERROR":
                    return {"success": False, "error": "Video processing failed on Instagram"}

        # Step 2: Publish
        publish_url = f"https://graph.facebook.com/v19.0/{IG_ACCOUNT_ID}/media_publish"
        r2 = requests.post(publish_url, data={
            "creation_id":  container_id,
            "access_token": FB_PAGE_ACCESS_TOKEN,
        }, timeout=30)
        d2 = r2.json()

        if "id" in d2:
            print(f"[INSTAGRAM] ✅ Posted — ID: {d2['id']}")
            return {"success": True, "post_id": d2["id"]}
        else:
            print(f"[INSTAGRAM] ❌ Error: {d2}")
            return {"success": False, "error": str(d2)}

    except Exception as e:
        print(f"[INSTAGRAM] Exception: {e}")
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
#  CORE POSTING FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def run_post():
    """
    Main post function — called by scheduler 4x per day.
    Picks media → generates caption → posts to FB + IG → logs result.
    """
    if not bot_state["running"]:
        print("[BOT] Paused — skipping scheduled post")
        return

    settings = load_settings()
    if not settings.get("auto_post", True):
        print("[BOT] Auto-post disabled — skipping")
        return

    print(f"\n{'='*55}")
    print(f"[BOT] Scheduled post firing — {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    print(f"{'='*55}")

    # Pick a media file
    media = pick_media()
    if not media:
        print("[BOT] ❌ No media found — upload photos/videos to the media folder")
        bot_state["last_post_result"] = "failed: no media"
        return

    media_path = media["path"]
    category   = media["category"]
    is_video   = media_path.suffix.lower() in VIDEO_EXTS

    print(f"[BOT] Media: {media_path.name} ({category}) | {'VIDEO' if is_video else 'PHOTO'}")

    # Generate caption
    caption = generate_caption(category, settings, is_video)
    print(f"[BOT] Caption ({len(caption)} chars):\n{caption[:120]}...")

    # Post to platforms
    fb_result = post_to_facebook(media_path, caption, is_video)
    ig_result = post_to_instagram(media_path, caption, is_video)

    # Log it
    log_post(str(media_path), caption, category, fb_result, ig_result)

    # Update state
    today = datetime.now(timezone.utc).date().isoformat()
    bot_state["posts_today"]    += 1
    bot_state["last_post_time"]  = datetime.now(timezone.utc).isoformat()
    bot_state["last_post_result"] = (
        "✅ Posted to FB + IG" if (fb_result.get("success") and ig_result.get("success"))
        else "⚠️ Partial" if (fb_result.get("success") or ig_result.get("success"))
        else "❌ Failed"
    )

    print(f"[BOT] Facebook: {fb_result}")
    print(f"[BOT] Instagram: {ig_result}")
    print(f"[BOT] Result: {bot_state['last_post_result']}")


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULER SETUP
# ══════════════════════════════════════════════════════════════════════════════

def setup_scheduler():
    """Set up the posting schedule from settings."""
    settings = load_settings()
    post_times = settings.get("post_times", POST_TIMES)

    scheduler = BackgroundScheduler()

    for t in post_times:
        try:
            hour, minute = map(int, t.strip().split(":"))
            scheduler.add_job(
                run_post,
                trigger="cron",
                hour=hour,
                minute=minute,
                id=f"post_{hour}_{minute}",
                replace_existing=True,
            )
            print(f"[SCHEDULER] Post scheduled at {t} UTC daily")
        except Exception as e:
            print(f"[SCHEDULER] Bad time format '{t}': {e}")

    scheduler.start()
    print(f"[SCHEDULER] ✅ {len(post_times)} daily posts scheduled")
    return scheduler

scheduler = setup_scheduler()


# ══════════════════════════════════════════════════════════════════════════════
#  API ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
def health():
    return {
        "status":      "Mulch Boss running",
        "bot_running": bot_state["running"],
        "posts_today": bot_state["posts_today"],
        "total_posts": bot_state["total_posts"],
        "last_post":   bot_state["last_post_time"],
    }


@app.get("/api/status")
def get_status():
    """Live bot status for dashboard."""
    log      = load_post_log()
    settings = load_settings()
    today    = datetime.now(timezone.utc).date().isoformat()
    today_posts = [p for p in log if p["timestamp"].startswith(today)]
    media    = get_all_media()

    return {
        "running":          bot_state["running"],
        "auto_post":        settings.get("auto_post", True),
        "posts_today":      len(today_posts),
        "total_posts":      len([p for p in log if p["status"] == "posted"]),
        "last_post_time":   bot_state["last_post_time"],
        "last_post_result": bot_state["last_post_result"],
        "media_count":      len(media),
        "media_by_category": {cat: len([m for m in media if m["category"] == cat]) for cat in CATEGORIES},
        "post_times":       settings.get("post_times", POST_TIMES),
        "business_name":    settings.get("business_name", BUSINESS_NAME),
        "recent_posts":     list(reversed(log[-10:])),
        "post_to_facebook": settings.get("post_facebook", True),
        "post_to_instagram":settings.get("post_instagram", True),
    }


@app.get("/api/posts")
def get_posts(limit: int = 50):
    """Return post history."""
    log = load_post_log()
    return {"posts": list(reversed(log[-limit:])), "total": len(log)}


@app.get("/api/media")
def get_media():
    """List all media files with metadata."""
    all_media = get_all_media()
    result = []
    for m in all_media:
        stat = m["path"].stat()
        result.append({
            "name":     m["name"],
            "category": m["category"],
            "type":     "video" if m["path"].suffix.lower() in VIDEO_EXTS else "image",
            "size_kb":  round(stat.st_size / 1024, 1),
            "url":      f"/media/{m['category']}/{m['name']}",
        })
    return {"media": result, "total": len(result)}


@app.post("/api/upload")
async def upload_media(
    category: str = Form(...),
    files: list[UploadFile] = File(...)
):
    """Upload photos or videos to a category folder."""
    if category not in CATEGORIES:
        return JSONResponse({"error": f"Invalid category. Use: {CATEGORIES}"}, status_code=400)

    uploaded = []
    for file in files:
        suffix = Path(file.filename).suffix.lower()
        if suffix not in MEDIA_EXTS:
            continue
        dest = MEDIA_DIR / category / file.filename
        dest.write_bytes(await file.read())
        uploaded.append(file.filename)
        print(f"[UPLOAD] {file.filename} → {category}/")

    return {"uploaded": uploaded, "count": len(uploaded)}


@app.delete("/api/media/{category}/{filename}")
def delete_media(category: str, filename: str):
    """Delete a media file."""
    path = MEDIA_DIR / category / filename
    if path.exists():
        path.unlink()
        return {"deleted": filename}
    return JSONResponse({"error": "File not found"}, status_code=404)


@app.post("/api/post-now")
def post_now():
    """Manually trigger a post immediately."""
    import threading
    threading.Thread(target=run_post, daemon=True).start()
    return {"status": "posting", "message": "Post firing now — check /api/status in 10 seconds"}


@app.post("/api/toggle-bot")
def toggle_bot():
    """Pause or resume the auto-poster."""
    bot_state["running"] = not bot_state["running"]
    status = "running" if bot_state["running"] else "paused"
    print(f"[BOT] {status.upper()} by user")
    return {"running": bot_state["running"], "status": status}


@app.get("/api/settings")
def get_settings():
    return load_settings()


@app.post("/api/settings")
async def update_settings(request: Request):
    """Update bot settings."""
    data = await request.json()
    settings = load_settings()
    settings.update(data)
    save_settings(settings)
    print(f"[SETTINGS] Updated: {list(data.keys())}")
    return {"saved": True, "settings": settings}


@app.post("/api/generate-caption")
async def generate_caption_endpoint(request: Request):
    """Generate a preview caption without posting."""
    data     = await request.json()
    category = data.get("category", "mulching")
    settings = load_settings()
    caption  = generate_caption(category, settings)
    return {"caption": caption, "category": category}


# ── Reset daily post counter at midnight ──────────────────────────────────────
def reset_daily():
    bot_state["posts_today"] = 0
    bot_state["errors_today"] = 0
    print("[BOT] Daily counters reset")

scheduler.add_job(reset_daily, trigger="cron", hour=0, minute=0)


# ── Graceful shutdown ──────────────────────────────────────────────────────────
def graceful_shutdown(signum=None, frame=None):
    print("[SHUTDOWN] Stopping scheduler...")
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        pass
    print("[SHUTDOWN] Done")

signal.signal(signal.SIGTERM, graceful_shutdown)
atexit.register(graceful_shutdown)

print(f"""
╔══════════════════════════════════════════════════════════════╗
║  MULCH BOSS STARTED                                          ║
║  Business : {BUSINESS_NAME:<47}║
║  Area     : {SERVICE_AREA:<47}║
║  Platforms: {'Facebook ✅' if POST_TO_FACEBOOK else 'Facebook ❌'}  {'Instagram ✅' if POST_TO_INSTAGRAM else 'Instagram ❌':<35}║
║  Schedule : {', '.join(POST_TIMES):<47}║
╚══════════════════════════════════════════════════════════════╝
""")
