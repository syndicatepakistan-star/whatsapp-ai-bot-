# Bulk send to existing quiz users

New leads are sent automatically via webhook. For **old leads already in the database**, use one of these:

---

## Option A — Django command (recommended)

Runs on your **Syndicate Website backend** (production or local with prod DB).

### 1. Preview first (no messages sent)

```bash
cd Backend
python manage.py bulk_send_quiz_whatsapp --dry-run
```

### 2. Send to all users with a phone

```bash
python manage.py bulk_send_quiz_whatsapp --delay 4
```

- `--delay 4` = wait 4 seconds between each person (helps avoid Whapi limits)
- `--limit 10` = only first 10 (good for testing)
- `--skip-id 42` = resume after user id 42 if a run stopped

### Requirements

- `LEAD_WEBHOOK_URL=https://web-production-7da44.up.railway.app/webhook/lead` on Django env
- Bot live on Railway with Whapi + Sheet configured

Each user gets the same flow: validate → Sheet row → WhatsApp message.

**Warning:** This re-messages people who already received WhatsApp. Test with `--limit 5` first.

---

## Option B — CSV export + local script

1. Django admin → Quiz users → **Export filtered** (CSV)
2. Save file e.g. `leads.csv`
3. From WhatsApp bot folder:

```powershell
cd "F:\Subhan\Whatsapp bot"
.\.venv\Scripts\python.exe scripts\bulk_send_leads.py --csv leads.csv --dry-run
.\.venv\Scripts\python.exe scripts\bulk_send_leads.py --csv leads.csv --delay 4
```

Uses Whapi credentials from your local `.env` (not the webhook).

---

## Tips

| Tip | Why |
|-----|-----|
| Start with `--limit 5` | Confirm message + sheet look correct |
| Use `--delay 3` to `5` | Safer for Whapi / WhatsApp |
| Check **Leads** tab in Sheet | See `wrong number` / `manual follow-up` / `sent` |
| Run during business hours | Better reply rates |
