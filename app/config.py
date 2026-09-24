from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8080
    webhook_secret: str = ""

    default_phone_region: str = "UK"

    google_service_account_file: str = "credentials/google-service-account.json"
    # Paste full service-account JSON as one line (preferred on Railway)
    google_service_account_json: str = ""
    google_sheet_id: str = ""
    google_sheet_worksheet: str = "Leads"

    # Used to build intake links when website does not send intake_url
    intake_base_url: str = "https://the-syndicate.com"

    # Whapi.Cloud (set WHAPI_TOKEN in .env / Railway — do not hardcode)
    whapi_token: str = ""
    whapi_api_url: str = "https://gate.whapi.cloud"
    # Placeholders: {name}, {intake_url}, {email}
    # Use \n for line breaks in Railway single-line env values
    whapi_message_text: str = (
        "Hi {name},\n\n"
        "Just complete the quick form and we will schedule the exclusive audit "
        "with our founder for some time this week\n\n"
        "See you on the inside\n\n"
        "With Honour\n"
        "The Syndicate"
    )
    # true = send link first (rich preview card), then text without raw URL
    whapi_link_separate: bool = True

    # Audit booking confirmation message.
    # Placeholders: {name} {email} {meet_link} {slot_local} {timezone}
    whapi_booking_message_text: str = (
        "Hi {name},\n\n"
        "Your founder audit is booked.\n\n"
        "Time: {slot_local}\n"
        "Join with Google Meet:\n{meet_link}\n\n"
        "With Honour\n"
        "The Syndicate"
    )
    # Reminder message. Same placeholders.
    whapi_booking_reminder_text: str = (
        "Hi {name},\n\n"
        "Reminder: your founder audit starts soon.\n\n"
        "Time: {slot_local}\n"
        "Join here:\n{meet_link}\n\n"
        "With Honour\n"
        "The Syndicate"
    )
    # Minutes before slot_start to send reminder (default 60).
    booking_reminder_minutes_before: int = 60
    # Worksheet name for audit bookings (same spreadsheet as leads).
    google_sheet_bookings_worksheet: str = "Bookings"
    # Optional shared secret for /cron/reminders (Railway cron header).
    cron_secret: str = ""

    # Sub-agent A: direct add to WhatsApp group after successful lead WA message.
    # Group ID from GET /groups, e.g. 120363...@g.us
    whapi_group_id: str = ""
    # true = attempt direct add after WhatsApp message is sent
    whapi_group_add_enabled: bool = True
    # Optional fallback invite link if WhatsApp privacy blocks direct add
    wa_group_invite_url: str = ""
    # Optional channel follow/invite link (sent in same fallback DM)
    wa_channel_invite_url: str = ""
    # Fallback DM when direct add fails. Placeholders: {name} {group_link} {channel_link}
    whapi_group_invite_fallback_text: str = (
        "Hi {name},\n\n"
        "Welcome inside The Syndicate.\n"
        "Join our private group here:\n{group_link}\n\n"
        "Follow the channel here:\n{channel_link}\n\n"
        "With Honour\n"
        "The Syndicate"
    )

    # Sub-agent B: sheet → WhatsApp group/channel content poster
    # Channel ID from GET /newsletters or GET /admin/channels (...@newsletter)
    whapi_channel_id: str = ""
    google_sheet_content_worksheet: str = "ContentCalendar"
    content_timezone: str = "Asia/Karachi"
    content_poster_enabled: bool = True
    content_poster_max_per_run: int = 5
    content_poster_delay_seconds: float = 2.0
    # Optional: only allow Drive files inside this folder (share folder once with SA)
    # Folder ID from drive.google.com/drive/folders/FOLDER_ID
    google_drive_content_folder_id: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
