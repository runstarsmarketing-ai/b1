import asyncio
import concurrent.futures
import html
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from urllib.parse import urlparse
import urllib.request

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dotenv import load_dotenv
import imageio_ffmpeg
import telegram.error
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
    constants,
)
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import yt_dlp
from instagram_downloader import download_instagram

# Premium Emojis helper import
from premium import get_emoji, get_emoji_id

# Helper to strip <tg-emoji> tags if rejected by Telegram
def strip_custom_emojis(text: str) -> str:
    return re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", text)

# Helper to strip custom emoji IDs and styles from buttons if rejected
def strip_markup(reply_markup):
    if not isinstance(reply_markup, InlineKeyboardMarkup):
        return reply_markup
    clean_keyboard = []
    for row in reply_markup.inline_keyboard:
        clean_row = []
        for btn in row:
            d = btn.to_dict()
            d.pop("style", None)
            d.pop("icon_custom_emoji_id", None)
            clean_row.append(InlineKeyboardButton(**d))
        clean_keyboard.append(clean_row)
    return InlineKeyboardMarkup(clean_keyboard)

async def safe_reply_text(message, text: str, **kwargs):
    try:
        return await message.reply_text(text, **kwargs)
    except telegram.error.BadRequest as e:
        err = str(e).lower()
        if "entity" in err or "parse" in err or "button" in err or "style" in err:
            clean_text = strip_custom_emojis(text)
            if "reply_markup" in kwargs:
                kwargs["reply_markup"] = strip_markup(kwargs["reply_markup"])
            return await message.reply_text(clean_text, **kwargs)
        raise

async def safe_edit_text(message, text: str, **kwargs):
    try:
        return await message.edit_text(text, **kwargs)
    except telegram.error.BadRequest as e:
        err = str(e).lower()
        if "entity" in err or "parse" in err or "button" in err or "style" in err:
            clean_text = strip_custom_emojis(text)
            if "reply_markup" in kwargs:
                kwargs["reply_markup"] = strip_markup(kwargs["reply_markup"])
            return await message.edit_text(clean_text, **kwargs)
        raise

async def safe_reply_video(message, video, caption: str, **kwargs):
    try:
        return await message.reply_video(video=video, caption=caption, **kwargs)
    except telegram.error.BadRequest as e:
        err = str(e).lower()
        if "entity" in err or "parse" in err or "button" in err or "style" in err:
            clean_caption = strip_custom_emojis(caption)
            if "reply_markup" in kwargs:
                kwargs["reply_markup"] = strip_markup(kwargs["reply_markup"])
            return await message.reply_video(video=video, caption=clean_caption, **kwargs)
        raise

async def safe_reply_photo(message, photo, caption: str, **kwargs):
    try:
        return await message.reply_photo(photo=photo, caption=caption, **kwargs)
    except telegram.error.BadRequest as e:
        err = str(e).lower()
        if "entity" in err or "parse" in err or "button" in err or "style" in err:
            clean_caption = strip_custom_emojis(caption)
            if "reply_markup" in kwargs:
                kwargs["reply_markup"] = strip_markup(kwargs["reply_markup"])
            return await message.reply_photo(photo=photo, caption=clean_caption, **kwargs)
        raise

async def safe_reply_audio(message, audio, caption: str, **kwargs):
    try:
        return await message.reply_audio(audio=audio, caption=caption, **kwargs)
    except telegram.error.BadRequest as e:
        err = str(e).lower()
        if "entity" in err or "parse" in err or "button" in err or "style" in err:
            clean_caption = strip_custom_emojis(caption)
            if "reply_markup" in kwargs:
                kwargs["reply_markup"] = strip_markup(kwargs["reply_markup"])
            return await message.reply_audio(audio=audio, caption=clean_caption, **kwargs)
        raise

# FFMPEG binary path resolution (Linux, Windows, macOS, Render, Docker)
FFMPEG_PATH = None
try:
    import shutil
    # 1. First check if ffmpeg is in system PATH (e.g. /usr/bin/ffmpeg on Linux)
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg and os.path.exists(system_ffmpeg):
        FFMPEG_PATH = system_ffmpeg
    else:
        # 2. Bundled imageio_ffmpeg binary
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and os.path.exists(bundled):
            FFMPEG_PATH = bundled
            if hasattr(os, "chmod"):
                try:
                    os.chmod(FFMPEG_PATH, 0o755)
                except Exception:
                    pass
except Exception:
    FFMPEG_PATH = None

# Inject FFmpeg directory into os.environ["PATH"] so yt-dlp finds it automatically
if FFMPEG_PATH and os.path.exists(FFMPEG_PATH):
    try:
        ffmpeg_dir = str(Path(FFMPEG_PATH).parent)
        current_path = os.environ.get("PATH", "")
        if ffmpeg_dir not in current_path:
            os.environ["PATH"] = f"{ffmpeg_dir}{os.pathsep}{current_path}"
    except Exception:
        pass

def extract_audio_mp3_sync(video_path: str, mp3_path: str) -> bool:
    """Extract audio track as MP3 using FFmpeg."""
    if not FFMPEG_PATH or not os.path.exists(FFMPEG_PATH):
        return False
    try:
        cmd = [
            FFMPEG_PATH,
            "-y",
            "-i", video_path,
            "-vn",
            "-acodec", "libmp3lame",
            "-ab", "192k",
            "-ar", "44100",
            mp3_path,
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) == 0:
            fallback_cmd = [
                FFMPEG_PATH,
                "-y",
                "-i", video_path,
                "-vn",
                "-c:a", "mp3",
                mp3_path,
            ]
            subprocess.run(fallback_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0
    except Exception as e:
        logger.warning(f"Audio extraction error: {e}")
        return False

def get_audio_info(filepath: str) -> tuple[bool, str]:
    """Check if a media file contains an audio stream, and return (has_audio, codec_name)."""
    if not FFMPEG_PATH or not os.path.exists(FFMPEG_PATH) or not os.path.exists(filepath):
        # Default to True if probe is unavailable to prevent false-positive silent detection
        return True, "unknown"
    try:
        cmd = [FFMPEG_PATH, "-hide_banner", "-i", filepath]
        res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, errors="ignore", timeout=15)
        for line in res.stderr.splitlines():
            if "Audio:" in line:
                parts = line.split("Audio:")[1].split()
                codec = parts[0].strip(",").lower() if parts else "unknown"
                return True, codec
        return False, ""
    except Exception as e:
        logger.warning(f"Error checking audio stream in {filepath}: {e}")
        return True, "unknown"

