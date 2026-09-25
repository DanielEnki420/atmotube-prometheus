#!/usr/bin/env python3
"""Unit tests for atmotube.py - no Bluetooth, no bleak.

Run:  python3 -m unittest discover -s tests -v

Built on REAL captures, not invented bytes: a hand-made packet only proves
that parser and test share the same mistake.
  - Raw packet (advertisement + scan response) and status payload from
    github.com/natekspencer/ha-atmo, tests/test_pyatmo.py (MIT licence)
  - GATT PM example from the same place (decode_pms)
"""

import asyncio
import contextlib
import importlib.util
import io
import os
import pathlib
import stat
import struct
import tempfile
import types
import unittest

# ATMOTUBE_SCRIPT: test a copy that lives elsewhere (e.g. vendored into another repo).
SCRIPT = pathlib.Path(os.environ.get("ATMOTUBE_SCRIPT")
                      or pathlib.Path(__file__).resolve().parent.parent / "atmotube.py")
spec = importlib.util.spec_from_file_location("atmotube", SCRIPT)
atmo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(atmo)
atmo.log.disabled = True
# Never touch a real system: no real metrics file, no real notifications -
# even when this file runs outside CI. Tests that need a file set their own.
atmo.METRICS_FILE = "/nonexistent/atmotube.prom"
atmo.NOTIFY_CMD = "/nonexistent/notify-stub"

# Real capture of an Atmotube PRO (firmware 74051E), advertisement and scan
# response back to back - the way Android delivers them.
RAW = bytes.fromhex(
    "0201060FFFFFFF00002DEB12FE0001418B4157090941544D4F545542451107B48A324A"
    "D96ED7AD18489A8E010045DB0CFFFFFF00010001000274051E")
# Second real capture, base payload only: VOC 259 ppb, 26 %, 25 °C.
BASE_2 = bytes.fromhex("01039e321a19000140ea4164")


def ad_structures(raw):
    """Split BLE advertising data into (type, content) - the way BlueZ does."""
    i, out = 0, []
    while i < len(raw) and raw[i]:
        length = raw[i]
        out.append((raw[i + 1], raw[i + 2:i + 1 + length]))
        i += 1 + length
    return out


def manufacturer_data(raw):
    """All manufacturer data (type 0xFF) as (id, payload) - the first two bytes
    are the manufacturer ID, little-endian. Exactly how bleak passes it on."""
    return [(int.from_bytes(d[:2], "little"), d[2:]) for t, d in ad_structures(raw) if t == 0xFF]


MFR = manufacturer_data(RAW)
BASE_1, PM_1 = MFR[0][1], MFR[1][1]


class Capture(unittest.TestCase):
    def test_two_packets_with_the_same_manufacturer_id(self):
        # The reason for the GATT fallback: both packets carry 0xFFFF.
        self.assertEqual([m[0] for m in MFR], [0xFFFF, 0xFFFF])
        self.assertEqual((len(BASE_1), len(PM_1)), (12, 9))

    def test_service_uuid_and_name_match(self):
        structures = dict(ad_structures(RAW))
        self.assertEqual(structures[0x09], b"ATMOTUBE")
        uuid = structures[0x07][::-1].hex()
        self.assertEqual(f"{uuid[:8]}-{uuid[8:12]}-{uuid[12:16]}-{uuid[16:20]}-{uuid[20:]}",
                         atmo.SERVICE_PRO)

    def test_base(self):
        b = atmo.decode_base(BASE_1)
        self.assertEqual((b["voc_ppb"], b["humidity"], b["temperature"], b["battery"]), (0, 18, -2.0, 87))
        self.assertAlmostEqual(b["pressure_hpa"], 823.15)
        bits = atmo.status_bits(b["status"])
        self.assertTrue(bits["pm_on"] and bits["voc_ready"])
        self.assertFalse(bits["charging"] or bits["error"])

    def test_base_second_capture(self):
        b = atmo.decode_base(BASE_2)
        self.assertEqual((b["voc_ppb"], b["humidity"], b["temperature"], b["battery"]), (259, 26, 25.0, 100))
        self.assertAlmostEqual(b["pressure_hpa"], 821.54)

    def test_pm(self):
        p = atmo.decode_pm(PM_1)
        self.assertEqual((p["pm1"], p["pm25"], p["pm10"], p["firmware"]), (1, 1, 2, "74051E"))

    def test_pm_sensor_off_is_no_value(self):
        p = atmo.decode_pm(bytes.fromhex("FFFFFFFFFFFF74051E"))
        self.assertIsNone(p["pm25"])
        self.assertEqual(p["firmware"], "74051E")

    def test_combined_and_lengths(self):
        self.assertEqual(atmo.decode_manufacturer_data(BASE_1 + PM_1)[0], "combined")
        self.assertEqual(atmo.decode_manufacturer_data(BASE_1)[0], "base")
        self.assertEqual(atmo.decode_manufacturer_data(PM_1)[0], "pm")
        self.assertEqual(atmo.decode_manufacturer_data(BASE_1[:11])[0], "unknown")

    def test_implausible_is_dropped(self):
        broken = bytearray(BASE_1)
        broken[4] = 200                              # 200 % humidity
        self.assertEqual(atmo.decode_manufacturer_data(bytes(broken))[0], "implausible")
        broken = bytearray(BASE_1)
        broken[6:10] = (0).to_bytes(4, "big")        # 0 hPa
        self.assertIsNone(atmo.decode_base(bytes(broken)))


