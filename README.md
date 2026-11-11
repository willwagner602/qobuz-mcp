# qobuz-mcp

A personal-use MCP server for Qobuz playlist management. **Unofficial.**
It talks to observed but undocumented Qobuz web-player API endpoints, may
violate Qobuz's terms of use, and can break without notice. It is not
Qobuz-approved; do not distribute or host it.

## Current status

- **Working now:** local stdio MCP server, read-only tool registration,
  config validation, credential redaction, and the offline test suite.
- **Session route (operator-authorized):** configuration via a session
  token you copy manually from your own logged-in Qobuz web session.
  This is **not Qobuz-issued** authorization; you accept the risk that
  unofficial API use may violate Qobuz terms or restrict your account.
- **Gated (not exposed by default):** playlist write tools
  (`create_playlist`, `add_tracks_to_playlist`, `remove_tracks_from_playlist`,
  `delete_playlist`). They register only when you set the write-enablement
  opt-in below, after running the read-only smoke test.

## Requirements

- Python >= 3.10
- An MCP client that speaks stdio JSON-RPC
- A Qobuz account whose playlists you own (writes affect only your account)

## Setup

```sh
cd ~/work/qobuz-mcp
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

## Configuration — manual web-session route

You copy three values **manually from your own** logged-in session at
`play.qobuz.com` (in a desktop browser, open DevTools):

1. `QOBUZ_APP_ID` — the value of the `x-app-id` request header on any
   `api.json` request the web player sends (Network tab).
2. `QOBUZ_USER_AUTH_TOKEN` — the value of the `X-User-Auth-Token` request
   header on the same requests.
3. `QOBUZ_USER_ID` (recommended) — your numeric user id, shown inside the
   `localuser` entry in Application → Local Storage.

Do not automate this: the copy is a one-time manual action by you, and the
server never touches your browser. Never share these values in chat, shell
history, or source control. Tokens can expire — when account calls start
failing, re-copy them.

Set them in your MCP client's `env` block:

- `QOBUZ_APP_ID` (required)
- `QOBUZ_USER_AUTH_TOKEN` (required)
- `QOBUZ_USER_ID` (recommended — remove/delete verify playlist ownership
  against this id and fail closed without it)
- `QOBUZ_ENABLE_WRITES` (see below)

Without the two required variables, every account operation fails closed
with a clear error; the server still starts and serves `server_status`
offline. `QOBUZ_APP_SECRET` is accepted but unused by this route; the
official alternative — Qobuz-issued app credentials for partners via
`api@qobuz.com` — remains the sanctioned path if you ever qualify for it.

## Write enablement

Playlist write tools stay hidden until you opt in:

1. Configure the session variables and run the read-only smoke test below.
2. If reads work, set `QOBUZ_ENABLE_WRITES=1` in the same `env` block and
   restart the MCP client. Write tools then appear in `tools/list`.
3. Every write still requires explicit `confirm: true` at call time;
   remove/delete additionally re-fetch the playlist and verify it is owned
   by `QOBUZ_USER_ID` immediately before acting, failing closed otherwise.

## Running

The server speaks MCP over stdio (newline-delimited JSON-RPC 2.0):

```sh
QOBUZ_APP_ID=... QOBUZ_USER_AUTH_TOKEN=... .venv/bin/qobuz-mcp
```

Example client registration:

```json
{
  "mcpServers": {
    "qobuz": {
      "command": "/home/wbw/work/qobuz-mcp/.venv/bin/qobuz-mcp",
      "env": {
        "QOBUZ_APP_ID": "...",
        "QOBUZ_USER_AUTH_TOKEN": "...",
        "QOBUZ_USER_ID": "...",
        "QOBUZ_ENABLE_WRITES": "1"
      }
    }
  }
}
```

## Tools

Read-only (available once the session is configured; fail closed without):

- `server_status` — offline state and policy report
- `search_tracks` — catalog search; returns stable `track_id`s
- `list_playlists` — the user's playlists (paginated)
- `get_playlist` — one playlist with its tracks, exposing the
  `playlist_track_id` identity needed for removal

Write tools (registered only with `QOBUZ_ENABLE_WRITES` set; every call
requires `confirm: true`; remove/delete fetch the playlist fresh and verify
ownership first, failing closed if ownership cannot be established):

- `create_playlist`
- `add_tracks_to_playlist`
- `remove_tracks_from_playlist`
- `delete_playlist`

## Security notes

- Local-only over stdio; no network listener.
- Session values are read from the process environment only; no config
  files with secrets are supported (so no file-permission pitfalls).
- Tokens, app ids, and user tokens are redacted from errors and logs.
- Passwords are never requested, stored, or handled. **Do not automate**
  credential extraction — the manual copy stays manual.
- HTTPS only, bounded request timeout, minimum interval between requests.

## Manual validation checklist

Run reads first, with a disposable private playlist for the write steps,
previewing each exact operation and confirming it:

1. `server_status` → `credentials_present: true`.
2. `list_playlists` → your playlists appear (validates the session).
3. `search_tracks` for a known track → note its `track_id`.
4. With `QOBUZ_ENABLE_WRITES=1`: `create_playlist` (confirm) → verify in
   `list_playlists`.
5. `add_tracks_to_playlist` (confirm) → verify via `get_playlist`.
6. `remove_tracks_from_playlist` (confirm, exact `playlist_track_id`) → verify.
7. `delete_playlist` (confirm) → verify it is gone from `list_playlists`.

Record each step's observed result. Any step that fails or returns an
unexpected shape means the observed endpoints have drifted — stop, do not
retry writes, and record the deviation before changing adapter constants.

## Limitations and risks

- Endpoint paths, parameters, and response shapes are transcribed from
  third-party client code and are unverified against the live service.
  A second source-only cross-check (tidalf raw.py, Minim docs, Music
  Assistant, gobuz) confirmed `playlist/create`, `addTracks` with catalog
  `track_ids`, and `deleteTracks` consuming playlist-entry IDs from
  `playlist/get`. Open items for the validation gate: the required `type`
  value on `playlist/getUserPlaylists` (provisionally `user`), and
  `catalog/search` vs the older `search/getResults` (query + type).
- The session route is not Qobuz-issued authorization. Qobuz may change or
  block access at any time and could restrict accounts using unofficial
  clients — you accept that risk. Stop using this server if access is
  blocked or Qobuz objects.
- Not suitable for production, shared hosting, or anyone other than the
  local operator.
