"""
monarch-recs - AI recommendations & smart search for the Monarch platform.

Content-based recommendation engine over the Jellyfin library, plus smart
search. Fully local (no external AI calls required):

  * /api/recommendations?user_id=...  - personalized picks from the user's
                                        watch history (fall back to trending)
  * /api/recommendations?item_id=...  - "more like this" for one item
  * /api/trending                     - most-played titles across all users
  * /api/search?q=...                 - ranked smart search (name/genre/year/
                                        cast/overview, with filters)
  * /api/describe?item_id=...         - LLM-generated description only when an
                                        OpenAI-compatible endpoint is
                                        configured (OPENAI_API_KEY etc.)

Internals: a lightweight TF-IDF vector per item built from the item name,
genres, tags, cast/people and overview; cosine similarity in pure Python
(sparse dicts - no numpy/sklearn dependency). Vectors are refreshed on a
schedule (REFRESH_INTERVAL) so new library additions are picked up.

Jellyfin access uses the admin token exported by monarch-init on first boot
(/docker/appdata/init/jellyfin-api-key.txt), falling back to JELLYFIN_API_KEY.
"""

import math
import os
import re
import threading
import time

import httpx
from fastapi import FastAPI, HTTPException, Query

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://jellyfin:8096").rstrip("/")
API_KEY = os.environ.get("JELLYFIN_API_KEY", "").strip()
KEY_FILE = os.environ.get("JELLYFIN_API_KEY_FILE", "").strip()
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "900"))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for",
    "with", "from", "by", "is", "it", "its", "as", "are", "was", "were",
    "be", "been", "this", "that", "have", "has", "had", "not", "no",
    "do", "does", "did", "but", "if", "then", "film", "movie", "show",
}


def _api_key() -> str:
    if API_KEY:
        return API_KEY
    try:
        with open(KEY_FILE, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
        if key:
            return key
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------------------
# Tokenizer / TF-IDF
# ---------------------------------------------------------------------------


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    out = []
    for tok in tokens:
        if tok in STOPWORDS or len(tok) < 2:
            continue
        # light stemming - helps match "horror" vs "horrors", "comedy" vs "comedies"
        for suffix in ("ing", "ies", "es", "ed", "s"):
            if tok.endswith(suffix) and len(tok) > len(suffix) + 2:
                tok = tok[: -len(suffix)]
                break
        out.append(tok)
    return out


def item_text(item: dict) -> str:
    name = item.get("Name") or ""
    genres = " ".join(item.get("Genres") or [])
    tags = " ".join(item.get("Tags") or [])
    overview = item.get("Overview") or ""
    people = " ".join(p.get("Name", "") for p in (item.get("People") or []))
    parts = [name, name, name, genres, tags, people, overview]  # name weighted x3
    return " ".join(parts)


class Corpus:
    """Sparse TF-IDF vectors + cosine similarity for a set of documents."""

    def __init__(self, docs: dict[str, list[str]]):
        self.vectors: dict[str, dict[str, float]] = {}
        self.doc_norms: dict[str, float] = {}
        self.idf: dict[str, float] = {}
        n = len(docs)
        df: dict[str, int] = {}
        tf: dict[str, dict[str, float]] = {}
        for doc_id, tokens in docs.items():
            counts: dict[str, int] = {}
            for tok in tokens:
                counts[tok] = counts.get(tok, 0) + 1
            total = sum(counts.values()) or 1
            term_tf: dict[str, float] = {}
            for tok, c in counts.items():
                term_tf[tok] = c / total
                df[tok] = df.get(tok, 0) + 1
            tf[doc_id] = term_tf
        for tok, freq in df.items():
            self.idf[tok] = math.log((1 + n) / (1 + freq)) + 1.0
        for doc_id, term_tf in tf.items():
            vec = {tok: w * self.idf.get(tok, 1.0) for tok, w in term_tf.items()}
            norm = math.sqrt(sum(w * w for w in vec.values())) or 1.0
            self.vectors[doc_id] = vec
            self.doc_norms[doc_id] = norm

    def cosine(self, a: dict[str, float], b: dict[str, float]) -> float:
        if not a or not b:
            return 0.0
        overlap = set(a) & set(b)
        if not overlap:
            return 0.0
        dot = sum(a[t] * b[t] for t in overlap)
        na = math.sqrt(sum(w * w for w in a.values()))
        nb = math.sqrt(sum(w * w for w in b.values()))
        return dot / ((na * nb) or 1.0)

    def similarity_to(self, doc_id: str, other_id: str) -> float:
        return self.cosine(self.vectors.get(doc_id, {}),
                           self.vectors.get(other_id, {}))


# ---------------------------------------------------------------------------
# Jellyfin library client
# ---------------------------------------------------------------------------


class Jellyfin:
    def __init__(self):
        # API key is read per request so it picks up the token monarch-init
        # exports on first boot even if this service started earlier.
        self._client = httpx.Client(base_url=JELLYFIN_URL, timeout=30)

    def get(self, path: str, **params) -> dict | list:
        params["api_key"] = _api_key()
        try:
            r = self._client.get(path, params=params)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502,
                                detail=f"Jellyfin request failed ({path}): {exc}")

    def library_items(self, item_types: tuple[str, ...] = ("Movie", "Series"),
                      limit: int = 200) -> list[dict]:
        """Paged fetch of top-level items with recommendation fields."""
        items: list[dict] = []
        start = 0
        while True:
            data = self.get(
                "/Items",
                Recursive="true",
                IncludeItemTypes=",".join(item_types),
                Fields="Genres,Tags,Overview,People,ProductionYear,CommunityRating",
                StartIndex=start, Limit=limit,
            )
            page = (data or {}).get("Items") or []
            items.extend(page)
            total = (data or {}).get("TotalRecordCount") or 0
            start += len(page)
            if not page or start >= total:
                break
        return items

    def users(self) -> list[dict]:
        return self.get("/Users")

    def watched_items(self, user_id: str) -> list[dict]:
        items: list[dict] = []
        start = 0
        while True:
            data = self.get(
                f"/Users/{user_id}/Items",
                Recursive="true", Filters="IsPlayed",
                IncludeItemTypes="Movie,Series",
                Fields="Genres,Tags,Overview,ProductionYear",
                StartIndex=start, Limit=200,
            )
            page = (data or {}).get("Items") or []
            items.extend(page)
            total = (data or {}).get("TotalRecordCount") or 0
            start += len(page)
            if not page or start >= total:
                break
        return items

    def item(self, item_id: str) -> dict:
        return self.get(f"/Items/{item_id}",
                        Fields="Genres,Tags,Overview,People,ProductionYear,CommunityRating")


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

