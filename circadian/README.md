# Circadian

Glides each light between hand-tuned keyframes through the day, so the house gets dimmer and warmer in the evening without anyone noticing a change. A sun-gated morning wake can turn a lit path on before you come downstairs.

- **Your changes win.** Anything changed by a person (voice with a level, a wall dimmer, an app) is held until that light is turned off and on, or a plain "on" is sent, or the morning reset.
- **Never turns a light on** outside a wake keyframe.
- **Kill switch:** turn off `input_boolean.circadian` (configurable) and it stops sending anything.
- **Boost:** turn on `input_boolean.circadian_boost` (configurable) on a dark day. The lights listed in `boost` ride the same curve, multiplied by `boost_factor` (default 1.5), with the schedule's colour; off lights that have a level right now come on. From sunset (or from when it was switched on, if later) it eases out to the next keyframe, then switches itself off. Switching it off by hand puts the house straight back and turns off what it turned on.
- **Shadow mode** (`shadow: true`, the default) logs what it would do and touches nothing.

Keyframes live in the app's options on your Home Assistant box, not in this repository.

## Keyframe format

```yaml
keyframes:
  - name: evening
    at: "19:00"            # or sunrise / sunset, with an offset: "sunset-00:30"
    lights:
      - light.example 40 3000       # brightness %, optional kelvin
  - name: night
    at: "21:00"
    lights:
      - light.example 8 2600
  - name: wake
    at: "07:00"
    step: true                      # don't glide into this keyframe; hold the previous value until it
    wake: true                      # may turn these lights on (dark mornings only, see below)
    wake_if_sunrise_after: "06:30"
    lights:
      - light.example 30 3300
      - light.bulb_behind_switch 50 3300 via light.switch_wrapper   # power it via another entity
      - switch.some_power on
      - light.later off             # not yet; a later wake keyframe brings it in
```

### Brightness and color on their own schedules

A keyframe sets both by default. `channel: brightness` sets only brightness; `channel: color` sets only
color, written `light.x - 3200`. Each light's brightness follows the keyframes that set brightness and its
color follows those that set color, so the evening can warm from sunset while dimming starts at a set time:

```yaml
  - {name: dusk, at: "18:30", channel: brightness, lights: [light.example 80]}
  - {name: evening, at: sunset, channel: color, lights: [light.example - 3200]}
  - {name: night, at: "21:30", lights: [light.example 12 2600]}
```

`boost_ease_from` (default `sunset`) is when Bright House starts easing back; it settles by the next
keyframe that sets brightness.

