# Sub-agent B — Content poster (sheet → WhatsApp group + channel)

## What this folder is

`sub_agent_b/` is a **subdirectory of the WhatsApp Bot** project.

It posts **text / image / video / voice / audio / document** to your WhatsApp
**group** and/or **channel**, using rows from a Google Sheet tab called
`ContentCalendar`. Timing is whatever you put in each row (`date` + `time`).

You paste a **TikTok / social / Drive / direct file URL** in the sheet.
Agent B downloads a real playable file, then uploads it to WhatsApp.
(The sheet only stores the URL text — not the video binary.)

## Flow

```
ContentCalendar row (status=pending, date+time due)
        ↓
POST /cron/content-posts  (Railway cron every 5–15 min)
        ↓
ContentPoster picks due rows
        ↓
Whapi send to group (@g.us) and/or channel (@newsletter)
        ↓
Mark status = posted  (or failed + notes)
```

## Files in this folder

| File | Job |
|------|-----|
| `poster.py` | Main Agent B logic (due rows → send → update sheet) |
| `content_sheets.py` | Read/update `ContentCalendar` tab |
| `media_resolve.py` | Download Drive / TikTok / social / direct URLs → real file |
| `media_whapi.py` | Whapi send (URL or multipart file) + list channels |
| `run_once.py` | Run one tick locally |
| `list_channels.py` | Fetch `@newsletter` channel IDs |
| `sample_content_calendar.csv` | Example sheet rows |
| `.env.example` | Agent B env vars |
| `README.md` | This guide |

Wired into the main bot:

- `app/config.py` — Agent B settings
- `app/main.py` — `/cron/content-posts` and `/admin/channels`
- root `.env.example` — same variables documented
- `run_once.py` — run one tick locally without HTTP
- `list_channels.py` — fetch `@newsletter` channel IDs
- `sample_content_calendar.csv` — example rows to paste into the sheet

## Sheet columns (`ContentCalendar`)

| Column | Example | Meaning |
|--------|---------|---------|
| date | 2026-09-22 | Post day |
| time | 10:00 | In `CONTENT_TIMEZONE` (default Asia/Karachi) |
| target | both | `group` / `channel` / `both` |
| type | video | `text` / `image` / `video` / `voice` / `audio` / `document` |
| file_url | local path **or** Drive file link | PC path / Drive link from shared folder |
| caption | Day 1 tip… | Caption or text body |
| status | pending | `pending` → `posted` / `failed` |
| posted_at | | Filled by bot |
| notes | | Success ids or error text |

The bot **creates the tab + headers** on first run if missing (same spreadsheet as `GOOGLE_SHEET_ID`).

## Env vars you must set

| Variable | Purpose |
|----------|---------|
| `WHAPI_GROUP_ID` | Already used by Agent A (`...@g.us`) |
| `WHAPI_CHANNEL_ID` | Channel id (`...@newsletter`) — use `GET /admin/channels` |
| `GOOGLE_SHEET_CONTENT_WORKSHEET` | Default `ContentCalendar` |
| `CONTENT_TIMEZONE` | Default `Asia/Karachi` |
| `CONTENT_POSTER_DELAY_SECONDS` | Pause between posts |
| `GOOGLE_DRIVE_CONTENT_FOLDER_ID` | Optional: only allow files in this Drive folder |

## Local file paths (PC only)

You can put a Windows path in `file_url`, e.g.:

`F:\Dropbox\The Syndicate\syn 1\C0052.MP4`

(no quotes)

Works with local:

```powershell
.\.venv\Scripts\python.exe -m sub_agent_b.run_once
```

**Does NOT work on Railway** — use Google Drive (below) for live.

## Google Drive — one shared folder (recommended for Railway)

One-time setup:

1. Create a Drive folder (e.g. `WA Content`).
2. Share it with your service account as **Viewer**:  
   `syndicate-sheet-bot@syndicate-whatsapp-bot.iam.gserviceaccount.com`
3. Copy the folder ID from the URL:  
   `https://drive.google.com/drive/folders/FOLDER_ID`
4. Set in `.env` / Railway:

```
GOOGLE_DRIVE_CONTENT_FOLDER_ID=FOLDER_ID
```

Daily use:

1. Drop the mp4/image into that folder (no per-file share).
2. Right-click file → **Copy link** → paste into sheet `file_url`.
3. Set `status=pending` — Railway cron or local `run_once` will download via Drive API and post.

If `GOOGLE_DRIVE_CONTENT_FOLDER_ID` is set, files outside that folder are rejected.

Videos must stay under ~**48 MB** after compress (Agent B auto-compresses when possible).

## How you use it (simple)

1. Put a **local path** (PC) or **Drive / https** URL in `file_url`
2. Set `type` = `text` | `image` | `video` | `voice` | `audio`
3. Set `target` = `both` (group + channel), `status` = `pending`
4. Agent B downloads → ffmpeg for video → uploads to group + channel

TikTok page links are unreliable — put the file in the Drive folder instead.

## Checklist

1. Share the spreadsheet with the service account (already done for Leads).
2. Share the **content Drive folder** once (steps above).
3. Set `WHAPI_CHANNEL_ID` (and confirm `WHAPI_GROUP_ID`).
4. Set `GOOGLE_DRIVE_CONTENT_FOLDER_ID` for live.
5. Add a test row (`status=pending`) and run local `run_once` or Railway cron `/cron/content-posts`.
6. Check group/channel + sheet status → `posted`.

## Railway cron

Same pattern as booking reminders: hit `/cron/content-posts` every 5–15 minutes.