class Gatt(unittest.TestCase):
    def test_pm_gatt_from_ha_atmo(self):
        p = atmo.decode_pm_gatt(bytes.fromhex("640000A30000E30000640000"))
        self.assertEqual((p["pm1"], p["pm25"], p["pm10"]), (1.0, 1.63, 2.27))

    def test_bme280(self):
        v = atmo.decode_bme280_gatt(struct.pack("<Bbih", 45, 22, 98765, 2247))
        self.assertEqual((v["humidity"], v["temperature"]), (45, 22.47))
        self.assertAlmostEqual(v["pressure_hpa"], 987.65)
        # A pressure in another unit (here hPa*10) fails the limits.
        self.assertIsNone(atmo.decode_bme280_gatt(struct.pack("<Bbih", 45, 22, 9876, 2247)))

    def test_status(self):
        s = atmo.decode_status_gatt(bytes([0x49, 80]))
        self.assertEqual(s["battery"], 80)
        self.assertTrue(atmo.status_bits(s["status"])["charging"])


def new_state():
    return atmo.State(1000.0)


class Reception(unittest.TestCase):
    def setUp(self):
        self.old = atmo.MAC

    def tearDown(self):
        atmo.MAC = self.old

    def receive(self, s, data, name="ATMOTUBE", uuids=(), address="aa:bb:cc:dd:ee:ff", now=1000.0):
        return atmo.on_advertisement(s, address, name, list(uuids), {0xFFFF: data}, -60, now)

    def test_other_device_with_testing_id_is_ignored(self):
        s = new_state()
        self.assertEqual(self.receive(s, BASE_1, name="SOMETHING"), "not-atmotube")
        self.assertIsNone(s.address)

    def test_recognised_by_service_uuid_alone(self):
        s = new_state()
        self.assertEqual(self.receive(s, PM_1, name=None, uuids=[atmo.SERVICE_PRO.upper()]), "pm")

    def test_pinned_address(self):
        atmo.MAC = "11:22:33:44:55:66"
        s = new_state()
        self.assertEqual(self.receive(s, BASE_1), "other-address")
        self.assertEqual(self.receive(s, BASE_1, address="11:22:33:44:55:66"), "base")

    def test_second_atmotube_stays_out(self):
        s = new_state()
        self.receive(s, BASE_1)
        self.assertEqual(self.receive(s, BASE_2, address="00:00:00:00:00:01"), "other-atmotube")
        self.assertEqual(s.value("humidity", 1000.0), 18)

    def test_pro2_detected_not_read(self):
        s = new_state()
        self.assertEqual(self.receive(s, BASE_1, uuids=[atmo.SERVICE_PRO2]), "pro2")
        self.assertTrue(s.pro2_reported)
        self.assertIsNone(s.value("humidity", 1000.0))

    def test_values_age(self):
        s = new_state()
        self.receive(s, BASE_1 + PM_1)
        self.assertEqual(s.value("humidity", 1000.0 + atmo.MAX_AGE_BASE), 18)
        self.assertIsNone(s.value("humidity", 1000.0 + atmo.MAX_AGE_BASE + 1))
        # PM may be older - the sensor only measures at intervals.
        self.assertEqual(s.value("pm25", 1000.0 + atmo.MAX_AGE_BASE + 1), 1)


