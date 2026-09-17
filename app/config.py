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

    default_phone_region: str = "PK"

    google_service_account_file: str = "credentials/google-service-account.json"
    # Paste full service-account JSON as one line (preferred on Railway)
    google_service_account_json: str = ""
    google_sheet_id: str = ""
    google_sheet_worksheet: str = "Leads"

    # Used to build intake links when website does not send intake_url
    intake_base_url: str = "https://the-syndicate.com"

    # Whapi.Cloud
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
