# Setup guide — Google Sheet + Whapi (easy steps)

Bot is switched to **Whapi**. Website still calls:
`https://web-production-7da44.up.railway.app/webhook/lead`

---

## A) Google Sheet (3 values)

### 1. Create the sheet
1. Open [Google Sheets](https://sheets.google.com) → blank spreadsheet.
2. Name it e.g. `Syndicate Leads`.
3. Copy the **Sheet ID** from the URL:

`https://docs.google.com/spreadsheets/d/GOOGLE_SHEET_ID_HERE/edit`

→ Railway variable: `GOOGLE_SHEET_ID=GOOGLE_SHEET_ID_HERE`  
→ `GOOGLE_SHEET_WORKSHEET=Leads` (bot creates this tab if missing)

### 2. Create a Google Cloud service account
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create/select a project
3. **APIs & Services** → enable **Google Sheets API** and **Google Drive API**
4. **IAM & Admin** → **Service Accounts** → **Create**
5. Open the service account → **Keys** → **Add key** → **JSON** → download file
6. Open that JSON file → copy **everything**
7. Railway → `GOOGLE_SERVICE_ACCOUNT_JSON` = paste that full JSON
8. In the same JSON, find `client_email` (ends with `.iam.gserviceaccount.com`)

### 3. Share the sheet
1. Open your Google Sheet → **Share**
2. Paste the `client_email`
3. Role: **Editor** → Send/Save

---

## B) Whapi (1 main value)

### 1. Connect WhatsApp in Whapi
1. Login at [whapi.cloud](https://whapi.cloud)
2. Create/open a **channel**
3. Scan QR so your WhatsApp is **connected / AUTH**

### 2. Copy API token
1. Channel dashboard → copy **API Token** (Bearer token)
2. Railway → `WHAPI_TOKEN=that_token`
3. Railway → `WHAPI_API_URL=https://gate.whapi.cloud`
4. Optional message text:

```env
WHAPI_MESSAGE_TEXT=Hi {name}, thanks for completing the Syn Diagnosis quiz. We'll be in touch shortly.
```

### 3. Whapi “Webhooks” page (your screenshot)
**Leave URL empty** for now.  
That page is for *incoming* WhatsApp events. Lead flow does not need it.

---

## C) Railway variables checklist

```env
DEFAULT_PHONE_REGION=PK
GOOGLE_SHEET_ID=...
GOOGLE_SHEET_WORKSHEET=Leads
GOOGLE_SERVICE_ACCOUNT_JSON={...full json...}
WHAPI_TOKEN=...
WHAPI_API_URL=https://gate.whapi.cloud
WHAPI_MESSAGE_TEXT=Hi {name}, thanks for completing the Syn Diagnosis quiz. We'll be in touch shortly.
```

Redeploy the Railway bot after saving.

---

## D) Test

1. Open `https://web-production-7da44.up.railway.app/health` → `ok`
2. Submit one quiz lead on the live site (phone with `+` country code)
3. Railway **Logs** → `Lead received`
4. Google Sheet → new row
5. WhatsApp → message received (if number exists on WA)

| Sheet status | Meaning |
|--------------|---------|
| wrong number | Invalid phone format |
| manual follow-up needed | Not on WhatsApp |
| whatsapp message sent | Success |
| whatsapp send failed | Token / Whapi / message error |

---

## How checks work now (Whapi)

1. **Valid number?** → `phonenumbers` library (US/UK/CA/PK… if `+` country code present)
2. **On WhatsApp?** → Whapi `POST /contacts` (`valid` / `invalid`)
3. **Send?** → Whapi `POST /messages/text`
