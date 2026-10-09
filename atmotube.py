#!/usr/bin/env python3
"""
Atmotube PRO -> Prometheus (node_exporter textfile collector) + alerts.

WHAT IT DOES
  Listens to an Atmotube PRO over Bluetooth LE, writes its readings every 30 s
  to a .prom file that node_exporter's textfile collector picks up, and runs a
  command of your choice when the air stays bad: PM2.5, VOC, humidity, battery.

WHERE THE READINGS COME FROM - two paths
  1. Advertisements, passive. The Atmotube broadcasts its readings unasked in
     two BLE packets, BOTH with manufacturer ID 0xFFFF:
       advertisement  12 bytes: VOC, device id, humidity, temperature,
                                pressure, status byte, battery
       scan response   9 bytes: PM1, PM2.5, PM10, firmware
     Format from Atmotube's own library (github.com/atmotube/
     atmotube-android-ble, AtmotubeUtils.java). The tests check it against a
     real capture from github.com/natekspencer/ha-atmo.
  2. A short GATT read. Why it exists: the Linux kernel merges advertisement
     and scan response into ONE event, and BlueZ keeps only ONE record per
     manufacturer ID. Because both packets carry 0xFFFF, the scan response can
     overwrite the advertisement before any program sees it - then only the
     PM values arrive. Whether that happens on your machine: `--diagnose`.
     If one packet type stays away for longer than ATMOTUBE_GATT_AFTER, the
     exporter connects briefly, reads four characteristics and disconnects.
     Formats from ha-atmo. Rule: advertisements win, GATT only fills gaps - so
     the graphs don't jump back and forth between two sources.

WHAT IT DELIBERATELY DOES NOT DO
  - Make up values. A value that is unknown or older than its maximum age is
    NOT written. A gap in Grafana is honest; a flat line repeating the last
    value looks exactly like clean air.
  - Crash without Bluetooth. atmotube_bluetooth_up goes to 0 and it retries
    every 60 s. A restart loop can look "active" in systemctl between crashes.
  - Read the Atmotube PRO 2. It uses a different protocol; --diagnose detects
    it and says so.

Usage:
  atmotube.py                        run (systemd: atmotube.service)
  atmotube.py --diagnose [seconds]   listen (default 30 s), print every packet
                                     raw and decoded, try one GATT read.
Configuration: ATMOTUBE_* environment variables, see atmotube.env.example.
"""

import asyncio
import logging
import os
import shlex
import signal
import struct
import subprocess
import sys
import time

__version__ = "0.1.1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [atmotube] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("atmotube")


