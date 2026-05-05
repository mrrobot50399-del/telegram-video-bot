import logging
import os
import re
import asyncio
import subprocess
import sys
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

# ==========================================
# تحديد مسار FFmpeg تلقائياً
# ==========================================
try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    print(f"✅ FFmpeg وُجد عبر imageio: {FFMPEG_PATH}")
except ImportError:
    FFMPEG_PATH = "ffmpeg"  # استخدام ffmpeg الافتراضي إن وُجد

# ==========================================
# إعدادات البوت
# ==========================================
BOT_TOKEN = "8690531077:AAHFKEkefd-SFqHzfONIvG_VQGuBE_R6vvA"   # <-- ضع توكن البوت من BotFather هنا
DOWNLOAD_PATH = "./downloads"
MAX_SIZE_MB = 50
MAX_SIZE_BYTES = MAX_SIZE_MB * 1024 * 1024

# ==========================================
# إعداد السجل
# ==========================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

os.makedirs(DOWNLOAD_PATH, exist_ok=True)

# ==========================================
# ⚡ حل مشكلة البروكسي - إزالة متغيرات البروكسي
# ==========================================
for _var in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
             "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"]:
    os.environ.pop(_var, None)

# ==========================================
# قاموس المواقع المدعومة
# ==========================================
SITE_PATTERNS = {
    "YouTube":     r"(youtube\.com|youtu\.be)",
    "TikTok":      r"tiktok\.com",
    "Instagram":   r"instagram\.com",
    "Twitter/X":   r"(twitter\.com|x\.com)",
    "Facebook":    r"(facebook\.com|fb\.watch)",
    "Reddit":      r"reddit\.com",
    "Vimeo":       r"vimeo\.com",
    "Dailymotion": r"dailymotion\.com",
    "Twitch":      r"twitch\.tv",
    "Rumble":      r"rumble\.com",
    "Odysee":      r"odysee\.com",
    "Kick":        r"kick\.com",
    "Bilibili":    r"bilibili\.com",
    "Pinterest":   r"pinterest\.",
    "Snapchat":    r"snapchat\.com",
    "LinkedIn":    r"linkedin\.com",
    "Streamable":  r"streamable\.com",
    "Mixcloud":    r"mixcloud\.com",
    "SoundCloud":  r"soundcloud\.com",
    "Bandcamp":    r"bandcamp\.com",
}

def detect_site(url: str) -> str:
    for site, pattern in SITE_PATTERNS.items():
        if re.search(pattern, url, re.IGNORECASE):
            return site
    return "Other"

def is_valid_url(url: str) -> bool:
    return bool(re.match(r'^https?://\S+', url))

def find_file(prefix: str) -> str | None:
    for fname in os.listdir(DOWNLOAD_PATH):
        if fname.startswith(prefix):
            full = os.path.join(DOWNLOAD_PATH, fname)
            if os.path.isfile(full):
                return full
    return None

def cleanup(path: str | None):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# ==========================================
# بناء خيارات yt-dlp حسب الموقع
# ==========================================
COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def build_ydl_opts(output_template: str, site: str) -> dict:
    opts = {
        "outtmpl":           output_template,
        "noplaylist":        True,
        "quiet":             True,
        "no_warnings":       True,
        "noprogress":        True,
        "nocheckcertificate":True,
        "geo_bypass":        True,
        "proxy":             "",        # ← تجاوز البروكسي تماماً
        "socket_timeout":    30,
        "retries":           5,
        "fragment_retries":  5,
        "http_headers":      COMMON_HEADERS.copy(),
        "merge_output_format": "mp4",
        "ffmpeg_location":   FFMPEG_PATH,   # ← مسار FFmpeg المحدد
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
    }

    if site == "YouTube":
        opts["format"] = (
            "bestvideo[height<=720][ext=mp4][filesize<45M]+bestaudio[ext=m4a]/"
            "bestvideo[height<=720][filesize<45M]+bestaudio/"
            "best[height<=720][filesize<45M]/best[filesize<45M]/best"
        )
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["web", "android"],
                "skip": ["hls", "dash"],
            }
        }

    elif site == "TikTok":
        opts["format"] = "best[filesize<45M]/best"
        opts["http_headers"]["Referer"] = "https://www.tiktok.com/"

    elif site == "Instagram":
        opts["format"] = "best[filesize<45M]/best"
        opts["http_headers"]["Referer"] = "https://www.instagram.com/"

    elif site in ("Twitter/X",):
        opts["format"] = "best[filesize<45M]/best"

    elif site == "Facebook":
        opts["format"] = "best[filesize<45M]/best"

    else:
        opts["format"] = (
            "bestvideo[filesize<45M]+bestaudio/"
            "best[filesize<45M]/best"
        )

    return opts