app = FastAPI(title="Monarch AI Recommendations", version="1.0.0")
jf = Jellyfin()

STATE = {
    "items": {},          # id -> normalized item dict
    "corpus": Corpus({}),  # id -> tf vector
    "watched": {},        # user_id -> set(item ids)
    "last_refresh": 0.0,
}
LOCK = threading.Lock()


def _public_item(item: dict) -> dict:
    img = (item.get("ImageTags") or {}).get("Primary")
    return {
        "id": item.get("Id"),
        "name": item.get("Name"),
        "type": item.get("Type"),
        "year": item.get("ProductionYear"),
        "genres": item.get("Genres") or [],
        "overview": (item.get("Overview") or "")[:300],
        "rating": item.get("CommunityRating"),
        "play_count": (item.get("UserData") or {}).get("PlayCount", 0),
        "image_tag": img,
        "image_url": f"{JELLYFIN_URL}/Items/{item.get('Id')}/Images/Primary" if img else None,
    }


def refresh() -> None:
    """Rebuild the index from the Jellyfin library. Safe to call repeatedly."""
    try:
        if not _api_key():
            return
        items = jf.library_items()
        docs: dict[str, list[str]] = {}
        normalized: dict[str, dict] = {}
        for it in items:
            iid = str(it.get("Id"))
            if not iid:
                continue
            normalized[iid] = it
            docs[iid] = tokenize(item_text(it))
        watched: dict[str, set[str]] = {}
        try:
            for user in jf.users():
                uid = str(user.get("Id"))
                if uid:
                    watched[uid] = {str(w.get("Id")) for w in
                                    jf.watched_items(uid) if w.get("Id")}
        except HTTPException:
            pass
        with LOCK:
            STATE["items"] = normalized
            STATE["corpus"] = Corpus(docs)
            STATE["watched"] = watched
            STATE["last_refresh"] = time.time()
        print(f"[monarch-recs] index refreshed: {len(normalized)} items, "
              f"{sum(len(v) for v in watched.values())} watch records",
              flush=True)
    except Exception as exc:  # noqa: BLE001 - never crash the service
        print(f"[monarch-recs] WARNING: index refresh failed: {exc}", flush=True)


def _ensure_fresh() -> None:
    if not STATE["last_refresh"] or \
            time.time() - STATE["last_refresh"] > REFRESH_INTERVAL:
        refresh()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "jellyfin": bool(_api_key()),
        "items_indexed": len(STATE["items"]),
        "last_refresh": STATE["last_refresh"],
        "llm_configured": bool(OPENAI_API_KEY),
    }


