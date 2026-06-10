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
import hashlib
import hmac
import subprocess
import requests
import anthropic
from pathlib import Path
from datetime import datetime, timezone
from fastapi import FastAPI, Request, UploadFile, File, Form, Cookie
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
POSTS_PER_DAY     = int(os.getenv("POSTS_PER_DAY", "2"))
POST_TIMES        = os.getenv("POST_TIMES", "16:00,01:00").split(",")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "mulchboss")
SECRET_KEY         = os.getenv("SECRET_KEY", uuid.uuid4().hex)  # set in Railway for persistence


# ── Auth helpers ───────────────────────────────────────────────────────────────
def make_session_token(password: str) -> str:
    """Create a signed session token from the password."""
    return hmac.new(SECRET_KEY.encode(), password.encode(), hashlib.sha256).hexdigest()

def is_authenticated(session: str = None) -> bool:
    if not session:
        return False
    expected = make_session_token(DASHBOARD_PASSWORD)
    return hmac.compare_digest(session, expected)

def auth_required(request: Request, session: str = Cookie(default=None)):
    """Returns redirect if not logged in, else None."""
    if not is_authenticated(session):
        return RedirectResponse(url="/login", status_code=302)
    return None

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
MEDIA_DIR   = Path(os.getenv("MEDIA_DIR", str(BASE_DIR / "media")))
DATA_DIR    = Path(os.getenv("DATA_DIR",  str(BASE_DIR / "data")))
POST_LOG           = DATA_DIR / "post_log.json"
SETTINGS_FILE      = DATA_DIR / "settings.json"
MARKETPLACE_FILE   = DATA_DIR / "marketplace_listing.json"
VOCAB_FILE         = DATA_DIR / "vocab.txt"

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
#  LOGO OVERLAY — watermarks every photo/video before posting
# ══════════════════════════════════════════════════════════════════════════════

LOGO_PATH         = DATA_DIR / "logo.png"
LOGO_SETTINGS_FILE = DATA_DIR / "logo_settings.json"

def load_logo_settings() -> dict:
    defaults = {"position": "bottom-right", "size_pct": 25}
    if LOGO_SETTINGS_FILE.exists():
        try:
            defaults.update(json.loads(LOGO_SETTINGS_FILE.read_text()))
        except Exception:
            pass
    return defaults

def _logo_xy(base_w: int, base_h: int, logo_w: int, logo_h: int, position: str) -> tuple:
    margin_x = max(8, int(base_w * 0.02))
    margin_y = max(8, int(base_h * 0.02))
    if position == "top-left":
        return margin_x, margin_y
    if position == "top-right":
        return base_w - logo_w - margin_x, margin_y
    if position == "bottom-left":
        return margin_x, base_h - logo_h - margin_y
    # default: bottom-right
    return base_w - logo_w - margin_x, base_h - logo_h - margin_y

def _ffmpeg_overlay_expr(position: str, size_pct: int) -> str:
    scale = size_pct / 100
    if position == "top-left":
        return f"[1:v]scale=iw*{scale}:-1[logo];[0:v][logo]overlay=W*0.02:H*0.02"
    if position == "top-right":
        return f"[1:v]scale=iw*{scale}:-1[logo];[0:v][logo]overlay=W-w-W*0.02:H*0.02"
    if position == "bottom-left":
        return f"[1:v]scale=iw*{scale}:-1[logo];[0:v][logo]overlay=W*0.02:H-h-H*0.02"
    # default: bottom-right
    return f"[1:v]scale=iw*{scale}:-1[logo];[0:v][logo]overlay=W-w-W*0.02:H-h-H*0.02"