def ensure_telegram_compatible_audio(video_path: str) -> tuple[str, bool]:
    """
    Ensure video has AAC audio codec for universal Telegram mobile/desktop playback.
    Returns (updated_video_path, has_audio).
    """
    if not FFMPEG_PATH or not os.path.exists(FFMPEG_PATH) or not os.path.exists(video_path):
        return video_path, True
    
    has_audio, codec = get_audio_info(video_path)
    if not has_audio:
        return video_path, False

    # AAC (mp4a) is natively supported by Telegram ExoPlayer & iOS AVPlayer
    if "aac" in codec or "mp4a" in codec:
        return video_path, True

    logger.info(f"Transcoding video audio from {codec} to standard AAC for Telegram playback in {video_path}...")
    base, ext = os.path.splitext(video_path)
    fixed_path = f"{base}_aac.mp4"
    cmd = [
        FFMPEG_PATH, "-y",
        "-i", video_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        fixed_path
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if os.path.exists(fixed_path) and os.path.getsize(fixed_path) > 0:
            os.replace(fixed_path, video_path)
            logger.info(f"Successfully converted audio to AAC: {video_path}")
            return video_path, True
    except Exception as e:
        logger.warning(f"Audio transcode to AAC failed for {video_path}: {e}")
        if os.path.exists(fixed_path):
            try:
                os.remove(fixed_path)
            except Exception:
                pass
    return video_path, True


# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/telegram").strip()
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "@username").strip()

# Admin & Force-Subscribe settings
raw_admins = os.getenv("ADMIN_ID", "").strip()
ADMIN_IDS = set()
if raw_admins:
    for part in raw_admins.split(","):
        part = part.strip()
        if part.isdigit() or (part.startswith("-") and part[1:].isdigit()):
            ADMIN_IDS.add(int(part))

FSUB_CHANNEL = os.getenv("FSUB_CHANNEL", "").strip()

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("AllInOneBot")

# Working directories
BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)
DB_PATH = BASE_DIR / "users.db"

# Max file size Telegram Bot API allows (50 MB)
MAX_FILESIZE_BYTES = 50 * 1024 * 1024
COOKIES_PATH = BASE_DIR / "cookies.txt"

# High-concurrency thread pool executor for non-blocking downloads
DOWNLOAD_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=32)

# In-memory cache for on-demand audio downloads
AUDIO_CACHE = {}

def cleanup_old_downloads():
    """Purge temporary files older than 10 minutes to save disk space while allowing instant on-demand audio."""
    now = time.time()
    try:
        for p in DOWNLOADS_DIR.iterdir():
            if p.is_file() and (now - p.stat().st_mtime > 600):
                try:
                    p.unlink()
                except Exception:
                    pass
    except Exception:
        pass
    expired = [k for k, v in list(AUDIO_CACHE.items()) if now - v.get("created_at", 0) > 900]
    for k in expired:
        AUDIO_CACHE.pop(k, None)

def download_audio_sync(url: str, output_template: str) -> dict:
    """Download audio track directly as MP3 using yt-dlp."""
    target_url = resolve_redirect_url(url)
    base, _ = os.path.splitext(output_template)
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": f"{base}.%(ext)s",
        "max_filesize": MAX_FILESIZE_BYTES,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "writethumbnail": False,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "remote_components": ["ejs:github"],
        "js_runtimes": {"node": {}, "deno": {}, "quickjs": {}},
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if COOKIES_PATH.exists() and COOKIES_PATH.stat().st_size > 0:
        ydl_opts["cookiefile"] = str(COOKIES_PATH)
    if FFMPEG_PATH and os.path.exists(FFMPEG_PATH):
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(target_url, download=True)
        if "entries" in info and info["entries"]:
            info = info["entries"][0]
        filename = ydl.prepare_filename(info)
        base_f, _ = os.path.splitext(filename)
        mp3_file = base_f + ".mp3"
        if not os.path.exists(mp3_file) and os.path.exists(filename):
            mp3_file = filename
        return {
            "file_path": mp3_file,
            "title": info.get("title", "Audio Track"),
            "uploader": info.get("uploader", info.get("channel", "Creator")),
            "duration": info.get("duration"),
            "filesize": os.path.getsize(mp3_file) if os.path.exists(mp3_file) else 0,
        }

# Optional YouTube cookies from environment variable
raw_cookies = os.getenv("YOUTUBE_COOKIES", "").strip()
if raw_cookies:
    try:
        import base64
        if not raw_cookies.startswith("# Netscape") and "\t" not in raw_cookies:
            decoded = base64.b64decode(raw_cookies).decode("utf-8")
            COOKIES_PATH.write_text(decoded, encoding="utf-8")
        else:
            COOKIES_PATH.write_text(raw_cookies, encoding="utf-8")
        logger.info("Loaded YouTube cookies from YOUTUBE_COOKIES env var.")
    except Exception as e:
        logger.warning(f"Could not load cookies from YOUTUBE_COOKIES: {e}")


# --- Database Helpers (SQLite) ---
def init_db():
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
        logger.info("SQLite database initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize SQLite database: {e}")

def add_user(user_id: int, username: str | None = None, first_name: str | None = None):
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, username, first_name)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = COALESCE(excluded.username, users.username),
                    first_name = COALESCE(excluded.first_name, users.first_name)
                """,
                (user_id, username, first_name),
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Error saving user {user_id}: {e}")

def get_total_users() -> int:
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM users")
            row = cur.fetchone()
            return row[0] if row else 0
    except Exception:
        return 0

def get_all_user_ids() -> list[int]:
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM users")
            return [r[0] for r in cur.fetchall()]
    except Exception:
        return []

def import_user_ids(id_list: list[int]) -> tuple[int, int]:
    new_added = 0
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            cur = conn.cursor()
            for uid in id_list:
                cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
                if cur.rowcount > 0:
                    new_added += 1
            conn.commit()
            cur.execute("SELECT COUNT(*) FROM users")
            total = cur.fetchone()[0]
            return new_added, total
    except Exception as e:
        logger.error(f"Error importing user ids: {e}")
        return 0, get_total_users()

def is_admin(user_id: int) -> bool:
    if not ADMIN_IDS:
        return False
    return user_id in ADMIN_IDS

def normalize_fsub_chat_id(channel_str: str):
    """Normalize any channel link, @username, or numeric ID into a valid Telegram chat_id."""
    if not channel_str:
        return None
    channel_str = channel_str.strip()
    if channel_str.lstrip("-").isdigit():
        return int(channel_str)
    if "t.me/" in channel_str:
        parts = channel_str.rstrip("/").split("t.me/")
        if len(parts) > 1:
            clean = parts[1].strip()
            if not clean.startswith("+") and not clean.startswith("joinchat/"):
                return f"@{clean.lstrip('@')}"
    if not channel_str.startswith("@") and not channel_str.startswith("-"):
        return f"@{channel_str}"
    return channel_str

async def is_subscribed(bot, user_id: int) -> bool:
    if not FSUB_CHANNEL:
        return True
    if is_admin(user_id):
        return True
    
    chat_id = normalize_fsub_chat_id(FSUB_CHANNEL)
    if not chat_id:
        return True

    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        status = member.status
        is_sub = status in [
            constants.ChatMemberStatus.MEMBER,
            constants.ChatMemberStatus.ADMINISTRATOR,
            constants.ChatMemberStatus.OWNER,
        ] or (status == constants.ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False))
        
        if not is_sub:
            status_str = str(status).lower()
            is_sub = any(valid in status_str for valid in ["member", "administrator", "creator", "owner"])
            
        return is_sub
    except telegram.error.BadRequest as e:
        err_msg = str(e).lower()
        logger.warning(f"FSUB check BadRequest for user {user_id} on {chat_id}: {e}")
        if "chat not found" in err_msg or "not enough rights" in err_msg or "bot is not a member" in err_msg:
            logger.error(f"FSUB ERROR: Bot cannot access channel {chat_id}! Make sure channel username/ID is correct and bot is an Administrator: {e}")
        return False
    except Exception as e:
        logger.warning(f"FSUB check error for user {user_id} on {chat_id}: {e}")
        return False

def get_fsub_keyboard() -> InlineKeyboardMarkup:
    channel_link = CHANNEL_URL
    if FSUB_CHANNEL:
        if FSUB_CHANNEL.startswith("http"):
            channel_link = FSUB_CHANNEL
        elif FSUB_CHANNEL.startswith("@"):
            channel_link = f"https://t.me/{FSUB_CHANNEL.lstrip('@')}"
        elif not FSUB_CHANNEL.startswith("-"):
            channel_link = f"https://t.me/{FSUB_CHANNEL}"

    buttons = [
        [
            InlineKeyboardButton(
                "Join Updates Channel",
                url=channel_link,
                style=constants.KeyboardButtonStyle.PRIMARY,
                icon_custom_emoji_id=get_emoji_id("CHANNEL"),
            )
        ],
        [
            InlineKeyboardButton(
                "I Have Joined (Verify)",
                callback_data="verify_fsub",
                style=constants.KeyboardButtonStyle.SUCCESS,
                icon_custom_emoji_id=get_emoji_id("SUCCESS"),
            )
        ],
    ]
    return InlineKeyboardMarkup(buttons)

def get_fsub_text() -> str:
    return f"""
{get_emoji("LOCK")} <b>ACCESS DENIED — JOIN CHANNEL</b> {get_emoji("LOCK")}
━━━━━━━━━━━━━━━━━━━━━
<blockquote>{get_emoji("WARNING")} <i>To use this bot and download high-speed media, you must join our official channel.</i></blockquote>

