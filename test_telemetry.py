import json
import threading
import unittest
from unittest.mock import patch

from lcd_telemetry import (Telemetry, Traffic, client_list, modem_info, nm_fields,
                           nm_properties, parse_counters, parse_leases, parse_stations)


class ParsingTests(unittest.TestCase):
    def test_nmcli_escaping_and_whitespace(self):
        self.assertEqual(nm_fields(r"ssid:Cafe\: A\\B"), ["ssid", "Cafe: A\\B"])
        self.assertEqual(nm_properties("ssid: spaced \nkey:a\\:b"), {"ssid": " spaced ", "key": "a:b"})

    def test_modem_operator_signal_and_roaming(self):
        value = {"modem": {"generic": {"state": "connected", "access-technologies": ["lte"],
                                      "signal-quality": {"value": "77", "recent": "yes"}},
                           "3gpp": {"operator-name": "Example Mobile", "registration-state": "roaming"}}}
        self.assertEqual(modem_info(value), {"name": "Example Mobile", "technology": "LTE",
                                           "state": "connected", "signal": 77, "roaming": True})
        value["modem"]["generic"]["signal-quality"]["recent"] = "no"
        self.assertIsNone(modem_info(value)["signal"])
        self.assertIsNone(modem_info({})["signal"])

    def test_lease_expiry_and_unknown_hostname(self):
        result = parse_leases("10 aa:bb:cc:dd:ee:01 192.168.100.2 Old *\n"
                              "0 aa:bb:cc:dd:ee:02 192.168.100.3 * *\n"
                              "30 aa:bb:cc:dd:ee:03 192.168.100.4 Phone *\nbad line", 20)
        self.assertNotIn("aa:bb:cc:dd:ee:01", result)
        self.assertEqual(result["aa:bb:cc:dd:ee:02"]["name"], "")
        self.assertEqual(result["aa:bb:cc:dd:ee:03"]["name"], "Phone")

    def test_only_stations_and_active_lan_neighbors_count(self):
        stations = parse_stations("Station aa:bb:cc:dd:ee:01 (on wlan0)\n\tsignal: -47 dBm\n")
        neighbors = [{"lladdr": "aa:bb:cc:dd:ee:01", "dev": "br0", "dst": "192.168.100.2", "state": ["STALE"]},
                     {"lladdr": "aa:bb:cc:dd:ee:02", "dev": "br0", "dst": "192.168.100.3", "state": ["REACHABLE"]},
                     {"lladdr": "aa:bb:cc:dd:ee:03", "dev": "br0", "dst": "192.168.100.4", "state": ["STALE"]},
                     {"lladdr": "aa:bb:cc:dd:ee:04", "dev": "wwan0", "dst": "10.0.0.1", "state": ["REACHABLE"]}]
        leases = {"aa:bb:cc:dd:ee:01": {"ip": "192.168.100.2", "name": "Phone"},
                  "aa:bb:cc:dd:ee:05": {"ip": "192.168.100.5", "name": "Offline"}}
        result = client_list(stations, neighbors, leases, {"br0", "wlan0"})
        self.assertEqual(len(result), 2)
        phone = next(client for client in result if client["name"] == "Phone")
        self.assertEqual(phone["signal"], -47)
        self.assertEqual(phone["kind"], "Wi-Fi")
        self.assertEqual(phone["ip"], "192.168.100.2")


class TrafficTests(unittest.TestCase):
    def test_parse_and_uplink_only_rates(self):
        counters = parse_counters("Inter-|\n wlan0: 100 0 0 0 0 0 0 0 50 0 0 0 0 0 0 0\ninvalid: x")
        self.assertEqual(counters, {"wlan0": (100, 50)})
        tracker = Traffic()
        self.assertIsNone(tracker.sample(counters, "wlan0", 1)["rx_rate"])
        result = tracker.sample({"wlan0": (500, 150), "br0": (999999, 999999)}, "wlan0", 3)
        self.assertEqual((result["rx_rate"], result["tx_rate"]), (200, 50))
        self.assertEqual(result["rx_total"], 500)

    def test_counter_reset_missing_uplink_and_interface_change(self):
        tracker = Traffic()
        tracker.sample({"wwan0": (1000, 500)}, "wwan0", 1)
        self.assertIsNone(tracker.sample({"wwan0": (10, 5)}, "wwan0", 2)["rx_rate"])
        self.assertIsNone(tracker.sample({}, None, 3)["rx_total"])
        self.assertEqual(tracker.sample({"wlan0": (100, 10)}, "wlan0", 4)["history"], [(None, None)])

    def test_history_is_bounded(self):
        tracker = Traffic()
        for index in range(100):
            result = tracker.sample({"wwan0": (index * 10, index * 2)}, "wwan0", index)
        self.assertEqual(len(result["history"]), 30)


class CollectorTests(unittest.TestCase):
    def test_wifi_secrets_only_read_for_ap(self):
        for mode in ("ap", "infrastructure"):
            calls = []
            def run(*args):
                calls.append(args)
                if args[-1] == "--active":
                    return "example-id:802-11-wireless:wlan0"
                if "-s" in args:
                    return r"802-11-wireless-security.psk:a\:b\\c"
                return (f"802-11-wireless.mode:{mode}\n802-11-wireless.ssid:Cafe\\: A\n"
                        "802-11-wireless.hidden:yes\n802-11-wireless-security.key-mgmt:wpa-psk")
            wifi = Telemetry(runner=run).wireless()
            self.assertEqual(wifi["ssid"], "Cafe: A")
            self.assertEqual(any("-s" in call for call in calls), mode == "ap")
            self.assertEqual(wifi.get("password"), "a:b\\c" if mode == "ap" else None)

    @patch("lcd_telemetry.Path.glob", return_value=[])
    def test_missing_tools_are_unavailable_not_invented_zeroes(self, glob):
        collector = Telemetry(runner=lambda *args: None, reader=lambda path: "")
        collector.collect(0)
        snapshot = collector.snapshot()
        self.assertFalse(snapshot["loading"])
        self.assertFalse(snapshot["clients_available"])
        self.assertEqual(snapshot["wifi"], {})
        self.assertIsNone(snapshot["traffic"]["rx_rate"])
        self.assertIsNone(snapshot["battery"]["voltage"])

    @patch("lcd_telemetry.Path.glob", return_value=[])
    def test_uplink_wifi_gateway_is_not_a_lan_client(self, glob):
        def run(*args):
            if "neigh" in args:
                return json.dumps([{"dev": "wlan0", "lladdr": "aa:bb:cc:dd:ee:01", "state": ["REACHABLE"]}])
            return None
        collector = Telemetry(runner=run)
        collector.wireless = lambda: {"mode": "infrastructure", "interface": "wlan0"}
        data = collector.snapshot()
        collector.details(data)
        self.assertEqual(data["clients"], [])

    def test_snapshot_is_independent_and_fast_during_blocked_poll(self):
        entered, release = threading.Event(), threading.Event()
        def run(*args):
            entered.set()
            release.wait(2)
            return None
        collector = Telemetry(runner=run, reader=lambda path: "")
        collector.start()
        try:
            self.assertTrue(entered.wait(1))
            snapshot = collector.snapshot()
            snapshot["wifi"]["password"] = "not-shared"
            self.assertEqual(collector.snapshot()["wifi"], {})
        finally:
            release.set()
            collector.close()