class Fallback(unittest.TestCase):
    """The case the GATT read exists for: BlueZ only passes on the scan response."""

    def test_pm_only_makes_gatt_due(self):
        s = new_state()
        atmo.on_advertisement(s, "AA:BB:CC:DD:EE:FF", "ATMOTUBE", [], {0xFFFF: PM_1}, -60, 1000.0)
        self.assertFalse(atmo.gatt_due(s, 1000.0 + atmo.GATT_AFTER))
        self.assertTrue(atmo.gatt_due(s, 1000.0 + atmo.GATT_AFTER + 1))

    def test_base_only_makes_gatt_due_too(self):
        # The other direction: which packet gets swallowed depends on which
        # one comes last in the merged event.
        s = new_state()
        atmo.on_advertisement(s, "AA:BB:CC:DD:EE:FF", "ATMOTUBE", [], {0xFFFF: BASE_1}, -60, 1100.0)
        self.assertTrue(atmo.gatt_due(s, 1000.0 + atmo.GATT_AFTER + 1))

    def test_both_packets_no_gatt(self):
        s = new_state()
        for t in (1100.0, 1200.0):
            atmo.on_advertisement(s, "AA:BB:CC:DD:EE:FF", "ATMOTUBE", [], {0xFFFF: BASE_1}, -60, t)
            atmo.on_advertisement(s, "AA:BB:CC:DD:EE:FF", "ATMOTUBE", [], {0xFFFF: PM_1}, -60, t)
        self.assertFalse(atmo.gatt_due(s, 1250.0))

    def test_no_gatt_without_device_in_range(self):
        s = new_state()
        atmo.on_advertisement(s, "AA:BB:CC:DD:EE:FF", "ATMOTUBE", [], {0xFFFF: PM_1}, -60, 1000.0)
        self.assertFalse(atmo.gatt_due(s, 1000.0 + 301))

    def test_advertisement_wins_gatt_fills_gaps(self):
        s = new_state()
        atmo.on_advertisement(s, "AA:BB:CC:DD:EE:FF", "ATMOTUBE", [], {0xFFFF: PM_1}, -60, 1000.0)
        raw = {atmo.CHAR_BME280: struct.pack("<Bbih", 45, 22, 98765, 2247),
               atmo.CHAR_PM: bytes.fromhex("640000A30000E30000640000")}
        self.assertTrue(atmo.apply_gatt(s, raw, 1010.0))
        self.assertEqual(s.value("temperature", 1010.0), 22.47)   # gap filled
        self.assertEqual(s.value("pm25", 1010.0), 1)              # advertisement stays


