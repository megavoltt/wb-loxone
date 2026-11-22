# Wiren Board: input modes, events, the bus

The bridge works with whatever the controller publishes to MQTT
(`/devices/#`) and never talks to the RS-485 bus itself. So RS-485 speed and
reliability are determined by the `wb-mqtt-serial` driver and its settings.
Here's what affects them.

## Input mode and events

WB modules with firmware that supports **Fast Modbus** can report input
changes to the driver themselves — "events". The driver then learns about a
press within single-digit milliseconds, without waiting for its polling round.

Whether the driver enables events for an input depends on the **input mode**
in the device template. For example, on WB-MR6C and WB-MRM2-mini:

* mode **3** ("Control disabled, input measures frequency") — no events, the
  input is polled in a round-robin cycle;
* any other mode (0, 1, 2, 4, 6) — events are enabled.

If an input should work only through Loxone and not touch the module's relay,
set mode **4 "Control by mapping matrix"** and leave the matrix empty (all
actions = 0). Behaviourally this is the same as mode 3, but with events.

### How to switch

In the controller's web UI: **Settings → Config files → Serial device driver
configuration** → the needed port → device → the group of the needed input →
**Mode** = "Control by mapping matrix". Make sure the outputs in that group
have "no action" selected. Save — the driver restarts by itself.

The exact menu names depend on the firmware version. If the UI looks
different, the same can be done in the `/etc/wb-mqtt-serial.conf` file — set
the device parameters:

```json
"in2_mode": 4,
"in2_out1_actions": 0,
"in2_out2_actions": 0
```

and restart the driver: `systemctl restart wb-mqtt-serial`. Save a copy of
the file first. During the restart (a few seconds) polling of all devices on
the bus stops.

### How to check that events are enabled

```bash
journalctl -u wb-mqtt-serial --since "-2min" | grep "modbus:171"
```
```
INFO: [modbus] Init setup register "Mode": <modbus:171:holding: 10><-- 4 (0x4)
INFO: [serial client] Events are enabled for <modbus:171:discrete: 1>
INFO: [serial client] Events are enabled for <modbus:171:input: 33>
```

`discrete: N` is the input itself, `input: N` is its counter. If there is no
`Events are enabled` line for the input, the module firmware doesn't support
events — update it from the controller's web UI.

## The RS-485 bus

The driver polls the devices of a port one after another. One round gets
longer with the number of devices and channels and depends heavily on the
baud rate.

Rough figures for 9600 baud (estimates, not measurements):

* one exchange with a device — about 20–40 ms, i.e. 30–40 exchanges per
  second;
* one WB module per round — 3–6 exchanges;
* hence 8–10 devices per port, if values that don't arrive as events should
  update about once a second.

What helps, in order of usefulness:

1. **Events for inputs and relays** (see above). Buttons and relays then
   barely depend on the round length, and 15–20 devices can stay on one port.
2. **Fix bus errors.** Every timeout occupies the bus for the response
   waiting time. Watch `journalctl -u wb-mqtt-serial` — lines like
   `request timed out`, `invalid crc`, `failed to write`, repeating every few
   seconds.
3. **Spread devices across ports.** The controller has two RS-485 ports; they
   are polled in parallel.
4. **115200 baud** — the strongest lever, but all devices on the port must
   support it. Third-party devices often don't.
5. **Polling period** (`read_period_ms`) for slowly changing channels —
   temperature, humidity, energy. They can be read once every few seconds,
   freeing the bus for everything else.

Commands from Loxone are sent by the driver out of turn: a relay switches
quickly even on a busy bus.

## What shows up in MQTT

The bridge shows all devices from `/devices/#`, including system ones
(`system`, `network`, `wb-adc`, `wb-gpio`…). System devices are collected at
the bottom of the page.

zigbee2mqtt devices are named by their IEEE address (`0xa4c1…`). Give them a
readable "Name in Loxone" in the bridge's UI.

Text channels holding non-numeric values (for example, the Zigbee button
`action`: `single`, `double`) are sent to Loxone as text: every known value
becomes a separate digital input in the template (details —
[loxone.en.md](loxone.en.md#zigbee-buttons-and-switches-zigbee2mqtt) and
[protocol.en.md](protocol.en.md)). Values that can't be sent (longer than 32
characters or without a single Latin letter/digit) are marked "text — not
sent". `true`/`false` values are sent as `1`/`0`.