# ==========================================
# دوال التحميل
# ==========================================
def _ytdlp_lib(url: str, opts: dict) -> dict | None:
    """تحميل باستخدام مكتبة yt-dlp مباشرة"""
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)
    except Exception as e:
        logger.warning(f"[yt-dlp lib] فشل: {e}")
        return None

def _ytdlp_cli(url: str, output_template: str) -> bool:
    """تحميل باستخدام yt-dlp عبر CLI كبديل"""
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist",
        "--proxy", "",
        "--geo-bypass",
        "--no-check-certificates",
        "--retries", "5",
        "--socket-timeout", "30",
        "--ffmpeg-location", FFMPEG_PATH,
        "-f", "bestvideo[filesize<45M]+bestaudio/best[filesize<45M]/best",
        "--merge-output-format", "mp4",
        "-o", output_template,
        url
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return result.returncode == 0
    except Exception as e:
        logger.warning(f"[yt-dlp CLI] فشل: {e}")
        return False

def _ytdlp_simple(url: str, output_template: str) -> bool:
    """تحميل بإعدادات مبسطة جداً كآخر محاولة"""
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--proxy", "",
        "--no-check-certificates",
        "--ffmpeg-location", FFMPEG_PATH,
        "--format", "best",
        "-o", output_template,
        url
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return result.returncode == 0
    except Exception as e:
        logger.warning(f"[yt-dlp simple] فشل: {e}")
        return False


