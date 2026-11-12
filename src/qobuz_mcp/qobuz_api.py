"""HTTP adapter for observed, undocumented Qobuz endpoints.

Endpoint paths, parameter names, and response shapes below are transcribed
from third-party client source (tidalf/plugin.audio.qobuz, GPLv3; Music
Assistant provider) and are UNVERIFIED against the live service. They are
kept as module constants so a validated smoke test can correct them in one
place. Treat every shape here as a hypothesis until confirmed.

Safety rules enforced by this module:
- No request is issued without a constructed client (app_id + app_secret).
- All URLs are HTTPS with a bounded request timeout and a minimum interval
  between requests (conservative throttling).
- Every write requires an explicit confirm flag; without it, nothing is sent.
- remove/delete always perform a fresh playlist read first and verify the
  playlist is owned by the authenticated user; if ownership cannot be
  established, they fail closed with OwnershipVerificationError.
- QobuzClient.validation_passed is the operator's write-enablement opt-in
  (QOBUZ_ENABLE_WRITES): an attestation, not a validation. Runtime guards
  are explicit confirm:true and fresh-read ownership verification.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

API_BASE = "https://www.qobuz.com/api.json/0.2"
REQUEST_TIMEOUT_SECONDS = 10.0
MIN_REQUEST_INTERVAL_SECONDS = 0.5
DEFAULT_PAGE_LIMIT = 50
MAX_PLAYLIST_PAGES = 50
# playlist/get pages its tracks.items and defaults to ~50 per page; ask for
# 100 per request (live-verified 2026-11) and follow offsets up to the page cap.
PLAYLIST_TRACKS_PAGE_LIMIT = 100

# Observed endpoints (unverified against live service).
EP_SEARCH = "catalog/search"  # current clients (Music Assistant). Older ones use search/getResults (query+type).
EP_PLAYLIST_GET = "playlist/get"
EP_PLAYLIST_USER = "playlist/getUserPlaylists"
# tidalf raw.py marks `type` REQUIRED on getUserPlaylists but does not fix a
# value; "user" is provisionally used and MUST be confirmed during gated
# endpoint validation before any live call.
PLAYLIST_TYPE_USER = "user"
EP_PLAYLIST_CREATE = "playlist/create"
EP_PLAYLIST_ADD_TRACKS = "playlist/addTracks"
EP_PLAYLIST_DELETE_TRACKS = "playlist/deleteTracks"
EP_PLAYLIST_DELETE = "playlist/delete"

HEADER_APP_ID = "x-app-id"
HEADER_AUTH_TOKEN = "x-user-auth-token"

WRITE_ENDPOINTS = frozenset(
    {
        EP_PLAYLIST_CREATE,
        EP_PLAYLIST_ADD_TRACKS,
        EP_PLAYLIST_DELETE_TRACKS,
        EP_PLAYLIST_DELETE,
    }
)


class CredentialsRequired(RuntimeError):
    """Raised when an operation needs credentials that are absent."""


class WriteNotConfirmed(RuntimeError):
    """Raised when a write is attempted without confirm=True."""


class OwnershipVerificationError(RuntimeError):
    """Raised when playlist ownership cannot be positively verified."""


Transport = Callable[[str, dict[str, str]], Any]


def default_transport(url: str, headers: dict[str, str]) -> Any:
    """GET via urllib with the bounded module timeout. Used in production."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _int_flag(value: bool) -> int:
    return 1 if value else 0


def _join_ids(ids: list[int | str]) -> str:
    return ",".join(str(i) for i in ids)


def _performer_name(item: dict[str, Any]) -> str | None:
    """Best-effort performing-artist name from observed shapes.

    catalog/search items carry the performing artist under `performer`
    (verified live 2026-11); track/get items use `artist`. Prefer
    `performer`, fall back to `artist`, else None.
    """
    for key in ("performer", "artist"):
        name = (item.get(key) or {}).get("name")
        if name:
            return name
    return None


def parse_tracks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "track_id": item.get("id"),
            "title": item.get("title"),
            "artist": _performer_name(item),
            "album": (item.get("album") or {}).get("title"),
        }
        for item in items
    ]