class Alerts(unittest.TestCase):
    def pm25(self):
        return next(a for a in atmo.build_alerts() if a.kind == "pm25")

    def test_short_spike_stays_quiet(self):
        a = self.pm25()
        self.assertIsNone(a.check(40, 0, False))
        self.assertIsNone(a.check(40, 599, False))
        self.assertIsNone(a.check(10, 700, False))   # over before 10 minutes passed

    def test_alert_hold_clear(self):
        a = self.pm25()
        a.check(30, 0, False)
        target, title, text = a.check(30, 600, False)
        self.assertTrue(target)
        self.assertIn("PM2.5 high", title)
        self.assertIn("30 µg/m³", text)
        a.active = True
        # Between the thresholds nothing happens - not even after a long time.
        # Otherwise a value around 20 would flip between "high" and "normal".
        self.assertIsNone(a.check(20, 700, False))
        self.assertIsNone(a.check(20, 2000, False))
        self.assertIsNone(a.check(None, 2100, False))  # no reading, no all-clear
        self.assertIsNone(a.check(10, 2200, False))
        target, title, _ = a.check(10, 2800, False)
        self.assertFalse(target)
        self.assertIn("back to normal", title)

    def test_german_texts(self):
        old_lang, old_room = atmo.LANG, atmo.ROOM
        atmo.LANG, atmo.ROOM = "de", "Wohnzimmer"
        try:
            a = next(a for a in atmo.build_alerts() if a.kind == "voc")
            a.check(1500, 0, False)
            _, title, text = a.check(1500, 900, False)
            self.assertEqual(title, "🧪 Wohnzimmer: viele Ausdünstungen (VOC)")
            self.assertIn("bei 1,5 ppm", text)                 # decimal comma
            h = next(a for a in atmo.build_alerts() if a.kind == "humidity")
            h.check(70, 0, False)
            self.assertIn("über 65 %", h.check(70, 1800, False)[2])
        finally:
            atmo.LANG, atmo.ROOM = old_lang, old_room

    def test_unknown_language_falls_back_to_english(self):
        old, atmo.LANG = atmo.LANG, "xx"
        try:
            a = next(a for a in atmo.build_alerts() if a.kind == "pm25")
            a.check(30, 0, False)
            self.assertIn("PM2.5 high", a.check(30, 600, False)[1])
        finally:
            atmo.LANG = old

    def test_quiet_hours_only_postpone(self):
        a = self.pm25()
        a.check(30, 0, True)
        self.assertIsNone(a.check(30, 3600, True))
        self.assertTrue(a.check(30, 3660, False)[0])   # in the morning, if it still applies

    def test_battery_alerts_downward(self):
        a = next(a for a in atmo.build_alerts() if a.kind == "battery")
        a.check(12, 0, False)
        self.assertTrue(a.check(12, 300, False)[0])

    def test_quiet_hours(self):
        self.assertTrue(atmo.in_quiet_hours("22-7", 23))
        self.assertTrue(atmo.in_quiet_hours("22-7", 3))
        self.assertFalse(atmo.in_quiet_hours("22-7", 7))
        self.assertFalse(atmo.in_quiet_hours("22-7", 12))
        self.assertTrue(atmo.in_quiet_hours("13-15", 14))
        self.assertFalse(atmo.in_quiet_hours("", 3))
        self.assertFalse(atmo.in_quiet_hours("nonsense", 3))

    def test_failed_delivery_retries_later(self):
        s = new_state()
        s.set("pm25", 40.0, 0.0, "advertisement")
        alerts = [self.pm25()]
        sent = []
        atmo.process_alerts(s, alerts, 0.0, hour=12, send=lambda t, x: False)
        s.set("pm25", 40.0, 600.0, "advertisement")
        atmo.process_alerts(s, alerts, 600.0, hour=12, send=lambda t, x: False)
        self.assertFalse(alerts[0].active)
        s.set("pm25", 40.0, 700.0, "advertisement")
        atmo.process_alerts(s, alerts, 700.0, hour=12, send=lambda t, x: sent.append(t) or True)
        self.assertEqual(sent, [])                     # still waiting
        s.set("pm25", 40.0, 1300.0, "advertisement")
        atmo.process_alerts(s, alerts, 1300.0, hour=12, send=lambda t, x: sent.append(t) or True)
        self.assertEqual(len(sent), 1)
        self.assertTrue(alerts[0].active)

    def test_missing_notify_command_is_a_failure_not_a_crash(self):
        self.assertFalse(atmo.notify("Title", "Text"))

    def test_notify_passes_arguments_without_a_shell(self):
        # Title and text go in as separate arguments - "; rm -rf" stays text.
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            script = os.path.join(d, "notify")
            with open(script, "w") as f:
                f.write(f'#!/bin/sh\nprintf "%s|%s" "$1" "$2" > "{out}"\n')
            os.chmod(script, 0o755)
            old, atmo.NOTIFY_CMD = atmo.NOTIFY_CMD, script
            try:
                self.assertTrue(atmo.notify("A; touch x", "B $(id)"))
            finally:
                atmo.NOTIFY_CMD = old
            with open(out) as f:
                self.assertEqual(f.read(), "A; touch x|B $(id)")
            self.assertFalse(os.path.exists(os.path.join(d, "x")))

    def test_without_notify_command_alerts_still_switch(self):
        s = new_state()
        s.set("pm25", 40.0, 0.0, "advertisement")
        alerts = [self.pm25()]
        old, atmo.NOTIFY_CMD = atmo.NOTIFY_CMD, ""
        try:
            atmo.process_alerts(s, alerts, 0.0, hour=12)
            s.set("pm25", 40.0, 600.0, "advertisement")
            atmo.process_alerts(s, alerts, 600.0, hour=12)
        finally:
            atmo.NOTIFY_CMD = old
        self.assertTrue(alerts[0].active)              # visible in Grafana anyway

    def test_voc_alerts_only_once_sensor_warmed_up(self):
        s = new_state()
        s.set("voc_ppb", 5000, 0.0, "advertisement")
        s.set("status", 0x01, 0.0, "advertisement")    # bit 6 missing: still warming up
        self.assertIsNone(atmo.alert_values(s, 0.0)["voc"])
        s.set("status", 0x41, 0.0, "advertisement")
        self.assertEqual(atmo.alert_values(s, 0.0)["voc"], 5000)


