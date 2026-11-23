import asyncio
import io
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "").strip()
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tehran").strip()
DEFAULT_CITY = os.getenv("CITY", "Tehran").strip()
DEFAULT_COUNTRY = os.getenv("COUNTRY", "IR").strip()
DEFAULT_CHANNELS = [x.strip() for x in os.getenv("CHANNELS", os.getenv("CHANNEL_ID", "")).split(",") if x.strip()]
DEFAULT_CITIES_RAW = os.getenv("CITIES", "")
DEFAULT_CITIES = []
for item in DEFAULT_CITIES_RAW.split(";"):
    item = item.strip()
    if item:
        bits = item.split(",")
        DEFAULT_CITIES.append((bits[0].strip(), bits[1].strip() if len(bits) > 1 else ""))
if not DEFAULT_CITIES:
    DEFAULT_CITIES = [(DEFAULT_CITY, DEFAULT_COUNTRY)]
POST_EVERY_HOURS = max(1, int(os.getenv("POST_EVERY_HOURS", "5")))
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", "/data/weather.db")
ALERT_RAIN_PERCENT = int(os.getenv("ALERT_RAIN_PERCENT", "65"))
ALERT_WIND_KMH = int(os.getenv("ALERT_WIND_KMH", "50"))
ALERT_COLD_C = float(os.getenv("ALERT_COLD_C", "0"))
ALERT_HOT_C = float(os.getenv("ALERT_HOT_C", "40"))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
)
log = logging.getLogger("weather-bot")

CURRENT_URL = "https://api.openweathermap.org/data/2.5/weather"
FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"
AIR_URL = "https://api.openweathermap.org/data/2.5/air_pollution"

# ----------------------- Persistence -----------------------

