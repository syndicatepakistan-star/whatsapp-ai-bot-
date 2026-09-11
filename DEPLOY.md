# Make this bot LIVE (Railway) — no ngrok

Your website and this bot must both be on the public internet.

Website  --POST-->  https://YOUR-BOT.up.railway.app/webhook/lead  -->  Sheet / WhatsApp

---

## 1. Push this project to GitHub

1. Create a new GitHub repo (e.g. `syndicate-whatsapp-bot`).
2. From this folder:

```bat
cd /d "F:\Subhan\Whatsapp bot"
git init
git add .
git commit -m "Initial WhatsApp lead bot"
git branch -M main
git remote add origin https://github.com/YOUR_USER/syndicate-whatsapp-bot.git
git push -u origin main
```

(Do not commit `.env` or `credentials/*.json` — already in `.gitignore`.)

---

## 2. Deploy on Railway

1. Go to https://railway.app → login.
2. **New Project** → **Deploy from GitHub repo** → select this repo.
3. Open the service → **Settings** → **Networking** → **Generate Domain**.
4. Copy the public URL, e.g. `https://syndicate-whatsapp-bot-production.up.railway.app`

---

## 3. Add environment variables (Railway → Variables)

```env
DEFAULT_PHONE_REGION=PK
GOOGLE_SHEET_ID=your_sheet_id
GOOGLE_SHEET_WORKSHEET=Leads
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account",...full json...}
WHATSAPP_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_TEMPLATE_NAME=
WHATSAPP_TEMPLATE_LANGUAGE=en
WHATSAPP_TEMPLATE_HAS_NAME_PARAM=true
WEBHOOK_SECRET=
```

For Google: open your service-account `.json`, copy **all** of it into `GOOGLE_SERVICE_ACCOUNT_JSON` (one line is fine).  
Share the Google Sheet with the service account email (Editor).

Redeploy after saving variables.

---

## 4. Confirm the bot is live

Open in browser:

`https://YOUR-BOT.up.railway.app/health`

Should return: `{"status":"ok"}`

Test webhook:

```bat
curl -X POST https://YOUR-BOT.up.railway.app/webhook/lead -H "Content-Type: application/json" -d "{\"name\":\"Test User\",\"email\":\"test@example.com\",\"phone\":\"+923001234567\",\"source\":\"test\"}"
```

Check Railway **Logs** for `Lead received`.

---

## 5. Link your live website

On Syndicate Website production backend `.env` / Railway variables:

```env
LEAD_WEBHOOK_URL=https://YOUR-BOT.up.railway.app/webhook/lead
```

Redeploy the website backend.

Remove any `127.0.0.1` or ngrok URL.

---

## 6. End-to-end check

1. Submit a quiz lead on the live site (with phone).
2. Railway bot logs → `Lead received`
3. Google Sheet → new row
4. WhatsApp → template sent (once Whapi/Meta credentials are set)

---

## Whapi note

This bot currently uses Meta-style WhatsApp Cloud fields.  
If you use **Whapi**, say so and we can switch the send code to Whapi’s API token + base URL — deploy steps above stay the same.