def _apply_logo_to_image(media_path: Path) -> Path:
    from PIL import Image
    cfg = load_logo_settings()
    size_pct = cfg.get("size_pct", 25)
    position = cfg.get("position", "bottom-right")

    with Image.open(media_path).convert("RGBA") as base:
        with Image.open(LOGO_PATH).convert("RGBA") as logo:
            logo_w = max(80, int(base.width * size_pct / 100))
            logo_h = int(logo.height * logo_w / logo.width)
            logo   = logo.resize((logo_w, logo_h), Image.LANCZOS)
            x, y   = _logo_xy(base.width, base.height, logo_w, logo_h, position)
            out    = base.copy()
            out.paste(logo, (x, y), logo)
            tmp = media_path.parent / f"_logo_{media_path.name}"
            if tmp.suffix.lower() in {".jpg", ".jpeg"}:
                out = out.convert("RGB")
            out.save(tmp, quality=95)
            return tmp

def _apply_logo_to_video(media_path: Path) -> Path:
    cfg      = load_logo_settings()
    position = cfg.get("position", "bottom-right")
    size_pct = cfg.get("size_pct", 25)
    tmp      = media_path.parent / f"_logo_{media_path.stem}.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-i", str(media_path),
        "-i", str(LOGO_PATH),
        "-filter_complex", _ffmpeg_overlay_expr(position, size_pct),
        "-codec:a", "copy",
        str(tmp),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=300)
    if result.returncode != 0:
        print(f"[LOGO] FFmpeg error: {result.stderr.decode()[:300]}")
        return media_path
    return tmp


