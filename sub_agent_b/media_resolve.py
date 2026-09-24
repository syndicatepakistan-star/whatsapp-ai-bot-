"""
Resolve sheet file_url into a local file Whapi can upload as playable media.

Supported sources (in order):
  1. Local PC path (run_once on your machine only)
  2. Google Drive file link (service account + one shared folder)
  3. Social page URL (yt-dlp — unreliable for TikTok)
  4. Direct https file URL

Videos are remuxed/compressed with ffmpeg to WhatsApp-friendly H.264 mp4.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials

from app.config import Settings

logger = logging.getLogger(__name__)

# Keep under Cloudflare/Whapi practical upload limit (413 happened ~100MB+).
MAX_UPLOAD_BYTES = 48 * 1024 * 1024

_DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive.readonly",)

_SOCIAL_HOST_HINTS = (
    "tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "instagram.com",
    "instagr.am",
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "fb.watch",
    "fb.com",
    "twitter.com",
    "x.com",
    "threads.net",
    "snapchat.com",
    "vimeo.com",
    "dailymotion.com",
    "reddit.com",
    "linkedin.com",
)

_DRIVE_ID_RE = re.compile(r"(?:/file/d/|id=|/d/)([a-zA-Z0-9_-]{10,})")
_DRIVE_FOLDER_ID_RE = re.compile(r"/folders/([a-zA-Z0-9_-]{10,})")


@dataclass
class ResolvedMedia:
    path: Path
    filename: str
    content_type: str = "application/octet-stream"
    source: str = ""
    cleanup_paths: list[Path] | None = None


def is_social_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return any(host == h or host.endswith("." + h) for h in _SOCIAL_HOST_HINTS)


def extract_drive_file_id(url: str) -> str | None:
    text = (url or "").strip()
    if not text:
        return None
    if "drive.google.com" not in text and "docs.google.com" not in text:
        return None
    # Folder links are not downloadable media
    if "/folders/" in text:
        return None
    match = _DRIVE_ID_RE.search(text)
    return match.group(1) if match else None


def extract_drive_folder_id(value: str) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if "drive.google.com" in text:
        match = _DRIVE_FOLDER_ID_RE.search(text)
        return match.group(1) if match else None
    # Bare folder id
    if re.fullmatch(r"[a-zA-Z0-9_-]{10,}", text):
        return text
    return None


def is_local_path(value: str) -> bool:
    """True for Windows/Unix filesystem paths (not http/https)."""
    text = (value or "").strip().strip('"').strip("'")
    if not text:
        return False
    lower = text.lower()
    if lower.startswith("http://") or lower.startswith("https://"):
        return False
    if lower.startswith("file:"):
        return True
    # Windows: F:\... or F:/... or \\server\share
    if re.match(r"^[a-zA-Z]:[\\/]", text):
        return True
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    # Unix absolute or relative existing path checked later
    if text.startswith("/") or text.startswith("./") or text.startswith("../"):
        return True
    # Bare relative path with extension (e.g. media/clip.mp4)
    if re.search(r"\.(mp4|mov|mkv|webm|avi|jpg|jpeg|png|gif|webp|ogg|mp3|m4a|wav|pdf)$", text, re.I):
        return True
    return False


def normalize_local_path(value: str) -> Path:
    text = (value or "").strip().strip('"').strip("'")
    if text.lower().startswith("file:"):
        # file:///F:/path or file://F:/path
        parsed = urlparse(text)
        path = parsed.path or ""
        # On Windows file:///F:/x → /F:/x
        if re.match(r"^/[a-zA-Z]:", path):
            path = path[1:]
        text = path
    return Path(text).expanduser()


class MediaResolver:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def resolve(self, file_url: str, *, media_type: str = "video") -> ResolvedMedia:
        url = (file_url or "").strip().strip('"').strip("'")
        if not url:
            raise ValueError("empty_file_url")

        kind = (media_type or "video").strip().lower()
        cleanup: list[Path] = []

        # 1) Local path → Drive (SA) → social → http
        if is_local_path(url):
            downloaded = self._resolve_local_path(url, media_type=kind)
        elif extract_drive_file_id(url):
            downloaded = self._resolve_drive(url, media_type=kind)
        elif is_social_url(url):
            downloaded = self._resolve_ytdlp(url, media_type=kind)
        else:
            try:
                downloaded = self._download_http(url, media_type=kind, source="http")
            except Exception as http_exc:
                logger.warning("Direct download failed (%s) — trying yt-dlp", http_exc)
                downloaded = self._resolve_ytdlp(url, media_type=kind)

        # Never delete the user's original local file — only temp downloads / ffmpeg outs
        if downloaded.source != "local_path":
            cleanup.append(downloaded.path)
        if downloaded.cleanup_paths:
            cleanup.extend(downloaded.cleanup_paths)

        # 2) Make videos WhatsApp-playable + small enough to upload
        if kind == "video":
            playable = self._ensure_playable_video(downloaded.path)
            if playable.path != downloaded.path:
                cleanup.append(playable.path)
            result = ResolvedMedia(
                path=playable.path,
                filename=playable.filename,
                content_type="video/mp4",
                source=f"{downloaded.source}+ffmpeg",
                cleanup_paths=cleanup,
            )
            return result

        if kind == "image":
            return ResolvedMedia(
                path=downloaded.path,
                filename=downloaded.filename,
                content_type=downloaded.content_type or "image/jpeg",
                source=downloaded.source,
                cleanup_paths=cleanup,
            )

        return ResolvedMedia(
            path=downloaded.path,
            filename=downloaded.filename,
            content_type=downloaded.content_type,
            source=downloaded.source,
            cleanup_paths=cleanup,
        )

    def _resolve_local_path(self, value: str, *, media_type: str) -> ResolvedMedia:
        path = normalize_local_path(value)
        if not path.is_file():
            raise RuntimeError(
                f"local_file_not_found: {path} — "
                "path must exist on THIS machine (local run only). "
                "On Railway use an https URL instead."
            )
        size = path.stat().st_size
        if size < 64:
            raise RuntimeError(f"local_file_too_small: {path}")
        logger.info("Using local file path=%s bytes=%s", path, size)
        return ResolvedMedia(
            path=path,
            filename=path.name,
            content_type="application/octet-stream",
            source="local_path",
            # Do NOT delete the user's original file — only ffmpeg outputs later
            cleanup_paths=[],
        )

    def _load_drive_credentials(self) -> Credentials:
        raw_json = (self.settings.google_service_account_json or "").strip()
        if raw_json:
            info = json.loads(raw_json)
            return Credentials.from_service_account_info(info, scopes=_DRIVE_SCOPES)

        creds_path = Path(self.settings.google_service_account_file)
        if not creds_path.is_file():
            raise RuntimeError(
                "Google credentials missing for Drive. Set GOOGLE_SERVICE_ACCOUNT_JSON "
                "or GOOGLE_SERVICE_ACCOUNT_FILE."
            )
        return Credentials.from_service_account_file(str(creds_path), scopes=_DRIVE_SCOPES)

    def _drive_access_token(self) -> str:
        creds = self._load_drive_credentials()
        creds.refresh(Request())
        if not creds.token:
            raise RuntimeError("drive_token_missing")
        return creds.token

    def _allowed_drive_folder_id(self) -> str | None:
        return extract_drive_folder_id(
            (self.settings.google_drive_content_folder_id or "").strip()
        )

    def _drive_get_meta(self, file_id: str, token: str) -> dict:
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
        params = {
            "fields": "id,name,mimeType,size,parents,shortcutDetails",
            "supportsAllDrives": "true",
        }
        with httpx.Client(timeout=60.0) as client:
            response = client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"drive_meta_error_{response.status_code}: {response.text[:300]}"
            )
        data = response.json()
        # Resolve shortcuts to the real target file
        shortcut = data.get("shortcutDetails") or {}
        target_id = shortcut.get("targetId")
        if target_id and target_id != file_id:
            return self._drive_get_meta(target_id, token)
        return data

    def _drive_file_in_folder(
        self, file_id: str, folder_id: str, token: str, *, depth: int = 0
    ) -> bool:
        """True if file is inside folder_id (direct or nested, max 8 levels)."""
        if depth > 8:
            return False
        meta = self._drive_get_meta(file_id, token)
        parents = meta.get("parents") or []
        if folder_id in parents:
            return True
        for parent in parents:
            if parent == folder_id:
                return True
            if self._drive_file_in_folder(parent, folder_id, token, depth=depth + 1):
                return True
        return False

    def _resolve_drive(self, url: str, *, media_type: str) -> ResolvedMedia:
        file_id = extract_drive_file_id(url)
        if not file_id:
            raise RuntimeError("drive_file_id_missing")

        try:
            return self._resolve_drive_api(file_id, media_type=media_type)
        except Exception as api_exc:
            logger.warning(
                "Drive API download failed (%s) — falling back to public uc?export",
                api_exc,
            )
            try:
                return self._download_http(url, media_type=media_type, source="drive_http")
            except Exception as http_exc:
                raise RuntimeError(
                    f"drive_download_failed: api={api_exc}; http={http_exc}. "
                    "Share the content folder with the service account as Viewer."
                ) from http_exc

    def _resolve_drive_api(self, file_id: str, *, media_type: str) -> ResolvedMedia:
        token = self._drive_access_token()
        meta = self._drive_get_meta(file_id, token)
        real_id = str(meta.get("id") or file_id)
        name = str(meta.get("name") or f"{real_id}.bin")
        mime = str(meta.get("mimeType") or "application/octet-stream")

        folder_id = self._allowed_drive_folder_id()
        if folder_id and not self._drive_file_in_folder(real_id, folder_id, token):
            raise RuntimeError(
                f"drive_file_not_in_content_folder: file={real_id} "
                f"expected_folder={folder_id}"
            )

        # Google Docs / Sheets etc. are not raw media
        if mime.startswith("application/vnd.google-apps."):
            raise RuntimeError(f"drive_unsupported_google_doc_type: {mime}")

        download_url = (
            f"https://www.googleapis.com/drive/v3/files/{real_id}"
            f"?alt=media&supportsAllDrives=true"
        )
        with httpx.Client(timeout=300.0, follow_redirects=True) as client:
            response = client.get(
                download_url,
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"drive_media_error_{response.status_code}: {response.text[:300]}"
            )
        data = response.content
        if not data or len(data) < 64:
            raise RuntimeError("drive_empty_download")

        suffix = Path(name).suffix or (
            ".mp4" if media_type == "video" else ".bin"
        )
        tmp = Path(tempfile.mkstemp(suffix=suffix)[1])
        tmp.write_bytes(data)
        logger.info(
            "Drive API downloaded id=%s name=%s bytes=%s",
            real_id,
            name,
            len(data),
        )
        return ResolvedMedia(
            path=tmp,
            filename=name,
            content_type=mime,
            source="drive_api",
            cleanup_paths=[],
        )

    def _ffmpeg_bin(self) -> str:
        found = shutil.which("ffmpeg")
        if found:
            return found
        for candidate in (
            "/usr/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "/nix/var/nix/profiles/default/bin/ffmpeg",
        ):
            if Path(candidate).is_file():
                return candidate
        return "ffmpeg"

    def _ensure_playable_video(self, src: Path) -> ResolvedMedia:
        """
        Remux/transcode to H.264 + AAC mp4 so WhatsApp can play it,
        and compress if over MAX_UPLOAD_BYTES.
        """
        size = src.stat().st_size
        # Fast path: already small mp4 with ftyp — still remux for compatibility
        out = Path(tempfile.mkstemp(suffix=".mp4")[1])

        # Pass 1: try stream copy remux (fast) if already mp4-ish and small enough
        if src.suffix.lower() == ".mp4" and size <= MAX_UPLOAD_BYTES:
            remux_ok = self._run_ffmpeg(
                [
                    self._ffmpeg_bin(),
                    "-y",
                    "-i",
                    str(src),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(out),
                ]
            )
            if remux_ok and out.is_file() and out.stat().st_size > 1000:
                if self._looks_like_mp4(out):
                    return ResolvedMedia(
                        path=out,
                        filename=src.stem[:50] + ".mp4",
                        content_type="video/mp4",
                        source="ffmpeg_remux",
                    )

        # Pass 2: re-encode H.264/AAC, scale down if needed, target under size cap
        crf = "28"
        if size > 80 * 1024 * 1024:
            crf = "30"
        if size > 120 * 1024 * 1024:
            crf = "32"

        encode_ok = self._run_ffmpeg(
            [
                self._ffmpeg_bin(),
                "-y",
                "-i",
                str(src),
                "-vf",
                "scale='min(1280,iw)':-2",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                crf,
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                "-pix_fmt",
                "yuv420p",
                str(out),
            ]
        )
        if not encode_ok or not out.is_file() or out.stat().st_size < 1000:
            raise RuntimeError("ffmpeg_failed_to_create_playable_mp4")

        # If still too big, one more aggressive pass
        if out.stat().st_size > MAX_UPLOAD_BYTES:
            out2 = Path(tempfile.mkstemp(suffix=".mp4")[1])
            ok2 = self._run_ffmpeg(
                [
                    self._ffmpeg_bin(),
                    "-y",
                    "-i",
                    str(out),
                    "-vf",
                    "scale='min(720,iw)':-2",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "32",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "96k",
                    "-movflags",
                    "+faststart",
                    "-pix_fmt",
                    "yuv420p",
                    str(out2),
                ]
            )
            try:
                out.unlink(missing_ok=True)
            except Exception:
                pass
            if not ok2 or not out2.is_file():
                raise RuntimeError("ffmpeg_compress_failed")
            out = out2

        if out.stat().st_size > MAX_UPLOAD_BYTES:
            raise RuntimeError(
                f"video_still_too_large_after_compress bytes={out.stat().st_size} "
                f"max={MAX_UPLOAD_BYTES}"
            )

        if not self._looks_like_mp4(out):
            raise RuntimeError("ffmpeg_output_not_valid_mp4")

        logger.info("Playable mp4 ready bytes=%s", out.stat().st_size)
        return ResolvedMedia(
            path=out,
            filename=(src.stem[:50] or "video") + ".mp4",
            content_type="video/mp4",
            source="ffmpeg_encode",
        )

    def _run_ffmpeg(self, cmd: list[str]) -> bool:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            if proc.returncode != 0:
                logger.warning("ffmpeg failed code=%s stderr=%s", proc.returncode, (proc.stderr or "")[-500:])
                return False
            return True
        except FileNotFoundError:
            raise RuntimeError("ffmpeg_not_found — install ffmpeg and add to PATH")
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg timed out")
            return False

    def _looks_like_mp4(self, path: Path) -> bool:
        head = path.read_bytes()[:64]
        return b"ftyp" in head

    def _resolve_ytdlp(self, url: str, *, media_type: str) -> ResolvedMedia:
        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError("yt-dlp is not installed. Run: pip install yt-dlp") from exc

        tmp_dir = Path(tempfile.mkdtemp(prefix="agentb_ytdlp_"))
        outtmpl = str(tmp_dir / "%(id)s.%(ext)s")
        ydl_opts: dict = {
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "restrictfilenames": True,
            "format": (
                "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/"
                "b[ext=mp4]/bv*+ba/b"
            ),
            "merge_output_format": "mp4",
            # Download can be larger; we compress afterward for upload.
            "max_filesize": 200 * 1024 * 1024,
        }
        if media_type in {"audio", "voice"}:
            ydl_opts["format"] = "bestaudio/best"
            ydl_opts["merge_output_format"] = "mp3"

        info = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info is None:
                    raise RuntimeError("ytdlp_no_info")
                if info.get("entries"):
                    entries = [e for e in info["entries"] if e]
                    if not entries:
                        raise RuntimeError("ytdlp_empty_playlist")
                    info = entries[0]
                prepared = Path(ydl.prepare_filename(info))
                candidates = [
                    prepared,
                    prepared.with_suffix(".mp4"),
                    prepared.with_suffix(".mp3"),
                    prepared.with_suffix(".m4a"),
                    prepared.with_suffix(".webm"),
                ]
        except Exception as exc:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise RuntimeError(f"ytdlp_failed: {exc}") from exc

        path = next((c for c in candidates if c.is_file()), None)
        if path is None:
            files = [p for p in tmp_dir.iterdir() if p.is_file()]
            if not files:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise RuntimeError("ytdlp_no_output_file")
            path = max(files, key=lambda p: p.stat().st_size)

        size = path.stat().st_size
        if size < 1000:
            raise RuntimeError(f"ytdlp_file_too_small bytes={size}")

        title = (info.get("title") or path.stem or "social_video") if isinstance(info, dict) else path.stem
        safe = re.sub(r"[^\w\-]+", "_", str(title))[:60] or "social_video"
        filename = f"{safe}{path.suffix or '.mp4'}"

        logger.info("yt-dlp downloaded bytes=%s file=%s", size, filename)
        return ResolvedMedia(
            path=path,
            filename=filename,
            content_type="video/mp4" if path.suffix.lower() == ".mp4" else "application/octet-stream",
            source="ytdlp",
            cleanup_paths=[tmp_dir],
        )

    def _download_http(
        self,
        url: str,
        *,
        media_type: str,
        source: str,
        preferred_name: str = "",
    ) -> ResolvedMedia:
        # If it's a Drive view link, use export=download (no service-account share required
        # when "anyone with link" works for small files). Prefer social/direct URLs.
        drive_id = extract_drive_file_id(url)
        if drive_id:
            url = f"https://drive.google.com/uc?export=download&id={drive_id}&confirm=t"

        with httpx.Client(timeout=180.0, follow_redirects=True) as client:
            response = client.get(url)
            if response.status_code >= 400:
                raise RuntimeError(f"http_download_error_{response.status_code}")

            data = response.content
            if not data or len(data) < 64:
                raise RuntimeError("http_empty_download")

            head = data[:200].lstrip().lower()
            if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
                raise RuntimeError(
                    "url_returned_html_not_media — use a direct media URL or TikTok/YouTube link"
                )

            ctype = (response.headers.get("content-type") or "").split(";")[0].strip()
            name = preferred_name or _filename_from_url(url, media_type)
            suffix = Path(name).suffix or _suffix_for_type(media_type)
            if not Path(name).suffix:
                name = f"{name}{suffix}"

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(data)
        tmp.close()
        logger.info("HTTP downloaded source=%s bytes=%s", source, len(data))
        return ResolvedMedia(
            path=Path(tmp.name),
            filename=name,
            content_type=ctype or "application/octet-stream",
            source=source,
        )


def _suffix_for_type(media_type: str) -> str:
    return {
        "image": ".jpg",
        "video": ".mp4",
        "voice": ".ogg",
        "audio": ".mp3",
        "document": ".bin",
    }.get((media_type or "").lower(), ".bin")


def _filename_from_url(url: str, media_type: str) -> str:
    path = url.split("?")[0].rstrip("/").split("/")[-1]
    if path and "." in path:
        return path
    return f"media{_suffix_for_type(media_type)}"