@app.get("/api/recommendations")
def recommendations(user_id: str | None = Query(default=None),
                    item_id: str | None = Query(default=None),
                    limit: int = Query(default=10, ge=1, le=50)) -> dict:
    _ensure_fresh()
    limit = min(limit, 50)
    if item_id:
        # "More like this": top items by cosine similarity to the given item.
        with LOCK:
            corpus = STATE["corpus"]
            items = STATE["items"]
        iid = str(item_id)
        if iid not in corpus.vectors:
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
        scores = sorted(
            ((other, corpus.similarity_to(iid, other))
             for other in corpus.vectors if other != iid),
            key=lambda kv: kv[1], reverse=True)
        recs = [_public_item(items.get(sid, {})) for sid, _ in scores[:limit]
                if items.get(sid)]
        return {"item_id": item_id, "strategy": "similar", "recommendations": recs}

    if user_id:
        # Personalized: aggregate similarity to everything the user watched.
        with LOCK:
            watched = STATE["watched"].get(str(user_id), set())
            corpus = STATE["corpus"]
        if watched:
            scores: dict[str, float] = {}
            for wid in watched:
                for other, vec in corpus.vectors.items():
                    if other in watched:
                        continue
                    sim = corpus.cosine(corpus.vectors.get(wid, {}), vec)
                    if sim > 0:
                        scores[other] = scores.get(other, 0.0) + sim
            ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
            recs = [_public_item(STATE["items"].get(sid, {}))
                    for sid, _ in ranked[:limit] if STATE["items"].get(sid)]
            return {"user_id": user_id, "strategy": "personalized",
                    "watched": len(watched), "recommendations": recs}

    # No user/item given (or user has no history): fall back to trending.
    trending = _trending(limit)
    return {"strategy": "trending", "recommendations": trending}


@app.get("/api/trending")
def trending(limit: int = Query(default=10, ge=1, le=50)) -> dict:
    _ensure_fresh()
    return {"recommendations": _trending(limit)}


def _trending(limit: int) -> list[dict]:
    items = sorted(STATE["items"].values(),
                   key=lambda it: (it.get("UserData") or {}).get("PlayCount", 0),
                   reverse=True)
    return [_public_item(it) for it in items[:limit]]


@app.get("/api/search")
def search(q: str = Query(default="", min_length=1),
           types: str | None = Query(default=None),
           genre: str | None = Query(default=None),
           year: int | None = Query(default=None),
           limit: int = Query(default=10, ge=1, le=50)) -> dict:
    _ensure_fresh()
    query_tokens = set(tokenize(q))
    if not query_tokens:
        raise HTTPException(status_code=400, detail="Empty query")
    query_vec = {tok: 1.0 for tok in query_tokens}
    results = []
    with LOCK:
        items = STATE["items"]
        corpus = STATE["corpus"]
    for iid, vec in corpus.vectors.items():
        item = items.get(iid, {})
        if types and item.get("Type") not in types.split(","):
            continue
        if genre and genre.lower() not in [g.lower() for g in (item.get("Genres") or [])]:
            continue
        if year and item.get("ProductionYear") not in (year, year - 1, year + 1):
            continue
        itokens = set(vec)
        overlap = len(query_tokens & itokens)
        if not overlap:
            continue
        # name-prefix matches rank far above incidental matches
        name = (item.get("Name") or "").lower()
        prefix = sum(1 for t in query_tokens if name.startswith(t))
        score = overlap + 2.0 * prefix + 0.5 * corpus.cosine(vec, query_vec)
        results.append((score, item))
    results.sort(key=lambda kv: kv[0], reverse=True)
    return {
        "query": q,
        "results": [_public_item(it) for _, it in results[:limit]],
        "total": len(results),
    }


@app.get("/api/describe")
def describe(item_id: str = Query(...)) -> dict:
    """LLM-generated description for an item (only when an OpenAI-compatible
    endpoint is configured - see .env.sample OPENAI_* variables)."""
    if not OPENAI_API_KEY:
        raise HTTPException(
            status_code=400,
            detail="LLM not configured - set OPENAI_API_KEY (+ optional "
                   "OPENAI_BASE_URL / OPENAI_MODEL) in .env and restart.")
    item = jf.item(item_id)
    prompt = (
        f"Write a two-sentence enticing blurb for this {item.get('Type', 'title')} "
        f"titled \"{item.get('Name')}\""
        + (f" ({item.get('ProductionYear')})" if item.get("ProductionYear") else "")
        + f". Genres: {', '.join(item.get('Genres') or [])}. "
        + f"Overview: {item.get('Overview') or 'not available'}."
    )
    url = f"{OPENAI_BASE_URL or 'https://api.openai.com/v1'}/chat/completions"
    body = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system",
             "content": "You write short, accurate, appealing content blurbs."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 200,
    }
    try:
        r = httpx.post(url, json=body, timeout=60, headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        })
        r.raise_for_status()
        text = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
        return {"item_id": item_id, "description": text.strip()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

threading.Thread(target=refresh, daemon=True).start()