class Metrics(unittest.TestCase):
    def test_air_quality_as_in_the_app(self):
        self.assertEqual(atmo.air_quality(0, 1, 1, 2), 98)
        self.assertEqual(atmo.air_quality(1000, 1, 1, 2), 61)
        self.assertEqual(atmo.air_quality(0, 1, 500, 2), 0)
        self.assertIsNone(atmo.air_quality(None, 1, 1, 2))

    def test_fresh_values_are_in(self):
        s = new_state()
        atmo.on_advertisement(s, "AA:BB:CC:DD:EE:FF", "ATMOTUBE", [], {0xFFFF: BASE_1 + PM_1}, -61, 1000.0)
        text = atmo.metrics_text(s, 1010.0)
        for line in ("atmotube_last_run_timestamp_seconds 1010", "atmotube_temperature_celsius -2",
                     "atmotube_pressure_hpa 823.15", "atmotube_voc_ppb 0",
                     'atmotube_pm_ugm3{size="PM2.5"} 1', "atmotube_rssi_dbm -61",
                     "atmotube_last_reading_timestamp_seconds 1000", "atmotube_air_quality_score 98",
                     'atmotube_info{firmware="74051E",source="advertisement"} 1'):
            self.assertIn(line + "\n", text)
        self.assertNotIn("None", text)
        self.assertNotIn("nan", text.lower())

    def test_old_values_are_missing_instead_of_lying(self):
        s = new_state()
        atmo.on_advertisement(s, "AA:BB:CC:DD:EE:FF", "ATMOTUBE", [], {0xFFFF: BASE_1}, -61, 1000.0)
        text = atmo.metrics_text(s, 1000.0 + atmo.MAX_AGE_BASE + 1)
        self.assertNotIn("atmotube_temperature_celsius", text)
        self.assertNotIn("atmotube_rssi_dbm", text)
        self.assertIn("atmotube_device_seen 0\n", text)
        self.assertIn("atmotube_last_run_timestamp_seconds", text)

    def test_silence_is_counted_from_start(self):
        text = atmo.metrics_text(new_state(), 5000.0)
        self.assertIn("atmotube_last_reading_timestamp_seconds 1000\n", text)
        self.assertNotIn("atmotube_last_pm_reading_timestamp_seconds", text)

    def test_every_metric_described_once_and_contiguous(self):
        s = new_state()
        atmo.on_advertisement(s, "AA:BB:CC:DD:EE:FF", "ATMOTUBE", [], {0xFFFF: BASE_1 + PM_1}, -61, 1000.0)
        lines = atmo.metrics_text(s, 1000.0, atmo.build_alerts()).splitlines()
        types_ = [ln.split()[2] for ln in lines if ln.startswith("# TYPE")]
        self.assertEqual(len(types_), len(set(types_)))
        # The samples of one metric must be contiguous, or node_exporter drops
        # the whole file.
        names = [ln.split("{")[0].split()[0] for ln in lines if not ln.startswith("#")]
        blocks = [n for i, n in enumerate(names) if i == 0 or names[i - 1] != n]
        self.assertEqual(len(blocks), len(set(blocks)))

    def test_thresholds_are_exported(self):
        text = atmo.metrics_text(new_state(), 1000.0, atmo.build_alerts())
        self.assertIn('atmotube_alert_threshold{kind="pm25",level="high"} %d\n' % atmo.PM25_HIGH, text)
        self.assertIn('atmotube_alert_threshold{kind="pm25",level="ok"} %d\n' % atmo.PM25_OK, text)
        self.assertIn('atmotube_alert_threshold{kind="battery",level="high"} %d\n' % atmo.BATTERY_LOW, text)
        self.assertEqual(text.count("atmotube_alert_threshold{"), 8)

    def test_file_atomic_and_readable_for_node_exporter(self):
        old = os.umask(0o077)                          # as under the systemd hardening
        try:
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "atmotube.prom")
                self.assertTrue(atmo.write_metrics("x 1\n", path))
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
                self.assertEqual(os.listdir(d), ["atmotube.prom"])
                self.assertFalse(atmo.write_metrics("x 1\n", os.path.join(d, "missing", "a.prom")))
        finally:
            os.umask(old)


# ── The whole exporter, with stubs instead of radio ─────────────────────────
def stub_scanner(packets, error=None):
    class Scanner:
        def __init__(self, detection_callback, bluez=None):
            self.cb = detection_callback

        async def __aenter__(self):
            if error:
                raise error
            for address, name, data in packets:
                self.cb(types.SimpleNamespace(address=address, name=name),
                        types.SimpleNamespace(local_name=name, service_uuids=[],
                                              manufacturer_data={0xFFFF: data}, rssi=-58))
            return self

        async def __aexit__(self, *a):
            return False
    return Scanner