def _number(name, default):
    """A number from the environment. A typo in the env file must not send the
    service into a restart loop - it falls back to the default, loudly."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r is not a number - using %s", name, raw, default)
        return default


# ── Paths (overridable, so the tests run without hardware) ──────────────────
# Debian's prometheus-node-exporter reads /var/lib/prometheus/node-exporter.
METRICS_FILE = os.environ.get("ATMOTUBE_METRICS_FILE", "/var/lib/prometheus/node-exporter/atmotube.prom")
# Called as: <command> <title> <text>. Empty = alerts are only logged (and
# still visible as atmotube_alert_active in Grafana).
NOTIFY_CMD = os.environ.get("ATMOTUBE_NOTIFY_CMD", "").strip()
ENV_FILE = "/etc/atmotube.env"

# ── Settings ────────────────────────────────────────────────────────────────
# Empty = take the first Atmotube that shows up. --diagnose prints the
# address; pinned, the exporter stays with your device even if a neighbour
# has one too.
MAC = os.environ.get("ATMOTUBE_MAC", "").strip().upper()
ROOM = os.environ.get("ATMOTUBE_ROOM", "").strip()
# Language of the alert texts: en or de. Metrics and logs stay English.
LANG = os.environ.get("ATMOTUBE_LANG", "en").strip().lower()
# No notifications between 22:00 and 07:00. Whatever becomes due at night is
# sent in the morning if it still applies. Empty = no quiet hours.
QUIET_HOURS = os.environ.get("ATMOTUBE_QUIET_HOURS", "22-7").strip()
# "auto": read via GATT when advertisements don't deliver both packet types.
# "never": listen only (then VOC & co. may be missing on merged packets).
GATT_MODE = os.environ.get("ATMOTUBE_GATT", "auto").strip().lower()

LISTEN = _number("ATMOTUBE_LISTEN_SECONDS", 20)    # seconds of listening per round ...
PAUSE = _number("ATMOTUBE_PAUSE_SECONDS", 10)      # ... then a pause: one round = 30 s
# The scanner is restarted after every round. That costs nothing and avoids
# the known BlueZ case of a long discovery silently going to sleep.
MAX_AGE_BASE = _number("ATMOTUBE_MAX_AGE", 600)    # s, after that a value counts as unknown
# Depending on the app setting the PM sensor only measures every few minutes
# and reports "off" (0xFFFF) in between - so a PM value may be older.
MAX_AGE_PM = _number("ATMOTUBE_MAX_AGE_PM", 1800)
GATT_AFTER = _number("ATMOTUBE_GATT_AFTER", 120)        # s without a packet type from advertisements
GATT_INTERVAL = _number("ATMOTUBE_GATT_INTERVAL", 120)  # connect at most this often

# Alert thresholds. Two per value: alert from HIGH, clear only below OK.
# PM2.5: the WHO guideline is 15 µg/m³ as a 24-hour mean. 25 for ten minutes
# is no longer a cooking spike.
PM25_HIGH, PM25_OK = _number("ATMOTUBE_PM25_HIGH", 25), _number("ATMOTUBE_PM25_OK", 15)
# VOC in ppb. Atmotube scores 0.5 ppm at 70 of 100 points, 1 ppm at 61 -
# from there on, ventilating makes sense.
VOC_HIGH, VOC_OK = _number("ATMOTUBE_VOC_HIGH", 1000), _number("ATMOTUBE_VOC_OK", 500)
# Above roughly 65 % relative humidity the risk of mould rises indoors.
HUMIDITY_HIGH, HUMIDITY_OK = _number("ATMOTUBE_HUMIDITY_HIGH", 65), _number("ATMOTUBE_HUMIDITY_OK", 60)
BATTERY_LOW, BATTERY_OK = _number("ATMOTUBE_BATTERY_LOW", 15), _number("ATMOTUBE_BATTERY_OK", 30)
RETRY_AFTER = 600   # s until the next attempt when a notification fails

# ── Protocol ────────────────────────────────────────────────────────────────
MANUFACTURER_ID = 0xFFFF
SERVICE_PRO = "db450001-8e9a-4818-add7-6ed94a328ab4"
# PRO 2: different protocol (github.com/atmotube/atmotube-pro2-android).
# Detected, not read.
SERVICE_PRO2 = "bda3c091-e5e0-4dac-8170-7fcef187a1d0"
CHAR_VOC = "db450002-8e9a-4818-add7-6ed94a328ab4"
CHAR_BME280 = "db450003-8e9a-4818-add7-6ed94a328ab4"
CHAR_STATUS = "db450004-8e9a-4818-add7-6ed94a328ab4"
CHAR_PM = "db450005-8e9a-4818-add7-6ed94a328ab4"
CHAR_NAMES = {CHAR_BME280: "BME280", CHAR_STATUS: "Status", CHAR_PM: "PM", CHAR_VOC: "VOC"}

BASE_FIELDS = ("voc_ppb", "temperature", "humidity", "pressure_hpa", "status", "battery")
PM_FIELDS = ("pm1", "pm25", "pm10")


# ── Decoding ────────────────────────────────────────────────────────────────
def status_bits(status):
    """The status byte. Meaning from AtmotubeInfo.java (firmware 74xxxx = PRO)."""
    return {
        "pm_on":     bool(status & 0x01),
        "error":     bool(status & 0x02),
        "bonded":    bool(status & 0x04),
        "charging":  bool(status & 0x08),
        # Bit 6 set: the VOC sensor has warmed up. Right after power-on it
        # calibrates itself, its readings are not reliable yet.
        "voc_ready": bool(status & 0x40),
    }


def _plausible(values):
    """Limits of the built-in sensors. Protects against other devices that also
    send the testing manufacturer ID 0xFFFF, and against wrong assumptions
    about a format: rather no value than a made-up one."""
    limits = {"humidity": (0, 100), "temperature": (-40, 85), "pressure_hpa": (300, 1100),
              "battery": (0, 100), "voc_ppb": (0, 60000),
              "pm1": (0, 1000), "pm25": (0, 1000), "pm10": (0, 1000)}
    return all(lo <= values[k] <= hi for k, (lo, hi) in limits.items() if values.get(k) is not None)


def decode_base(d):
    """Advertisement, 12 bytes after the manufacturer ID. All big-endian.
    VOC in ppb; temperature in whole degrees only; pressure in Pa (= hPa * 100)."""
    if len(d) != 12:
        return None
    v = {
        "voc_ppb":      int.from_bytes(d[0:2], "big"),
        "device_id":    d[2:4].hex().upper(),
        "humidity":     d[4],
        "temperature":  float(int.from_bytes(d[5:6], "big", signed=True)),
        "pressure_hpa": int.from_bytes(d[6:10], "big") / 100,
        "status":       d[10],
        "battery":      d[11],
    }
    return v if _plausible(v) else None


def decode_pm(d):
    """Scan response, 9 bytes: PM1, PM2.5, PM10 (2 bytes big-endian each, whole
    µg/m³) and the firmware (3 bytes). 0xFFFF = sensor currently off - then
    there is no value, but the packet is valid."""
    if len(d) != 9:
        return None
    raw = [int.from_bytes(d[i:i + 2], "big") for i in (0, 2, 4)]
    v = {"firmware": d[6:9].hex().upper(), "pm1": None, "pm25": None, "pm10": None}
    if 0xFFFF in raw:
        return v
    v["pm1"], v["pm25"], v["pm10"] = (float(x) for x in raw)
    return v if _plausible(v) else None


def decode_manufacturer_data(d):
    """-> (kind, base, pm). kind: base | pm | combined | unknown | implausible.
    21 bytes = both packets back to back; some stacks deliver it that way."""
    d = bytes(d)
    if len(d) == 12:
        b = decode_base(d)
        return ("base" if b else "implausible"), b, None
    if len(d) == 9:
        p = decode_pm(d)
        return ("pm" if p else "implausible"), None, p
    if len(d) == 21:
        b, p = decode_base(d[:12]), decode_pm(d[12:])
        return ("combined" if b and p else "implausible"), b, p
    return "unknown", None, None


def decode_bme280_gatt(d):
    """GATT characteristic BME280: humidity, coarse temperature, pressure in Pa
    (int32), temperature in 1/100 degree - little-endian (ha-atmo: "<bbih")."""
    if len(d) >= 8:
        humidity, _coarse, pressure, fine = struct.unpack("<Bbih", bytes(d[:8]))
        v = {"humidity": humidity, "temperature": fine / 100, "pressure_hpa": pressure / 100}
    elif len(d) >= 6:
        humidity, coarse, pressure = struct.unpack("<Bbi", bytes(d[:6]))
        v = {"humidity": humidity, "temperature": float(coarse), "pressure_hpa": pressure / 100}
    else:
        return None
    return v if _plausible(v) else None


def decode_status_gatt(d):
    """GATT characteristic status: status byte, battery in %."""
    if len(d) < 2:
        return None
    v = {"status": d[0], "battery": d[1]}
    return v if _plausible(v) else None


def decode_pm_gatt(d):
    """GATT characteristic PM: 3 bytes little-endian each in 1/100 µg/m³, order
    PM1, PM2.5, PM10 (then PM4, not needed here). 0xFFFFFF = off."""
    if len(d) < 9:
        return None
    raw = [int.from_bytes(bytes(d[i:i + 3]), "little") for i in (0, 3, 6)]
    if 0xFFFFFF in raw:
        return {"pm1": None, "pm25": None, "pm10": None}
    v = {"pm1": raw[0] / 100, "pm25": raw[1] / 100, "pm10": raw[2] / 100}
    return v if _plausible(v) else None


def decode_voc_gatt(d):
    """GATT characteristic VOC, ppb in the first 2 bytes, little-endian.
    ⚠️ The only format NOT backed by a capture: ha-atmo reads it this way
    ("<hh") but never uses it. --diagnose compares it with the VOC from the
    advertisement as soon as both are available."""
    if len(d) < 2:
        return None
    v = {"voc_ppb": int.from_bytes(bytes(d[0:2]), "little")}
    return v if _plausible(v) else None


# ── Air quality score as in the Atmotube app ────────────────────────────────
# Points 0-100 from AtmotubeUtils.getAQS(): the worst sub-score counts.
PM1_STEPS = (14, 34, 61, 95, 100)
PM25_STEPS = (20, 50, 90, 140, 170)
PM10_STEPS = (30, 75, 125, 200, 250)


def _score_voc(ppm):
    if ppm < 0.5:
        p = 100 - 60 * ppm
    elif ppm < 2:
        p = (118 - 26 * ppm) / 1.5
    else:
        p = (374 - 44 * ppm) / 6.5
    return max(int(p), 0)


def _score_pm(pm, steps):
    i = next((i for i, s in enumerate(steps) if pm <= s), len(steps) - 1)
    lower = steps[i - 1] if i > 0 else 0
    return max(int(100 - 20 * i - 20 * ((pm - lower) / (steps[i] - lower))), 0)


def air_quality(voc_ppb, pm1, pm25, pm10):
    if None in (voc_ppb, pm1, pm25, pm10):
        return None
    return min(_score_voc(voc_ppb / 1000), _score_pm(pm1, PM1_STEPS),
               _score_pm(pm25, PM25_STEPS), _score_pm(pm10, PM10_STEPS))


# ── State ───────────────────────────────────────────────────────────────────
class State:
    def __init__(self, start):
        self.start = start
        self.address = None       # the Atmotube this exporter listens to
        self.device = None        # its bleak device object (for GATT reads)
        self.others = set()
        self.pro2_reported = False
        self.values = {}          # field -> (value, time, source)
        self.firmware = None
        self.rssi = None
        self.heard = None         # last packet of any kind
        self.adv_base = None      # last base values FROM ADVERTISEMENTS
        self.adv_pm = None        # last PM packet from advertisements (incl. "sensor off")
        self.last_gatt = 0.0
        self.bluetooth_up = False
        self.bt_error = None
        self.packets = {"base": 0, "pm": 0, "combined": 0, "unknown": 0, "implausible": 0}
        self.gatt_reads = {"ok": 0, "error": 0}

    def set(self, field, value, now, source):
        if value is not None:
            self.values[field] = (value, now, source)

    def value(self, field, now):
        """Only fresh values. Anything older than its maximum age counts as
        unknown - the exporter does not pass it on."""
        e = self.values.get(field)
        if not e:
            return None
        limit = MAX_AGE_PM if field in PM_FIELDS else MAX_AGE_BASE
        return e[0] if now - e[1] <= limit else None

    def fresh_from_advertisement(self, field, now):
        e = self.values.get(field)
        return bool(e) and e[2] == "advertisement" and now - e[1] <= GATT_AFTER

    def time_of(self, fields):
        times = [self.values[f][1] for f in fields if f in self.values]
        return max(times) if times else None

    def source(self):
        e = self.values.get("humidity") or self.values.get("voc_ppb")
        return e[2] if e else "none"


def on_advertisement(s, address, name, uuids, manufacturer_data, rssi, now, device=None):
    """Process one advertisement. Returns what happened to it - for the tests
    and for --diagnose."""
    uuids = [str(u).lower() for u in (uuids or [])]
    if SERVICE_PRO2 in uuids:
        if not s.pro2_reported:
            log.warning("%s is an Atmotube PRO 2 - different protocol, not read", address)
            s.pro2_reported = True
        return "pro2"
    data = (manufacturer_data or {}).get(MANUFACTURER_ID)
    if data is None:
        return "no-data"
    # 0xFFFF is the manufacturer ID "for testing" - others use it too. Only
    # the name or the service UUID makes it an Atmotube.
    if not ((name or "").upper().startswith("ATMOTUBE") or SERVICE_PRO in uuids):
        return "not-atmotube"
    address = address.upper()
    if MAC and address != MAC:
        return "other-address"
    if s.address is None:
        s.address = address
        log.info("Atmotube found: %s (signal %s dBm)", address, rssi)
    elif address != s.address:
        if address not in s.others:
            s.others.add(address)
            log.info("Ignoring another Atmotube %s (listening to %s)", address, s.address)
        return "other-atmotube"

    s.device = device or s.device
    s.rssi, s.heard = rssi, now
    kind, base, pm = decode_manufacturer_data(data)
    s.packets[kind] += 1
    if base:
        for field in BASE_FIELDS:
            s.set(field, base[field], now, "advertisement")
        s.adv_base = now
    if pm:
        s.firmware = pm["firmware"]
        s.adv_pm = now
        for field in PM_FIELDS:
            s.set(field, pm[field], now, "advertisement")
    return kind


def gatt_due(s, now):
    if GATT_MODE != "auto" or s.address is None:
        return False
    # Only while the device is in range at all - otherwise a connection
    # attempt would run into the void every two minutes.
    if s.heard is None or now - s.heard > 300:
        return False
    if now - s.last_gatt < GATT_INTERVAL:
        return False
    # Due as soon as EITHER packet type stays away. Which one the kernel
    # swallows depends on which comes last in the merged event.
    older = min(s.adv_base or s.start, s.adv_pm or s.start)
    return now - older > GATT_AFTER


def apply_gatt(s, raw, now):
    """Merge GATT answers. Advertisements win, GATT fills gaps. Returns whether
    at least one value was read."""
    parts = [decode_bme280_gatt(raw.get(CHAR_BME280, b"")),
             decode_status_gatt(raw.get(CHAR_STATUS, b"")),
             decode_pm_gatt(raw.get(CHAR_PM, b"")),
             decode_voc_gatt(raw.get(CHAR_VOC, b""))]
    read = False
    for part in parts:
        if not part:
            continue
        read = True
        for field, value in part.items():
            if not s.fresh_from_advertisement(field, now):
                s.set(field, value, now, "gatt")
    return read


# ── Alerts ──────────────────────────────────────────────────────────────────
class Alert:
    """An alert with two thresholds and a minimum duration.

    Two thresholds, so a value hovering around 25 doesn't send "high" and
    "back to normal" every few minutes. The minimum duration filters short
    spikes (searing, blowing out a candle). An unknown value does NOT clear
    an alert: without a reading nobody knows whether the air got better."""

    def __init__(self, kind, high, ok, duration, duration_ok, text_high, text_ok, downward=False):
        self.kind, self.high, self.ok = kind, high, ok
        self.duration, self.duration_ok = duration, duration_ok
        self.text_high, self.text_ok = text_high, text_ok
        self.downward = downward      # battery: bad means LOW
        self.active = False
        self.bad_since = self.ok_since = None
        self.next_attempt = 0.0

    def _bad(self, v):
        return v <= self.high if self.downward else v >= self.high

    def _good(self, v):
        return v > self.ok if self.downward else v < self.ok

    def check(self, value, now, quiet):
        """-> (new_state, title, text) when a notification is due now."""
        if value is None:
            self.bad_since = self.ok_since = None
            return None
        if self._bad(value):
            self.ok_since = None
            if self.bad_since is None:
                self.bad_since = now
        elif self._good(value):
            self.bad_since = None
            if self.ok_since is None:
                self.ok_since = now
        else:
            self.bad_since = self.ok_since = None
        target = self.active
        if self.bad_since is not None and now - self.bad_since >= self.duration:
            target = True
        elif self.ok_since is not None and now - self.ok_since >= self.duration_ok:
            target = False
        if target == self.active or quiet or now < self.next_attempt:
            return None
        title, text = (self.text_high if target else self.text_ok)(value)
        return target, title, text


# Alert texts. {r} = "Room: " prefix (ATMOTUBE_ROOM), {v} = value, {limit} = threshold.
TEXTS = {
    "en": {
        "pm25": ("🌫️ {r}PM2.5 high",
                 "PM2.5 has been at {v} µg/m³ for 10 minutes (WHO 24-hour guideline: 15). "
                 "Ventilate or turn on an air purifier.",
                 "✅ {r}PM2.5 back to normal", "PM2.5 now {v} µg/m³."),
        "voc": ("🧪 {r}High VOC level",
                "VOC has been at {v} ppm for 15 minutes. Typical after cooking, cleaning, "
                "candles or new furniture. Ventilating helps.",
                "✅ {r}VOC back to normal", "VOC now {v} ppm."),
        "humidity": ("💧 {r}Humidity high",
                     "{v} % for 30 minutes - above {limit} % the risk of mould rises. "
                     "Air the room briefly with the windows wide open.",
                     "✅ {r}Humidity back to normal", "Now {v} %."),
        "battery": ("🔋 Atmotube battery low",
                    "{v} % left. Please charge it, or the readings will stop soon.",
                    "✅ Atmotube battery charged", "Now {v} %."),
    },
    "de": {
        "pm25": ("🌫️ {r}Feinstaub hoch",
                 "PM2.5 liegt seit 10 Minuten bei {v} µg/m³ (WHO-Richtwert im Tagesmittel: 15). "
                 "Lüften oder Luftreiniger einschalten.",
                 "✅ {r}Feinstaub wieder normal", "PM2.5 jetzt {v} µg/m³."),
        "voc": ("🧪 {r}viele Ausdünstungen (VOC)",
                "VOC seit 15 Minuten bei {v} ppm. Typisch nach Kochen, Putzen, Kerzen oder "
                "neuen Möbeln. Lüften hilft.",
                "✅ {r}VOC wieder normal", "VOC jetzt {v} ppm."),
        "humidity": ("💧 {r}Luftfeuchte hoch",
                     "Seit 30 Minuten {v} % – über {limit} % steigt die Schimmelgefahr. "
                     "Stoßlüften (5–10 Minuten Fenster ganz auf).",
                     "✅ {r}Luftfeuchte wieder normal", "Jetzt {v} %."),
        "battery": ("🔋 Atmotube: Akku schwach",
                    "Noch {v} %. Bitte ans Ladekabel – sonst fehlen bald die Messwerte.",
                    "✅ Atmotube: Akku wieder geladen", "Jetzt {v} %."),
    },
}


def _fmt(x, digits=0):
    s = f"{x:.{digits}f}"
    return s.replace(".", ",") if LANG == "de" else s


def build_alerts():
    texts = TEXTS.get(LANG, TEXTS["en"])
    r = f"{ROOM}: " if ROOM else ""

    def messages(kind, digits_high=0, digits_ok=0, scale=1.0, limit=None):
        title_high, text_high, title_ok, text_ok = texts[kind]
        lim = _fmt(limit) if limit is not None else ""
        return (lambda v: (title_high.format(r=r), text_high.format(v=_fmt(v * scale, digits_high), limit=lim)),
                lambda v: (title_ok.format(r=r), text_ok.format(v=_fmt(v * scale, digits_ok), limit=lim)))

    return [
        Alert("pm25", PM25_HIGH, PM25_OK, 600, 600, *messages("pm25")),
        # VOC is kept in ppb but told in ppm: 1 decimal when high, 2 when back to normal.
        Alert("voc", VOC_HIGH, VOC_OK, 900, 600, *messages("voc", 1, 2, scale=0.001)),
        Alert("humidity", HUMIDITY_HIGH, HUMIDITY_OK, 1800, 600, *messages("humidity", limit=HUMIDITY_HIGH)),
        Alert("battery", BATTERY_LOW, BATTERY_OK, 300, 300, *messages("battery"), downward=True),
    ]


def in_quiet_hours(span, hour):
    if not span:
        return False
    try:
        start, end = (int(x) for x in span.split("-"))
    except ValueError:
        return False
    if start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


def alert_values(s, now):
    status = s.value("status", now)
    bits = status_bits(status) if status is not None else {}
    battery = s.value("battery", now)
    return {
        "pm25": s.value("pm25", now),
        # While the VOC sensor is still warming up its readings are unreliable.
        "voc": s.value("voc_ppb", now) if bits.get("voc_ready") else None,
        "humidity": s.value("humidity", now),
        # On the charger a low battery is no reason to alert.
        "battery": 100 if bits.get("charging") else battery,
    }


def notify(title, text):
    """Run ATMOTUBE_NOTIFY_CMD with title and text as separate arguments. No
    shell: the texts contain readings, and the device name that ended up in a
    log line comes from the radio - nothing from outside gets interpreted."""
    if not NOTIFY_CMD:
        return True
    try:
        argv = shlex.split(NOTIFY_CMD) + [title, text]
        r = subprocess.run(argv, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        log.error("Notification command failed: %s", e)
        return False


def process_alerts(s, alerts, now, hour=None, send=None):
    hour = time.localtime(now).tm_hour if hour is None else hour
    quiet = in_quiet_hours(QUIET_HOURS, hour)
    send = send or notify
    values = alert_values(s, now)
    for a in alerts:
        due = a.check(values[a.kind], now, quiet)
        if not due:
            continue
        target, title, text = due
        if send(title, text):
            a.active = target
            log.info("%s: %s", title, text)
        else:
            a.next_attempt = now + RETRY_AFTER
            log.warning("Alert '%s' not delivered - retrying in %d min", title, RETRY_AFTER // 60)


# ── Metrics ─────────────────────────────────────────────────────────────────
def _num(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int) or float(v).is_integer():
        return str(int(v))
    return f"{v:.3f}".rstrip("0")


class _Prom:
    def __init__(self):
        self.lines, self.known = [], set()

    # metric_type, not "type"/"kind": those are label names below, and a
    # keyword clash would silently turn a label into the metric type.
    def __call__(self, name, value, help_text, metric_type="gauge", **labels):
        if name not in self.known:
            self.known.add(name)
            self.lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}"]
        lab = ("{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}") if labels else ""
        self.lines.append(f"{name}{lab} {_num(value)}")


def metrics_text(s, now, alerts=()):
    m = _Prom()
    # Heartbeat: if it stops, the exporter isn't running (rule AtmotubeExporterStalled).
    m("atmotube_last_run_timestamp_seconds", int(now), "Last round of the exporter (Unix time)")
    m("atmotube_bluetooth_up", s.bluetooth_up, "1 while the Bluetooth scanner is running")
    seen = s.heard is not None and now - s.heard <= MAX_AGE_BASE
    m("atmotube_device_seen", seen, "1 if the Atmotube was heard within ATMOTUBE_MAX_AGE")
    # Counted from the start of the exporter, so AtmotubeNoReadings also fires
    # when NOTHING has arrived since the start.
    base = s.time_of(("voc_ppb", "temperature", "humidity"))
    m("atmotube_last_reading_timestamp_seconds", int(max(base or 0, s.start)),
      "Last base reading (VOC/temperature/humidity), at least the exporter start (Unix time)")
    pm_time = s.time_of(PM_FIELDS)
    if pm_time:
        m("atmotube_last_pm_reading_timestamp_seconds", int(pm_time), "Last PM reading (Unix time)")

    v = lambda field: s.value(field, now)  # noqa: E731
    if v("temperature") is not None:
        m("atmotube_temperature_celsius", v("temperature"), "Temperature")
    if v("humidity") is not None:
        m("atmotube_humidity_percent", v("humidity"), "Relative humidity")
    if v("pressure_hpa") is not None:
        m("atmotube_pressure_hpa", v("pressure_hpa"), "Air pressure")
    if v("voc_ppb") is not None:
        m("atmotube_voc_ppb", v("voc_ppb"), "Volatile organic compounds (TVOC)")
    if v("battery") is not None:
        m("atmotube_battery_percent", v("battery"), "Battery level")
    if v("status") is not None:
        bits = status_bits(v("status"))
        m("atmotube_charging", bits["charging"], "1 while the Atmotube is on the charger")
        m("atmotube_pm_sensor_active", bits["pm_on"], "1 while the PM sensor is measuring")
        m("atmotube_voc_ready", bits["voc_ready"], "1 once the VOC sensor has warmed up")
        m("atmotube_device_error", bits["error"], "1 if the Atmotube reports an error")
    for field, size in (("pm1", "PM1"), ("pm25", "PM2.5"), ("pm10", "PM10")):
        if v(field) is not None:
            m("atmotube_pm_ugm3", v(field), "Particulate matter (mass concentration)", size=size)
    score = air_quality(v("voc_ppb"), v("pm1"), v("pm25"), v("pm10"))
    if score is not None:
        m("atmotube_air_quality_score", score,
          "Air quality 0-100 as in the Atmotube app (worst sub-score of VOC and PM)")
    if seen and s.rssi is not None:
        m("atmotube_rssi_dbm", s.rssi, "Signal strength at the receiver")
    for kind, n in s.packets.items():
        m("atmotube_advertisements_total", n, "Advertisements received since start", "counter", type=kind)
    for result, n in s.gatt_reads.items():
        m("atmotube_gatt_reads_total", n, "GATT reads since start", "counter", result=result)
    for a in alerts:
        m("atmotube_alert_active", a.active, "1 while an alert is active", kind=a.kind)
    # The thresholds as well, so dashboards can colour by them instead of
    # keeping a copy that silently drifts when you change the env file. A
    # separate loop: the samples of one metric must be contiguous.
    for a in alerts:
        for level, value in (("high", a.high), ("ok", a.ok)):
            m("atmotube_alert_threshold", value, "Alert threshold: alert from high, clear below ok "
              "(battery reversed: low is bad)", kind=a.kind, level=level)
    m("atmotube_info", 1, "Firmware and source of the base readings (advertisement/gatt)",
      firmware=s.firmware or "unknown", source=s.source())
    return "\n".join(m.lines) + "\n"


_last_write_error = None


def write_metrics(text, path=None):
    """Atomic: write completely, then rename - otherwise node_exporter may read
    half a file and drop the whole scrape. Mode 644 explicitly: the systemd
    hardening sets UMask=0077, and node_exporter usually runs as another user."""
    global _last_write_error
    path = path or METRICS_FILE
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            os.fchmod(f.fileno(), 0o644)
        os.replace(tmp, path)
        _last_write_error = None
        return True
    except OSError as e:
        if str(e) != _last_write_error:
            log.error("Cannot write metrics (%s): %s", path, e)
            _last_write_error = str(e)
        return False


# ── Bluetooth ───────────────────────────────────────────────────────────────
def bt_error_text(e):
    """The common causes in plain words - nobody understands "No such file or
    directory" otherwise."""
    raw = f"{type(e).__name__}: {e}"
    if isinstance(e, ImportError):
        return "bleak is missing (sudo apt install python3-bleak, or pip install bleak)"
    if isinstance(e, FileNotFoundError):
        return f"D-Bus system bus not found - is dbus/bluetooth running? ({raw})"
    if "AccessDenied" in raw:
        return f"no access to BlueZ - is the user in the bluetooth group? ({raw})"
    if "NotReady" in raw or "No powered Bluetooth adapters" in raw:
        return f"Bluetooth is off - rfkill unblock bluetooth, bluetoothctl power on ({raw})"
    if "No Bluetooth adapters found" in raw:
        return f"no Bluetooth adapter - dtoverlay=disable-bt in config.txt? ({raw})"
    return raw


