import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "@Stx_proxy_vpn").strip()
CITY = os.getenv("CITY", "Tehran").strip()
COUNTRY = os.getenv("COUNTRY", "IR").strip()
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tehran").strip()
POST_EVERY_HOURS = int(os.getenv("POST_EVERY_HOURS", "5"))
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
)
log = logging.getLogger("weather-bot")

CURRENT_URL = "https://api.openweathermap.org/data/2.5/weather"
FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"


def keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌤 وضعیت فعلی", callback_data="current", style="primary"),
            InlineKeyboardButton("🕒 پیش‌بینی ۲۴ ساعت", callback_data="hours", style="success"),
        ],
        [
            InlineKeyboardButton("📅 پیش‌بینی ۵ روزه", callback_data="days", style="primary"),
            InlineKeyboardButton("🔄 بروزرسانی", callback_data="current", style="success"),
        ],
        [InlineKeyboardButton("📢 کانال هواشناسی", url="https://t.me/Stx_proxy_vpn")],
    ])


async def fetch_json(url: str, params: dict):
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


async def get_current():
    return await fetch_json(CURRENT_URL, {
        "q": f"{CITY},{COUNTRY}",
        "appid": OPENWEATHER_API_KEY,
        "units": "metric",
        "lang": "fa",
    })


async def get_forecast():
    return await fetch_json(FORECAST_URL, {
        "q": f"{CITY},{COUNTRY}",
        "appid": OPENWEATHER_API_KEY,
        "units": "metric",
        "lang": "fa",
    })


def weather_text(data):
    w = data["weather"][0]
    main = data["main"]
    wind = data.get("wind", {})
    temp = round(main["temp"])
    feels = round(main.get("feels_like", temp))
    local_now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y/%m/%d - %H:%M")
    return (
        f"🌦 <b>گزارش هواشناسی {CITY}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"<b>{temp}°C</b>  |  {w.get('description', 'نامشخص').capitalize()}\n"
        f"🌡 دمای احساسی: {feels}°C\n"
        f"🔺 بیشینه: {round(main.get('temp_max', temp))}°C\n"
        f"🔻 کمینه: {round(main.get('temp_min', temp))}°C\n"
        f"💧 رطوبت: {main.get('humidity', '—')}٪\n"
        f"🧭 فشار: {main.get('pressure', '—')} hPa\n"
        f"💨 باد: {round(wind.get('speed', 0) * 3.6)} km/h\n"
        f"☁️ ابرناکی: {data.get('clouds', {}).get('all', '—')}٪\n"
        f"━━━━━━━━━━━━━━\n"
        f"🕒 آخرین بروزرسانی: {local_now}\n"
        f"<i>منبع داده: OpenWeather</i>"
    )


def hourly_text(data):
    entries = data.get("list", [])[:8]
    lines = [f"🕒 <b>پیش‌بینی ۲۴ ساعت آینده | {CITY}</b>", "━━━━━━━━━━━━━━"]
    for item in entries:
        dt = datetime.fromtimestamp(item["dt"], ZoneInfo(TIMEZONE)).strftime("%H:%M")
        desc = item["weather"][0].get("description", "")
        temp = round(item["main"]["temp"])
        rain = item.get("pop", 0) * 100
        lines.append(f"▪️ {dt}  |  {temp}°C  |  {desc}  |  🌧 {rain:.0f}٪")
    lines.append("━━━━━━━━━━━━━━\n<i>پیش‌بینی تقریبی بر اساس داده‌های OpenWeather</i>")
    return "\n".join(lines)


def daily_text(data):
    grouped = {}
    for item in data.get("list", []):
        day = datetime.fromtimestamp(item["dt"], ZoneInfo(TIMEZONE)).strftime("%Y/%m/%d")
        grouped.setdefault(day, []).append(item)
    lines = [f"📅 <b>پیش‌بینی ۵ روزه | {CITY}</b>", "━━━━━━━━━━━━━━"]
    for day, entries in list(grouped.items())[:5]:
        temps = [x["main"]["temp"] for x in entries]
        desc = entries[len(entries)//2]["weather"][0].get("description", "")
        pop = max(x.get("pop", 0) for x in entries) * 100
        lines.append(
            f"📍 <b>{day}</b>\n"
            f"🌡 {round(min(temps))}° تا {round(max(temps))}°  |  {desc}\n"
            f"🌧 احتمال بارش: {pop:.0f}٪"
        )
    lines.append("━━━━━━━━━━━━━━\n<i>این سرویس از پیش‌بینی ۳ ساعته OpenWeather خلاصه‌سازی می‌کند.</i>")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 <b>به ربات هواشناسی هوشمند خوش آمدید</b>\n\n"
        f"📍 شهر پیش‌فرض: <b>{CITY}</b>\n"
        "از دکمه‌های زیر برای مشاهده گزارش و پیش‌بینی استفاده کنید."
    )
    await update.effective_message.reply_text(
        msg, parse_mode=ParseMode.HTML, reply_markup=keyboard()
    )


async def send_current_to_user(query):
    data = await get_current()
    await query.edit_message_text(
        weather_text(data), parse_mode=ParseMode.HTML, reply_markup=keyboard()
    )


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        if query.data == "current":
            await send_current_to_user(query)
        elif query.data == "hours":
            data = await get_forecast()
            await query.edit_message_text(
                hourly_text(data), parse_mode=ParseMode.HTML, reply_markup=keyboard()
            )
        elif query.data == "days":
            data = await get_forecast()
            await query.edit_message_text(
                daily_text(data), parse_mode=ParseMode.HTML, reply_markup=keyboard()
            )
    except httpx.HTTPStatusError as exc:
        log.exception("Weather API returned an error")
        await query.edit_message_text(
            "⚠️ دریافت اطلاعات هواشناسی ناموفق بود. کلید API و محدودیت سرویس را بررسی کنید.",
            reply_markup=keyboard(),
        )
    except Exception:
        log.exception("Callback failed")
        await query.edit_message_text(
            "⚠️ خطای موقت رخ داد. کمی بعد دوباره تلاش کنید.",
            reply_markup=keyboard(),
        )


async def post_channel(context: ContextTypes.DEFAULT_TYPE):
    try:
        data = await get_current()
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=weather_text(data),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard(),
            disable_web_page_preview=True,
        )
        log.info("Weather report posted to %s", CHANNEL_ID)
    except Exception:
        log.exception("Scheduled channel post failed")


async def post_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_IDS and update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ این دستور فقط برای مدیر ربات است.")
        return
    await post_channel(context)
    await update.effective_message.reply_text("درخواست ارسال گزارش به کانال انجام شد؛ لاگ سرور را برای نتیجه بررسی کنید.")


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("✅ ربات فعال است.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled bot error", exc_info=context.error)


def main():
    missing = [name for name, value in [
        ("BOT_TOKEN", BOT_TOKEN),
        ("OPENWEATHER_API_KEY", OPENWEATHER_API_KEY),
    ] if not value]
    if missing:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("weather", start))
    app.add_handler(CommandHandler("postnow", post_now))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_error_handler(on_error)

    if app.job_queue is None:
        raise SystemExit('Install dependencies with: pip install "python-telegram-bot[job-queue]"')

    app.job_queue.run_repeating(
        post_channel,
        interval=POST_EVERY_HOURS * 60 * 60,
        first=10,
        name="channel-weather",
    )
    log.info("Starting weather bot; scheduled every %s hours", POST_EVERY_HOURS)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