def stub_client(answers):
    class Client:
        def __init__(self, target, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def read_gatt_char(self, char):
            if char not in answers:
                raise OSError("not readable")
            return bytearray(answers[char])
    return Client


class Exporter(unittest.TestCase):
    def setUp(self):
        self.old = {k: getattr(atmo, k) for k in
                    ("METRICS_FILE", "LISTEN", "PAUSE", "GATT_AFTER", "GATT_INTERVAL", "NOTIFY_CMD")}
        self.tmp = tempfile.TemporaryDirectory()
        atmo.METRICS_FILE = os.path.join(self.tmp.name, "atmotube.prom")
        atmo.LISTEN, atmo.GATT_AFTER, atmo.GATT_INTERVAL, atmo.NOTIFY_CMD = 0, -1, 0, ""

    def tearDown(self):
        for k, v in self.old.items():
            setattr(atmo, k, v)
        self.tmp.cleanup()

    def file(self):
        with open(atmo.METRICS_FILE, encoding="utf-8") as f:
            return f.read()

    def test_pm_by_radio_rest_by_gatt(self):
        atmo.PAUSE = 0
        scanner = stub_scanner([("AA:BB:CC:DD:EE:FF", "ATMOTUBE", PM_1)])
        client = stub_client({
            atmo.CHAR_BME280: struct.pack("<Bbih", 45, 22, 98765, 2247),
            atmo.CHAR_STATUS: bytes([0x41, 90]),
            atmo.CHAR_VOC: (350).to_bytes(2, "little") + b"\x00\x00"})
        asyncio.run(atmo.run(scanner, client, rounds=1))
        text = self.file()
        for line in ("atmotube_bluetooth_up 1", "atmotube_temperature_celsius 22.47",
                     "atmotube_voc_ppb 350", "atmotube_battery_percent 90",
                     'atmotube_pm_ugm3{size="PM10"} 2',
                     'atmotube_gatt_reads_total{result="ok"} 1',
                     'atmotube_info{firmware="74051E",source="gatt"} 1'):
            self.assertIn(line + "\n", text)

    def test_survives_without_bluetooth(self):
        atmo.PAUSE = 60                                 # wait after the error: 0 s
        scanner = stub_scanner([], error=OSError("no adapter"))
        asyncio.run(atmo.run(scanner, stub_client({}), rounds=1))
        text = self.file()
        self.assertIn("atmotube_bluetooth_up 0\n", text)
        self.assertIn("atmotube_last_run_timestamp_seconds ", text)
        self.assertNotIn("atmotube_temperature_celsius", text)

    def diagnose(self, packets, answers):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = asyncio.run(atmo.diagnose(0, stub_scanner(packets), stub_client(answers)))
        return rc, buf.getvalue()

    def test_diagnose_detects_swallowed_base_readings(self):
        rc, out = self.diagnose([("AA:BB:CC:DD:EE:FF", "ATMOTUBE", PM_1)],
                                {atmo.CHAR_STATUS: bytes([0x41, 90])})
        self.assertEqual(rc, 0)
        self.assertIn("Base readings do NOT arrive by advertisement", out)
        self.assertIn("✓ PM readings arrive by advertisement", out)
        self.assertIn("battery 90 %", out)
        self.assertIn("ATMOTUBE_MAC=AA:BB:CC:DD:EE:FF", out)

    def test_diagnose_compares_voc_from_both_sources(self):
        rc, out = self.diagnose([("AA:BB:CC:DD:EE:FF", "ATMOTUBE", BASE_2), ("AA:BB:CC:DD:EE:FF", "ATMOTUBE", PM_1)],
                                {atmo.CHAR_VOC: (262).to_bytes(2, "little") + b"\x00\x00"})
        self.assertIn("✓ Base readings", out)
        self.assertIn("VOC cross-check: advertisement 259 ppb, GATT 262 ppb", out)

    def test_diagnose_without_atmotube(self):
        rc, out = self.diagnose([], {})
        self.assertEqual(rc, 1)
        self.assertIn("No Atmotube heard", out)

    def test_failed_gatt_read_is_counted(self):
        atmo.PAUSE = 0
        scanner = stub_scanner([("AA:BB:CC:DD:EE:FF", "ATMOTUBE", PM_1)])
        asyncio.run(atmo.run(scanner, stub_client({}), rounds=1))
        self.assertIn('atmotube_gatt_reads_total{result="error"} 1\n', self.file())


if __name__ == "__main__":
    unittest.main(verbosity=1)