def parse_playlist(
    data: dict[str, Any], items: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Normalize a playlist/get payload.

    Removal needs the playlist-entry identity (playlist_track_id), which is
    distinct from the catalog track id. Minim's docs confirm deleteTracks
    consumes playlist-entry IDs obtained from get_playlist; when the field
    is absent the track id is used as a fallback. `items` lets get_playlist
    supply fully paginated track items; when omitted the payload's first
    page is used.
    """
    owner = data.get("owner") or {}
    if items is None:
        items = ((data.get("tracks") or {}).get("items")) or []
    return {
        "playlist_id": data.get("id"),
        "name": data.get("name"),
        "owner_id": owner.get("id"),
        "tracks": [
            {
                "track_id": item.get("id"),
                "playlist_track_id": item.get("playlist_track_id", item.get("id")),
                "title": item.get("title"),
            }
            for item in items
        ],
    }


def parse_playlist_summaries(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "playlist_id": item.get("id"),
            "name": item.get("name"),
            "owner_id": (item.get("owner") or {}).get("id"),
            "track_count": item.get("tracks_count"),
        }
        for item in items
    ]


class QobuzClient:
    """Minimal client for observed Qobuz endpoints. Fail-closed by design."""

    def __init__(
        self,
        app_id: str,
        app_secret: str | None = None,
        transport: Transport | None = None,
    ) -> None:
        if not app_id:
            raise CredentialsRequired(
                "QOBUZ_APP_ID and a session token are required "
                "(see README: manual web-session route)"
            )
        self.app_id = app_id
        self.app_secret = app_secret  # unused by the session route; reserved
        self.auth_token: str | None = None
        self.user_id: int | None = None
        # Operator write-enablement opt-in (QOBUZ_ENABLE_WRITES). This is an
        # attestation, not a programmatic validation; it gates MCP write-tool
        # registration only — safety rests on confirm/ownership guards.
        self.validation_passed = False
        self._transport = transport or default_transport
        self._last_request_at: float | None = None

    # -- request plumbing -------------------------------------------------

    def set_session(self, auth_token: str, user_id: int | None = None) -> None:
        """Attach an authorized session for account operations.

        auth_token must come from an eligible, authorized credential setup.
        """
        self.auth_token = auth_token
        self.user_id = user_id

    def _headers(self) -> dict[str, str]:
        headers = {HEADER_APP_ID: self.app_id}
        if self.auth_token:
            headers[HEADER_AUTH_TOKEN] = self.auth_token
        return headers

    def _throttle(self) -> None:
        now = time.monotonic()
        if self._last_request_at is not None:
            elapsed = now - self._last_request_at
            if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
                time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()

    def _get(self, endpoint: str, params: dict[str, Any]) -> Any:
        url = f"{API_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
        self._throttle()
        return self._transport(url, self._headers())

    # -- reads ------------------------------------------------------------

    def search_tracks(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        data = self._get(EP_SEARCH, {"query": query, "limit": limit})
        tracks = (data or {}).get("tracks") or {}
        return parse_tracks(tracks.get("items") or [])

    def get_user_playlists(
        self,
        limit: int = DEFAULT_PAGE_LIMIT,
        max_pages: int = MAX_PLAYLIST_PAGES,
    ) -> list[dict[str, Any]]:
        """List the authenticated user's playlists, following pagination.

        Bounded by max_pages so a misbehaving server that always returns a
        full page cannot loop forever.
        """
        results: list[dict[str, Any]] = []
        offset = 0
        pages = 0
        while True:
            data = self._get(EP_PLAYLIST_USER, {"type": PLAYLIST_TYPE_USER, "limit": limit, "offset": offset})
            payload = (data or {}).get("playlists") or {}
            items = payload.get("items") or []
            results.extend(parse_playlist_summaries(items))
            pages += 1
            if len(items) < limit or pages >= max_pages:
                break
            offset += len(items)
        return results

    def get_playlist(self, playlist_id: int | str) -> dict[str, Any]:
        """Fetch a playlist, following track pagination.

        playlist/get pages its `tracks.items` (live-verified 2026-11: a
        54-track playlist returned a 50-item first page without explicit
        limit/offset). Removal needs entry IDs at ANY position, so fetch
        every page, bounded by MAX_PLAYLIST_PAGES so a misbehaving server
        cannot loop forever.
        """
        all_items: list[dict[str, Any]] = []
        last_page: dict[str, Any] = {}
        offset = 0
        pages = 0
        while True:
            data = (
                self._get(
                    EP_PLAYLIST_GET,
                    {
                        "playlist_id": playlist_id,
                        "extra": "tracks",
                        "limit": PLAYLIST_TRACKS_PAGE_LIMIT,
                        "offset": offset,
                    },
                )
                or {}
            )
            last_page = data
            page_items = ((data.get("tracks") or {}).get("items")) or []
            all_items.extend(page_items)
            pages += 1
            total = (data.get("tracks") or {}).get("total")
            if pages >= MAX_PLAYLIST_PAGES:
                break
            if isinstance(total, int):
                if len(all_items) >= total:
                    break
            elif len(page_items) < PLAYLIST_TRACKS_PAGE_LIMIT:
                # No total reported: fall back to the short-page heuristic.
                break
            offset += len(page_items)
        return parse_playlist(last_page, all_items)

    # -- writes (gated) ---------------------------------------------------

    def _require_confirm(self, confirm: bool) -> None:
        if not confirm:
            raise WriteNotConfirmed(
                "Write operations require confirm=True after the exact operation "
                "has been previewed and explicitly confirmed"
            )

    def _verify_ownership_fresh(self, playlist_id: int | str) -> dict[str, Any]:
        """Fresh read + positive ownership check; fail closed on uncertainty."""
        playlist = self.get_playlist(playlist_id)
        owner_id = playlist.get("owner_id")
        if self.user_id is None or owner_id is None:
            raise OwnershipVerificationError(
                "Ownership cannot be verified (missing user identity or owner data); "
                "refusing to mutate"
            )
        if owner_id != self.user_id:
            raise OwnershipVerificationError(
                f"Playlist {playlist_id} is owned by {owner_id!r}, "
                f"not the authenticated user {self.user_id!r}; refusing to mutate"
            )
        return playlist

    def create_playlist(
        self,
        name: str,
        confirm: bool = False,
        is_public: bool = False,
        is_collaborative: bool = False,
    ) -> dict[str, Any]:
        self._require_confirm(confirm)
        return self._get(
            EP_PLAYLIST_CREATE,
            {
                "name": name,
                "is_public": _int_flag(is_public),
                "is_collaborative": _int_flag(is_collaborative),
            },
        )

    def add_tracks(
        self,
        playlist_id: int | str,
        track_ids: list[int | str],
        confirm: bool = False,
    ) -> dict[str, Any]:
        self._require_confirm(confirm)
        if not track_ids:
            raise ValueError("track_ids must not be empty")
        return self._get(
            EP_PLAYLIST_ADD_TRACKS,
            {"playlist_id": playlist_id, "track_ids": _join_ids(track_ids)},
        )

    def remove_tracks(
        self,
        playlist_id: int | str,
        playlist_track_ids: list[int | str],
        confirm: bool = False,
    ) -> dict[str, Any]:
        self._require_confirm(confirm)
        if not playlist_track_ids:
            raise ValueError("playlist_track_ids must not be empty")
        # Fresh read + ownership gate happens BEFORE the write request.
        self._verify_ownership_fresh(playlist_id)
        return self._get(
            EP_PLAYLIST_DELETE_TRACKS,
            {
                "playlist_id": playlist_id,
                "playlist_track_ids": _join_ids(playlist_track_ids),
            },
        )

    def delete_playlist(self, playlist_id: int | str, confirm: bool = False) -> dict[str, Any]:
        self._require_confirm(confirm)
        # Fresh read + ownership gate happens BEFORE the write request.
        self._verify_ownership_fresh(playlist_id)
        return self._get(EP_PLAYLIST_DELETE, {"playlist_id": playlist_id})
