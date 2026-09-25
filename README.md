# atmotube-prometheus

Reads an **Atmotube PRO** air sensor over Bluetooth LE and hands its readings to
**Prometheus** through node_exporter's textfile collector — PM1/PM2.5/PM10, VOC,
temperature, humidity, pressure, battery — plus threshold alerts to any command
you like. One Python file, no cloud, no Home Assistant required.

---

## Why this exists

I wanted the air in my living room next to everything else my homelab already
graphs, with an alert when it stays bad — not an app I have to open. The Atmotube
PRO broadcasts its readings over BLE anyway. The existing integration is for
Home Assistant, which I don't run.

The part that took longer than expected is a quirk of how the device talks.
It sends its readings in **two** packets — an advertisement (VOC, temperature,
humidity, pressure, battery) and a scan response (PM1/2.5/10) — and **both carry
manufacturer ID `0xFFFF`**. The Linux kernel merges advertisement and scan
response into one report, and BlueZ keeps manufacturer data in a map keyed by
ID. Same key twice: one packet can overwrite the other before any program sees
it, and you silently lose half the readings. On my Raspberry Pi 5 both arrive.
Whether they do on yours depends on the controller and on timing, so the
exporter doesn't assume — it notices when one packet type stays away and reads
the missing values over a short GATT connection instead. `--diagnose` shows you
which case you're in.

## How it works

- **Listen, then fill gaps.** Every round (30 s) it listens for 20 s. If base or
  PM packets have been missing for two minutes, it connects, reads four
  characteristics and disconnects. Advertisements always win; GATT only fills
  gaps, so graphs don't jump between two sources.
- **No made-up values.** A reading older than its maximum age (10 min, 30 min
  for PM — the sensor measures at intervals) is **left out** of the file, not
  repeated. A gap in Grafana is honest. A flat line holding the last value looks
  exactly like clean air.
- **Implausible data is dropped.** `0xFFFF` is the manufacturer ID "for
  testing" and other devices use it too. Only the name `ATMOTUBE` or the
  Atmotube service UUID counts, and every value is checked against the sensor's
  physical limits.
- **Atomic writes, mode 644.** node_exporter never reads half a file, and the
  file stays readable under a hardened unit with `UMask=0077`.
- **Alerts with hysteresis.** Alert from HIGH after a minimum duration, clear
  only below OK — a value hovering around the threshold doesn't flap. An unknown
  value never clears an alert: without a reading, nobody knows the air got
  better. Quiet hours postpone, they don't drop.
- **No Bluetooth, no crash.** `atmotube_bluetooth_up` goes to 0 and it retries
  every minute. A service stuck in a restart loop can look "active" in
  `systemctl` between crashes; this one keeps writing its heartbeat.

The first evening produced both cases the design is for. PM2.5 went to
185 µg/m³ within minutes of setup — real, just unflattering — and the alert
arrived after the configured ten minutes. Later, fifteen minutes of complete
silence: the Atmotube phone app had connected, and while a phone holds the
connection the device stops advertising. That's what `AtmotubeNoReadings` in
the Prometheus rules is for, and why a failed GATT read while the app is open is
expected, not an error.

## Metrics

Example file content, shortened (`# HELP`/`# TYPE` lines and some samples omitted):

```
atmotube_last_run_timestamp_seconds 1790380030
atmotube_bluetooth_up 1
atmotube_device_seen 1
atmotube_last_reading_timestamp_seconds 1790380026
atmotube_last_pm_reading_timestamp_seconds 1790380026
atmotube_temperature_celsius 22
atmotube_humidity_percent 47
atmotube_pressure_hpa 968.1
atmotube_voc_ppb 212
atmotube_battery_percent 90
atmotube_charging 0
atmotube_pm_sensor_active 1
atmotube_voc_ready 1
atmotube_device_error 0
atmotube_pm_ugm3{size="PM1"} 3
atmotube_pm_ugm3{size="PM2.5"} 5
atmotube_pm_ugm3{size="PM10"} 7
atmotube_air_quality_score 87
atmotube_rssi_dbm -62
atmotube_advertisements_total{type="base"} 55
atmotube_advertisements_total{type="pm"} 1482
atmotube_gatt_reads_total{result="ok"} 1
atmotube_gatt_reads_total{result="error"} 1
atmotube_alert_active{kind="pm25"} 0
atmotube_alert_threshold{kind="pm25",level="high"} 25
atmotube_alert_threshold{kind="pm25",level="ok"} 15
atmotube_info{firmware="74051E",source="advertisement"} 1
```

