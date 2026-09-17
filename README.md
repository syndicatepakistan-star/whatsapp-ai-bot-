# Syndicate Lead → WhatsApp Bot (Python)

Receives quiz leads from your website webhook, then:

1. Validates the phone number  
2. If invalid → Google Sheet (`wrong number`)  
3. If valid but not on WhatsApp → Google Sheet (`manual follow-up needed`)  
4. If on WhatsApp → sends an approved WhatsApp **template** message  

## Webhook purpose (simple)

| Piece | Role |
|--------|------|
| Your website | When lead is saved, POSTs name/email/phone |
| This app (`POST /webhook/lead`) | Receives that POST and runs the workflow |

The webhook URL is the bridge. Example after deploy:

`https://YOUR-SERVER/webhook/lead`

Local test URL:

`http://127.0.0.1:8080/webhook/lead`

---

## Setup

### 1. Install

```powershell
cd "F:\Subhan\Whatsapp bot"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

### 2. Google Sheet

1. Create a Google Cloud service account → download JSON key.  
2. Save it as `credentials/google-service-account.json`.  
3. Create a Google Sheet; share it with the service account **email** (Editor).  
4. Put the Sheet ID in `.env` as `GOOGLE_SHEET_ID`  
   (from `https://docs.google.com/spreadsheets/d/SHEET_ID/edit`).  
5. First row headers are created automatically:  
   `timestamp, name, email, phone, status, notes`

## WhatsApp (Whapi)

Uses **Whapi.Cloud** (`WHAPI_TOKEN`, `WHAPI_API_URL`, `WHAPI_MESSAGE_TEXT`).

Message placeholders: `{name}`, `{email}`, `{intake_url}`  
(Bot uses website `intake_url` if sent, otherwise builds from email.)

See **SETUP.md** for step-by-step Google Sheet + Whapi + Railway.

### 4. Run

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Health check: open `http://127.0.0.1:8080/health`

### 5. Test webhook

```powershell
curl -X POST http://127.0.0.1:8080/webhook/lead -H "Content-Type: application/json" -d "{\"name\":\"Test User\",\"email\":\"test@example.com\",\"phone\":\"+923001234567\",\"source\":\"syn_diagnosis_quiz\"}"
```

---

## Website connection

On The Syndicate Website Django backend, set:

```env
LEAD_WEBHOOK_URL=http://127.0.0.1:8080/webhook/lead
```

(Use your public HTTPS URL in production.)

Your site should POST:

```json
{
  "name": "...",
  "email": "...",
  "phone": "...",
  "intake_url": "https://the-syndicate.com/quiz/intake?email=...",
  "source": "syn_diagnosis_quiz"
}
```

(`intake_url` is optional — bot can build it from email.)
Optional header if you set `WEBHOOK_SECRET` here:

`X-Webhook-Secret: your-secret`

---

## Audit booking WhatsApp (Meet link + reminder)

Website sets:

```env
BOOKING_WEBHOOK_URL=https://YOUR-SERVER/webhook/booking
BOOKING_WEBHOOK_SECRET=   # optional; match WEBHOOK_SECRET here
```

After a successful book, the website POSTs:

```json
{
  "name": "...",
  "email": "...",
  "phone": "+44...",
  "meet_link": "https://meet.google.com/...",
  "slot_start": "2026-09-23T10:00:00Z",
  "slot_end": "2026-09-23T10:30:00Z",
  "timezone": "Asia/Karachi",
  "source": "audit_booking",
  "booking_id": 12
}
```

Bot sends Whapi message and logs to Google Sheet tab **Bookings**.

### Reminders

Hit every 5–15 minutes (Railway cron):

`GET or POST https://YOUR-SERVER/cron/reminders`  
Header: `X-Cron-Secret: YOUR_CRON_SECRET` (or `X-Webhook-Secret` if using `WEBHOOK_SECRET`)

Sends reminder when the slot is within `BOOKING_REMINDER_MINUTES_BEFORE` (default 60).

---

## Deploy tip

Use Railway / Render / any VPS. Expose HTTPS. Point `LEAD_WEBHOOK_URL` on the website to that public URL.

Without WhatsApp credentials configured, existence-check is skipped in a limited way and send will fail → row logged as `whatsapp send failed` (so Sheets still works while you finish Meta setup).