{get_emoji("BULB")} <b>Steps to Unlock:</b>
1️⃣ Tap the <b>Join Updates Channel</b> button below.
2️⃣ Join our channel.
3️⃣ Return here & tap <b>I Have Joined (Verify)</b> to unlock all features!
"""

# Platform recognizer helper - Premium emojis integrated
def detect_platform(url: str) -> dict:
    url_lower = url.lower()
    if "instagram.com" in url_lower:
        return {
            "name": "Instagram",
            "emoji": get_emoji("INSTAGRAM"),
            "emoji_plain": get_emoji("INSTAGRAM", plain=True),
            "tag": "Reel / Post",
        }
    elif "youtube.com" in url_lower or "youtu.be" in url_lower:
        return {
            "name": "YouTube",
            "emoji": get_emoji("YOUTUBE"),
            "emoji_plain": get_emoji("YOUTUBE", plain=True),
            "tag": "Short / Video",
        }
    elif "tiktok.com" in url_lower:
        return {
            "name": "TikTok",
            "emoji": get_emoji("TIKTOK"),
            "emoji_plain": get_emoji("TIKTOK", plain=True),
            "tag": "HD Video",
        }
    elif "pinterest.com" in url_lower or "pin.it" in url_lower:
        return {
            "name": "Pinterest",
            "emoji": get_emoji("PINTEREST"),
            "emoji_plain": get_emoji("PINTEREST", plain=True),
            "tag": "Pin Media",
        }
    elif "twitter.com" in url_lower or "x.com" in url_lower:
        return {
            "name": "X (Twitter)",
            "emoji": get_emoji("X_TWITTER"),
            "emoji_plain": get_emoji("X_TWITTER", plain=True),
            "tag": "Clip",
        }
    elif "facebook.com" in url_lower or "fb.watch" in url_lower:
        return {
            "name": "Facebook",
            "emoji": get_emoji("FACEBOOK"),
            "emoji_plain": get_emoji("FACEBOOK", plain=True),
            "tag": "Video Post",
        }
    else:
        return {
            "name": "Web Media",
            "emoji": get_emoji("WEB"),
            "emoji_plain": get_emoji("WEB", plain=True),
            "tag": "Universal Stream",
        }

def format_bytes(size: int | float) -> str:
    if not size:
        return "Unknown"
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"

def format_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "N/A"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

# Modern English Welcome Card
def get_welcome_text() -> str:
    return f"""
{get_emoji("SPARKLES")} <b>ALL-IN-ONE MEDIA DOWNLOADER PRO</b> {get_emoji("SPARKLES")}
━━━━━━━━━━━━━━━━━━━━━
<blockquote>{get_emoji("LIGHTNING")} <i>Fast, high-definition video & audio downloader bot.</i></blockquote>

{get_emoji("DIAMOND")} <b>Supported Platforms:</b>
• {get_emoji("INSTAGRAM")} <b>Instagram</b>
• {get_emoji("YOUTUBE")} <b>YouTube</b>
• {get_emoji("TIKTOK")} <b>TikTok</b>
• {get_emoji("PINTEREST")} <b>Pinterest</b>
• {get_emoji("X_TWITTER")} <b>X (Twitter)</b>
• {get_emoji("FACEBOOK")} <b>Facebook</b>

━━━━━━━━━━━━━━━━━━━━━
{get_emoji("BULB")} <b>How to Use:</b>
Just <b>copy & send any media link</b> here directly! The bot will automatically analyze and deliver the media in original quality.
"""

def get_welcome_keyboard(bot_username: str = "") -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                "Updates Channel",
                url=CHANNEL_URL,
                style=constants.KeyboardButtonStyle.PRIMARY,
                icon_custom_emoji_id=get_emoji_id("CHANNEL"),
            ),
            InlineKeyboardButton(
                "Support & Dev",
                url=f"https://t.me/{OWNER_USERNAME.lstrip('@')}",
                style=constants.KeyboardButtonStyle.SUCCESS,
                icon_custom_emoji_id=get_emoji_id("DEVELOPER"),
            ),
        ],
        [
            InlineKeyboardButton(
                "Add to Group",
                url=f"https://t.me/{bot_username}?startgroup=true" if bot_username else "https://t.me",
                style=constants.KeyboardButtonStyle.PRIMARY,
                icon_custom_emoji_id=get_emoji_id("ADD_GROUP") or get_emoji_id("ROCKET"),
            ),
        ],
    ]
    return InlineKeyboardMarkup(buttons)

def resolve_redirect_url(url: str) -> str:
    """Resolve shortened URLs like pin.it, vm.tiktok.com, youtu.be to canonical URL."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            resolved = response.geturl()
            if "pin.it" in url and resolved.rstrip("/") == "https://www.pinterest.com":
                return url
            return resolved
    except Exception:
        return url

