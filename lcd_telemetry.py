"""Read-only LCD telemetry. Slow commands never run on the button event loop."""
from collections import deque
import copy
import json
import logging
from pathlib import Path
import re
import subprocess
import threading
import time

LOG = logging.getLogger("openstick-device")
MAC = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def command(*args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=1.5,
                                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
        return result.stdout.rstrip("\n") if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def json_value(value, fallback):
    try:
        return json.loads(value) if value is not None else fallback
    except (ValueError, TypeError):
        return fallback


def nm_fields(line):
    """Split nmcli's escaped terse output without damaging SSIDs or keys."""
    fields, field, escaped = [], "", False
    for char in line:
        if escaped:
            field += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append(field)
            field = ""
        else:
            field += char
    return fields + [field + ("\\" if escaped else "")]


def nm_properties(value):
    result = {}
    for line in (value or "").splitlines():
        fields = nm_fields(line)
        if len(fields) >= 2:
            result[fields[0]] = ":".join(fields[1:])
    return result


def parse_counters(value):
    result = {}
    for line in value.splitlines():
        name, separator, values = line.partition(":")
        fields = values.split()
        if separator and len(fields) >= 16:
            try:
                result[name.strip()] = (int(fields[0]), int(fields[8]))
            except ValueError:
                continue
    return result


class Traffic:
    def __init__(self):
        self.previous = {}
        self.interface = None
        self.history = deque(maxlen=30)

    def sample(self, counters, interface, now):
        if interface != self.interface:
            self.history.clear()
            self.interface = interface
        current = counters.get(interface)
        previous = self.previous.get(interface)
        if current is None:
            self.history.clear()
        rx_rate = tx_rate = None
        if current and previous and now > previous[0]:
            if current[0] >= previous[1] and current[1] >= previous[2]:
                elapsed = now - previous[0]
                rx_rate = (current[0] - previous[1]) / elapsed
                tx_rate = (current[1] - previous[2]) / elapsed
        self.previous = {name: (now, *values) for name, values in counters.items()}
        if current:
            self.history.append((rx_rate, tx_rate))
        return {"interface": interface, "rx_total": current[0] if current else None,
                "tx_total": current[1] if current else None, "rx_rate": rx_rate,
                "tx_rate": tx_rate, "history": list(self.history)}


def modem_info(value):
    modem = value.get("modem", {})
    generic, radio = modem.get("generic", {}), modem.get("3gpp", {})
    quality = generic.get("signal-quality", {})
    try:
        signal = max(0, min(100, int(quality.get("value"))))
        if quality.get("recent") not in (True, "yes", "true"):
            signal = None
    except (ValueError, TypeError, AttributeError):
        signal = None
    technologies = generic.get("access-technologies", [])
    if isinstance(technologies, str):
        technologies = [technologies]
    technology = "/".join(item.upper() for item in technologies if item not in ("unknown", "--"))
    name = radio.get("operator-name") or radio.get("operator-code") or ""
    return {"name": "" if name == "--" else name, "technology": technology,
            "state": generic.get("state", "unavailable"), "signal": signal,
            "roaming": radio.get("registration-state") == "roaming"}


def parse_stations(value):
    result, station = {}, None
    for line in (value or "").splitlines():
        fields = line.strip().split()
        if len(fields) >= 2 and fields[0] == "Station" and MAC.fullmatch(fields[1].lower()):
            station = {"mac": fields[1].lower(), "kind": "Wi-Fi", "signal": None}
            result[station["mac"]] = station
        elif station is not None and len(fields) >= 2 and fields[0] == "signal:":
            try:
                station["signal"] = int(fields[1])
            except ValueError:
                pass
    return result


def parse_leases(value, now):
    result = {}
    for line in value.splitlines():
        fields = line.split()
        if len(fields) < 4 or not MAC.fullmatch(fields[1].lower()):
            continue
        try:
            expiry = int(fields[0])
        except ValueError:
            continue
        if expiry != 0 and expiry <= now:
            continue
        result[fields[1].lower()] = {"ip": fields[2], "name": "" if fields[3] == "*" else fields[3]}
    return result


def client_list(stations, neighbors, leases, interfaces):
    # A lease or STALE neighbor alone is not evidence of a connected client.
    clients = copy.deepcopy(stations)
    for entry in neighbors:
        mac = entry.get("lladdr", "").lower()
        if not MAC.fullmatch(mac) or entry.get("dev") not in interfaces:
            continue
        states = entry.get("state", [])
        if isinstance(states, str):
            states = [states]
        active = bool(set(states) & {"REACHABLE", "DELAY", "PROBE"})
        if mac not in clients and not active:
            continue
        client = clients.setdefault(mac, {"mac": mac, "kind": "LAN", "signal": None})
        if ":" not in entry.get("dst", "") or not client.get("ip"):
            client["ip"] = entry.get("dst", "")
    for mac, client in clients.items():
        lease = leases.get(mac, {})
        client["name"] = lease.get("name", "")
        client["ip"] = client.get("ip") or lease.get("ip", "")
    return sorted(clients.values(), key=lambda entry: (entry["kind"], entry.get("name") or entry["mac"]))


def empty_snapshot():
    return {"loading": True, "sampled_at": None, "details_at": None, "uplink": {},
            "modem": {}, "wifi": {}, "clients": [], "clients_available": False,
            "traffic": {}, "battery": {}, "lan_ip": ""}


class Telemetry:
    def __init__(self, runner=command, reader=read_text):
        self.runner, self.reader = runner, reader
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.data = empty_snapshot()
        self.traffic = Traffic()
        self.next_details = 0

    def start(self):
        self.thread = threading.Thread(target=self.run, name="lcd-telemetry", daemon=True)
        self.thread.start()

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=0.2)

    def run(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.collect(started)
            except Exception:
                # Don't include command output: the worker may hold Wi-Fi secrets.
                LOG.warning("LCD telemetry refresh failed; retaining last sample")
            self.stop_event.wait(max(0.1, 2 - (time.monotonic() - started)))

    def routes(self):
        routes = json_value(self.runner("ip", "-j", "route", "get", "1.1.1.1"), [])
        if not routes:
            routes = json_value(self.runner("ip", "-6", "-j", "route", "get", "2606:4700:4700::1111"), [])
        route = routes[0] if routes else {}
        return {"interface": route.get("dev"), "ip": route.get("prefsrc") or route.get("src", ""),
                "gateway": route.get("gateway", "")}

    def wireless(self):
        fields = "802-11-wireless.mode,802-11-wireless.ssid,802-11-wireless.hidden,802-11-wireless-security.key-mgmt"
        active = self.runner("nmcli", "-t", "--escape", "yes", "-f", "UUID,TYPE,DEVICE", "connection", "show", "--active")
        fallback = {}
        for line in (active or "").splitlines():
            row = nm_fields(line)
            if len(row) != 3 or row[1] not in ("802-11-wireless", "wifi"):
                continue
            properties = nm_properties(self.runner("nmcli", "-t", "--escape", "yes", "-f", fields,
                                                   "connection", "show", "uuid", row[0]))
            if not properties:
                continue
            wifi = {"interface": row[2], "mode": properties.get("802-11-wireless.mode"),
                    "ssid": properties.get("802-11-wireless.ssid", ""),
                    "hidden": properties.get("802-11-wireless.hidden") == "yes",
                    "security": properties.get("802-11-wireless-security.key-mgmt", "")}
            if wifi["mode"] == "ap":
                # Never read or display the key of a Wi-Fi-client/uplink profile.
                if wifi["security"] in ("wpa-psk", "sae"):
                    secret = nm_properties(self.runner("nmcli", "-s", "-t", "--escape", "yes", "-f",
                                                       "802-11-wireless-security.psk", "connection", "show", "uuid", row[0]))
                    password = secret.get("802-11-wireless-security.psk", "")
                    wifi["password"] = "" if password == "--" else password
                return wifi
            fallback = wifi
        return fallback

    def details(self, data):
        data["uplink"] = self.routes()
        data["wifi"] = self.wireless()
        modems = json_value(self.runner("mmcli", "-L", "-J"), {}).get("modem-list", [])
        data["modem"] = modem_info(json_value(self.runner("mmcli", "-m", modems[0], "-J"), {})) if modems else {}
        wifi = data["wifi"]
        station_output = self.runner("iw", "dev", wifi["interface"], "station", "dump") if wifi.get("mode") == "ap" else None
        neighbors_output = self.runner("ip", "-j", "neigh", "show")
        neighbors = json_value(neighbors_output, [])
        leases = {}
        for pattern in ("/var/lib/misc/dnsmasq*.leases", "/var/lib/NetworkManager/*.leases",
                        "/run/NetworkManager/*.leases", "/var/lib/dnsmasq/*.leases"):
            for path in sorted(Path("/").glob(pattern.lstrip("/"))):
                leases.update(parse_leases(self.reader(path), time.time()))
        lan_interfaces = {"br0", "usb0", "usb1"}
        if wifi.get("mode") == "ap":
            lan_interfaces.add(wifi.get("interface"))
        data["clients"] = client_list(parse_stations(station_output), neighbors, leases, lan_interfaces)
        data["clients_available"] = neighbors_output is not None and (wifi.get("mode") != "ap" or station_output is not None)
        addresses = json_value(self.runner("ip", "-j", "-4", "address", "show", "dev", "br0"), [])
        data["lan_ip"] = next((item.get("local", "") for entry in addresses
                               for item in entry.get("addr_info", []) if item.get("family") == "inet"), "")
        data["details_at"] = time.monotonic()

    def collect(self, now):
        data = self.snapshot()
        if now >= self.next_details:
            self.details(data)
            self.next_details = time.monotonic() + 10
        counters = parse_counters(self.reader("/proc/net/dev"))
        data["traffic"] = self.traffic.sample(counters, data["uplink"].get("interface"), time.monotonic())
        battery = "/sys/class/power_supply/pm8916-bms-vm/"
        voltage = self.reader(battery + "voltage_now")
        data["battery"] = {"voltage": int(voltage) / 1e6 if voltage.isdigit() and int(voltage) > 0 else None,
                           "health": self.reader(battery + "health")}
        data["loading"], data["sampled_at"] = False, time.monotonic()
        with self.lock:
            self.data = data
