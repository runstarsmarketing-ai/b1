# premium.py
# Yahan aap apne sabhi premium/custom emojis ki unique IDs store kar sakte hain.
# Emoji ID nikalne ke liye json dump bot (@unbcmbot) ka use karein.
# Agar ID blank ho, to bot automatic high-quality unicode emoji use karega.

PREMIUM_EMOJIS = {
    # Platforms
    "INSTAGRAM": {"id": "5359321549851598370", "fallback": "📸"},
    "YOUTUBE": {"id": "5400213482505260650", "fallback": "▶️"},
    "TIKTOK": {"id": "5397930647257890579", "fallback": "🎵"},
    "PINTEREST": {"id": "5206525339517344010", "fallback": "📌"},
    "X_TWITTER": {"id": "5843805916813596368", "fallback": "🐦"},
    "FACEBOOK": {"id": "5355254460635428635", "fallback": "📘"},
    "WEB": {"id": "5282843764451195532", "fallback": "🌐"},

    # Aesthetics & UI Cards
    "SPARKLES": {"id": "5325547803936572038", "fallback": "✨"},
    "LIGHTNING": {"id": "5377834924776627189", "fallback": "⚡"},
    "DIAMOND": {"id": "5152371303767868448", "fallback": "💎"},
    "BULLET": {"id": "6084717714847306634", "fallback": "•"},
    "BULB": {"id": "5224596414415256150", "fallback": "💡"},
    "BOOK": {"id": "5803151379887297481", "fallback": "📖"},

    # Status & Progress
    "HOURGLASS": {"id": "6122764622509380932", "fallback": "⏳"},
    "DOWNLOAD": {"id": "6156513311585211842", "fallback": "📥"},
    "UPLOAD": {"id": "5911100572508885928", "fallback": "📤"},
    "SUCCESS": {"id": "6217630318250168874", "fallback": "✅"},
    "FAILED": {"id": "5161208387957950108", "fallback": "❌"},
    "WARNING": {"id": "6276132901012640832", "fallback": "⚠️"},
    "ROCKET": {"id": "5222381031629283378", "fallback": "🚀"},

    # Media Info & Metadata
    "MOVIE": {"id": "6256053744220245230", "fallback": "🎬"},
    "TAG": {"id": "5801165090656883290", "fallback": "🏷"},
    "DURATION": {"id": "5467728512972511445", "fallback": "⏱"},
    "SIZE": {"id": "4967897119760319376", "fallback": "💾"},
    "AUTHOR": {"id": "6021540399485555133", "fallback": "👤"},
    "AUDIO": {"id": "5337241526709791054", "fallback": "🎵"},
    "HEADPHONES": {"id": "5463107823946717464", "fallback": "🎧"},

    # Button Icons & Admin
    "LINK": {"id": "6150183436029012165", "fallback": "🔗"},
    "CHANNEL": {"id": "6269303009658802514", "fallback": "📢"},
    "DEVELOPER": {"id": "5276527873308499560", "fallback": "👨‍💻"},
    "ADD_GROUP": {"id": "5285402925009490289", "fallback": "➕"},
    "LOCK": {"id": "", "fallback": "🔒"},
    "USERS": {"id": "", "fallback": "👥"},
    "BROADCAST": {"id": "", "fallback": "📢"},
    "SHIELD": {"id": "", "fallback": "🛡️"},
}

def get_emoji(name: str, plain: bool = False) -> str:
    """Helper function jo premium emoji ka HTML tag ya plain fallback return karti hai."""
    emoji_data = PREMIUM_EMOJIS.get(name.upper())
    if not emoji_data:
        return ""
    fallback = emoji_data.get("fallback", "")
    if plain:
        return fallback
    emoji_id = emoji_data.get("id", "").strip()
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
    return fallback

def get_emoji_id(name: str) -> str | None:
    """Helper function to get custom emoji ID for inline keyboard button icons."""
    clean_name = name.upper().replace(" ", "_").replace("(", "").replace(")", "")
    if "TWITTER" in clean_name or clean_name == "X":
        clean_name = "X_TWITTER"
    emoji_data = PREMIUM_EMOJIS.get(clean_name) or PREMIUM_EMOJIS.get(name.upper())
    if emoji_data and emoji_data.get("id"):
        emoji_id = emoji_data["id"].strip()
        if emoji_id:
            return emoji_id
    return None
