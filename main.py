import os
import sqlite3
import asyncio
import logging
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from google import genai
from google.genai import types

try:
    import fitz
except ImportError:
    fitz = None

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_API_KEY_2 = os.getenv("GEMINI_API_KEY_2", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()
DB_PATH = os.getenv("DB_PATH", "data/neet_ai.db")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("NEET_WALA_BOT")

clients = {}

def db():
    p = Path(DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            score INTEGER DEFAULT 0,
            quizzes INTEGER DEFAULT 0,
            correct INTEGER DEFAULT 0,
            wrong INTEGER DEFAULT 0,
            last_topic TEXT
        )
        """)

def ensure_user(user):
    with db() as c:
        c.execute("""
        INSERT INTO users(user_id,name,username) VALUES(?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, username=excluded.username
        """, (user.id, user.full_name or user.username or str(user.id), user.username))

def get_user(uid):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def add_score(uid, topic, correct, wrong):
    with db() as c:
        c.execute("""
        UPDATE users SET score=score+?, quizzes=quizzes+1,
        correct=correct+?, wrong=wrong+?, last_topic=? WHERE user_id=?
        """, (correct * 4 - wrong, correct, wrong, topic, uid))

def get_client(key):
    if key not in clients:
        clients[key] = genai.Client(api_key=key)
    return clients[key]

def model_candidates(client):
    preferred = [
        GEMINI_MODEL,
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash",
    ]
    found = []
    try:
        for m in client.models.list():
            name = getattr(m, "name", "") or ""
            if name.startswith("models/"):
                name = name[7:]
            if name and name not in found:
                found.append(name)
    except Exception as e:
        log.warning("Could not list Gemini models: %s", e)

    result = []
    for m in preferred:
        if m and m not in result and (not found or m in found):
            result.append(m)

    if found:
        for m in found:
            low = m.lower()
            if "gemini" in low and not any(x in low for x in ("embedding", "tts", "image", "audio")):
                if m not in result:
                    result.append(m)
    return result or [GEMINI_MODEL]

def ask_ai(prompt, system=None):
    keys = [k for k in (GEMINI_API_KEY, GEMINI_API_KEY_2) if k]
    if not keys:
        raise RuntimeError("GEMINI_API_KEY missing in Railway Variables.")

    last = None
    for key in keys:
        client = get_client(key)
        for model in model_candidates(client):
            try:
                config = types.GenerateContentConfig(
                    system_instruction=system or "",
                    max_output_tokens=6000,
                )
                response = client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
                text = getattr(response, "text", None)
                if text:
                    log.info("Gemini success with model=%s", model)
                    return text.strip()
            except Exception as e:
                last = e
                log.warning("Gemini model %s failed: %s", model, e)
    raise RuntimeError(f"Gemini request failed: {last}")

def chunks(text, n=3900):
    return [text[i:i+n] for i in range(0, len(text), n)]

SYSTEM = """तुम NEET तैयारी के लिए हिंदी AI Study Assistant हो।
उत्तर सरल, स्पष्ट और NCERT-केंद्रित रखो। गलत तथ्य या मनगढ़ंत citation मत बनाओ।"""

HELP = """📚 NEET AI STUDY BOT

/start — Bot शुरू करें
/help — Commands
/quiz विषय [प्रश्न] — AI MCQ
/chapter अध्याय [प्रश्न] — Chapter MCQ
/leaderboard — Top 10
/profile — आपकी progress
/pdf — PDF भेजें
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await update.message.reply_text("👋 नमस्ते!\n\n" + HELP)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await update.message.reply_text(HELP)

async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    if not context.args:
        await update.message.reply_text("उदाहरण: /quiz कोशिका 20")
        return

    count = 20
    if context.args[-1].isdigit():
        count = max(5, min(60, int(context.args[-1])))
        topic = " ".join(context.args[:-1]).strip()
    else:
        topic = " ".join(context.args).strip()

    if not topic:
        topic = "जीव विज्ञान"

    await update.message.reply_text(f"⏳ {topic} पर {count} MCQ बन रहे हैं...")
    prompt = f"""विषय: {topic}
{count} NEET स्तर के मूल MCQ बनाओ।

Format:
Q1. प्रश्न
A) विकल्प
B) विकल्प
C) विकल्प
D) विकल्प
सही उत्तर: B
व्याख्या: 1-2 पंक्तियाँ

केवल हिंदी, 4 विकल्प, NCERT आधारित और duplicate नहीं।"""

    try:
        text = await asyncio.to_thread(ask_ai, prompt, SYSTEM)
        for part in chunks(text):
            await update.message.reply_text(part)
    except Exception as e:
        await update.message.reply_text(f"❌ AI error:\n{str(e)[:900]}")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    with db() as c:
        rows = c.execute("""
        SELECT name,score,quizzes,correct FROM users
        ORDER BY score DESC, correct DESC LIMIT 10
        """).fetchall()
    if not rows:
        await update.message.reply_text("Leaderboard अभी खाली है.")
        return
    lines = ["🏆 TOP 10 LEADERBOARD", ""]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['name']} — {r['score']} अंक | Quiz {r['quizzes']} | सही {r['correct']}")
    await update.message.reply_text("\n".join(lines))

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    r = get_user(update.effective_user.id)
    total = r["correct"] + r["wrong"]
    acc = round(r["correct"] / total * 100, 1) if total else 0
    await update.message.reply_text(
        f"👤 {r['name']}\n🏆 Score: {r['score']}\n"
        f"📝 Quiz: {r['quizzes']}\n✅ सही: {r['correct']}\n"
        f"❌ गलत: {r['wrong']}\n🎯 Accuracy: {acc}%"
    )

async def pdf_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.document:
        return
    if fitz is None:
        await update.message.reply_text("❌ PDF support package missing.")
        return
    doc = update.message.document
    if doc.file_size and doc.file_size > 15 * 1024 * 1024:
        await update.message.reply_text("❌ PDF 15 MB से छोटी रखें.")
        return
    await update.message.reply_text("📄 PDF मिल गई, text पढ़ रहा हूँ...")
    path = None
    try:
        tg = await doc.get_file()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            path = f.name
        await tg.download_to_drive(path)
        pdf = fitz.open(path)
        text = "\n".join(p.get_text() for p in pdf).strip()
        pdf.close()
        if not text:
            await update.message.reply_text("❌ PDF में selectable text नहीं मिला.")
            return
        prompt = f"""नीचे PDF का text है। इससे 10 मूल NEET MCQ बनाओ।
हर प्रश्न में 4 विकल्प, सही उत्तर और छोटी explanation हो।
PDF TEXT:
{text[:50000]}"""
        answer = await asyncio.to_thread(ask_ai, prompt, SYSTEM)
        for part in chunks(answer):
            await update.message.reply_text(part)
    except Exception as e:
        await update.message.reply_text(f"❌ PDF/AI error:\n{str(e)[:900]}")
    finally:
        if path:
            Path(path).unlink(missing_ok=True)

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    ensure_user(update.effective_user)
    try:
        answer = await asyncio.to_thread(ask_ai, text, SYSTEM)
        for part in chunks(answer):
            await update.message.reply_text(part)
    except Exception as e:
        await update.message.reply_text(f"❌ AI error:\n{str(e)[:900]}")

def build():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing in Railway Variables.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("quiz", quiz))
    app.add_handler(CommandHandler("chapter", quiz))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(MessageHandler(filters.Document.PDF, pdf_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    return app

if __name__ == "__main__":
    log.info("Starting NEET WALA BOT")
    build().run_polling(allowed_updates=["message"])