# Core Downloader Logic
def download_media_sync(url: str, output_template: str) -> dict:
    """Download video or photo synchronously with yt-dlp or dedicated platform engines."""
    target_url = resolve_redirect_url(url)

    # 1. Delegate Instagram downloads to dedicated Instagram engine
    if "instagram.com" in target_url.lower():
        return download_instagram(target_url, output_template)

    # 2. Universal media downloader (YouTube, TikTok, Pinterest, X, Facebook, etc.)
    ydl_opts = {
        "format": "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "outtmpl": output_template,
        "max_filesize": MAX_FILESIZE_BYTES,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "writethumbnail": False,
        "merge_output_format": "mp4",
        "postprocessor_args": {
            "merger": ["-c:v", "copy", "-c:a", "aac"],
            "Merger": ["-c:v", "copy", "-c:a", "aac"],
            "VideoConvertor": ["-c:v", "copy", "-c:a", "aac"],
        },
        "remote_components": ["ejs:github"],
        "js_runtimes": {"node": {}, "deno": {}, "quickjs": {}},
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    if COOKIES_PATH.exists() and COOKIES_PATH.stat().st_size > 0:
        ydl_opts["cookiefile"] = str(COOKIES_PATH)

    if FFMPEG_PATH and os.path.exists(FFMPEG_PATH):
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(target_url, download=True)
        except Exception as e:
            err_str = str(e).lower()
            is_yt = any(x in target_url.lower() for x in ["youtube.com", "youtu.be"])
            if is_yt and any(term in err_str for term in ["player", "format", "sabr", "extract", "bot"]):
                logger.info(f"Retrying YouTube with fallback player clients: {e}")
                fallback_clients = [
                    ["mweb", "android_creator", "ios"],
                    ["tv", "android"],
                    ["web_safari", "mweb"],
                ]
                last_yt_err = e
                info = None
                for client_set in fallback_clients:
                    try:
                        fb_opts = dict(ydl_opts)
                        fb_opts["extractor_args"] = {"youtube": {"player_client": client_set}}
                        with yt_dlp.YoutubeDL(fb_opts) as ydl_fb:
                            info = ydl_fb.extract_info(target_url, download=True)
                            if info:
                                break
                    except Exception as fb_e:
                        last_yt_err = fb_e
                if not info:
                    if target_url != url:
                        info = ydl.extract_info(url, download=True)
                    else:
                        raise last_yt_err
            elif target_url != url:
                info = ydl.extract_info(url, download=True)
            else:
                raise

        if "entries" in info and info["entries"]:
            info = info["entries"][0]

        filename = ydl.prepare_filename(info)
        if not os.path.exists(filename):
            base, _ = os.path.splitext(filename)
            for ext in [".mp4", ".mkv", ".webm", ".jpg", ".png"]:
                if os.path.exists(base + ext):
                    filename = base + ext
                    break

        # Verify audio stream & ensure Telegram AAC compatibility
        has_audio, audio_codec = get_audio_info(filename)
        if has_audio:
            filename, has_audio = ensure_telegram_compatible_audio(filename)

        return {
            "file_path": filename,
            "title": info.get("title", "Media Video"),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", info.get("channel", "Creator")),
            "filesize": info.get("filesize") or info.get("filesize_approx") or (os.path.getsize(filename) if os.path.exists(filename) else 0),
            "width": info.get("width"),
            "height": info.get("height"),
            "is_photo": False,
            "has_audio": has_audio,
            "extra_photos": [],
        }

# Command: /start
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        add_user(
            user_id=update.effective_user.id,
            username=update.effective_user.username,
            first_name=update.effective_user.first_name,
        )

    if not await is_subscribed(context.bot, update.effective_user.id):
        await safe_reply_text(
            update.message,
            text=get_fsub_text(),
            parse_mode=constants.ParseMode.HTML,
            reply_markup=get_fsub_keyboard(),
            disable_web_page_preview=True,
        )
        return

    bot_info = await context.bot.get_me()
    await safe_reply_text(
        update.message,
        text=get_welcome_text(),
        parse_mode=constants.ParseMode.HTML,
        reply_markup=get_welcome_keyboard(bot_info.username),
        disable_web_page_preview=True,
    )

# Command: /help
async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        add_user(
            user_id=update.effective_user.id,
            username=update.effective_user.username,
            first_name=update.effective_user.first_name,
        )

    help_text = f"""
{get_emoji("BOOK")} <b>Help & Instructions</b>
━━━━━━━━━━━━━━━━━━━━━
1️⃣ Copy any media share link (Instagram, YouTube, TikTok, Pinterest, etc.).
2️⃣ Paste and send the link here in this chat.
3️⃣ The bot will instantly fetch and deliver the media stream!

{get_emoji("WARNING")} <b>Important Notes:</b>
• Private accounts & protected posts cannot be accessed.
• Telegram Bot API upload limit is <b>50 MB</b> per file.
"""
    await safe_reply_text(
        update.message,
        text=help_text,
        parse_mode=constants.ParseMode.HTML,
        disable_web_page_preview=True,
    )

# Command: /ping
async def ping_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        add_user(
            user_id=update.effective_user.id,
            username=update.effective_user.username,
            first_name=update.effective_user.first_name,
        )

    start_time = time.time()
    msg = await safe_reply_text(
        update.message,
        f"{get_emoji('LIGHTNING')} <i>Checking connection speed...</i>",
        parse_mode=constants.ParseMode.HTML,
    )
    end_time = time.time()
    latency_ms = round((end_time - start_time) * 1000)
    await safe_edit_text(
        msg,
        f"{get_emoji('ROCKET')} <b>Pong!</b> <code>{latency_ms}ms</code>\n{get_emoji('DIAMOND')} <b>Server Status:</b> <i>Optimal & Operational</i>",
        parse_mode=constants.ParseMode.HTML,
    )

# Main Message Handler for URLs
async def link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        add_user(
            user_id=update.effective_user.id,
            username=update.effective_user.username,
            first_name=update.effective_user.first_name,
        )

    if not await is_subscribed(context.bot, update.effective_user.id):
        await safe_reply_text(
            update.message,
            text=get_fsub_text(),
            parse_mode=constants.ParseMode.HTML,
            reply_markup=get_fsub_keyboard(),
            disable_web_page_preview=True,
        )
        return

    text = update.message.text or ""
    url_match = re.search(r"https?://[^\s]+", text)
    if not url_match:
        return

    url = url_match.group(0)
    platform = detect_platform(url)

    status_card = (
        f"{platform['emoji']} <b>{platform['name']} Link Detected!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"<blockquote>{get_emoji('LIGHTNING')} <i>Analyzing media stream & resolving headers...</i></blockquote>\n"
        f"{get_emoji('HOURGLASS')} <b>Status:</b> <code>[ 1/3 ] Fetching Info...</code>"
    )

    status_msg = await safe_reply_text(
        update.message,
        text=status_card,
        parse_mode=constants.ParseMode.HTML,
        disable_web_page_preview=True,
    )

    task_id = f"{update.effective_user.id}_{int(time.time())}"
    output_template = str(DOWNLOADS_DIR / f"{task_id}.%(ext)s")
    file_path = None
    audio_path = None
    is_photo = False
    extra_photos = []

    try:
        await asyncio.sleep(0.4)
        await safe_edit_text(
            status_msg,
            f"{platform['emoji']} <b>{platform['name']} Processing</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"<blockquote>{get_emoji('DOWNLOAD')} <i>Downloading best quality stream...</i></blockquote>\n"
            f"{get_emoji('HOURGLASS')} <b>Status:</b> <code>[ 2/3 ] Downloading Media...</code>",
            parse_mode=constants.ParseMode.HTML,
        )

        # Concurrently execute media download without blocking event loop or other users
        loop = asyncio.get_running_loop()
        media_info = await loop.run_in_executor(
            DOWNLOAD_EXECUTOR, download_media_sync, url, output_template
        )
        file_path = media_info["file_path"]

        if not os.path.exists(file_path):
            raise FileNotFoundError("Downloaded file could not be found.")

        file_size = os.path.getsize(file_path)
        if file_size > MAX_FILESIZE_BYTES:
            await safe_edit_text(
                status_msg,
                f"{get_emoji('WARNING')} <b>File Size Limit Exceeded</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"The requested media (<b>{format_bytes(file_size)}</b>) exceeds Telegram's <b>50 MB</b> bot upload limit.\n"
                f"{get_emoji('BULB')} <i>Tip: Please choose a shorter clip or lower resolution.</i>",
                parse_mode=constants.ParseMode.HTML,
            )
            return

        await safe_edit_text(
            status_msg,
            f"{platform['emoji']} <b>{platform['name']} Ready!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"<blockquote>{get_emoji('UPLOAD')} <i>Uploading high-speed to Telegram...</i></blockquote>\n"
            f"{get_emoji('HOURGLASS')} <b>Status:</b> <code>[ 3/3 ] Uploading...</code>",
            parse_mode=constants.ParseMode.HTML,
        )

        clean_title = html.escape(media_info["title"][:70] + ("..." if len(media_info["title"]) > 70 else ""))
        clean_uploader = html.escape(str(media_info["uploader"])[:40])
        bot_info = await context.bot.get_me()
        bot_user = bot_info.username if bot_info.username else "FastVidsSaverBot"
        bot_mention = f"@{bot_user}"

        has_audio = media_info.get("has_audio", True)
        extra_photos = media_info.get("extra_photos") or []
        is_photo = media_info.get("is_photo") or (Path(file_path).suffix.lower() in [".jpg", ".jpeg", ".png", ".webp"])

        caption = (
            f"{get_emoji('MOVIE')} <b>{clean_title}</b>\n\n"
            "<blockquote>"
            f"{get_emoji('TAG')} <b>Platform:</b> {platform['emoji']} {platform['tag']}\n"
            f"{get_emoji('DURATION')} <b>Duration:</b> {format_duration(media_info['duration'])}\n"
            f"{get_emoji('SIZE')} <b>Size:</b> {format_bytes(file_size)}\n"
            f"{get_emoji('AUTHOR')} <b>Author:</b> {clean_uploader}\n"
            "</blockquote>\n"
            f"{get_emoji('SPARKLES')} <i>Downloaded by</i> <b>{bot_mention}</b>"
        )
        if not is_photo and not has_audio:
            caption += f"\n\n<blockquote>🔇 <i>Note: This video/Reel contains no audio track (original post was silent or muted on Instagram).</i></blockquote>"

        # Cache video info for fast on-demand audio extraction
        audio_token = f"a_{int(time.time())}_{update.effective_user.id % 10000}"
        if not is_photo and has_audio:
            AUDIO_CACHE[audio_token] = {
                "url": url,
                "file_path": file_path,
                "title": clean_title,
                "uploader": clean_uploader,
                "duration": media_info.get("duration"),
                "created_at": time.time(),
            }

        buttons = []
        if not is_photo:
            first_row = []
            if has_audio:
                first_row.append(
                    InlineKeyboardButton(
                        "Audio (MP3)",
                        callback_data=f"aud:{audio_token}",
                        style=constants.KeyboardButtonStyle.PRIMARY,
                        icon_custom_emoji_id=get_emoji_id("AUDIO"),
                    )
                )
            first_row.append(
                InlineKeyboardButton(
                    "Original Source",
                    url=url,
                    style=constants.KeyboardButtonStyle.PRIMARY,
                    icon_custom_emoji_id=get_emoji_id(platform["name"]) or get_emoji_id("LINK"),
                )
            )
            buttons.append(first_row)
        else:
            buttons.append([
                InlineKeyboardButton(
                    "Original Source",
                    url=url,
                    style=constants.KeyboardButtonStyle.PRIMARY,
                    icon_custom_emoji_id=get_emoji_id(platform["name"]) or get_emoji_id("LINK"),
                ),
            ])

        buttons.append([
            InlineKeyboardButton(
                "Channel",
                url=CHANNEL_URL,
                style=constants.KeyboardButtonStyle.SUCCESS,
                icon_custom_emoji_id=get_emoji_id("CHANNEL"),
            ),
            InlineKeyboardButton(
                "Share Bot",
                url=f"https://t.me/share/url?url=https://t.me/{bot_user}&text=Check%20out%20this%20awesome%20All-in-One%20Video%20Downloader%20Bot!",
                style=constants.KeyboardButtonStyle.PRIMARY,
                icon_custom_emoji_id=get_emoji_id("ROCKET"),
            ),
        ])
        reply_markup = InlineKeyboardMarkup(buttons)
        
        # 1. Send Video, Photo, or Multi-Photo Carousel Album
        try:
            if is_photo:
                if extra_photos:
                    # Multi-photo Carousel Album
                    all_paths = [file_path] + extra_photos
                    files_to_close = []
                    media_group = []
                    try:
                        for i, p in enumerate(all_paths):
                            f = open(p, "rb")
                            files_to_close.append(f)
                            media_group.append(
                                InputMediaPhoto(
                                    media=f,
                                    caption=caption if i == 0 else "",
                                    parse_mode=constants.ParseMode.HTML,
                                )
                            )
                        await update.message.reply_media_group(
                            media=media_group,
                            read_timeout=180,
                            write_timeout=180,
                        )
                        # Send button card below the album
                        await safe_reply_text(
                            update.message,
                            text=f"{get_emoji('SUCCESS')} <b>Instagram Carousel Album Delivered!</b> ({len(all_paths)} Photos)",
                            reply_markup=reply_markup,
                            parse_mode=constants.ParseMode.HTML,
                        )
                    finally:
                        for f in files_to_close:
                            try:
                                f.close()
                            except Exception:
                                pass
                else:
                    # Single Photo
                    with open(file_path, "rb") as media_file:
                        await safe_reply_photo(
                            update.message,
                            photo=media_file,
                            caption=caption,
                            parse_mode=constants.ParseMode.HTML,
                            reply_markup=reply_markup,
                            read_timeout=180,
                            write_timeout=180,
                        )
            else:
                # Video Post / Reel
                with open(file_path, "rb") as media_file:
                    await safe_reply_video(
                        update.message,
                        video=media_file,
                        caption=caption,
                        parse_mode=constants.ParseMode.HTML,
                        supports_streaming=True,
                        duration=int(media_info["duration"]) if media_info["duration"] else None,
                        width=media_info.get("width"),
                        height=media_info.get("height"),
                        reply_markup=reply_markup,
                        read_timeout=360,
                        write_timeout=360,
                    )
        except Exception as e:
            logger.error(f"Error sending media: {e}")

        # Delete processing status message
        try:
            await status_msg.delete()
        except Exception:
            pass

        # Clean up files older than 10 mins
        cleanup_old_downloads()

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Download error: {e}")
        err_msg = str(e)
        if "Private video" in err_msg or "login" in err_msg.lower():
            reason = "This content is private or requires login authentication."
        elif "Unsupported URL" in err_msg:
            reason = "This platform or URL format is currently not supported."
        else:
            reason = "Unable to download media stream. Please verify the URL."

        await safe_edit_text(
            status_msg,
            f"{get_emoji('FAILED')} <b>Download Failed</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"<blockquote>{get_emoji('WARNING')} {html.escape(reason)}</blockquote>\n\n"
            f"{get_emoji('BULB')} <i>Tip: Ensure the post is publicly accessible without login restrictions.</i>",
            parse_mode=constants.ParseMode.HTML,
        )

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        await safe_edit_text(
            status_msg,
            f"{get_emoji('FAILED')} <b>Processing Error</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "<blockquote>An unexpected error occurred while processing your request.</blockquote>\n"
            f"{get_emoji('BULB')} <i>Please try again in a few moments.</i>",
            parse_mode=constants.ParseMode.HTML,
        )

    finally:
        # For photos, clean up immediately
        if locals().get("is_photo", False):
            clean_list = [file_path] if file_path else []
            if locals().get("extra_photos"):
                clean_list.extend(extra_photos)
            for p in clean_list:
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass

# Command: /admin or /stats
async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    start_time = time.time()
    total_users = get_total_users()
    latency_ms = round((time.time() - start_time) * 1000)

    fsub_status = html.escape(FSUB_CHANNEL) if FSUB_CHANNEL else "<i>Disabled / Not Configured</i>"

    admin_text = f"""
{get_emoji("SHIELD")} <b>ADMIN CONTROL PANEL</b> {get_emoji("SHIELD")}
━━━━━━━━━━━━━━━━━━━━━
{get_emoji("USERS")} <b>Total Registered Users:</b> <code>{total_users}</code>
{get_emoji("CHANNEL")} <b>Force-Sub Channel:</b> {fsub_status}
{get_emoji("ROCKET")} <b>Bot Latency:</b> <code>{latency_ms}ms</code>

━━━━━━━━━━━━━━━━━━━━━
{get_emoji("BOOK")} <b>Admin Commands Quick Guide:</b>
• <code>/stats</code> — View bot user statistics & status
• <code>/broadcast</code> — Reply to any message with /broadcast to send to all users
• <code>/export_users</code> — Download all user IDs as a <code>.txt</code> file
• <code>/import_users</code> — Send/reply to a <code>.txt</code> file to restore user IDs
"""

    buttons = [
        [
            InlineKeyboardButton(
                "Export Users (.txt)",
                callback_data="admin_export",
                style=constants.KeyboardButtonStyle.PRIMARY,
                icon_custom_emoji_id=get_emoji_id("DOWNLOAD"),
            ),
            InlineKeyboardButton(
                "Refresh Stats",
                callback_data="admin_refresh",
                style=constants.KeyboardButtonStyle.SUCCESS,
                icon_custom_emoji_id=get_emoji_id("LIGHTNING"),
            ),
        ]
    ]

    await safe_reply_text(
        update.message,
        text=admin_text,
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )

# Command: /export_users
async def export_users_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    status_msg = await safe_reply_text(
        update.message,
        f"{get_emoji('HOURGLASS')} <i>Fetching user list and generating backup file...</i>",
        parse_mode=constants.ParseMode.HTML,
    )

    user_ids = get_all_user_ids()
    export_filename = f"users_export_{int(time.time())}.txt"
    export_path = DOWNLOADS_DIR / export_filename

    try:
        with open(export_path, "w", encoding="utf-8") as f:
            f.write(f"# FastVidsSaverBot Users Backup\n")
            f.write(f"# Export Date: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"# Total Users: {len(user_ids)}\n\n")
            for uid in user_ids:
                f.write(f"{uid}\n")

        with open(export_path, "rb") as doc_file:
            await update.message.reply_document(
                document=doc_file,
                filename=export_filename,
                caption=(
                    f"{get_emoji('SUCCESS')} <b>Users Backup Exported!</b>\n\n"
                    f"{get_emoji('USERS')} <b>Total IDs:</b> <code>{len(user_ids)}</code>\n"
                    f"{get_emoji('BULB')} <i>You can use this file anytime with <code>/import_users</code> to restore your user database.</i>"
                ),
                parse_mode=constants.ParseMode.HTML,
            )
        try:
            await status_msg.delete()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Error exporting users: {e}")
        await safe_edit_text(
            status_msg,
            f"{get_emoji('FAILED')} <b>Export Failed:</b> <code>{html.escape(str(e))}</code>",
            parse_mode=constants.ParseMode.HTML,
        )
    finally:
        if os.path.exists(export_path):
            try:
                os.remove(export_path)
            except Exception:
                pass

# Command: /import_users
async def import_users_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    doc = None
    if update.message.document:
        doc = update.message.document
    elif update.message.reply_to_message and update.message.reply_to_message.document:
        doc = update.message.reply_to_message.document

    if not doc:
        help_text = f"""
{get_emoji("UPLOAD")} <b>How to Import / Restore Users:</b>
━━━━━━━━━━━━━━━━━━━━━
1️⃣ Send a <code>.txt</code> file containing user IDs (one per line) with the caption <code>/import_users</code>.
<b>OR</b>
2️⃣ Reply to any previously sent <code>.txt</code> file with <code>/import_users</code>.

{get_emoji("DIAMOND")} <i>The bot will extract all numeric IDs and merge them into the database without losing any existing users!</i>
"""
        await safe_reply_text(update.message, help_text, parse_mode=constants.ParseMode.HTML)
        return

    status_msg = await safe_reply_text(
        update.message,
        f"{get_emoji('HOURGLASS')} <i>Downloading and parsing user backup file...</i>",
        parse_mode=constants.ParseMode.HTML,
    )

    temp_path = DOWNLOADS_DIR / f"import_{int(time.time())}.txt"
    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(custom_path=temp_path)

        with open(temp_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        found_ids = [int(x) for x in re.findall(r"\b\d{5,15}\b", content)]
        unique_ids = list(dict.fromkeys(found_ids))

        if not unique_ids:
            await safe_edit_text(
                status_msg,
                f"{get_emoji('WARNING')} <b>No Valid User IDs Found!</b>\n"
                "Please make sure the text file contains numeric Telegram user IDs.",
                parse_mode=constants.ParseMode.HTML,
            )
            return

        new_added, total_in_db = import_user_ids(unique_ids)

        result_text = f"""
{get_emoji("SUCCESS")} <b>USER IMPORT COMPLETED!</b> {get_emoji("SUCCESS")}
━━━━━━━━━━━━━━━━━━━━━
{get_emoji("BULLET")} <b>IDs Scanned in File:</b> <code>{len(unique_ids)}</code>
{get_emoji("BULLET")} <b>New Users Added:</b> <code>+{new_added}</code>
{get_emoji("USERS")} <b>Total Users in Database:</b> <code>{total_in_db}</code>

{get_emoji("SPARKLES")} <i>Database is fully updated and active!</i>
"""
        await safe_edit_text(status_msg, result_text, parse_mode=constants.ParseMode.HTML)
    except Exception as e:
        logger.error(f"Import users error: {e}")
        await safe_edit_text(
            status_msg,
            f"{get_emoji('FAILED')} <b>Import Failed:</b> <code>{html.escape(str(e))}</code>",
            parse_mode=constants.ParseMode.HTML,
        )
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

# Document upload handler (triggers import if caption starts with /import)
async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = (update.message.caption or "").strip()
    if caption.startswith("/import"):
        await import_users_handler(update, context)

# Command: /broadcast
async def broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    if not update.message.reply_to_message:
        help_text = f"""
{get_emoji("BROADCAST")} <b>How to Broadcast a Message:</b>
━━━━━━━━━━━━━━━━━━━━━
1️⃣ Send or forward any message here (Text, Photo, Video, Audio, Sticker, etc.).
2️⃣ Reply to that message with <code>/broadcast</code>.

{get_emoji("BULB")} <i>The bot will copy and send the exact message to every registered user in your database.</i>
"""
        await safe_reply_text(update.message, help_text, parse_mode=constants.ParseMode.HTML)
        return

    target_msg = update.message.reply_to_message
    user_ids = get_all_user_ids()

    if not user_ids:
        await safe_reply_text(
            update.message,
            f"{get_emoji('WARNING')} <b>No Users Found:</b> The user database is currently empty.",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    status_msg = await safe_reply_text(
        update.message,
        f"{get_emoji('ROCKET')} <b>Broadcasting Started...</b>\n"
        f"Target Recipients: <code>{len(user_ids)}</code> users\n"
        f"{get_emoji('HOURGLASS')} <i>Please wait...</i>",
        parse_mode=constants.ParseMode.HTML,
    )

    sent = 0
    blocked = 0
    failed = 0
    last_update = time.time()

    for idx, uid in enumerate(user_ids, start=1):
        try:
            await context.bot.copy_message(
                chat_id=uid,
                from_chat_id=target_msg.chat_id,
                message_id=target_msg.message_id,
            )
            sent += 1
        except telegram.error.Forbidden:
            blocked += 1
        except telegram.error.RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await context.bot.copy_message(
                    chat_id=uid,
                    from_chat_id=target_msg.chat_id,
                    message_id=target_msg.message_id,
                )
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1

        if (time.time() - last_update > 3.0) or (idx == len(user_ids)):
            last_update = time.time()
            try:
                await safe_edit_text(
                    status_msg,
                    f"{get_emoji('BROADCAST')} <b>Broadcasting in Progress...</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{get_emoji('HOURGLASS')} <b>Progress:</b> <code>{idx}/{len(user_ids)}</code>\n"
                    f"{get_emoji('SUCCESS')} <b>Sent:</b> <code>{sent}</code>\n"
                    f"{get_emoji('WARNING')} <b>Blocked:</b> <code>{blocked}</code>\n"
                    f"{get_emoji('FAILED')} <b>Failed:</b> <code>{failed}</code>",
                    parse_mode=constants.ParseMode.HTML,
                )
            except Exception:
                pass

        await asyncio.sleep(0.04)

    summary_text = f"""
{get_emoji("SUCCESS")} <b>BROADCAST COMPLETED!</b> {get_emoji("SUCCESS")}
━━━━━━━━━━━━━━━━━━━━━
{get_emoji("USERS")} <b>Total Targeted:</b> <code>{len(user_ids)}</code>
{get_emoji("SUCCESS")} <b>Successfully Delivered:</b> <code>{sent}</code>
{get_emoji("WARNING")} <b>Blocked / Deactivated:</b> <code>{blocked}</code>
{get_emoji("FAILED")} <b>Failed:</b> <code>{failed}</code>
"""
    await safe_edit_text(status_msg, summary_text, parse_mode=constants.ParseMode.HTML)

# Callback Query Router
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    user_id = query.from_user.id

    if data == "verify_fsub":
        subscribed = await is_subscribed(context.bot, user_id)
        if not subscribed:
            await asyncio.sleep(0.5)
            subscribed = await is_subscribed(context.bot, user_id)

        if subscribed:
            await query.answer("✅ Verification successful! Bot unlocked.", show_alert=True)
            bot_info = await context.bot.get_me()
            try:
                await query.edit_message_text(
                    text=get_welcome_text(),
                    parse_mode=constants.ParseMode.HTML,
                    reply_markup=get_welcome_keyboard(bot_info.username),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
        else:
            await query.answer("❌ You haven't joined yet! Please join the channel first.", show_alert=True)

    elif data == "admin_export":
        if not is_admin(user_id):
            await query.answer("⛔ Access Denied!", show_alert=True)
            return
        await query.answer("Exporting users...")
        user_ids = get_all_user_ids()
        export_filename = f"users_export_{int(time.time())}.txt"
        export_path = DOWNLOADS_DIR / export_filename
        try:
            with open(export_path, "w", encoding="utf-8") as f:
                f.write(f"# FastVidsSaverBot Users Backup\n")
                f.write(f"# Export Date: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"# Total Users: {len(user_ids)}\n\n")
                for uid in user_ids:
                    f.write(f"{uid}\n")

            with open(export_path, "rb") as doc_file:
                await context.bot.send_document(
                    chat_id=query.message.chat_id,
                    document=doc_file,
                    filename=export_filename,
                    caption=(
                        f"{get_emoji('SUCCESS')} <b>Users Backup Exported!</b>\n\n"
                        f"{get_emoji('USERS')} <b>Total IDs:</b> <code>{len(user_ids)}</code>\n"
                        f"{get_emoji('BULB')} <i>You can use this file with <code>/import_users</code> to restore.</i>"
                    ),
                    parse_mode=constants.ParseMode.HTML,
                )
        finally:
            if os.path.exists(export_path):
                try:
                    os.remove(export_path)
                except Exception:
                    pass

    elif data == "admin_refresh":
        if not is_admin(user_id):
            await query.answer("⛔ Access Denied!", show_alert=True)
            return
        await query.answer("Refreshing stats...")
        total_users = get_total_users()
        fsub_status = html.escape(FSUB_CHANNEL) if FSUB_CHANNEL else "<i>Disabled / Not Configured</i>"
        admin_text = f"""
{get_emoji("SHIELD")} <b>ADMIN CONTROL PANEL</b> {get_emoji("SHIELD")}
━━━━━━━━━━━━━━━━━━━━━
{get_emoji("USERS")} <b>Total Registered Users:</b> <code>{total_users}</code>
{get_emoji("CHANNEL")} <b>Force-Sub Channel:</b> {fsub_status}
{get_emoji("ROCKET")} <b>Bot Latency:</b> <code>OK</code>

━━━━━━━━━━━━━━━━━━━━━
{get_emoji("BOOK")} <b>Admin Commands Quick Guide:</b>
• <code>/stats</code> — Refresh this dashboard
• <code>/broadcast</code> — Reply to any message with /broadcast to send to all users
• <code>/export_users</code> — Download all user IDs as a <code>.txt</code> file
• <code>/import_users</code> — Send/reply to a <code>.txt</code> file to restore user IDs
"""
        buttons = [
            [
                InlineKeyboardButton(
                    "Export Users (.txt)",
                    callback_data="admin_export",
                    style=constants.KeyboardButtonStyle.PRIMARY,
                    icon_custom_emoji_id=get_emoji_id("DOWNLOAD"),
                ),
                InlineKeyboardButton(
                    "Refresh Stats",
                    callback_data="admin_refresh",
                    style=constants.KeyboardButtonStyle.SUCCESS,
                    icon_custom_emoji_id=get_emoji_id("LIGHTNING"),
                ),
            ]
        ]
        try:
            await query.edit_message_text(
                text=admin_text,
                parse_mode=constants.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        except Exception:
            pass

    elif data.startswith("aud:"):
        token = data.split(":", 1)[1]
        item = AUDIO_CACHE.get(token)
        if not item:
            await query.answer("⚠️ Audio request expired. Please resend the media link.", show_alert=True)
            return

        await query.answer("🎵 Extracting MP3 audio...")

        status_msg = await query.message.reply_text(
            f"{get_emoji('AUDIO')} <b>Audio Track Processing</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"<blockquote>{get_emoji('HOURGLASS')} <i>Extracting high-quality MP3 stream...</i></blockquote>",
            parse_mode=constants.ParseMode.HTML,
        )

        audio_task_id = f"aud_{query.from_user.id}_{int(time.time())}"
        audio_path = str(DOWNLOADS_DIR / f"{audio_task_id}.mp3")
        file_path = item.get("file_path")
        media_url = item.get("url")
        success = False

        try:
            # 1. Try local extraction first if video file is still present
            if file_path and os.path.exists(file_path):
                loop = asyncio.get_running_loop()
                success = await loop.run_in_executor(
                    DOWNLOAD_EXECUTOR, extract_audio_mp3_sync, file_path, audio_path
                )

            # 2. If video was purged, download audio track directly with yt-dlp
            if not success or not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
                audio_template = str(DOWNLOADS_DIR / f"{audio_task_id}.%(ext)s")
                loop = asyncio.get_running_loop()
                dl_info = await loop.run_in_executor(
                    DOWNLOAD_EXECUTOR, download_audio_sync, media_url, audio_template
                )
                if dl_info and os.path.exists(dl_info.get("file_path", "")):
                    audio_path = dl_info["file_path"]
                    success = True

            if success and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
                audio_size = os.path.getsize(audio_path)
                bot_info = await context.bot.get_me()
                bot_user = bot_info.username if bot_info.username else "FastVidsSaverBot"
                clean_title = item.get("title", "Audio Track")
                clean_uploader = item.get("uploader", "Creator")
                duration = item.get("duration")

                audio_caption = (
                    f"{get_emoji('AUDIO')} <b>Audio Track (MP3)</b>\n\n"
                    "<blockquote>"
                    f"{get_emoji('MOVIE')} <b>Title:</b> {clean_title}\n"
                    f"{get_emoji('AUTHOR')} <b>Artist:</b> {clean_uploader}\n"
                    f"{get_emoji('DURATION')} <b>Duration:</b> {format_duration(duration)}\n"
                    f"{get_emoji('SIZE')} <b>Size:</b> {format_bytes(audio_size)}\n"
                    "</blockquote>\n"
                    f"{get_emoji('SPARKLES')} <i>Downloaded by</i> <b>@{bot_user}</b>"
                )

                with open(audio_path, "rb") as af:
                    await safe_reply_audio(
                        query.message,
                        audio=af,
                        caption=audio_caption,
                        title=str(clean_title)[:60],
                        performer=str(clean_uploader)[:40],
                        duration=int(duration) if duration else None,
                        parse_mode=constants.ParseMode.HTML,
                        read_timeout=240,
                        write_timeout=240,
                    )
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            else:
                await safe_edit_text(
                    status_msg,
                    f"{get_emoji('FAILED')} <b>Audio Extraction Failed</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━\n"
                    "<blockquote>Unable to extract audio from this media.</blockquote>",
                    parse_mode=constants.ParseMode.HTML,
                )
        except Exception as e:
            logger.error(f"Error handling on-demand audio: {e}", exc_info=True)
            await safe_edit_text(
                status_msg,
                f"{get_emoji('FAILED')} <b>Audio Extraction Error:</b> <code>{html.escape(str(e)[:100])}</code>",
                parse_mode=constants.ParseMode.HTML,
            )
        finally:
            if audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except Exception:
                    pass

# --- Embedded Web Server for Render / Cloud Hosting ---
class RenderHealthCheckServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        html_page = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FastVidsSaverBot - Online</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            background: #0f172a;
            color: #f8fafc;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
            padding: 1rem;
        }
        .card {
            background: #1e293b;
            padding: 2.5rem;
            border-radius: 1.25rem;
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5);
            text-align: center;
            border: 1px solid #334155;
            max-width: 450px;
            width: 100%;
        }
        h1 {
            color: #38bdf8;
            font-size: 1.75rem;
            margin: 0 0 0.5rem 0;
        }
        p {
            color: #94a3b8;
            margin: 0.5rem 0 1.5rem 0;
            font-size: 1rem;
            line-height: 1.5;
        }
        .badge {
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.4rem 1rem;
            background: rgba(16, 185, 129, 0.15);
            border: 1px solid #10b981;
            color: #34d399;
            border-radius: 9999px;
            font-size: 0.875rem;
            font-weight: 600;
        }
        .dot {
            width: 8px;
            height: 8px;
            background: #10b981;
            border-radius: 50%;
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.5; transform: scale(0.85); }
        }
    </style>
</head>
<body>
    <div class="card">
        <h1>✨ FastVidsSaverBot</h1>
        <p>All-in-One Telegram Video & Audio Downloader Service is operational and listening.</p>
        <div class="badge">
            <span class="dot"></span> 200 OK — Render Web Service Active
        </div>
    </div>
</body>
</html>"""
        self.wfile.write(html_page.encode("utf-8"))

    def log_message(self, format, *args):
        # Silence access logs to keep bot output clean
        pass

def run_web_server():
    port = int(os.getenv("PORT", "8080"))
    try:
        server = HTTPServer(("0.0.0.0", port), RenderHealthCheckServer)
        logger.info(f"Render web service listening on port {port}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Render web server error: {e}")

def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("\n" + "=" * 60)
        print(" [!] ERROR: Telegram BOT_TOKEN not found!")
        print(" [>] Please enter your BOT_TOKEN in '.env' file.")
        print(" [>] Obtain a token from @BotFather on Telegram.")
        print("=" * 60 + "\n")
        return

    # Initialize SQLite database
    init_db()

    # Start background web server for Render / Koyeb / Heroku / Cloud health checks
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    print("🚀 Starting All-in-One Downloader Bot...")
    t_request = HTTPXRequest(
        connection_pool_size=64,
        connect_timeout=60.0,
        read_timeout=180.0,
        write_timeout=180.0,
        media_write_timeout=360.0,
    )
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(t_request)
        .concurrent_updates(True)
        .build()
    )

    # Handlers (block=False enables instant parallel execution for all users)
    app.add_handler(CommandHandler("start", start_handler, block=False))
    app.add_handler(CommandHandler("help", help_handler, block=False))
    app.add_handler(CommandHandler("ping", ping_handler, block=False))
    app.add_handler(CommandHandler("admin", admin_handler, block=False))
    app.add_handler(CommandHandler("stats", admin_handler, block=False))
    app.add_handler(CommandHandler("broadcast", broadcast_handler, block=False))
    app.add_handler(CommandHandler("export_users", export_users_handler, block=False))
    app.add_handler(CommandHandler("import_users", import_users_handler, block=False))
    app.add_handler(CallbackQueryHandler(callback_router, block=False))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler, block=False))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, link_handler, block=False))

    print("✨ Bot is active and listening for messages! Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
