# NEET WALA BOT — Final AI + Poll Edition

## Features

- AI-powered `/ask`, `/quiz`, `/chapter`, `/poll`, PDF analysis and normal chat
- Interactive inline-button quiz
- Native Telegram quiz polls
- Polls have **NO TIMER**: `/poll` does not set `open_period` or `close_date`
- Poll answers are recorded into SQLite
- Automatic +4 for correct and -1 for wrong
- Top 10 leaderboard
- Profile and resume
- PDF → AI summary + 10 MCQs
- Gemini model discovery/fallback
- Railway ready
- No `bot/` package required: all application code is in root `main.py`

## Railway Variables

Set:
- TELEGRAM_BOT_TOKEN
- GEMINI_API_KEY

Optional:
- GEMINI_API_KEY_2
- GEMINI_MODEL=gemini-3.6-flash

## Commands

`/start`
`/help`
`/ask आपका सवाल`
`/quiz कोशिका 10`
`/chapter मानव प्रजनन 10`
`/poll कोशिका`
`/leaderboard`
`/profile`
`/resume`
`/pdf`
`/neet720`

## Important

Never commit real API keys to GitHub.
For a durable SQLite database on Railway, attach a persistent volume.


## Unique AR commands

इस version में सभी commands के अंत में `ar` है ताकि दूसरे Telegram bot के same commands से conflict न हो:

- `/startar`
- `/helpar`
- `/quizar कोशिका 10`
- `/chapterar मानव प्रजनन 10`
- `/pollar कोशिका`
- `/askar आपका सवाल`
- `/leaderboardar`
- `/profilear`
- `/resumear`
- `/pdfar`
- `/neet720ar`

`/pollar` native Telegram quiz poll बनाता है और उसमें कोई timer/close date नहीं लगाई जाती।


## PDF → 90 NEET Biology Polls

`/pdfar` भेजने के बाद इसी chat/group में Biology PDF upload करें.

Bot:
1. PDF का selectable text पढ़ता है.
2. उसी material के concepts से अधिकतम **90** original NEET Biology MCQs बनाता है.
3. लगभग 50% conceptual, 25% Assertion-Reason/कथन-कारण और 25% statement-based रखता है.
4. हर question को native Telegram **Quiz Poll** में इसी group/chat में भेजता है.
5. Poll में `open_period` और `close_date` नहीं हैं, इसलिए **कोई timer नहीं**.
6. Correct answer और explanation bot database में रखता है.

> Scanned/image-only PDF के लिए OCR अलग से चाहिए.
> PDF upload के लिए 15 MB application limit रखी गई है.

\n## Fixed in this build

- Ignores old `gemini-1.5-flash` / other deprecated model IDs; default is `gemini-3.1-flash-lite`.
- Normal messages in groups are ignored; no automatic AI replies.
- Interactive `/quizar` now continues to Q2, Q3, etc.
- `/pollar` and PDF-generated polls remain without a timer.

\n## Group AI trigger

Group में bot सामान्य messages का जवाब **नहीं** देगा.
AI जवाब के लिए केवल:
`/ASHISH आपका सवाल`

उदाहरण:
`/ASHISH माइटोकॉन्ड्रिया को powerhouse क्यों कहते हैं?`

`/pollar` group में native Telegram Quiz Poll भेजता है. Poll के लिए bot के पास group में message भेजने की permission होनी चाहिए; permission error आए तो bot को admin बनाकर **Send Messages / Send Polls** अनुमति दें.


## Multi-Poll behavior

Use:

`/pollar कोशिका 5`

This creates **5 separate native Telegram Quiz Polls** in the current chat.
They have **no timer** (`open_period` and `close_date` are not set), so members can answer whenever they want.

- Group members can answer the polls.
- The bot does **not** post the explanation after every answer in the group.
- The answer + explanation is sent by **private DM to the member who answered**, when Telegram permits the bot to message that user.
- The user must have opened/started the bot in private chat at least once for the DM to work.
- Maximum `/pollar` batch size is 20.


## Duplicate-question fix

The PDF and `/pollar` question generator no longer fails just because the AI
returned duplicate questions. It automatically requests extra questions and
removes duplicates until the requested number of unique valid questions is
reached (within a safe retry limit).


## PDF with only 22 original questions

A PDF does **not** need to contain 90 original questions. If it contains 22
questions, the bot uses the concepts/facts/content in the PDF to create new,
original NEET-level questions. It does not simply copy the 22 questions.

The bot tries multiple batches and removes duplicates. If it still cannot
produce 90 valid unique questions from the available material, it posts the
valid questions it did produce instead of failing with a duplicate error.


## PDF poll sender fix

Fixed `name 'post_pdf_polls' is not defined`.
PDF questions can now be sent as native Telegram Quiz Polls after generation.

The PDF polls have no `open_period` or `close_date`, so there is no timer.


## PDF default
- `/pdfar` = 50 questions by default.
- `/pdfar 30` = 30 questions.
- `/pdfar 50` = 50 questions.
- `/pdfar 90` = 90 questions.
No other bot features were intentionally changed in this build.


## Final fixes
- `/pollar` defaults to 5 polls; `/pollar topic 10` sends 10.
- `/quizar` and `/chapterar` generate and continue through all requested questions.
- Interactive quiz has a 30-second timer; timeout automatically advances.
- `/pdfar` defaults to 50; `/pdfar 30`, `/pdfar 50`, `/pdfar 90` are supported.
- PDF duplicate questions are refilled with new AI-generated questions instead of failing at 85/90 or 89/90.
- Gemini uses only `GEMINI_MODEL` (default `gemini-3.6-flash`) and falls back from API key 1 to API key 2 on quota/API failure.
- PDF polls and `/pollar` polls have no timer.


## Final fixes
- Uses `pymupdf` instead of deprecated `fitz` import.
- `/pdfar` defaults to 50 and accepts 1–90.
- PDF generation refills duplicate/invalid questions instead of stopping at 85/90.
- `/pollar` generates the requested number of untimed native quiz polls.
- `/quizar` and `/chapterar` generate questions in small batches with duplicate-safe refill.
- Interactive quiz keeps the session in SQLite and advances after an answer or 30-second timeout.
- `GEMINI_MODEL` defaults to `gemini-3.6-flash`.


## Daily Scheduler
`/shedulear 21:00 कोशिका 5` creates a daily IST schedule for 5 untimed quiz polls.
`/shedulearlistrar` lists schedules. `/sheduleardel 1` deletes schedule #1.
Only Telegram group admins can manage schedules. Schedules persist in SQLite across Railway restarts.


## PDF poll 300-character fix

Telegram Quiz Poll questions have a maximum length of 300 characters.
Generated PDF questions are now safely truncated before sending, so a long
AI-generated question cannot crash the PDF poll batch.


## Final AI performance update
- Default Gemini model: `gemini-3.7-flash`
- Question generation no longer requests or sends explanations.
- Poll answers send only correct/incorrect + score update, not explanations.
- This reduces generated output and helps prevent the bot from stalling during question generation/answering.


## Gemini automatic fallback

Default model: `gemini-3.7-flash`.

If Gemini returns a temporary `503 UNAVAILABLE`, the bot automatically tries
the fallback models in order:
1. Gemini 3.7 Flash
2. Gemini 3.6 Flash
3. Gemini 3.5 Flash

The existing two-API-key setup is preserved.