async def _wait(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), timeout=max(seconds, 0))
    except asyncio.TimeoutError:
        pass


# DuplicateData=True: BlueZ reports every packet, not only changed data.
# Otherwise nothing would arrive for minutes while the air stays the same, and
# "last heard ... ago" would be wrong.
_SCAN_FILTER = {"filters": {"DuplicateData": True}}


async def listen(s, seconds, stop, scanner_cls=None):
    if scanner_cls is None:
        from bleak import BleakScanner as scanner_cls

    def seen(device, adv):
        try:
            on_advertisement(s, device.address, adv.local_name or device.name, adv.service_uuids,
                             adv.manufacturer_data, adv.rssi, time.time(), device)
        except Exception as e:  # one broken packet must not stop the scanner
            log.warning("Could not process packet: %s", e)

    async with scanner_cls(detection_callback=seen, bluez=_SCAN_FILTER):
        await _wait(stop, seconds)


async def read_gatt(target, client_cls=None):
    """Connect briefly, read four characteristics, disconnect. -> {uuid: bytes}"""
    if client_cls is None:
        from bleak import BleakClient as client_cls
    raw = {}
    async with client_cls(target, timeout=15.0) as c:
        for ch in (CHAR_BME280, CHAR_STATUS, CHAR_PM, CHAR_VOC):
            try:
                raw[ch] = bytes(await c.read_gatt_char(ch))
            except Exception as e:
                log.debug("Characteristic %s not readable: %s", CHAR_NAMES[ch], e)
    return raw