def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS cities (
            name TEXT NOT NULL, country TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(name,country)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS channels (
            channel TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS alert_state (
            city TEXT NOT NULL, alert_key TEXT NOT NULL, sent_at TEXT NOT NULL,
            PRIMARY KEY(city,alert_key)
        )""")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('interval_hours',?)", (str(POST_EVERY_HOURS),))
        for city, country in DEFAULT_CITIES:
            conn.execute("INSERT OR IGNORE INTO cities(name,country,enabled) VALUES(?,?,1)", (city,country))
        for channel in DEFAULT_CHANNELS:
            conn.execute("INSERT OR IGNORE INTO channels(channel,enabled) VALUES(?,1)", (channel,))


def setting(key, fallback=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else fallback


def set_setting(key, value):
    with db() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key,str(value)))


def cities():
    with db() as conn:
        rows = conn.execute("SELECT name,country FROM cities WHERE enabled=1 ORDER BY name").fetchall()
    return [(r["name"], r["country"]) for r in rows]


def channels():
    with db() as conn:
        rows = conn.execute("SELECT channel FROM channels WHERE enabled=1 ORDER BY channel").fetchall()
    return [r["channel"] for r in rows]


def add_city(name, country=""):
    with db() as conn:
        conn.execute("INSERT INTO cities(name,country,enabled) VALUES(?,?,1) ON CONFLICT(name,country) DO UPDATE SET enabled=1", (name,country))


def remove_city(name):
    with db() as conn:
        conn.execute("UPDATE cities SET enabled=0 WHERE lower(name)=lower(?)", (name,))


def add_channel(channel):
    channel = channel if channel.startswith("@") or channel.startswith("-100") else "@" + channel
    with db() as conn:
        conn.execute("INSERT INTO channels(channel,enabled) VALUES(?,1) ON CONFLICT(channel) DO UPDATE SET enabled=1", (channel,))


def remove_channel(channel):
    with db() as conn:
        conn.execute("UPDATE channels SET enabled=0 WHERE channel=?", (channel,))


def is_admin(update):
    return bool(update.effective_user and update.effective_user.id in ADMIN_IDS)


def admin_only(update):
    return is_admin(update)


# ----------------------- Weather API -----------------------

async def fetch_json(url, params):
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


async def get_current(city, country=""):
    return await fetch_json(CURRENT_URL, {
        "q": f"{city},{country}" if country else city,
        "appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "fa",
    })


async def get_forecast(city, country=""):
    return await fetch_json(FORECAST_URL, {
        "q": f"{city},{country}" if country else city,
        "appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "fa",
    })


async def get_air(city, country=""):
    current = await get_current(city, country)
    lat, lon = current["coord"]["lat"], current["coord"]["lon"]
    data = await fetch_json(AIR_URL, {
        "lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY,
    })
    return data["list"][0] if data.get("list") else None


# ----------------------- Graphic weather cards -----------------------

def font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def make_weather_card(city, data):
    # Clean branded card; Persian text is kept in Telegram caption for font compatibility.
    width, height = 1200, 675
    img = Image.new("RGB", (width, height), "#101d35")
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((35,35,width-35,height-35), radius=42, fill="#172b4d", outline="#45658c", width=3)
    draw.text((85,80), "WEATHER REPORT", font=font(38, True), fill="#9ec5ff")
    draw.text((85,145), city.upper(), font=font(58, True), fill="#ffffff")
    main = data["main"]
    weather = data["weather"][0]
    temp = round(main["temp"])
    draw.text((85,250), f"{temp}°C", font=font(120, True), fill="#ffffff")
    draw.text((95,410), weather.get("description","").upper(), font=font(34, True), fill="#c9dcf7")
    draw.line((85,490,width-85,490), fill="#45658c", width=2)
    metrics = [
        ("FEELS LIKE", f"{round(main.get('feels_like',temp))}°C"),
        ("HUMIDITY", f"{main.get('humidity','—')}%"),
        ("WIND", f"{round(data.get('wind',{}).get('speed',0)*3.6)} km/h"),
        ("PRESSURE", f"{main.get('pressure','—')} hPa"),
    ]
    x_positions = [90, 365, 650, 925]
    for x, (label, value) in zip(x_positions, metrics):
        draw.text((x,535), label, font=font(20, True), fill="#9ec5ff")
        draw.text((x,575), value, font=font(30, True), fill="#ffffff")
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    out.name = "weather.png"
    return out


# ----------------------- Presentation -----------------------

def local_time(ts=None):
    return datetime.fromtimestamp(ts, ZoneInfo(TIMEZONE)) if ts else datetime.now(ZoneInfo(TIMEZONE))


def current_caption(city, data):
    w, m, wind = data["weather"][0], data["main"], data.get("wind",{})
    temp = round(m["temp"])
    return (
        f"🌦 <b>گزارش هواشناسی {city}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"<b>{temp}°C</b> | {w.get('description','نامشخص')}\n"
        f"🌡 دمای احساسی: {round(m.get('feels_like',temp))}°C\n"
        f"🔺 بیشینه: {round(m.get('temp_max',temp))}°C | 🔻 کمینه: {round(m.get('temp_min',temp))}°C\n"
        f"💧 رطوبت: {m.get('humidity','—')}٪ | 🧭 فشار: {m.get('pressure','—')} hPa\n"
        f"💨 باد: {round(wind.get('speed',0)*3.6)} km/h | ☁️ ابرناکی: {data.get('clouds',{}).get('all','—')}٪\n"
        f"🕒 {local_time().strftime('%Y/%m/%d - %H:%M')}\n"
        f"<i>منبع: OpenWeather</i>"
    )


def forecast_text(city, data, hours=8):
    rows = data.get("list",[])[:hours]
    lines = [f"🕒 <b>پیش‌بینی ساعات آینده | {city}</b>", "━━━━━━━━━━━━━━"]
    for item in rows:
        when = local_time(item["dt"]).strftime("%d/%m %H:%M")
        temp = round(item["main"]["temp"])
        desc = item["weather"][0].get("description","")
        rain = round(item.get("pop",0)*100)
        lines.append(f"▪️ {when} | {temp}°C | {desc} | 🌧 {rain}٪")
    return "\n".join(lines)


def daily_text(city, data):
    grouped = {}
    for item in data.get("list",[]):
        day = local_time(item["dt"]).strftime("%Y/%m/%d")
        grouped.setdefault(day,[]).append(item)
    lines = [f"📅 <b>پیش‌بینی ۵ روزه | {city}</b>", "━━━━━━━━━━━━━━"]
    for day, items in list(grouped.items())[:5]:
        temps = [x["main"]["temp"] for x in items]
        mid = items[len(items)//2]
        pop = round(max(x.get("pop",0) for x in items)*100)
        lines.append(
            f"📍 <b>{day}</b>\n🌡 {round(min(temps))}° تا {round(max(temps))}° | "
            f"{mid['weather'][0].get('description','')}\n🌧 احتمال بارش: {pop}٪"
        )
    return "\n".join(lines)


def aqi_description(aqi):
    return {1:"خوب 🟢",2:"قابل قبول 🟡",3:"متوسط 🟠",4:"ناسالم 🔴",5:"بسیار ناسالم 🟣"}.get(aqi,"نامشخص")


def air_text(city, air):
    if not air:
        return f"اطلاعات کیفیت هوا برای {city} در دسترس نیست."
    comp = air.get("components",{})
    return (
        f"🌫 <b>کیفیت هوای {city}</b>\n"
        f"شاخص AQI: <b>{air.get('main',{}).get('aqi','—')} — "
        f"{aqi_description(air.get('main',{}).get('aqi'))}</b>\n"
        f"PM2.5: {comp.get('pm2_5','—')} µg/m³\n"
        f"PM10: {comp.get('pm10','—')} µg/m³\n"
        f"NO₂: {comp.get('no2','—')} µg/m³ | O₃: {comp.get('o3','—')} µg/m³"
    )


def main_keyboard(city):
    # city may be a token such as Tehran,IR so country-specific lookups remain unambiguous
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌤 وضعیت فعلی", callback_data=f"current|{city}", style="primary"),
         InlineKeyboardButton("🕒 پیش‌بینی ساعتی", callback_data=f"hours|{city}", style="success")],
        [InlineKeyboardButton("📅 پیش‌بینی ۵ روزه", callback_data=f"days|{city}", style="primary"),
         InlineKeyboardButton("🌫 کیفیت هوا", callback_data=f"air|{city}", style="danger")],
        [InlineKeyboardButton("📍 انتخاب شهر", callback_data="cities"),
         InlineKeyboardButton("🔄 بروزرسانی", callback_data=f"current|{city}", style="success")],
        [InlineKeyboardButton("⚙️ پنل مدیریت", callback_data="admin")],
    ])


def city_keyboard():
    rows = []
    for city, country in cities():
        token = f"{city},{country}" if country else city
        rows.append([InlineKeyboardButton(f"📍 {city}", callback_data=f"current|{token}")])
    rows.append([InlineKeyboardButton("↩️ بازگشت", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ افزودن شهر", callback_data="help_addcity"),
         InlineKeyboardButton("➖ حذف شهر", callback_data="help_delcity")],
        [InlineKeyboardButton("➕ افزودن کانال", callback_data="help_addchannel"),
         InlineKeyboardButton("➖ حذف کانال", callback_data="help_delchannel")],
        [InlineKeyboardButton("⏱ تنظیم فاصله ارسال", callback_data="help_interval"),
         InlineKeyboardButton("📋 تنظیمات فعلی", callback_data="admin_status")],
        [InlineKeyboardButton("↩️ منوی اصلی", callback_data="home")],
    ])


# ----------------------- User commands and callbacks -----------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = DEFAULT_CITY
    await update.effective_message.reply_text(
        f"👋 <b>به هواشناسی هوشمند خوش آمدید</b>\n📍 شهر پیش‌فرض: <b>{city}</b>\nاز منوی زیر استفاده کنید.",
        parse_mode=ParseMode.HTML, reply_markup=main_keyboard(f"{city},{DEFAULT_COUNTRY}")
    )


async def show_current(target, city):
    data = await get_current(city)
    card = make_weather_card(city, data)
    caption = current_caption(city, data)
    if hasattr(target, "edit_message_text") and target.message and target.message.photo:
        await target.message.delete()
        await target.message.reply_photo(photo=card, caption=caption, parse_mode=ParseMode.HTML, reply_markup=main_keyboard(city))
    elif hasattr(target, "edit_message_text"):
        await target.edit_message_text(caption, parse_mode=ParseMode.HTML, reply_markup=main_keyboard(city))
        await target.message.reply_photo(photo=card, caption="🖼 کارت گرافیکی وضعیت فعلی")
    else:
        await target.reply_photo(photo=card, caption=caption, parse_mode=ParseMode.HTML, reply_markup=main_keyboard(city))


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        if q.data == "home":
            await q.edit_message_text("منوی اصلی هواشناسی:", reply_markup=main_keyboard(f"{DEFAULT_CITY},{DEFAULT_COUNTRY}"))
            return
        if q.data == "cities":
            await q.edit_message_text("شهر موردنظر را انتخاب کنید:", reply_markup=city_keyboard())
            return
        if q.data == "admin":
            if not is_admin(update):
                await q.edit_message_text("⛔ این بخش فقط برای مدیر ربات است.")
            else:
                await q.edit_message_text("⚙️ <b>پنل مدیریت</b>\nبرای اجرای عملیات، دستور مربوطه را در چت ربات ارسال کنید.", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
            return
        if q.data.startswith("help_"):
            tips = {
                "help_addcity": "برای افزودن شهر بفرستید:\n/addcity London,GB",
                "help_delcity": "برای حذف شهر بفرستید:\n/delcity London",
                "help_addchannel": "برای افزودن کانال بفرستید:\n/addchannel @channel",
                "help_delchannel": "برای حذف کانال بفرستید:\n/delchannel @channel",
                "help_interval": "برای تغییر فاصله ارسال (ساعت) بفرستید:\n/setinterval 5",
            }
            await q.edit_message_text(tips.get(q.data,""), reply_markup=admin_keyboard())
            return
        if q.data == "admin_status":
            if not is_admin(update):
                await q.edit_message_text("⛔ دسترسی ندارید.")
                return
            city_list = ", ".join(f"{c} ({co})" for c,co in cities()) or "ندارد"
            channel_list = ", ".join(channels()) or "ندارد"
            await q.edit_message_text(
                f"📋 <b>تنظیمات فعلی</b>\nشهرها: {city_list}\nکانال‌ها: {channel_list}\nفاصله ارسال: هر {setting('interval_hours',POST_EVERY_HOURS)} ساعت",
                parse_mode=ParseMode.HTML, reply_markup=admin_keyboard()
            )
            return
        action, city_token = q.data.split("|",1)
        city_parts = city_token.rsplit(",",1)
        if len(city_parts) == 2 and len(city_parts[1]) == 2:
            city, country = city_parts[0], city_parts[1]
        else:
            city, country = city_token, ""
        if action == "current":
            data = await get_current(city,country)
            card = make_weather_card(city,data)
            await q.message.reply_photo(photo=card, caption=current_caption(city,data), parse_mode=ParseMode.HTML, reply_markup=main_keyboard(city_token))
        elif action == "hours":
            data = await get_forecast(city,country)
            await q.edit_message_text(forecast_text(city,data), parse_mode=ParseMode.HTML, reply_markup=main_keyboard(city_token))
        elif action == "days":
            data = await get_forecast(city,country)
            await q.edit_message_text(daily_text(city,data), parse_mode=ParseMode.HTML, reply_markup=main_keyboard(city_token))
        elif action == "air":
            air = await get_air(city,country)
            await q.edit_message_text(air_text(city,air), parse_mode=ParseMode.HTML, reply_markup=main_keyboard(city_token))
    except httpx.HTTPStatusError as exc:
        log.exception("Weather API HTTP error")
        msg = "⚠️ دریافت اطلاعات ناموفق بود. کلید API، دسترسی سرویس و نام شهر را بررسی کنید."
        try:
            await q.edit_message_text(msg, reply_markup=main_keyboard(f"{DEFAULT_CITY},{DEFAULT_COUNTRY}"))
        except Exception:
            await q.message.reply_text(msg)
    except Exception:
        log.exception("Callback failed")
        await q.message.reply_text("⚠️ خطای موقت رخ داد. کمی بعد دوباره تلاش کنید.")


# ----------------------- Admin commands -----------------------

async def addcity_cmd(update, context):
    if not admin_only(update):
        return await update.effective_message.reply_text("⛔ فقط مدیر مجاز است.")
    raw = " ".join(context.args)
    if not raw:
        return await update.effective_message.reply_text("نمونه: /addcity London,GB")
    parts = raw.split(",",1)
    add_city(parts[0].strip(), parts[1].strip() if len(parts)>1 else "")
    await update.effective_message.reply_text(f"✅ شهر {parts[0].strip()} اضافه شد.")


async def delcity_cmd(update, context):
    if not admin_only(update):
        return await update.effective_message.reply_text("⛔ فقط مدیر مجاز است.")
    if not context.args:
        return await update.effective_message.reply_text("نمونه: /delcity London")
    remove_city(" ".join(context.args))
    await update.effective_message.reply_text("✅ شهر غیرفعال شد.")


async def addchannel_cmd(update, context):
    if not admin_only(update):
        return await update.effective_message.reply_text("⛔ فقط مدیر مجاز است.")
    if not context.args:
        return await update.effective_message.reply_text("نمونه: /addchannel @channel")
    add_channel(context.args[0])
    await update.effective_message.reply_text("✅ کانال اضافه شد. ربات باید در آن ادمین و مجاز به ارسال پیام باشد.")


async def delchannel_cmd(update, context):
    if not admin_only(update):
        return await update.effective_message.reply_text("⛔ فقط مدیر مجاز است.")
    if not context.args:
        return await update.effective_message.reply_text("نمونه: /delchannel @channel")
    remove_channel(context.args[0])
    await update.effective_message.reply_text("✅ کانال غیرفعال شد.")


async def setinterval_cmd(update, context):
    if not admin_only(update):
        return await update.effective_message.reply_text("⛔ فقط مدیر مجاز است.")
    try:
        hours = int(context.args[0])
        if not 1 <= hours <= 24:
            raise ValueError()
    except (IndexError, ValueError):
        return await update.effective_message.reply_text("عدد بین ۱ تا ۲۴ وارد کن. مثال: /setinterval 5")
    set_setting("interval_hours", hours)
    job = context.application.job_queue.get_jobs_by_name("weather-publisher")
    for j in job:
        j.schedule_removal()
    context.application.job_queue.run_repeating(publish_all, interval=hours*3600, first=10, name="weather-publisher")
    await update.effective_message.reply_text(f"✅ فاصله انتشار روی هر {hours} ساعت تنظیم شد.")


async def postnow_cmd(update, context):
    if not admin_only(update):
        return await update.effective_message.reply_text("⛔ فقط مدیر مجاز است.")
    await publish_all(context)
    await update.effective_message.reply_text("✅ تلاش برای ارسال گزارش فوری انجام شد. لاگ را در صورت خطا بررسی کنید.")


async def health_cmd(update, context):
    await update.effective_message.reply_text("✅ ربات فعال است.")


# ----------------------- Alert engine and publisher -----------------------

def alert_was_sent(city, key):
    with db() as conn:
        row = conn.execute("SELECT sent_at FROM alert_state WHERE city=? AND alert_key=?", (city,key)).fetchone()
    if not row:
        return False
    try:
        sent = datetime.fromisoformat(row["sent_at"])
        return datetime.now(ZoneInfo(TIMEZONE)) - sent < timedelta(hours=12)
    except Exception:
        return False


def mark_alert_sent(city, key):
    with db() as conn:
        conn.execute("INSERT INTO alert_state(city,alert_key,sent_at) VALUES(?,?,?) ON CONFLICT(city,alert_key) DO UPDATE SET sent_at=excluded.sent_at", (city,key,datetime.now(ZoneInfo(TIMEZONE)).isoformat()))


async def check_alerts(context, city, country, data):
    alerts = []
    temp = data["main"]["temp"]
    wind = data.get("wind",{}).get("speed",0)*3.6
    if temp <= ALERT_COLD_C:
        alerts.append(("cold", f"🥶 هشدار سرمای شدید در {city}: دمای فعلی {round(temp)}°C"))
    if temp >= ALERT_HOT_C:
        alerts.append(("hot", f"🥵 هشدار گرمای شدید در {city}: دمای فعلی {round(temp)}°C"))
    if wind >= ALERT_WIND_KMH:
        alerts.append(("wind", f"💨 هشدار باد شدید در {city}: سرعت باد حدود {round(wind)} کیلومتر بر ساعت"))
    try:
        forecast = await get_forecast(city,country)
        if forecast.get("list"):
            max_pop = max(x.get("pop",0) for x in forecast["list"][:8])
            if max_pop*100 >= ALERT_RAIN_PERCENT:
                alerts.append(("rain", f"🌧 احتمال بارندگی بالا در {city}: تا {round(max_pop*100)}٪ طی ساعات آینده"))
    except Exception:
        log.exception("Could not check rain alert")
    for key, message in alerts:
        alert_id = f"{city},{country}"
        if not alert_was_sent(alert_id,key):
            for channel in channels():
                try:
                    await context.bot.send_message(channel, message)
                except Exception:
                    log.exception("Could not send alert to %s", channel)
            mark_alert_sent(alert_id,key)


async def publish_all(context):
    target_channels = channels()
    target_cities = cities()
    if not target_channels or not target_cities:
        log.warning("No enabled channels or cities configured")
        return
    for city, country in target_cities:
        try:
            data = await get_current(city,country)
            card = make_weather_card(city,data)
            caption = current_caption(city,data)
            for channel in target_channels:
                try:
                    await context.bot.send_photo(
                        chat_id=channel, photo=card, caption=caption,
                        parse_mode=ParseMode.HTML, reply_markup=main_keyboard(f"{city},{country}"),
                    )
                    card.seek(0)
                except Exception:
                    log.exception("Failed posting %s to %s", city, channel)
            await check_alerts(context,city,country,data)
        except Exception:
            log.exception("Failed to publish city %s", city)


async def post_initial(context):
    await publish_all(context)


async def error_handler(update, context):
    log.exception("Unhandled bot error", exc_info=context.error)


def main():
    if not BOT_TOKEN or not OPENWEATHER_API_KEY:
        raise SystemExit("Missing BOT_TOKEN or OPENWEATHER_API_KEY Railway variable.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start","weather"], start))
    app.add_handler(CommandHandler("health", health_cmd))
    app.add_handler(CommandHandler("postnow", postnow_cmd))
    app.add_handler(CommandHandler("addcity", addcity_cmd))
    app.add_handler(CommandHandler("delcity", delcity_cmd))
    app.add_handler(CommandHandler("addchannel", addchannel_cmd))
    app.add_handler(CommandHandler("delchannel", delchannel_cmd))
    app.add_handler(CommandHandler("setinterval", setinterval_cmd))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_error_handler(error_handler)
    if app.job_queue is None:
        raise SystemExit('Install python-telegram-bot[job-queue].')
    interval = int(setting("interval_hours",POST_EVERY_HOURS))
    app.job_queue.run_repeating(publish_all, interval=interval*3600, first=10, name="weather-publisher")
    log.info("Weather bot started; publish interval=%s hours; cities=%s; channels=%s", interval, cities(), channels())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
