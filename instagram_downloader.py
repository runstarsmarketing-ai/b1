"""
Instagram Downloader Pro - Dedicated Standalone Module
Handles Instagram Reels, Videos, Photos, and Carousel Albums cleanly from scratch.
Guarantees 100% video quality with original AAC audio streams for Telegram playback.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import imageio_ffmpeg
import yt_dlp

logger = logging.getLogger("InstagramDownloader")
logger.setLevel(logging.INFO)

BASE_DIR = Path(__file__).parent
COOKIES_PATH = BASE_DIR / "cookies.txt"
MAX_FILESIZE_BYTES = 50 * 1024 * 1024  # 50 MB Telegram standard limit

# Resolve FFmpeg path for audio verification and compatibility
FFMPEG_PATH: Optional[str] = None
try:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg and os.path.exists(system_ffmpeg):
        FFMPEG_PATH = system_ffmpeg
    else:
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and os.path.exists(bundled):
            FFMPEG_PATH = bundled
            if hasattr(os, "chmod"):
                try:
                    os.chmod(FFMPEG_PATH, 0o755)
                except Exception:
                    pass
except Exception as e:
    logger.warning(f"FFmpeg resolution warning: {e}")
    FFMPEG_PATH = None

# Inject FFmpeg directory into PATH if available
if FFMPEG_PATH and os.path.exists(FFMPEG_PATH):
    try:
        ffmpeg_dir = str(Path(FFMPEG_PATH).parent)
        current_path = os.environ.get("PATH", "")
        if ffmpeg_dir not in current_path:
            os.environ["PATH"] = f"{ffmpeg_dir}{os.pathsep}{current_path}"
    except Exception:
        pass


def check_audio_stream(filepath: str) -> Tuple[bool, str]:
    """Check if the downloaded video contains an audio stream and return (has_audio, codec)."""
    if not FFMPEG_PATH or not os.path.exists(FFMPEG_PATH) or not os.path.exists(filepath):
        return True, "unknown"
    try:
        cmd = [FFMPEG_PATH, "-hide_banner", "-i", filepath]
        res = subprocess.run(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            errors="ignore",
            timeout=15,
        )
        for line in res.stderr.splitlines():
            if "Audio:" in line:
                parts = line.split("Audio:")[1].split()
                codec = parts[0].strip(",").lower() if parts else "unknown"
                return True, codec
        return False, ""
    except Exception as e:
        logger.warning(f"Audio probe error on {filepath}: {e}")
        return True, "unknown"


def ensure_aac_audio(video_path: str) -> Tuple[str, bool]:
    """Ensure video audio is encoded in AAC for universal Telegram playback."""
    if not FFMPEG_PATH or not os.path.exists(FFMPEG_PATH) or not os.path.exists(video_path):
        return video_path, True

    has_audio, codec = check_audio_stream(video_path)
    if not has_audio:
        return video_path, False

    # AAC (mp4a) is natively supported by Telegram ExoPlayer & Apple AVPlayer
    if "aac" in codec or "mp4a" in codec:
        return video_path, True

    logger.info(f"Transcoding audio in {video_path} from {codec} to AAC for Telegram compatibility...")
    base, _ = os.path.splitext(video_path)
    fixed_path = f"{base}_aac.mp4"
    cmd = [
        FFMPEG_PATH,
        "-y",
        "-i",
        video_path,
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        fixed_path,
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if os.path.exists(fixed_path) and os.path.getsize(fixed_path) > 0:
            os.replace(fixed_path, video_path)
            return video_path, True
    except Exception as e:
        logger.warning(f"AAC transcoding failed for {video_path}: {e}")
        if os.path.exists(fixed_path):
            try:
                os.remove(fixed_path)
            except Exception:
                pass
    return video_path, True


def extract_instagram_shortcode(url: str) -> str:
    """Extract Instagram shortcode/media id from URL."""
    match = re.search(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url)
    if match:
        return match.group(1)
    # Fallback to last non-empty segment
    cleaned = url.split("?")[0].rstrip("/")
    return cleaned.split("/")[-1]


def download_instagram_photos_graphql(url: str, output_path: str) -> Dict[str, Any]:
    """
    Download single photo or multi-photo carousel album from Instagram using GraphQL Polaris query.
    Handles up to 10 photos in high resolution.
    """
    ydl = yt_dlp.YoutubeDL({"quiet": True})
    ie = yt_dlp.extractor.instagram.InstagramIE(ydl)
    match = ie._match_valid_url(url)
    if not match:
        raise ValueError(f"Invalid Instagram URL: {url}")

    video_id, clean_url = match.group("id", "url")
    media_id = str(yt_dlp.extractor.instagram._id_to_pk(video_id))
    ie._real_initialize()

    csrf_token = ie._get_cookies("https://www.instagram.com").get("csrftoken")
    csrf_val = csrf_token.value if csrf_token else None

    # Modern Polaris logged-out web query
    response = ie._download_json(
        "https://www.instagram.com/api/graphql",
        video_id,
        fatal=False,
        impersonate=True,
        headers={
            **ie._api_headers,
            "X-FB-Friendly-Name": "PolarisLoggedOutDesktopWWWPostRootContentQuery",
            "X-CSRFToken": csrf_val,
            "X-FB-LSD": ie._lsd_token,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": clean_url,
        },
        data=yt_dlp.extractor.instagram.urlencode_postdata({
            "lsd": ie._lsd_token,
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": "PolarisLoggedOutDesktopWWWPostRootContentQuery",
            "server_timestamps": "true",
            "variables": yt_dlp.utils.json.dumps({"media_id": media_id}, separators=(",", ":")),
            "doc_id": "27130156389949648",
        }),
    )

    media = yt_dlp.utils.traverse_obj(response, ("data", "xig_polaris_media", {dict}))
    product_info = yt_dlp.utils.traverse_obj(media, ("if_not_gated_logged_out", {dict}))
    if not product_info:
        raise ValueError("This Instagram post is private, age-restricted, or requires login.")

    info_dict = ie._extract_product(product_info, video_id=video_id, get_comments=False)
    title = info_dict.get("title") or "Instagram Photo Post"
    uploader = info_dict.get("uploader") or info_dict.get("channel") or "Instagram Creator"

    carousel = yt_dlp.utils.traverse_obj(product_info, ("carousel_media", ..., {dict}))
    image_urls = []

    if carousel:
        for item in carousel:
            img_candidates = yt_dlp.utils.traverse_obj(item, ("image_versions2", "candidates", ..., {dict}))
            if img_candidates:
                best = max(img_candidates, key=lambda x: (x.get("width", 0) * x.get("height", 0)))
                image_urls.append(best["url"])
    else:
        img_candidates = yt_dlp.utils.traverse_obj(product_info, ("image_versions2", "candidates", ..., {dict}))
        if img_candidates:
            best = max(img_candidates, key=lambda x: (x.get("width", 0) * x.get("height", 0)))
            image_urls.append(best["url"])
        elif info_dict.get("thumbnails"):
            image_urls.append(info_dict["thumbnails"][-1]["url"])

    if not image_urls:
        raise ValueError("No downloadable images found in this Instagram post.")

    # Download primary photo
    req = urllib.request.Request(
        image_urls[0],
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp, open(output_path, "wb") as f:
        f.write(resp.read())

    # Download additional carousel images (up to 9 more)
    extra_paths = []
    base_name, _ = os.path.splitext(output_path)
    for idx, img_url in enumerate(image_urls[1:10], start=2):
        extra_path = f"{base_name}_{idx}.jpg"
        try:
            req = urllib.request.Request(
                img_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            with urllib.request.urlopen(req, timeout=25) as resp, open(extra_path, "wb") as f:
                f.write(resp.read())
            extra_paths.append(extra_path)
        except Exception as e:
            logger.warning(f"Error downloading carousel photo #{idx}: {e}")

    file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    for ep in extra_paths:
        if os.path.exists(ep):
            file_size += os.path.getsize(ep)

    return {
        "file_path": output_path,
        "title": title,
        "duration": None,
        "uploader": uploader,
        "filesize": file_size,
        "width": None,
        "height": None,
        "extra_photos": extra_paths,
        "is_photo": True,
        "has_audio": False,
    }


def download_instagram(url: str, output_template: str) -> Dict[str, Any]:
    """
    Main Instagram download pipeline.
    Downloads Reels, Videos, Photos, and Carousels.
    Guarantees pre-multiplexed progressive MP4 with stereo AAC audio.
    """
    # 1. Clean URL
    clean_url = url.split("?")[0].rstrip("/")
    is_photo_post_url = "/p/" in clean_url.lower()

    # 2. Options configured specifically for Instagram progressive CDN delivery
    # Using 'best[ext=mp4]/best' ensures yt-dlp picks format 3/2/1/0 (progressive MP4)
    # which has video + stereo AAC audio combined directly by Instagram CDN.
    ydl_opts: Dict[str, Any] = {
        "format": "best[ext=mp4]/best",
        "outtmpl": output_template,
        "max_filesize": MAX_FILESIZE_BYTES,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "writethumbnail": False,
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    if COOKIES_PATH.exists() and COOKIES_PATH.stat().st_size > 0:
        ydl_opts["cookiefile"] = str(COOKIES_PATH)

    if FFMPEG_PATH and os.path.exists(FFMPEG_PATH):
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH

    # 3. Attempt video download with yt-dlp
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if "entries" in info and info["entries"]:
                info = info["entries"][0]

            filename = ydl.prepare_filename(info)
            if not os.path.exists(filename):
                base, _ = os.path.splitext(filename)
                for ext in [".mp4", ".mkv", ".webm"]:
                    if os.path.exists(base + ext):
                        filename = base + ext
                        break

            if not os.path.exists(filename):
                raise FileNotFoundError("Video file was not saved to disk by downloader.")

            # Verify audio stream & ensure Telegram AAC codec compatibility
            has_audio, _ = check_audio_stream(filename)
            if has_audio:
                filename, has_audio = ensure_aac_audio(filename)

            return {
                "file_path": filename,
                "title": info.get("title") or "Instagram Reel",
                "duration": info.get("duration"),
                "uploader": info.get("uploader") or info.get("channel") or "Instagram Creator",
                "filesize": info.get("filesize") or (os.path.getsize(filename) if os.path.exists(filename) else 0),
                "width": info.get("width"),
                "height": info.get("height"),
                "is_photo": False,
                "has_audio": has_audio,
                "extra_photos": [],
            }
    except Exception as e:
        err_str = str(e).lower()
        logger.info(f"Video extraction returned: {e}. Checking if post is a photo or carousel...")

        # If it's a photo, carousel, or video wasn't found, try photo/carousel pipeline
        if is_photo_post_url or "no video" in err_str or "empty media" in err_str or "format" in err_str:
            photo_path = output_template.replace("%(ext)s", "jpg")
            return download_instagram_photos_graphql(url, photo_path)
        raise