`atmotube_air_quality_score` is 0–100 the way the Atmotube app computes it: the
worst sub-score of VOC, PM1, PM2.5 and PM10. The thresholds are exported too, so
a dashboard can colour by them instead of keeping a copy that drifts the day you
change one.

## Setup (Raspberry Pi OS / Debian)

```bash
sudo apt install python3-bleak prometheus-node-exporter
sudo useradd --system --no-create-home --shell /usr/sbin/nologin --groups bluetooth atmotube
sudo git clone https://github.com/DanielEnki420/atmotube-prometheus /opt/atmotube-prometheus

# let the exporter write into node_exporter's textfile directory
sudo chgrp atmotube /var/lib/prometheus/node-exporter
sudo chmod g+w /var/lib/prometheus/node-exporter
```

Find your device and see which data path works on your machine:

```bash
sudo -u atmotube python3 /opt/atmotube-prometheus/atmotube.py --diagnose
```

```
  AA:BB:CC:DD:EE:FF  ATMOTUBE    -58 dBm  12 bytes  base        01039E321A19000140EA4164
      -> 25 °C · 26 % · 821.5 hPa · VOC 0.259 ppm · battery 100 % · status 0x41 (pm_on, voc_ready)
  AA:BB:CC:DD:EE:FF  ATMOTUBE    -58 dBm   9 bytes  pm          00030005000774051E
      -> PM1 3 / PM2.5 5 / PM10 7 µg/m³ · firmware 74051E

Atmotube PRO: AA:BB:CC:DD:EE:FF
  ✓ Base readings (VOC, temperature, humidity, pressure, battery) arrive by advertisement
  ✓ PM readings arrive by advertisement

GATT read from AA:BB:CC:DD:EE:FF ...
  BME280   8 bytes  1A19EA400100D009           -> 25.12 °C · 26 % · 821.5 hPa
  Status   2 bytes  4164                       -> battery 100 % · status 0x41 (pm_on, voc_ready)
  PM      12 bytes  2C0100F40100BC02004C0400   -> PM1 3 / PM2.5 5 / PM10 7 µg/m³
  VOC      4 bytes  06010000                   -> VOC 0.262 ppm

  VOC cross-check: advertisement 259 ppb, GATT 262 ppb - should be close.

To pin this device, put into /etc/atmotube.env:
  ATMOTUBE_MAC=AA:BB:CC:DD:EE:FF
```

Close the Atmotube phone app first — while it's connected, the device doesn't
advertise. Then configure and start:

```bash
sudo cp /opt/atmotube-prometheus/deploy/atmotube.env.example /etc/atmotube.env
sudo chmod 600 /etc/atmotube.env
sudoedit /etc/atmotube.env        # at least ATMOTUBE_MAC
sudo cp /opt/atmotube-prometheus/deploy/atmotube.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now atmotube
journalctl -u atmotube -f
```

Pin the address. Without `ATMOTUBE_MAC` the exporter takes the first Atmotube
it hears — possibly your neighbour's — and says so in the log.

Then add `prometheus/atmotube-rules.yml` to your Prometheus `rule_files` and
import `grafana/atmotube-dashboard.json` into Grafana (it asks for your
Prometheus data source).

### About the hardening

The unit runs under a strict systemd sandbox. One line in it matters more than
the rest: `ReadWritePaths=` for the textfile directory. `ProtectSystem=strict`
makes `/var` read-only, and a write that fails there doesn't make anything go
red: the old `.prom` file stays, node_exporter keeps serving it, and Grafana
draws flat, perfectly plausible lines. I found exactly that on the same box,
with a different exporter, fifteen days after hardening it. If your textfile
directory lives elsewhere, add it there. `AtmotubeExporterStalled` catches it
if you don't.

