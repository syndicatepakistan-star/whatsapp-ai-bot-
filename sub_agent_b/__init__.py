"""Sub-agent B: sheet-driven WhatsApp group/channel content poster."""

# Keep imports lazy so tools like list_channels work without gspread loaded.
__all__ = ["ContentPoster", "ContentRunResult"]


def __getattr__(name: str):
    if name in {"ContentPoster", "ContentRunResult"}:
        from sub_agent_b.poster import ContentPoster, ContentRunResult

        return ContentPoster if name == "ContentPoster" else ContentRunResult
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