async def run(scanner_cls=None, client_cls=None, rounds=None):
    s = State(time.time())
    alerts = build_alerts()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    log.info("Start: device %s, GATT %s, notify %s, quiet hours %s",
             MAC or "first one heard", GATT_MODE, "on" if NOTIFY_CMD else "off (log only)",
             QUIET_HOURS or "none")
    if not MAC:
        log.warning("ATMOTUBE_MAC is not set - listening to the first Atmotube heard, which "
                    "could be a neighbour's. Run --diagnose and pin your device's address.")
    n = 0
    while not stop.is_set():
        try:
            await listen(s, LISTEN, stop, scanner_cls)
            if not s.bluetooth_up:
                log.info("Bluetooth scanner running")
            s.bluetooth_up, s.bt_error = True, None
        except Exception as e:
            text = bt_error_text(e)
            if text != s.bt_error:
                log.error("Bluetooth not usable - retrying every minute. %s", text)
            s.bluetooth_up, s.bt_error = False, text
            await _wait(stop, max(60 - PAUSE, 0))

        now = time.time()
        if not stop.is_set() and gatt_due(s, now):
            s.last_gatt = now
            try:
                raw = await asyncio.wait_for(read_gatt(s.device or s.address, client_cls), 45)
                ok = apply_gatt(s, raw, time.time())
            except Exception as e:
                # Most common reason: the phone app is connected to the Atmotube.
                log.info("GATT read failed: %s: %s", type(e).__name__, e)
                ok = False
            s.gatt_reads["ok" if ok else "error"] += 1

        now = time.time()
        process_alerts(s, alerts, now)
        write_metrics(metrics_text(s, now, alerts))
        n += 1
        if rounds and n >= rounds:
            break
        await _wait(stop, PAUSE)
    log.info("Stopped")


