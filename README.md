# NEET WALA AI Study Bot — Final

## Commands
- `/startar`
- `/helpar`
- `/quizar <topic> <N>` — inline MCQ quiz
- `/chapterar <topic> <N>`
- `/pollar <topic> <N>` — separate native Telegram Quiz Polls, no timer
- `/pdfar [N]` — upload a Biology PDF after the command; default 90, maximum 90
- `/askar <question>`
- `/leaderboardar`
- `/profilear`
- `/resumear`
- `/neet720ar`
- `/ASHISH <question>` — group AI trigger only

## PDF → Poll
Use:
`/pdfar 50`
then upload the PDF in the same chat/group.

The bot reads the PDF's selectable text and generates **new NEET Biology questions
from the concepts in the source**, not merely copies the original questions.
It supports conceptual, statement-based, and Assertion-Reason questions.

- `/pdfar` = 90 questions by default
- `/pdfar 50` = 50 questions
- `/pdfar 30` = 30 questions
- Maximum = 90
- Every question is a separate native Telegram Quiz Poll.
- Polls have **no `open_period` and no `close_date`**, so there is no timer.
- Duplicate questions are removed and the generator requests extra batches until
  the requested number of unique valid questions is reached.
- If the source is too small/limited and the requested count cannot be produced,
  the bot reports that clearly instead of falsely claiming the count was reached.
- Scanned/image-only PDFs need OCR and are not supported by the current text extractor.
- PDF upload limit: 15 MB.

## Group AI
Normal group messages are ignored. The bot answers group AI questions only when
the message starts with `/ASHISH`, e.g.:
`/ASHISH mitochondria का कार्य क्या है?`

## Poll explanations
The explanation is not posted into the group. When a user answers a quiz poll,
the bot attempts to send the result and explanation by private DM. The user must
have opened/started the bot in private chat for Telegram to allow that DM.

## Railway Variables
Set:
- `TELEGRAM_BOT_TOKEN`
- `GEMINI_API_KEY`

Optional:
- `GEMINI_API_KEY_2`
- `GEMINI_MODEL=gemini-3.1-flash-lite`
- `DB_PATH=data/neet_ai.db`

The code dynamically checks available Gemini models and ignores deprecated
1.x/2.0/2.5 model IDs if they are left in old configuration.