## Alerts

Set `ATMOTUBE_NOTIFY_CMD` to any executable. It's called as
`<command> <title> <text>` — argument list, no shell, 30 s timeout. Without it,
alerts are only logged, and still visible as `atmotube_alert_active`.

`examples/notify-telegram.sh` sends to a Telegram chat. Put `TELEGRAM_BOT_TOKEN`
and `TELEGRAM_CHAT_ID` into `/etc/atmotube.env`. The script hands the token to
curl on stdin rather than as an argument, because the arguments of running
processes are visible to every user via `ps`.

| Alert | Fires | Clears |
|---|---|---|
| PM2.5 | ≥ 25 µg/m³ for 10 min | < 15 for 10 min |
| VOC | ≥ 1 ppm for 15 min (only once the sensor has warmed up) | < 0.5 ppm for 10 min |
| Humidity | ≥ 65 % for 30 min | < 60 % for 10 min |
| Battery | ≤ 15 % for 5 min (not while charging) | > 30 % for 5 min |

Quiet hours default to 22–7: whatever becomes due at night is sent in the
morning if it still applies. If sending fails, the alert state doesn't change
and the next attempt comes ten minutes later.

## Configuration

All settings are environment variables; `deploy/atmotube.env.example` lists
every one with its default. The ones you're most likely to touch:

| Variable | Default | |
|---|---|---|
| `ATMOTUBE_MAC` | *(first one heard)* | Pin your device |
| `ATMOTUBE_METRICS_FILE` | `/var/lib/prometheus/node-exporter/atmotube.prom` | Your textfile collector directory |
| `ATMOTUBE_NOTIFY_CMD` | *(none)* | Alert command |
| `ATMOTUBE_ROOM` | *(none)* | Prefix for alert titles |
| `ATMOTUBE_LANG` | `en` | Alert texts: `en` or `de` |
| `ATMOTUBE_QUIET_HOURS` | `22-7` | Empty = none |
| `ATMOTUBE_GATT` | `auto` | `never` = listen only |
| `ATMOTUBE_PM25_HIGH` / `_OK` | `25` / `15` | Likewise `VOC`, `HUMIDITY`, `BATTERY_LOW`/`_OK` |

## Tests

```bash
python3 -m unittest discover -s tests -v
```

No Bluetooth and no bleak needed — the radio is replaced by stubs. The decoding
tests run on **real captures** from [ha-atmo](https://github.com/natekspencer/ha-atmo),
not on bytes I made up: a hand-built packet only proves that the parser and the
test share the same misunderstanding. The notify tests include one that passes
`; touch x` and `$(id)` through and checks that nothing gets executed.

## Limitations

- **Atmotube PRO only.** The PRO 2 uses a different protocol; `--diagnose`
  detects it and says so, the exporter ignores it.
- **Temperature from advertisements is whole degrees.** GATT reads give
  1/100 °C.
- **On the charger the Atmotube warms itself**, so temperature and humidity are
  slightly off while charging.
- **GATT reads fail while the phone app is connected** — and the device stops
  advertising altogether. Close the app when you don't need it.
- **The VOC GATT format is the one field not backed by a capture.** ha-atmo
  reads it the same way but never uses it; `--diagnose` compares it with the
  advertised VOC so you can see whether they agree.
- **Tested on** a Raspberry Pi 5, Raspberry Pi OS (Debian trixie), BlueZ 5.82,
  bleak 0.22, with node_exporter in Docker. Other setups should work; reports
  welcome.

## Credits

Packet layout from Atmotube's own library,
[atmotube-android-ble](https://github.com/atmotube/atmotube-android-ble)
(Apache 2.0; no code copied). Test captures and GATT formats from
[ha-atmo](https://github.com/natekspencer/ha-atmo) by Nathan Spencer (MIT).
See `NOTICE`.

Atmotube is a trademark of Atmotube, Inc. This project is not affiliated with or
endorsed by Atmotube, Inc.

## License

MIT
