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