# ==========================================
# معالجات البوت
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *مرحباً في بوت تحميل الفيديوهات!*\n\n"
        "📥 أرسل أي رابط فيديو وسأحمله لك فوراً\n\n"
        "✅ *المواقع المدعومة:*\n"
        "▶️ YouTube  •  🎵 TikTok  •  📸 Instagram\n"
        "🐦 Twitter/X  •  👥 Facebook  •  👽 Reddit\n"
        "🎬 Vimeo  •  🎮 Twitch  •  📺 Dailymotion\n"
        "🔴 Rumble  •  🌊 Odysee  •  🟢 Kick\n"
        "🎵 SoundCloud  •  📻 Mixcloud  •  🎸 Bandcamp\n"
        "➕ وأكثر من *1000 موقع* آخر!\n\n"
        "⚠️ *الحد الأقصى للحجم:* 50 MB"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *المساعدة:*\n\n"
        "1️⃣ انسخ رابط الفيديو\n"
        "2️⃣ أرسله هنا مباشرة\n"
        "3️⃣ انتظر وستحصل على الفيديو!\n\n"
        "📌 *الأوامر:*\n"
        "/start - رسالة الترحيب\n"
        "/help  - هذه الرسالة\n\n"
        "❓ *مشاكل شائعة:*\n"
        "• فيديو خاص/محذوف → لا يمكن تحميله\n"
        "• حجم >50MB → جرب جودة أقل\n"
        "• Instagram/Facebook → تأكد أن المنشور عام"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not is_valid_url(url):
        await update.message.reply_text(
            "❌ هذا ليس رابطاً صالحاً!\n"
            "أرسل رابطاً يبدأ بـ https://"
        )
        return

    site = detect_site(url)
    prefix = f"{update.message.message_id}_"
    output_tpl = os.path.join(DOWNLOAD_PATH, f"{prefix}%(title).60s.%(ext)s")

    status_msg = await update.message.reply_text(
        f"🌐 *{site}* — جاري التحميل...\n⏳ يرجى الانتظار",
        parse_mode="Markdown"
    )

    downloaded_file = None
    info = None
    loop = asyncio.get_event_loop()

    try:
        # ── المحاولة 1: yt-dlp مكتبة بإعدادات متقدمة ──
        await status_msg.edit_text(f"🌐 *{site}*\n📥 المحاولة 1/3...", parse_mode="Markdown")
        ydl_opts = build_ydl_opts(output_tpl, site)
        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _ytdlp_lib(url, ydl_opts)),
                timeout=180
            )
        except asyncio.TimeoutError:
            info = None

        # ── المحاولة 2: yt-dlp CLI ──
        if not info and not find_file(prefix):
            await status_msg.edit_text(f"🌐 *{site}*\n📥 المحاولة 2/3...", parse_mode="Markdown")
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: _ytdlp_cli(url, output_tpl)),
                    timeout=180
                )
                if find_file(prefix):
                    info = {"title": "video", "ext": "mp4"}
            except asyncio.TimeoutError:
                pass

        # ── المحاولة 3: إعدادات مبسطة جداً ──
        if not info and not find_file(prefix):
            await status_msg.edit_text(f"🌐 *{site}*\n📥 المحاولة 3/3...", parse_mode="Markdown")
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: _ytdlp_simple(url, output_tpl)),
                    timeout=180
                )
                if find_file(prefix):
                    info = {"title": "video", "ext": "mp4"}
            except asyncio.TimeoutError:
                pass

        # ── التحقق من نجاح التحميل ──
        downloaded_file = find_file(prefix)

        if not downloaded_file:
            await status_msg.edit_text(
                "❌ *فشل التحميل من جميع المحاولات*\n\n"
                "أسباب محتملة:\n"
                "• الفيديو خاص أو محذوف\n"
                "• الموقع يحتاج تسجيل دخول\n"
                "• الرابط غير صحيح\n"
                "• الفيديو محمي بحقوق النشر\n"
                "• مشكلة في الشبكة",
                parse_mode="Markdown"
            )
            return

        # ── التحقق من الحجم ──
        file_size = os.path.getsize(downloaded_file)
        if file_size > MAX_SIZE_BYTES:
            await status_msg.edit_text(
                f"❌ *الملف كبير جداً!*\n"
                f"📦 الحجم: {file_size/1024/1024:.1f} MB\n"
                f"⚠️ الحد الأقصى: {MAX_SIZE_MB} MB\n\n"
                f"جرب رابطاً بجودة أقل",
                parse_mode="Markdown"
            )
            cleanup(downloaded_file)
            return

        # ── إرسال الفيديو ──
        await status_msg.edit_text("📤 جاري الإرسال...")

        title     = str(info.get("title") or "فيديو")[:200] if info else "فيديو"
        uploader  = str(info.get("uploader") or info.get("channel") or "") if info else ""
        duration  = info.get("duration") if info else None
        size_mb   = file_size / 1024 / 1024

        lines = [f"🎬 *{title}*"]
        if uploader:
            lines.append(f"👤 {uploader}")
        lines.append(f"📦 {size_mb:.1f} MB")
        if duration:
            m, s = divmod(int(duration), 60)
            lines.append(f"⏱ {m}:{s:02d}")
        lines.append(f"🌐 {site}")
        caption = "\n".join(lines)

        with open(downloaded_file, "rb") as vf:
            await update.message.reply_video(
                video=vf,
                caption=caption,
                parse_mode="Markdown",
                supports_streaming=True,
                write_timeout=300,
                read_timeout=300,
                connect_timeout=60,
            )

        await status_msg.delete()
        logger.info(f"✅ أُرسل: {title} | {size_mb:.1f} MB | {site}")

    except Exception as e:
        err = str(e)
        logger.error(f"خطأ في {url}: {err}")

        if "Video unavailable" in err or "Private" in err:
            msg = "❌ الفيديو غير متاح أو خاص."
        elif "429" in err or "rate" in err.lower():
            msg = "⚠️ تجاوزنا حد الطلبات. حاول بعد دقيقة."
        elif "copyright" in err.lower():
            msg = "❌ الفيديو محمي بحقوق النشر."
        elif "login" in err.lower() or "Login" in err:
            msg = "❌ هذا الفيديو يحتاج تسجيل دخول."
        elif "ffmpeg" in err.lower():
            msg = f"❌ خطأ في FFmpeg\nالمسار: `{FFMPEG_PATH}`"
        elif "Proxy" in err or "proxy" in err:
            msg = "⚠️ مشكلة في الشبكة (بروكسي). حاول مرة أخرى."
        else:
            msg = f"❌ حدث خطأ:\n`{err[:300]}`"

        try:
            await status_msg.edit_text(msg, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(msg, parse_mode="Markdown")

    finally:
        cleanup(downloaded_file)


# ==========================================
# نقطة الدخول
# ==========================================
def main():
    print("🤖 جاري تشغيل البوت...")
    print(f"📁 مجلد التحميلات: {os.path.abspath(DOWNLOAD_PATH)}")

    # التحقق من FFmpeg
    try:
        subprocess.run([FFMPEG_PATH, "-version"], capture_output=True, check=True)
        print(f"✅ FFmpeg يعمل: {FFMPEG_PATH}")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(f"⚠️  FFmpeg لا يعمل في المسار: {FFMPEG_PATH}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_video))

    print("✅ البوت يعمل! اضغط Ctrl+C للإيقاف\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()