def prepare_media_with_logo(media_path: Path, is_video: bool) -> Path:
    """
    Returns a logo-watermarked copy of the media file.
    If no logo is uploaded yet, returns the original path unchanged.
    Caller must delete the returned temp file when done (if it differs from media_path).
    """
    if not LOGO_PATH.exists():
        return media_path
    try:
        if is_video:
            return _apply_logo_to_video(media_path)
        else:
            return _apply_logo_to_image(media_path)
    except Exception as e:
        print(f"[LOGO] Overlay failed ({e}) — posting without logo")
        return media_path


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

    # Core service hashtags — forestry mulching specific
    service_tags = [
        "#ForestryMulching", "#ForestryMulcher", "#LandClearing",
        "#BrushClearing", "#LandReclamation", "#OvergrownLand",
        "#PastureReclamation", "#BrushRemoval", "#TreeClearing",
        "#LandManagement", "#ErosionControl", "#PropertyClearing",
    ]

    # Location hashtags — local search dominance
    location_tags = [
        f"#{city}TN", f"#{city}{state}", f"#{city}LandClearing",
        f"#{city}ForestryMulching", f"#{state}LandClearing", f"#{state}ForestryMulching",
        f"#{city}PropertyClearing",
    ]

    # Category-specific tags
    category_tags = {
        "before_after":  ["#BeforeAndAfter", "#Transformation", "#LandTransformation"],
        "mulching":      ["#ForestryMulch", "#LandReclaimed", "#ClearedLand"],
        "cleanup":       ["#PropertyCleanup", "#BrushCleared", "#LandRestoration"],
        "edging":        ["#FenceLineClearing", "#RightOfWay", "#CleanBoundaries"],
        "landscaping":   ["#LandCleared", "#PropertyValue", "#UsableLand"],
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
        "mulching":      "forestry mulching job — overgrown land cleared, brush and trees ground down into natural mulch that stays on site, land reclaimed and looking clean",
        "before_after":  "dramatic before & after — land that was thick with brush, overgrowth or trees is now completely cleared and mulched, transformation is unreal",
        "cleanup":       "property cleanup — invasive species, dead trees, overgrown brush all cleared and mulched in one pass, land is usable again",
        "edging":        "clean boundary work — fence lines, pasture edges, right-of-way cleared tight and clean with the forestry mulcher",
        "landscaping":   "land clearing and mulching — property transformed from overgrown mess to clean usable land, no hauling debris, mulch stays on site feeding the soil",
    }.get(category, "forestry mulching job — land cleared, brush ground up, property transformed")

    if not claude:
        return _fallback_caption(category, settings, hashtags)

    # Load custom vocab samples if uploaded
    vocab_section = ""
    if VOCAB_FILE.exists():
        vocab_text = VOCAB_FILE.read_text(encoding="utf-8").strip()
        if vocab_text:
            vocab_section = f"\nHere are real examples of how this business owner actually talks — match this voice exactly:\n---\n{vocab_text}\n---\n"

    try:
        prompt = f"""You are the owner of {name}, a forestry mulching business in {city}, {state}. You've lived in West Tennessee your whole life. Write a Facebook post about this job the way you'd actually type it on your phone.

Job: {category_context}
{vocab_section}
VOICE — You are a working man from Lexington, Henderson County TN. Write EXACTLY like this:
- Drop your g's: workin', clearin', haulin', fixin', mulchin'
- Use "fixin' to" (about to), "might could" (might be able to), "ain't", "y'all", "gonna", "gotta", "wanna"
- Say "piece of property" not "property", "tore up" for overgrown/rough condition, "wide open" for cleared land
- Start with what happened: "Just finished up...", "Had a property out...", "Man you shoulda seen...", "Got called out to..."
- Tell it like a short story — the situation, what you did, how it turned out
- Reference real West Tennessee areas naturally: Henderson County, Lexington, Parsons, Savannah, Jackson area
- Sound proud of the work but matter-of-fact about it — "that's what we do", "we get 'er done"
- SHORT — 2-4 sentences max. Working people don't write essays.

NEVER use: transform, seamless, solution, efficiently, comprehensive, thrilled, innovative, pleased to announce, our team
NEVER use perfect grammar or punctuation
NEVER sound like an ad

End with this on its own line: {cta}
No hashtags. Write the post only, nothing else."""

        msg = claude.messages.create(
            model="claude-haiku-4-5-20251001",
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
    city  = settings.get("city", "Lexington")
    state = settings.get("state", "TN")
    phone = settings.get("phone", "555-555-5555")
    name  = settings.get("business_name", "SRF Forestry Mulching")

    templates = {
        "mulching": [
            f"That overgrowth didn't stand a chance. 🌿 Forestry mulcher went in and turned years of brush into clean ground in a single pass. Land is ready to use. Serving {city}, {state} and surrounding areas.\n\nCall/text for a FREE quote: {phone}",
            f"One machine. No hauling. No burning. Just clean land. Forestry mulching is the most efficient way to reclaim overgrown property in {city}, {state} — and the mulch stays on site to protect and feed your soil.\n\nCall/text: {phone}",
            f"Thick brush, downed trees, invasive growth — gone. The forestry mulcher grinds it all down and leaves behind a clean, mulched surface that actually improves the land over time. Another satisfied property owner in {city}, {state}.\n\nFree quotes: {phone}",
        ],
        "before_after": [
            f"This is what we do. Land that was completely locked up by overgrowth, brush and trees — cleared, mulched and reclaimed in one day. No debris hauled off, no burning, just results. {city}, {state}.\n\nCall/text for a FREE quote: {phone}",
            f"Before the mulcher, this property was unusable. After — clean ground, healthy soil, and land you can actually work with again. That's the power of forestry mulching. Serving {city}, {state} and surrounding areas.\n\nCall/text: {phone}",
            f"Left side was what the customer started with. Right side is what they got. Forestry mulching transforms overgrown land fast — and the ground mulch left behind is good for the soil. {city}, {state}.\n\nFree quotes: {phone}",
        ],
        "cleanup": [
            f"Fence lines buried in brush. Pasture edges swallowed by invasive growth. Not anymore. The forestry mulcher clears it clean and tight — no hand cutting, no hauling. Serving {city}, {state}.\n\nCall/text: {phone}",
            f"Property cleanup done right means the land is actually better after we leave. The mulch we leave behind protects against erosion and breaks down naturally into the soil. {city}, {state} and surrounding areas.\n\nFree quotes: {phone}",
            f"Overgrown land costs you money and use of your property. One pass with the forestry mulcher and you've got clean, usable ground again. Fast, efficient, no mess to deal with. Serving {city}, {state}.\n\nCall/text: {phone}",
        ],
        "edging": [
            f"Clean fence lines and property boundaries make a real difference — not just how it looks, but how the land functions. The forestry mulcher gets in tight where other equipment can't. {city}, {state}.\n\nCall/text for a FREE quote: {phone}",
            f"Right-of-way clearing, fence line cleanup, pasture boundary work — we get it done clean and fast. No hand crews, no burning. Just the mulcher and clean results. Serving {city}, {state}.\n\nCall/text: {phone}",
            f"Tight lines. Clean edges. That's how we leave every property in {city}, {state}. Forestry mulching handles the boundary work that other equipment leaves behind.\n\nFree quotes: {phone}",
        ],
        "landscaping": [
            f"Land clearing doesn't have to mean leaving a mess behind. The forestry mulcher grinds everything down and the mulch stays on the ground — protecting the soil, preventing erosion, breaking down naturally. {city}, {state}.\n\nCall/text: {phone}",
            f"Reclaiming overgrown land in {city}, {state}. Whether it's pasture reclamation, lot clearing or property cleanup — one machine handles it all and leaves the land better than we found it.\n\nFree quotes: {phone}",
            f"This is forestry mulching done right. No burning. No hauling. One efficient machine that clears land and improves it at the same time. Serving {city}, {state} and surrounding areas.\n\nCall/text: {phone}",
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
            # Video → post to page timeline feed
            url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/videos"
            with open(media_path, "rb") as f:
                resp = requests.post(url, data={
                    "description":  caption,
                    "published":    "true",
                    "access_token": FB_PAGE_ACCESS_TOKEN,
                }, files={"source": f}, timeout=120)
        else:
            # Photo — 2-step: upload then post to feed so it always hits the timeline
            # Step 1: upload photo unpublished to get a photo ID
            photo_url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/photos"
            with open(media_path, "rb") as f:
                r1 = requests.post(photo_url, data={
                    "published":    "false",
                    "access_token": FB_PAGE_ACCESS_TOKEN,
                }, files={"source": f}, timeout=60)
            d1 = r1.json()
            print(f"[FACEBOOK] Photo upload response: {d1}")

            if "id" not in d1:
                return {"success": False, "error": f"Photo upload failed: {d1}"}

            photo_id = d1["id"]

            # Step 2: post to page feed with photo attached → always appears on timeline
            import json as _json
            feed_url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/feed"
            r2 = requests.post(feed_url, data={
                "message":        caption,
                "attached_media": _json.dumps([{"media_fbid": photo_id}]),
                "access_token":   FB_PAGE_ACCESS_TOKEN,
            }, timeout=30)
            resp = r2

        data = resp.json()
        if "id" in data:
            print(f"[FACEBOOK] ✅ Posted to timeline — ID: {data['id']}")
            return {"success": True, "post_id": data["id"]}
        else:
            print(f"[FACEBOOK] ❌ Error: {data}")
            return {"success": False, "error": str(data)}

    except Exception as e:
        print(f"[FACEBOOK] Exception: {e}")
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
#  FACEBOOK STORY POSTER  (videos only → Stories)
# ══════════════════════════════════════════════════════════════════════════════

def post_photo_to_story(media_path: Path) -> dict:
    """Post a photo to Facebook Page Story."""
    if not FB_PAGE_ID or not FB_PAGE_ACCESS_TOKEN:
        return {"success": False, "error": "FB credentials not configured"}
    try:
        url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/photo_stories"
        with open(media_path, "rb") as f:
            r = requests.post(url, data={"access_token": FB_PAGE_ACCESS_TOKEN},
                              files={"source": f}, timeout=60)
        d = r.json()
        if d.get("success") or "post_id" in d:
            print(f"[STORY] ✅ Photo story posted")
            return {"success": True}
        else:
            print(f"[STORY] ❌ Photo story error: {d}")
            return {"success": False, "error": str(d)}
    except Exception as e:
        print(f"[STORY] Photo story exception: {e}")
        return {"success": False, "error": str(e)}


def post_to_facebook_story(media_path: Path) -> dict:
    """
    Post a video to Facebook Page Story.
    Uses the 3-step video story upload:
      1. Start upload session → get video_id + upload_url
      2. Upload raw video bytes to upload_url
      3. Finish → publishes the story
    """
    if not FB_PAGE_ID or not FB_PAGE_ACCESS_TOKEN:
        return {"success": False, "error": "FB credentials not configured"}

    base_url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/video_stories"

    try:
        file_size = media_path.stat().st_size

        # Step 1 — start upload session
        r1 = requests.post(base_url, data={
            "upload_phase":  "start",
            "access_token":  FB_PAGE_ACCESS_TOKEN,
        }, timeout=30)
        d1 = r1.json()
        print(f"[STORY] Start response: {d1}")

        if "video_id" not in d1 or "upload_url" not in d1:
            return {"success": False, "error": f"Story start error: {d1}"}

        video_id   = d1["video_id"]
        upload_url = d1["upload_url"]

        # Step 2 — upload raw video bytes
        with open(media_path, "rb") as f:
            r2 = requests.post(upload_url, data=f, headers={
                "Authorization":  f"OAuth {FB_PAGE_ACCESS_TOKEN}",
                "Content-Type":   "application/octet-stream",
                "offset":         "0",
                "file_size":      str(file_size),
            }, timeout=180)
        print(f"[STORY] Upload response: {r2.status_code}")

        # Step 3 — finish / publish
        r3 = requests.post(base_url, data={
            "upload_phase":  "finish",
            "video_id":      video_id,
            "access_token":  FB_PAGE_ACCESS_TOKEN,
        }, timeout=30)
        d3 = r3.json()
        print(f"[STORY] Finish response: {d3}")

        if d3.get("success"):
            print(f"[STORY] ✅ Video story posted — video_id: {video_id}")
            return {"success": True, "video_id": video_id}
        else:
            return {"success": False, "error": str(d3)}

    except Exception as e:
        print(f"[STORY] Exception: {e}")
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
    Main post function — called by scheduler 2x per day.

    Routing logic:
      📹 VIDEO  → Facebook Story only  (disappears after 24h, shows at top of feed)
      📸 PHOTO  → Facebook Feed only   (stays on page permanently, with AI caption)
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

    print(f"[BOT] Media: {media_path.name} ({category}) | {'VIDEO' if is_video else 'PHOTO'} → FEED + STORY")

    # Generate caption for feed post
    caption = generate_caption(category, settings, is_video=is_video)
    print(f"[BOT] Caption ({len(caption)} chars):\n{caption[:120]}...")

    # Apply logo watermark
    post_path = prepare_media_with_logo(media_path, is_video)
    used_logo = post_path != media_path
    if used_logo:
        print(f"[LOGO] Watermarked → {post_path.name}")

    # ── Post to Feed (timeline) ─────────────────────────────────
    print("[BOT] Posting to Facebook Feed...")
    fb_result = post_to_facebook(post_path, caption, is_video=is_video)
    print(f"[BOT] Feed result: {fb_result}")

    # ── Post to Story ───────────────────────────────────────────
    print("[BOT] Posting to Facebook Story...")
    if is_video:
        story_result = post_to_facebook_story(post_path)
    else:
        story_result = post_photo_to_story(post_path)
    print(f"[BOT] Story result: {story_result}")

    # Clean up temp logo file
    if used_logo and post_path.exists():
        post_path.unlink()

    ig_result = {"success": False, "skipped": "Instagram disabled"}
    log_post(str(media_path), caption, category, fb_result, ig_result)

    feed_ok  = fb_result.get("success", False)
    story_ok = story_result.get("success", False)
    bot_state["last_post_result"] = (
        f"{'✅' if feed_ok else '❌'} Feed | {'✅' if story_ok else '❌'} Story"
    )

    bot_state["posts_today"]   += 1
    bot_state["last_post_time"] = datetime.now(timezone.utc).isoformat()
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

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = None):
    error_html = '<p style="color:#e94560;margin-top:10px">❌ Wrong password — try again</p>' if error else ''
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head>
<title>Mulch Boss — Login</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:Arial,sans-serif; background:#1a1a2e; display:flex;
    align-items:center; justify-content:center; min-height:100vh; }}
  .card {{ background:#16213e; border:1px solid #0f3460; border-radius:12px;
    padding:40px 32px; width:100%; max-width:360px; text-align:center; }}
  h1 {{ color:#4ecca3; font-size:24px; margin-bottom:6px; }}
  p {{ color:#888; font-size:13px; margin-bottom:24px; }}
  input {{ width:100%; background:#0f3460; border:1px solid #1a4a6a; color:#eee;
    padding:12px 14px; border-radius:8px; font-size:15px; margin-bottom:14px; }}
  input:focus {{ outline:2px solid #4ecca3; }}
  button {{ width:100%; background:#4ecca3; color:#1a1a2e; border:none;
    border-radius:8px; padding:12px; font-size:16px; font-weight:bold; cursor:pointer; }}
  button:hover {{ opacity:0.88; }}
</style>
</head>
<body>
  <div class="card">
    <h1>🌿 SRF Forestry Mulching Bot</h1>
    <p>Control Panel — Lexington, TN</p>
    <form method="POST" action="/login">
      <input type="password" name="password" placeholder="Enter password" autofocus>
      <button type="submit">Login</button>
    </form>
    {error_html}
  </div>
</body></html>""")


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if password == DASHBOARD_PASSWORD:
        token = make_session_token(password)
        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie(
            key="session",
            value=token,
            httponly=True,
            max_age=60 * 60 * 24 * 30,  # 30 days
            samesite="lax",
        )
        return response
    return RedirectResponse(url="/login?error=1", status_code=302)


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session")
    return response


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: str = Cookie(default=None)):
    if not is_authenticated(session):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/privacy", response_class=HTMLResponse)
def privacy_policy(request: Request):
    settings = load_settings()
    name  = settings.get("business_name", "SRF Forestry Mulching")
    city  = settings.get("city",  "Lexington")
    state = settings.get("state", "TN")
    phone = settings.get("phone", "555-555-5555")
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>Privacy Policy — {name}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:Arial,sans-serif;max-width:800px;margin:40px auto;padding:0 20px;color:#333;line-height:1.7}}
h1{{color:#2d6a2d}}h2{{color:#444;margin-top:30px}}a{{color:#2d6a2d}}</style>
</head><body>
<h1>Privacy Policy</h1>
<p><strong>{name}</strong> — {city}, {state}</p>
<p><em>Last updated: June 2026</em></p>

<h2>1. Information We Collect</h2>
<p>We do not collect personal information from visitors. Our Facebook page automation tool only accesses our own business Facebook Page to publish posts on our behalf. We do not collect, store, or share data from any users or followers.</p>

<h2>2. How We Use Information</h2>
<p>Our automated posting tool uses the Facebook Pages API solely to publish business content (photos, videos, and captions) to the <strong>{name}</strong> Facebook Page. No user data is accessed, stored, or processed.</p>

<h2>3. Facebook Data</h2>
<p>This application uses the Meta/Facebook Graph API to post content to our own business page. We only request the minimum permissions required: <code>pages_manage_posts</code>, <code>pages_show_list</code>, and <code>pages_read_engagement</code>. We do not access any user data, friend lists, messages, or personal profiles.</p>

<h2>4. Third-Party Services</h2>
<p>We use the following services to operate our business:</p>
<ul>
<li><strong>Meta/Facebook</strong> — for business page posting</li>
<li><strong>Anthropic Claude AI</strong> — for generating post captions (no personal data is sent)</li>
</ul>

<h2>5. Data Retention</h2>
<p>We retain records of posts made to our Facebook page for business purposes. No personal user data is retained.</p>

<h2>6. Contact Us</h2>
<p>If you have any questions about this Privacy Policy, contact us at:</p>
<p><strong>{name}</strong><br>
{city}, {state}<br>
Phone: {phone}</p>
</body></html>""")


@app.get("/health")
def health():
    return {
        "status":      "Mulch Boss running",
        "bot_running": bot_state["running"],
        "posts_today": bot_state["posts_today"],
        "total_posts": bot_state["total_posts"],
        "last_post":   bot_state["last_post_time"],
    }


def require_auth(session: str = Cookie(default=None)):
    """Shared auth check for all API routes."""
    if not is_authenticated(session):
        raise __import__('fastapi').HTTPException(status_code=401, detail="Unauthorized")


@app.get("/api/status")
def get_status(session: str = Cookie(default=None)):
    require_auth(session)
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


@app.post("/api/upload-logo")
async def upload_logo(
    file: UploadFile = File(...),
    session: str = Cookie(default=None),
):
    """Upload the business logo used to watermark all posts."""
    require_auth(session)
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg"}:
        return JSONResponse({"error": "Logo must be PNG or JPG"}, status_code=400)
    data = await file.read()
    # Always save as PNG so transparency is preserved
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    img.save(str(LOGO_PATH), "PNG")
    print(f"[LOGO] Saved logo → {LOGO_PATH} ({len(data)//1024}KB)")
    return {"saved": True, "path": str(LOGO_PATH), "size_kb": round(LOGO_PATH.stat().st_size / 1024, 1)}


@app.get("/api/logo-status")
def logo_status(session: str = Cookie(default=None)):
    """Check if a logo has been uploaded."""
    require_auth(session)
    if LOGO_PATH.exists():
        return {"uploaded": True, "size_kb": round(LOGO_PATH.stat().st_size / 1024, 1)}
    return {"uploaded": False}


@app.get("/api/logo-settings")
def get_logo_settings(session: str = Cookie(default=None)):
    require_auth(session)
    return load_logo_settings()

@app.post("/api/logo-settings")
async def save_logo_settings_endpoint(request: Request, session: str = Cookie(default=None)):
    require_auth(session)
    data = await request.json()
    cfg  = load_logo_settings()
    if "position" in data and data["position"] in {"top-left","top-right","bottom-left","bottom-right"}:
        cfg["position"] = data["position"]
    if "size_pct" in data:
        cfg["size_pct"] = max(5, min(50, int(data["size_pct"])))
    LOGO_SETTINGS_FILE.write_text(json.dumps(cfg, indent=2))
    return {"saved": True, "settings": cfg}

@app.get("/data-logo")
def serve_logo(session: str = Cookie(default=None)):
    """Serve the uploaded logo image for dashboard preview."""
    require_auth(session)
    from fastapi.responses import FileResponse
    if LOGO_PATH.exists():
        return FileResponse(str(LOGO_PATH), media_type="image/png")
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="No logo uploaded")


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


@app.get("/api/marketplace-listing")
def get_marketplace_listing():
    """Get the current Marketplace listing."""
    return load_marketplace_listing()


@app.get("/api/vocab")
async def get_vocab(request: Request):
    """Get current vocab/voice samples."""
    auth = auth_required(request)
    if auth: return auth
    text = VOCAB_FILE.read_text(encoding="utf-8").strip() if VOCAB_FILE.exists() else ""
    return {"vocab": text}

@app.post("/api/vocab")
async def save_vocab(request: Request):
    """Save custom vocab/voice samples."""
    auth = auth_required(request)
    if auth: return auth
    body = await request.json()
    text = body.get("vocab", "").strip()
    VOCAB_FILE.write_text(text, encoding="utf-8")
    return {"status": "saved", "chars": len(text)}


@app.post("/api/marketplace-refresh")
def refresh_marketplace_listing():
    """Manually trigger a fresh Marketplace listing."""
    import threading
    threading.Thread(target=run_marketplace_refresh, daemon=True).start()
    return {"status": "generating", "message": "Fresh listing generating — refresh in 10 seconds"}


# ══════════════════════════════════════════════════════════════════════════════
#  FACEBOOK MARKETPLACE LISTING GENERATOR  (every 2 weeks)
# ══════════════════════════════════════════════════════════════════════════════

def generate_marketplace_listing() -> str:
    """
    Uses Claude to write a fresh Facebook Marketplace service listing.
    Falls back to a solid template if API unavailable.
    """
    settings = load_settings()
    city     = settings.get("city",  "Lexington")
    state    = settings.get("state", "TN")
    phone    = settings.get("phone", "555-555-5555")
    name     = settings.get("business_name", "SRF Forestry Mulching")

    if claude:
        try:
            prompt = f"""Write a Facebook Marketplace service listing for a forestry mulching business.

Business: {name}
Location: {city}, {state}
Phone: {phone}

About forestry mulching: A forestry mulcher is a powerful machine that grinds trees, brush, saplings and overgrowth directly into mulch on-site in a single pass. No hauling debris, no burning. Used for land clearing, pasture reclamation, fence line clearing, right-of-way clearing, fire break creation, and invasive species removal. The mulch stays on the ground protecting soil and preventing erosion.

Write a Marketplace listing that sounds like it was written by the business owner — confident, knowledgeable, genuine. Someone who knows their equipment and their craft.

Format it exactly like this:
TITLE: [short punchy title, max 10 words]

DESCRIPTION:
[3-5 sentences about the service — specific, knowledgeable, confident. Mention what jobs we handle. Mention the city/state and surrounding areas. End with call to action and phone number.]

SERVICES OFFERED:
[bullet list of 6-8 specific services]

SERVICE AREA:
[city, state and surrounding areas]

CONTACT:
[phone number and call/text instruction]

Make every listing unique — different angle, different opening, different energy. Never sound like a template."""

            msg = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}]
            )
            return msg.content[0].text.strip()
        except Exception as e:
            print(f"[MARKETPLACE] Claude error: {e} — using template")

    # Fallback template
    return f"""TITLE: Forestry Mulching & Land Clearing — {city}, {state}

DESCRIPTION:
{name} provides professional forestry mulching and land clearing services in {city}, {state} and surrounding areas. Our forestry mulcher grinds trees, brush and overgrowth into mulch on-site in a single pass — no hauling, no burning, just clean usable land. Whether you need a pasture reclaimed, fence lines cleared, or a lot cleared for development, we get it done right. Call or text for a free quote: {phone}

SERVICES OFFERED:
• Land clearing & brush removal
• Pasture reclamation
• Fence line & boundary clearing
• Right-of-way clearing
• Fire break creation
• Invasive species removal
• Lot clearing for construction
• Overgrown property cleanup

SERVICE AREA:
{city}, {state} and surrounding West Tennessee areas

CONTACT:
Call or text for a FREE quote: {phone}"""


def run_marketplace_refresh():
    """Generate a fresh Marketplace listing and save it — runs every 2 weeks."""
    print("[MARKETPLACE] Generating fresh listing...")
    listing = generate_marketplace_listing()
    data = {
        "listing":      listing,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "due_date":     "Post this to Facebook Marketplace now",
    }
    MARKETPLACE_FILE.write_text(json.dumps(data, indent=2))
    print("[MARKETPLACE] ✅ Fresh listing saved — check dashboard to copy & post")


def load_marketplace_listing() -> dict:
    if MARKETPLACE_FILE.exists():
        try:
            return json.loads(MARKETPLACE_FILE.read_text())
        except Exception:
            pass
    return {"listing": None, "generated_at": None}


# Generate one on startup if none exists yet
if not MARKETPLACE_FILE.exists():
    import threading
    threading.Thread(target=run_marketplace_refresh, daemon=True).start()

# Schedule refresh every 14 days at noon UTC
scheduler.add_job(
    run_marketplace_refresh,
    trigger="interval",
    days=14,
    id="marketplace_refresh",
    replace_existing=True,
)
print("[SCHEDULER] Marketplace listing refresh scheduled every 14 days")


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
