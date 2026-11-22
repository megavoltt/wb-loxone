# Protocol and settings

For those who want to connect the bridge to something other than Loxone, or
to build something of their own.

## WB → Loxone: UDP

The bridge sends one packet per change to `<miniserver>:<udp_port>`:

```
wb.<device>.<channel>=<number>
```

* `<device>` and `<channel>` are the MQTT IDs transliterated to Latin:
  Cyrillic is transliterated, everything except `A–Z a–z 0–9 _ -` becomes `_`.
  `Терморегулятор/Температура в помещении` → `wb.Termoregulyator.Temperatura_v_pomeschenii`.
* Button events: `wb.<device>.Input_1_click`, `…_double`, `…_triple`,
  `…_long`. A pulse is two packets, `=1` and, `pulse_ms` later, `=0`.
* Values: numbers as is (dot as the decimal separator); `true/on` → `1`,
  `false/off` → `0`; RGB `R;G;B` → `BBBGGGRRR` in percent (Loxone format).
* Text that isn't a number is sent as is:
  `wb.<device>.<channel>=<text>` — the string in the same form as from the
  device (Cyrillic and spaces are converted to Latin/`_` by the same rules).
  In the template such a channel gets one digital input per known value: the
  bridge knows typical values by channel name (`action`, `mode`, `state`…)
  and remembers new ones in the config (`textvals`) so they appear in the
  template. Strings longer than 32 characters or without a single Latin
  letter/digit are not sent.

When it is sent:

* on every change of a value in MQTT (for text channels — also on a repeat of
  the same value: that is a new event, not a duplicate);
* no more often than `throttle_ms` per channel — the last value is sent when
  the window expires, intermediate ones are skipped. There is no limit for
  `switch`, `alarm`, `pushbutton`, text events (zigbee2mqtt `action` etc.)
  and button events;
* every `resync_s` — all selected values in a row.

## Loxone → WB: HTTP

```
GET /set/<key>/<device>/<channel>/<value>
```

`<device>` and `<channel>` are the original MQTT IDs, URL-encoded. The bridge
publishes the value to `/devices/<device>/controls/<channel>/on`.

| Response | When |
|---|---|
| `200 ok` | published |
| `403 wrong key` | the key didn't match |
| `403 channel not selected for Loxone` | the channel is not ticked in the UI |
| `403 channel is read-only` | `readonly` in the channel description |
| `404 no such channel` | not present in MQTT |
| `400 didn't understand the value` | not a number for a numeric channel |

Value conversion: for `switch`/`pushbutton`/`alarm` any non-zero → `1`; for
numeric channels — clamping to the channel's `min`/`max`; for `rgb` —
`BBBGGGRRR` → `R;G;B` (or `r;g;b` as is); for `text` — the string as is.

## Settings file

`/mnt/data/etc/wb-loxone.json`; a different path via the `WBLOX_CONFIG`
environment variable.

| Key | Default | What it is |
|---|---|---|
| `miniserver` | `""` | Miniserver IP |
| `udp_port` | `7001` | virtual UDP input port |
| `http_port` | `8099` | web UI and commands port (after changing — restart the service) |
| `wb_host` | `""` | WB address in the templates; empty — detected by the route to the Miniserver |
| `key` | random | the key in the command URL |
| `title` | `WirenBoard` | name of the common block |
| `throttle_ms` | `500` | no more than once per N ms per channel |
| `resync_s` | `300` | full resend every N s, `0` — off |
| `click_ms` | `250` | wait for the next press of a series |
| `long_ms` | `600` | long press threshold |
| `pulse_ms` | `150` | event pulse duration |
| `button_log` | `false` | detailed press log |
| `ui_password` | `""` | web UI password (Basic Auth, any username) |
| `selected` | `{}` | selected channels, `"device/channel": true` |
| `aliases` | `{}` | device names for Loxone |

Environment variables: `WBLOX_MQTT_HOST` (default `127.0.0.1`),
`WBLOX_MQTT_PORT` (`1883`).

## Web UI API

| Request | What it does |
|---|---|
| `GET /api/state` | devices, channels, values, settings, counters |
| `POST /api/config` | change settings (JSON with keys from the table above, except `key`) |
| `POST /api/newkey` | generate a new command key |
| `POST /api/resync` | resend all selected values |
| `POST /api/test` | `{"dev","ctrl","value"}` — a command as if from Loxone |
| `GET /template/viu.xml?dev=<id>` | device input template (without `dev` — the common one) |
| `GET /template/vo.xml?dev=<id>` | device output template (without `dev` — the common one) |
| `GET /template/all.zip` | all templates, per device |

If `ui_password` is set, all requests except `/set/…` require Basic Auth.

## Inside the bridge

A single file `wbloxone.py`, standard library only, Python 3.9:

* its own MQTT 3.1.1 client (QoS 0), subscribed to `/devices/#`;
* a model of devices and channels built from the `meta` topics (the new JSON
  format and the old separate `meta/type`, `meta/readonly`… topics);
* press recognition — the `Buttons` class; the `step(now)` tick doesn't
  depend on real time and is covered by tests (`tests/`);
* an HTTP server for the UI, commands and templates.

Thread stacks to the log — `kill -USR1 $(systemctl show -p MainPID --value wb-loxone)`.