# ── Diagnose ────────────────────────────────────────────────────────────────
def describe(v):
    parts = []
    if v.get("temperature") is not None:
        parts.append(f"{v['temperature']:g} °C")
    if v.get("humidity") is not None:
        parts.append(f"{v['humidity']} %")
    if v.get("pressure_hpa") is not None:
        parts.append(f"{v['pressure_hpa']:.1f} hPa")
    if v.get("voc_ppb") is not None:
        parts.append(f"VOC {v['voc_ppb'] / 1000:.3f} ppm")
    if v.get("battery") is not None:
        parts.append(f"battery {v['battery']} %")
    if v.get("status") is not None:
        on = [k for k, x in status_bits(v["status"]).items() if x]
        parts.append(f"status 0x{v['status']:02X} ({', '.join(on) or '-'})")
    if "pm25" in v:
        parts.append("PM off" if v["pm25"] is None else
                     f"PM1 {v['pm1']:g} / PM2.5 {v['pm25']:g} / PM10 {v['pm10']:g} µg/m³")
    if v.get("firmware"):
        parts.append(f"firmware {v['firmware']}")
    return " · ".join(parts)


async def diagnose(seconds, scanner_cls=None, client_cls=None):
    print(f"Atmotube diagnose: listening for {seconds:g} s ...\n")
    found = {}

    def seen(device, adv):
        name = adv.local_name or device.name or ""
        uuids = [str(u).lower() for u in (adv.service_uuids or [])]
        data = (adv.manufacturer_data or {}).get(MANUFACTURER_ID)
        pro = SERVICE_PRO in uuids or name.upper().startswith("ATMOTUBE")
        pro2 = SERVICE_PRO2 in uuids
        if not (pro or pro2 or data is not None or "ATMO" in name.upper()):
            return
        f = found.setdefault(device.address.upper(), {"name": "", "pro": False, "pro2": False,
                                                      "kinds": {}, "base": None, "device": None})
        f["name"] = name or f["name"]
        f["pro"] |= pro
        f["pro2"] |= pro2
        f["device"] = device
        if data is None:
            return
        h = bytes(data).hex().upper()
        if h in f["kinds"]:
            return
        kind, base, pm = decode_manufacturer_data(data)
        f["kinds"][h] = kind
        f["base"] = base or f["base"]
        print(f"  {device.address}  {(name or '?'):10} {adv.rssi:>4} dBm  "
              f"{len(data):2} bytes  {kind:11} {h}")
        for part in (base, pm):
            if part:
                print(f"      -> {describe(part)}")

    try:
        if scanner_cls is None:
            from bleak import BleakScanner as scanner_cls
        async with scanner_cls(detection_callback=seen, bluez=_SCAN_FILTER):
            await asyncio.sleep(seconds)
    except Exception as e:
        print(f"✗ Bluetooth not usable: {bt_error_text(e)}")
        if not isinstance(e, ImportError):
            print("  Check:  bluetoothctl show  ·  rfkill list bluetooth  ·  systemctl status bluetooth")
        return 2

    print()
    if not found:
        print("✗ No Atmotube heard. Is it switched on? Closer than about 10 m?")
        print("  Is the Atmotube phone app connected right now? Then it may not advertise - close the app.")
        return 1
    for address, f in found.items():
        if f["pro2"]:
            print(f"! {address} is an Atmotube PRO 2 - different protocol, not supported.")
    pros = [a for a, f in found.items() if f["pro"] and not f["pro2"]]
    if not pros:
        print("✗ Heard manufacturer ID 0xFFFF, but nothing with an Atmotube name or service.")
        return 1
    address = MAC if MAC in pros else pros[0]
    f = found[address]
    kinds = set(f["kinds"].values())
    print(f"Atmotube PRO: {address}")
    if kinds & {"base", "combined"}:
        print("  ✓ Base readings (VOC, temperature, humidity, pressure, battery) arrive by advertisement")
    else:
        print("  ! Base readings do NOT arrive by advertisement - BlueZ merges the two packets.")
        print("    The exporter reads them over GATT instead (ATMOTUBE_GATT=auto).")
    if kinds & {"pm", "combined"}:
        print("  ✓ PM readings arrive by advertisement")
    else:
        print("  ! No PM readings by advertisement")
    if kinds & {"unknown", "implausible"}:
        print("  ! Packets with unknown layout or implausible values - please keep this output")

    print(f"\nGATT read from {address} ...")
    try:
        raw = await asyncio.wait_for(read_gatt(f["device"] or address, client_cls), 45)
    except Exception as e:
        print(f"  ✗ failed: {type(e).__name__}: {e}")
        print("    Most common reason: the phone app is connected. Close it and try again.")
        raw = {}
    for ch, decoder in ((CHAR_BME280, decode_bme280_gatt), (CHAR_STATUS, decode_status_gatt),
                        (CHAR_PM, decode_pm_gatt), (CHAR_VOC, decode_voc_gatt)):
        if ch in raw:
            part = decoder(raw[ch])
            print(f"  {CHAR_NAMES[ch]:7} {len(raw[ch]):2} bytes  {raw[ch].hex().upper():26} "
                  f"-> {describe(part) if part else 'IMPLAUSIBLE'}")
    voc_gatt = decode_voc_gatt(raw.get(CHAR_VOC, b""))
    if voc_gatt and f["base"]:
        print(f"\n  VOC cross-check: advertisement {f['base']['voc_ppb']} ppb, GATT "
              f"{voc_gatt['voc_ppb']} ppb - should be close.")

    print(f"\nTo pin this device, put into {ENV_FILE}:\n  ATMOTUBE_MAC={address}")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("--version", "-V"):
        print(__version__)
        return 0
    if argv and argv[0] == "--diagnose":
        try:
            seconds = float(argv[1]) if len(argv) > 1 else 30.0
        except ValueError:
            print(__doc__)
            return 2
        return asyncio.run(diagnose(seconds))
    if argv:
        print(__doc__)
        return 2
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
