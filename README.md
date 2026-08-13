# NEET AI Telegram Bot — Railway Ready

यह project पुराने अलग-अलग ZIP modules को एक single working project में merge करता है।

## Features

- `/start`, `/help`
- `/quiz विषय 20`
- `/chapter अध्याय 20`
- PDF upload → text extraction → AI MCQ
- `/leaderboard`
- `/profile`
- `/resume`
- सामान्य text → AI NEET study assistant
- SQLite database
- Gemini API key failover
- Gemini model discovery ताकि पुराने fixed model से 404 होने पर दूसरा उपलब्ध model try हो सके
- Railway-ready `railway.toml`

## Railway Variables

Railway → Service → Variables में:

`TELEGRAM_BOT_TOKEN` = BotFather से मिला bot token

`GEMINI_API_KEY` = Google AI Studio की Gemini API key

Optional:

`GEMINI_API_KEY_2` = दूसरी Gemini API key

`GEMINI_MODEL` = `gemini-3.6-flash`

## Local run

```bash
pip install -r requirements.txt
python main.py
```

## Commands

```text
/start
/help
/quiz कोशिका 20
/chapter मानव प्रजनन 20
/leaderboard
/profile
/resume
```

PDF भेजने पर bot PDF का text पढ़कर 10 MCQ बनाने की कोशिश करेगा।

## Important

API keys को GitHub में upload मत करना। `.env` की जगह Railway Variables इस्तेमाल करें।
SQLite database local disk पर रखता है; Railway पर persistent storage चाहिए तो Railway Volume जोड़ना बेहतर है।
