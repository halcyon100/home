# home

Home automation code for the house, packaged as Home Assistant apps.

Add this repository in Home Assistant under **Settings → Apps → ⋮ → Repositories**:
`https://github.com/halcyon100/home`

| App | What it does |
|---|---|
| [Frame](frame/) | Samsung Frame controller: Art Mode watchdog, photo hanging, collections, brightness schedule |
| [Circadian](circadian/) | Glides lights between hand-tuned keyframes through the day, with a sun-gated morning wake; manual changes win |

## Shared UI

`shared/` holds the app bar (`appnav.js`) and typeface used by every app with a page (House, Frame). Edit it there, then run `tools/sync-shared` to copy it into each app; `tests/test_shared_in_sync.py` fails if a copy drifts.
