# House

A portal to the house, opened from the Home Assistant sidebar (or a phone home-screen icon). It shows today's light from Circadian as one drawing, coloured by the light's actual colour temperature, plus shortcuts, motion, curtains and a filtered record of the day.

- Runs inside Home Assistant through ingress: no ports, Home Assistant handles sign-in.
- `panel_admin: false`, so non-admin household members see it too.
- Only the shortcuts and shades listed in the options can be run from the page.
- Which entities appear is set in the app options on your Home Assistant box, not in this repository.
- Type: Archivo (SIL Open Font License, `www/fonts/OFL.txt`).
