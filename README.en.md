# wb-loxone — Wiren Board devices in Loxone

[Русская версия](README.md) · Full documentation is in Russian (`docs/`).

A bridge that runs on a [Wiren Board](https://wirenboard.com) controller and
exposes its devices — relays, inputs, sensors, meters, Zigbee via zigbee2mqtt,
wb-rules virtual devices, anything published to MQTT — to a
[Loxone](https://www.loxone.com) Miniserver.

```
 WB devices ──► MQTT ──► wb-loxone ──UDP──► Virtual UDP input   ┐
                         (on WB)                                 │ Loxone
             ◄───────────────────◄──HTTP── Virtual output        ┘ Miniserver
```

* **States are pushed on change** over UDP — no polling from Loxone.
* **Commands** from Loxone virtual outputs take a few milliseconds inside the bridge.
* **Per-device templates** for Loxone Config (`VIU_<name>.xml`, `VO_<name>.xml`),
  generated in the web UI.
* **Buttons:** single, double, triple and long press are detected from the
  module's hardware pulse counter, so short presses are not lost on a slow RS-485
  bus. Each event reaches Loxone as a separate pulse.
* **Nothing to install:** a single Python 3.9 file using only the standard library.

## Install

On the Wiren Board controller, as root:

```bash
wget -qO- https://raw.githubusercontent.com/megavoltt/wb-loxone/main/install.sh | sh
```

Then open `http://<controller IP>:8099`, set the Miniserver IP, select channels,
download the templates and import them in Loxone Config
(Virtual Inputs / Virtual Outputs → Import template).

Running the same command again updates the bridge and keeps the settings.

## Buttons in short

* Press count comes from `Input N counter`, hold time from `Input N`.
* After release the bridge waits `click_ms` (250 ms) for the next press — but only
  if a longer series (double/triple) is selected for Loxone; otherwise the event
  is sent immediately.
* Long press fires after `long_ms` (600 ms) while the button is still held.
* For fast inputs set the module input mode to **4 (mapping matrix, empty)**
  instead of 3 (control disabled): only then does `wb-mqtt-serial` use Fast Modbus
  events for the input instead of polling it.

## Security

Intended for a local network. The web UI has no password by default (set one in
the settings); commands require a key in the URL and are accepted only for
selected writable channels. Do not expose the port to the internet.

## License

[MIT](LICENSE)
