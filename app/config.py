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
    whapi_message_text: str = (
        "Hi {name}, your Syn Diagnosis follow-up is ready.\n"
        "Open your form here: {intake_url}"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
