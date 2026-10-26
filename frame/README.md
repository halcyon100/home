# Frame

A small web app for a Samsung Frame TV hung in portrait. It runs as a Home Assistant app and is
reached at `http://<home-assistant>:8800`.

- Keeps the TV in Art Mode (pulls it back when Samsung's software switches to TV)
- Hangs photos: crop, frame and straighten them in the browser, uploaded at 2160×3840 with no matte
- Collections, and playing a collection on any interval
- Keeps each photo's original (resized to a 4096 px long edge, JPEG) and its frame/crop recipe, so
  any picture, or a whole collection, can be reframed later: Select → Reframe. The new version
  replaces the old one on the TV and keeps its place in collections. Photos hung before 1.1 have no
  original kept; re-add them to make them reframable.
- Brightness schedule: editable times anchored to sunrise, sunset or a clock time, each with its own
  brightness, light-sensor and warmth setting

Settings (Configuration tab): `tv_ip`, the Frame's address on the network.

State, cache and logs live in the app's `/data` folder, so they survive updates. On first start,
`seed_state.json` provides the collections and schedule carried over from the laptop version.

## Releasing a change
Bump `version` in `config.yaml`, push, then click **Update** on the app in Home Assistant.
