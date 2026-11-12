"""API adapter safeguards: HTTPS/timeouts, parsing, pagination, confirmation
gate, ownership verification, and fresh-read-before-write (AC.2, AC.3, AC.4)."""

import urllib.parse

import pytest

from qobuz_mcp import qobuz_api as qa
from qobuz_mcp.qobuz_api import (
    API_BASE,
    CredentialsRequired,
    OwnershipVerificationError,
    QobuzClient,
    WriteNotConfirmed,
)


class FakeTransport:
    """Records every (url, headers) call; pops scripted responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_client(responses, user_id=7) -> tuple[QobuzClient, FakeTransport]:
    transport = FakeTransport(responses)
    client = QobuzClient(app_id="app911", app_secret="shh", transport=transport)
    client.set_session(auth_token="tok-xyz", user_id=user_id)
    return client, transport


def query_of(client: QobuzClient, transport: FakeTransport, index: int = 0) -> dict:
    parsed = urllib.parse.urlparse(transport.calls[index][0])
    return urllib.parse.parse_qs(parsed.query)


def path_of(transport: FakeTransport, index: int = 0) -> str:
    return urllib.parse.urlparse(transport.calls[index][0]).path


def test_https_and_timeout_configuration():
    client, transport = make_client([{"tracks": {"items": []}}])
    client.search_tracks("x")
    url = transport.calls[0][0]
    assert url.startswith("https://")
    assert url.startswith(API_BASE)
    assert isinstance(qa.REQUEST_TIMEOUT_SECONDS, float) and qa.REQUEST_TIMEOUT_SECONDS > 0


def test_request_headers_present():
    client, transport = make_client([{"tracks": {"items": []}}])
    client.search_tracks("x")
    headers = transport.calls[0][1]
    assert headers[qa.HEADER_APP_ID] == "app911"
    assert headers[qa.HEADER_AUTH_TOKEN] == "tok-xyz"


def test_requires_app_id_to_construct():
    with pytest.raises(CredentialsRequired):
        QobuzClient(app_id="")


# --- reads -----------------------------------------------------------------

SEARCH_FIXTURE = {
    "tracks": {
        "items": [
            {
                # Shape observed live from catalog/search (2026-11): the
                # performing artist is under `performer`, not `artist`.
                "id": 1,
                "title": "The Hanging Star",
                "performer": {"name": "Chumbawamba", "id": 108993},
                "album": {"title": "English Rebel Songs"},
            },
            {
                # Some endpoints (e.g. track/get) use `artist` instead.
                "id": 2,
                "title": "On Exactly Our Own Terms",
                "artist": {"name": "Chumbawamba"},
            },
        ]
    }
}


def test_search_tracks_parses_results():
    client, transport = make_client([SEARCH_FIXTURE])
    results = client.search_tracks("chumbawamba", limit=5)
    assert results == [
        {
            "track_id": 1,
            "title": "The Hanging Star",
            "artist": "Chumbawamba",
            "album": "English Rebel Songs",
        },
        {
            "track_id": 2,
            "title": "On Exactly Our Own Terms",
            "artist": "Chumbawamba",
            "album": None,
        },
    ]
    qs = query_of(client, transport)
    assert qs["query"] == ["chumbawamba"]
    assert qs["limit"] == ["5"]


def _page(items):
    return {"playlists": {"items": items, "limit": 2, "offset": 0, "total": 3}}


def _summary(pid):
    return {"id": pid, "name": f"pl{pid}", "owner": {"id": 7}, "tracks_count": 1}


def test_list_playlists_paginates():
    client, transport = make_client([_page([_summary(1), _summary(2)]), _page([_summary(3)])])
    playlists = client.get_user_playlists(limit=2)
    assert [p["playlist_id"] for p in playlists] == [1, 2, 3]
    assert query_of(client, transport, 0)["offset"] == ["0"]
    assert query_of(client, transport, 0)["type"] == [qa.PLAYLIST_TYPE_USER]
    assert query_of(client, transport, 1)["offset"] == ["2"]


PLAYLIST_FIXTURE = {
    "id": 42,
    "name": "Labor Ballads",
    "owner": {"id": 7},
    "tracks": {
        "items": [
            {"id": 900, "playlist_track_id": 555001, "title": "Song A"},
            {"id": 901, "playlist_track_id": 555002, "title": "Song B"},
        ]
    },
}


def test_get_playlist_items_exposes_item_ids():
    client, _ = make_client([PLAYLIST_FIXTURE])
    playlist = client.get_playlist(42)
    assert playlist["playlist_id"] == 42
    assert playlist["owner_id"] == 7
    ids = [t["playlist_track_id"] for t in playlist["tracks"]]
    assert ids == [555001, 555002]  # removal identity, not the catalog track id


def test_get_playlist_paginates_track_items():
    # Live-verified 2026-11: playlist/get pages tracks.items (a 54-track
    # playlist returned a 50-item first page without explicit limit/offset).
    # The adapter must follow every page so removals can target any entry.
    def _page(offset, count):
        return {
            "id": 42,
            "name": "Labor Ballads",
            "owner": {"id": 7},
            "tracks": {
                "limit": qa.PLAYLIST_TRACKS_PAGE_LIMIT,
                "offset": offset,
                "total": 3,
                "items": [
                    {
                        "id": 900 + offset + i,
                        "playlist_track_id": 555001 + offset + i,
                        "title": f"Song {offset + i}",
                    }
                    for i in range(count)
                ],
            },
        }

    client, transport = make_client([_page(0, 2), _page(2, 1)])
    playlist = client.get_playlist(42)
    assert [t["playlist_track_id"] for t in playlist["tracks"]] == [555001, 555002, 555003]
    qs0 = query_of(client, transport, 0)
    qs1 = query_of(client, transport, 1)
    assert qs0["offset"] == ["0"]
    assert qs0["limit"] == [str(qa.PLAYLIST_TRACKS_PAGE_LIMIT)]
    assert qs1["offset"] == ["2"]
    assert path_of(transport, 0).endswith(qa.EP_PLAYLIST_GET)
    assert path_of(transport, 1).endswith(qa.EP_PLAYLIST_GET)


# --- writes: confirmation gate ----------------------------------------------


def test_write_requires_confirmation():
    # Transport raises on ANY request; confirm=False must short-circuit first.
    client, transport = make_client([])
    with pytest.raises(WriteNotConfirmed):
        client.create_playlist("X")
    with pytest.raises(WriteNotConfirmed):
        client.add_tracks(42, [1])
    with pytest.raises(WriteNotConfirmed):
        client.remove_tracks(42, [555001])
    with pytest.raises(WriteNotConfirmed):
        client.delete_playlist(42)
    assert transport.calls == []


def test_create_playlist_request():
    client, transport = make_client([{"id": 99, "name": "Labor"}])
    client.create_playlist("Labor", confirm=True)
    assert path_of(transport).endswith(qa.EP_PLAYLIST_CREATE)
    qs = query_of(client, transport)
    assert qs["name"] == ["Labor"]
    assert qs["is_public"] == ["0"]
    assert qs["is_collaborative"] == ["0"]


def test_add_tracks_request():
    client, transport = make_client([{"id": 42}])
    client.add_tracks(42, [900, 901], confirm=True)
    assert path_of(transport).endswith(qa.EP_PLAYLIST_ADD_TRACKS)
    qs = query_of(client, transport)
    assert qs["playlist_id"] == ["42"]
    assert qs["track_ids"] == ["900,901"]


# --- writes: ownership gate --------------------------------------------------


def test_unknown_or_mismatched_ownership_rejected():
    # Unknown user identity: fresh read happens, then fail closed.
    client, transport = make_client([PLAYLIST_FIXTURE], user_id=None)
    with pytest.raises(OwnershipVerificationError):
        client.remove_tracks(42, [555001], confirm=True)
    assert len(transport.calls) == 1  # only the fresh read, no write

    # Mismatched owner: refuse.
    client, transport = make_client([PLAYLIST_FIXTURE], user_id=8)
    with pytest.raises(OwnershipVerificationError):
        client.delete_playlist(42, confirm=True)
    assert len(transport.calls) == 1
    assert not path_of(transport).endswith(qa.EP_PLAYLIST_DELETE)


def test_remove_delete_use_fresh_playlist_read_before_write():
    # remove: fresh read (call 0) precedes deleteTracks (call 1)
    client, transport = make_client([PLAYLIST_FIXTURE, {"status": "ok"}])
    client.remove_tracks(42, [555001, 555002], confirm=True)
    assert path_of(transport, 0).endswith(qa.EP_PLAYLIST_GET)
    assert path_of(transport, 1).endswith(qa.EP_PLAYLIST_DELETE_TRACKS)
    qs = query_of(client, transport, 1)
    assert qs["playlist_id"] == ["42"]
    assert qs["playlist_track_ids"] == ["555001,555002"]

    # delete: fresh read (call 0) precedes playlist/delete (call 1)
    client, transport = make_client([PLAYLIST_FIXTURE, {"status": "ok"}])
    client.delete_playlist(42, confirm=True)
    assert path_of(transport, 0).endswith(qa.EP_PLAYLIST_GET)
    assert path_of(transport, 1).endswith(qa.EP_PLAYLIST_DELETE)
    assert query_of(client, transport, 1)["playlist_id"] == ["42"]


def test_remove_selected_playlist_items_only():
    client, transport = make_client([PLAYLIST_FIXTURE, {"status": "ok"}])
    client.remove_tracks(42, [555002], confirm=True)
    qs = query_of(client, transport, 1)
    assert qs["playlist_track_ids"] == ["555002"]


def test_list_playlists_pagination_is_bounded():
    # A server that always returns a full page cannot loop forever.
    client, transport = make_client([_page([_summary(1)]) for _ in range(6)])
    playlists = client.get_user_playlists(limit=1, max_pages=3)
    assert len(playlists) == 3
    assert len(transport.calls) == 3
