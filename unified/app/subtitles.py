"""Subtitles — the one job Bazarr did, in the same process as everything else.

Bazarr existed to answer one question per video file: *does a subtitle in my
language sit next to this file, and if not, can I find one?* That is a small
amount of work around a slow network call, so it lives here:

  * the video is identified the way subtitle sites expect — an OpenSubtitles
    hash (size plus the first and last 64 KiB) *and* a parsed title/year, so a
    hash miss still finds the release;
  * the file is written next to the video as `Name.en.srt`, the naming every
    player already understands, and never overwrites a subtitle that exists;
  * what was tried is recorded, so a file that genuinely has no subtitle is not
    re-queried every pass;
  * after an import the same routine runs for the new file only.

The provider is a small interface on purpose. `OpenSubtitles` is the one that
ships; adding another is a class with `search()` and `download()`, not a second
service.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Protocol

import httpx

from . import db, library
from .parser import parse

log = logging.getLogger("core.subtitles")

SUBTITLE_EXT = {".srt", ".sub", ".ass", ".ssa", ".vtt"}
# Two-letter codes plus the handful of three-letter ones subtitle sites use.
LANGUAGE_CODES = {
    "en", "es", "fr", "de", "it", "pt", "nl", "pl", "ru", "tr", "ar", "he",
    "sv", "no", "da", "fi", "cs", "sk", "hu", "ro", "el", "uk", "bg", "hr",
    "sr", "sl", "et", "lv", "lt", "th", "vi", "id", "ms", "fa", "hi", "bn",
    "ta", "te", "zh", "ja", "ko",
}
# Only ISO 639-2 codes are aliased. Full words ("English") are deliberately
# absent: they are far more likely to be part of a title than a language tag.
LANGUAGE_ALIASES = {
    "eng": "en", "spa": "es", "esp": "es", "fre": "fr",
    "fra": "fr", "ger": "de", "deu": "de", "ita": "it", "por": "pt",
    "pob": "pt-br", "dut": "nl", "nld": "nl", "pol": "pl", "rus": "ru",
    "tur": "tr", "ara": "ar", "swe": "sv", "nor": "no", "dan": "da",
    "fin": "fi", "cze": "cs", "ces": "cs", "slo": "sk", "slk": "sk",
    "hun": "hu", "rum": "ro", "ron": "ro", "gre": "el", "ell": "el",
    "ukr": "uk", "chi": "zh", "zho": "zh", "jpn": "ja", "kor": "ko",
}
LANG_TOKEN = re.compile(r"(?:^|[.\s_\[(-])([a-zA-Z]{2,3})(?=[.\s_\])-]|$)")


# ------------------------------------------------------------------ identity

def moviehash(path: Path) -> str:
    """The OpenSubtitles file hash: size + first and last 64 KiB, little-endian.

    Subtitle sites use it to find a subtitle timed for *this exact release*, so
    it is the strongest signal available before any text is compared.
    """
    try:
        size = path.stat().st_size
        if size < 131072:
            return ""
        with path.open("rb") as handle:
            head = handle.read(65536)
            handle.seek(size - 65536)
            tail = handle.read(65536)
    except OSError:
        return ""
    total = size
    for offset in range(0, 65536, 8):
        total += struct.unpack("<Q", head[offset:offset + 8])[0]
        total += struct.unpack("<Q", tail[offset:offset + 8])[0]
    return f"{total & 0xFFFFFFFFFFFFFFFF:016x}"


def normalise_language(value: str) -> str:
    code = (value or "").strip().lower().replace("_", "-")
    if not code:
        return ""
    return LANGUAGE_ALIASES.get(code, code)


def languages_in(name: str) -> list[str]:
    """Languages a file name advertises: `Film.2020.1080p.en.srt` -> ['en']."""
    stem = Path(name).stem
    parts = [p for p in re.split(r"[.\s_\[\]()]+", stem) if p]
    found: list[str] = []
    # Only the trailing tokens count: a title like "English Patient" is not a
    # language tag, but the last one or two segments after the release group
    # usually are.
    for part in reversed(parts[-3:]):
        code = normalise_language(part)
        if code in LANGUAGE_CODES or code.split("-")[0] in LANGUAGE_CODES:
            found.append(code)
    return list(dict.fromkeys(found))


def subtitle_path(video: Path, language: str) -> Path:
    """`movies/Film (2020)/Film.mkv` + `en` -> `.../Film.en.srt`."""
    return video.with_name(f"{video.stem}.{language}.srt")


def existing_subtitles(video: Path) -> dict[str, Path]:
    """Every subtitle sitting next to a video, keyed by language code.

    An untagged `Film.srt` is recorded under `""` so a caller can treat "there
    is a subtitle but nobody said which language" as a real state rather than
    downloading a duplicate.
    """
    found: dict[str, Path] = {}
    try:
        siblings = list(video.parent.iterdir())
    except OSError:
        return found
    for path in siblings:
        if path.suffix.lower() not in SUBTITLE_EXT:
            continue
        if not path.name.startswith(video.stem):
            continue
        tags = languages_in(path.name)
        if tags:
            for tag in tags:
                found.setdefault(tag, path)
        elif path.stem == video.stem:
            found.setdefault("", path)
    return found


def missing_languages(video: Path, languages: Iterable[str]) -> list[str]:
    present = existing_subtitles(video)
    return [lang for lang in languages if lang not in present and lang.split("-")[0] not in present]


# ------------------------------------------------------------------ provider

@dataclass(frozen=True)
class SubtitleRequest:
    video: Path
    title: str
    year: int | None
    kind: str
    hash: str = ""
    size: int = 0
    season: int | None = None
    episode: int | None = None


@dataclass(frozen=True)
class Candidate:
    provider: str
    identifier: str
    language: str
    release: str = ""
    downloads: int = 0
    rating: float = 0.0
    hash_match: bool = False
    hearing_impaired: bool = False
    format: str = "srt"

    def score(self) -> float:
        # A hash match is the only signal that says "timed for this release";
        # everything else is popularity, which mostly correlates with quality.
        return (1000.0 if self.hash_match else 0.0) + min(self.downloads, 5000) / 100.0 + self.rating


class Provider(Protocol):
    name: str

    def search(self, request: SubtitleRequest, languages: list[str]) -> list[Candidate]:
        ...

    def download(self, candidate: Candidate) -> bytes:
        ...


class SubtitleError(RuntimeError):
    pass


class OpenSubtitles:
    """OpenSubtitles.com REST API (a free API key covers a household)."""

    name = "opensubtitles"
    default_base = "https://api.opensubtitles.com/api/v1"

    def __init__(self, api_key: str, username: str = "", password: str = "",
                 user_agent: str = "monarch-core v1.0", timeout: float = 30.0,
                 base: str = "") -> None:
        self.api_key = api_key
        self.username = username
        self.password = password
        self.user_agent = user_agent
        self.timeout = timeout
        # Only overridden for tests and for an on-network mirror; the shipped
        # default is the public API.
        self.base = (base or self.default_base).rstrip("/")
        self.token = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        headers = {"Api-Key": self.api_key, "User-Agent": self.user_agent,
                   "Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def login(self, client: httpx.Client) -> bool:
        if self.token or not (self.username and self.password):
            return bool(self.token)
        response = client.post(f"{self.base}/login", headers=self._headers(),
                               json={"username": self.username, "password": self.password})
        if response.status_code != 200:
            raise SubtitleError(f"login failed: {response.status_code}")
        self.token = str((response.json() or {}).get("token") or "")
        return bool(self.token)

    def search(self, request: SubtitleRequest, languages: list[str]) -> list[Candidate]:
        if not self.configured or not languages:
            return []
        params: dict[str, Any] = {"languages": ",".join(languages), "order_by": "download_count"}
        if request.hash:
            params["moviehash"] = request.hash
            params["moviebytesize"] = str(request.size)
        if request.kind == "episode" and request.season is not None:
            params["query"] = request.title
            params["season_number"] = str(request.season)
            if request.episode is not None:
                params["episode_number"] = str(request.episode)
        else:
            params["query"] = request.title
            if request.year:
                params["year"] = str(request.year)

        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                self.login(client)
                response = client.get(f"{self.base}/subtitles", headers=self._headers(), params=params)
        except (httpx.HTTPError, SubtitleError, OSError) as exc:
            log.warning("opensubtitles search failed for %s: %s", request.video.name, exc)
            return []
        if response.status_code != 200:
            log.warning("opensubtitles search %s for %s", response.status_code, request.video.name)
            return []

        candidates: list[Candidate] = []
        for row in (response.json() or {}).get("data", []):
            attributes = row.get("attributes") or {}
            files = attributes.get("files") or []
            if not files:
                continue
            ratings = attributes.get("ratings") or 0
            candidates.append(Candidate(
                provider=self.name,
                identifier=str(files[0].get("file_id") or ""),
                language=normalise_language(str(attributes.get("language") or "")),
                release=str(attributes.get("release") or ""),
                downloads=int(attributes.get("download_count") or 0),
                rating=float(ratings or 0) / 2.0,
                hash_match=bool(attributes.get("moviehash_match")),
                hearing_impaired=bool(attributes.get("hearing_impaired")),
                format=str(files[0].get("file_name") or "srt").rsplit(".", 1)[-1].lower(),
            ))
        return [c for c in candidates if c.identifier]

    def download(self, candidate: Candidate) -> bytes:
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            self.login(client)
            response = client.post(f"{self.base}/download", headers=self._headers(),
                                   json={"file_id": int(candidate.identifier)})
            if response.status_code == 406:
                raise SubtitleError("daily download quota reached")
            if response.status_code != 200:
                raise SubtitleError(f"download refused: {response.status_code}")
            link = str((response.json() or {}).get("link") or "")
            if not link:
                raise SubtitleError("no download link")
            payload = client.get(link, headers={"User-Agent": self.user_agent})
            if payload.status_code != 200 or not payload.content:
                raise SubtitleError(f"subtitle fetch failed: {payload.status_code}")
            return payload.content


class OpenSubtitlesLegacy:
    """The older OpenSubtitles.org XML-RPC service.

    It needs no API key — an anonymous session is enough — which is why it is the
    fallback: a platform that has never had a subtitle provider gets working
    subtitles without anyone signing up for anything first. The `.com` provider
    is preferred when a key exists because it is the maintained API.
    """

    name = "opensubtitles-legacy"
    default_endpoint = "https://api.opensubtitles.org/xml-rpc"
    user_agent = "Gizmo v1.1"

    def __init__(self, username: str = "", password: str = "",
                 endpoint: str = "", timeout: float = 30.0) -> None:
        self.username = username
        self.password = password
        self.endpoint = endpoint or self.default_endpoint
        self.timeout = timeout
        self.token = ""
        self.configured = True  # anonymous access is a supported session

    def _call(self, method: str, *params: Any) -> Any:
        import xmlrpc.client

        payload = xmlrpc.client.dumps(params, methodname=method, allow_none=True).encode()
        response = httpx.post(
            self.endpoint, content=payload,
            headers={"Content-Type": "text/xml", "User-Agent": self.user_agent},
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise SubtitleError(f"xml-rpc returned {response.status_code}")
        # A methodResponse yields ((value,), None) — one parameter, no name.
        loaded, _method = xmlrpc.client.loads(response.text)
        if isinstance(loaded, (tuple, list)):
            loaded = loaded[0] if loaded else {}
        result = loaded
        if not isinstance(result, dict):
            raise SubtitleError("unexpected xml-rpc reply")
        status = str(result.get("status") or "")
        if not status.startswith("200"):
            raise SubtitleError(f"xml-rpc status {status or 'missing'}")
        return result

    def login(self) -> str:
        if self.token:
            return self.token
        result = self._call("LogIn", self.username, self.password, "en", self.user_agent)
        self.token = str(result.get("token") or "")
        if not self.token:
            raise SubtitleError("no session token")
        return self.token

    def search(self, request: SubtitleRequest, languages: list[str]) -> list[Candidate]:
        candidates: list[Candidate] = []
        try:
            token = self.login()
        except (SubtitleError, httpx.HTTPError, OSError) as exc:
            log.warning("opensubtitles.org login failed: %s", exc)
            return candidates

        for language in languages:
            queries: list[dict[str, Any]] = []
            if request.hash:
                queries.append({"sublanguageid": language, "moviehash": request.hash,
                                "moviebytesize": str(request.size)})
            queries.append({"sublanguageid": language, "query": request.title})
            for query in queries:
                try:
                    result = self._call("SearchSubtitles", token, [query], {"limit": "25"})
                except (SubtitleError, httpx.HTTPError, OSError) as exc:
                    log.warning("opensubtitles.org search failed: %s", exc)
                    break
                rows = result.get("data") or []
                found = legacy_candidates(rows)
                candidates.extend(found)
                # A hash query that hit does not need the looser title query.
                if any(c.hash_match for c in found):
                    break
        return candidates

    def download(self, candidate: Candidate) -> bytes:
        token = self.login()
        result = self._call("DownloadSubtitles", token, [candidate.identifier])
        rows = result.get("data") or []
        if not rows:
            raise SubtitleError("no download link")
        link = str(rows[0].get("SubDownloadLink") or "")
        if not link:
            raise SubtitleError("no download link")
        response = httpx.get(link, timeout=self.timeout, follow_redirects=True,
                             headers={"User-Agent": self.user_agent})
        if response.status_code != 200 or not response.content:
            raise SubtitleError(f"subtitle fetch failed: {response.status_code}")
        return response.content


def legacy_candidates(rows: Iterable[dict]) -> list[Candidate]:
    """Map OpenSubtitles.org rows onto the shared candidate shape."""
    found: list[Candidate] = []
    for row in rows:
        identifier = str(row.get("IDSubtitleFile") or row.get("IDSubtitle") or "")
        if not identifier:
            continue
        language = normalise_language(str(row.get("SubLanguageID") or ""))
        found.append(Candidate(
            provider=OpenSubtitlesLegacy.name,
            identifier=identifier,
            language=language,
            release=str(row.get("MovieReleaseName") or row.get("SubFileName") or ""),
            downloads=int(float(row.get("SubDownloadsCnt") or 0)),
            rating=float(row.get("SubRating") or 0) / 2.0,
            hash_match=str(row.get("MatchedBy") or "") == "moviehash",
            format=str(row.get("SubFormat") or "srt").lower(),
        ))
    return found


def make_provider(settings) -> Provider | None:
    """Pick a provider: `.com` when a key exists, the keyless legacy one otherwise."""
    choice = (getattr(settings, "subtitle_provider", "") or "auto").lower()
    if choice in {"auto", "opensubtitles"} and settings.opensubtitles_api_key:
        return OpenSubtitles(
            settings.opensubtitles_api_key, settings.opensubtitles_username,
            settings.opensubtitles_password, base=settings.opensubtitles_base_url,
        )
    if choice in {"auto", "legacy"}:
        return OpenSubtitlesLegacy(
            settings.opensubtitles_username, settings.opensubtitles_password,
            endpoint=getattr(settings, "subtitles_legacy_url", ""),
        )
    return None


# ------------------------------------------------------------------ choosing

def pick_best(candidates: Iterable[Candidate], language: str) -> Candidate | None:
    """The candidate to download for one language.

    Hash matches first (they are timed for this release), then popularity and
    rating, then plain-text `.srt` over any other container so the file needs
    no conversion to be playable.
    """
    wanted = normalise_language(language)
    pool = [c for c in candidates if normalise_language(c.language) == wanted]
    if not pool:
        return None
    return max(pool, key=lambda c: (c.hash_match, c.format in {"srt", ""}, c.score()))


def decode_payload(payload: bytes) -> bytes:
    """Subtitle payloads arrive plain or gzipped; a stray BOM is stripped."""
    if payload[:2] == b"\x1f\x8b":
        try:
            payload = gzip.decompress(payload)
        except OSError as exc:
            raise SubtitleError(f"gzip: {exc}") from exc
    if payload.startswith(b"\xef\xbb\xbf"):
        payload = payload[3:]
    return payload


def looks_like_subtitle(payload: bytes) -> bool:
    """Cheap validity check: a cue arrow, or a numbered SRT block."""
    head = payload[:8192]
    return b"-->" in head or re.match(rb"\s*1\s*\r?\n", head) is not None


def write_subtitle(video: Path, language: str, payload: bytes) -> Path:
    """Write atomically next to the video; an existing file is never replaced."""
    target = subtitle_path(video, language)
    if target.exists():
        return target
    payload = decode_payload(payload)
    if not looks_like_subtitle(payload):
        raise SubtitleError("payload does not look like a subtitle")
    temporary = target.with_suffix(target.suffix + ".part")
    temporary.write_bytes(payload)
    os.replace(temporary, target)
    return target


# -------------------------------------------------------------- the job itself

def request_for(video: Path, kind: str | None = None) -> SubtitleRequest:
    release = parse(video.name, kind)
    try:
        size = video.stat().st_size
    except OSError:
        size = 0
    return SubtitleRequest(
        video=video, title=release.title, year=release.year, kind=release.kind,
        hash=moviehash(video), size=size, season=release.season, episode=release.episode,
    )


def fetch_for(conn, video: Path, languages: list[str], provider: Provider,
              kind: str | None = None) -> list[dict]:
    """Find and write every missing subtitle for one file. Never raises."""
    results: list[dict] = []
    missing = missing_languages(video, languages)
    if not missing:
        return results
    request = request_for(video, kind)
    for language in missing:
        key = (str(video), language)
        record = db.one(
            conn, "SELECT * FROM subtitles WHERE video_path = ? AND language = ?", key,
        )
        try:
            candidates = provider.search(request, [language])
        except Exception as exc:  # a provider bug must not stop the pass
            candidates, error = [], str(exc)
        else:
            error = ""
        choice = pick_best(candidates, language)
        if choice is None:
            _record(conn, video, language, None, provider.name,
                    "failed" if error else "missing", error or "no candidate")
            results.append({"video": str(video), "language": language,
                            "status": "failed" if error else "missing"})
            continue
        try:
            target = write_subtitle(video, language, provider.download(choice))
        except Exception as exc:
            _record(conn, video, language, None, choice.provider, "failed", str(exc))
            results.append({"video": str(video), "language": language, "status": "failed",
                            "error": str(exc)})
            continue
        _record(conn, video, language, target, choice.provider, "ok", "")
        results.append({"video": str(video), "language": language, "status": "ok",
                        "path": str(target), "release": choice.release})
    return results


def _record(conn, video: Path, language: str, target: Path | None, source: str,
            status: str, error: str) -> None:
    conn.execute(
        "INSERT INTO subtitles(video_path, language, subtitle_path, source, status,"
        " attempts, last_error, updated_at) VALUES(?,?,?,?,?,1,?,?)"
        " ON CONFLICT(video_path, language) DO UPDATE SET"
        " subtitle_path = excluded.subtitle_path, source = excluded.source,"
        " status = excluded.status, attempts = subtitles.attempts + 1,"
        " last_error = excluded.last_error, updated_at = excluded.updated_at",
        (str(video), language, str(target) if target else None, source, status,
         error[:400] or None, db.now()),
    )


def missing_report(root: Path, languages: list[str], limit: int = 200) -> dict[str, Any]:
    """What the library still lacks, without touching the network."""
    files = 0
    missing: list[dict] = []
    by_language = {lang: 0 for lang in languages}
    for video in library.iter_files(root):
        files += 1
        gaps = missing_languages(video, languages)
        if not gaps:
            continue
        for language in gaps:
            by_language[language] = by_language.get(language, 0) + 1
        if len(missing) < limit:
            missing.append({"path": str(video), "missing": gaps})
    return {"videos": files, "missing": missing, "count": len(missing),
            "by_language": by_language}


def pending_files(conn, root: Path, languages: list[str], limit: int = 25,
                  retry_after: int = 86400) -> Iterator[Path]:
    """Videos worth asking about: gaps in the library, minus recent failures."""
    emitted = 0
    for video in library.iter_files(root):
        if emitted >= limit:
            return
        if not missing_languages(video, languages):
            continue
        row = db.one(
            conn,
            "SELECT status, updated_at FROM subtitles WHERE video_path = ?"
            " AND updated_at > datetime('now', ?) AND status = 'missing'",
            (str(video), f"-{retry_after} seconds"),
        )
        if row:
            continue
        emitted += 1
        yield video


def run_pass(conn, settings, provider: Provider | None, limit: int | None = None,
             delay: float | None = None) -> dict[str, Any]:
    """One subtitle pass over the library. Returns what it did, including gaps."""
    provider = provider or make_provider(settings)
    languages = list(settings.subtitle_languages)
    if not settings.subtitles_enabled or provider is None \
            or not getattr(provider, "configured", True):
        return {"enabled": False, "fetched": [], "count": 0,
                "reason": "no subtitle provider configured"}

    delay = settings.subtitle_delay if delay is None else delay
    budget = settings.subtitle_batch if limit is None else limit
    fetched: list[dict] = []
    for video in pending_files(conn, settings.library_root, languages, limit=budget):
        results = fetch_for(conn, video, languages, provider)
        fetched.extend(results)
        if delay and any(r["status"] == "ok" for r in results):
            time.sleep(delay)
    return {"enabled": True, "fetched": fetched, "count": len(fetched),
            "ok": sum(1 for r in fetched if r["status"] == "ok")}


def stats(conn) -> dict[str, Any]:
    def count(sql: str, params: tuple = ()) -> int:
        row = db.one(conn, sql, params)
        return int(row[0]) if row else 0

    return {
        "ok": count("SELECT count(*) FROM subtitles WHERE status = 'ok'"),
        "missing": count("SELECT count(*) FROM subtitles WHERE status = 'missing'"),
        "failed": count("SELECT count(*) FROM subtitles WHERE status = 'failed'"),
        "last": (db.one(conn, "SELECT max(updated_at) FROM subtitles") or [None])[0],
    }
