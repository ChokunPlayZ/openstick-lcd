"""Pixel-sized MF800H UI, also renderable off-device without a framebuffer."""
from pathlib import Path
import time

PAGES = ("overview", "isp", "transfer", "clients", "wifi", "network", "battery")
BG, CARD, LINE = "#080e19", "#121e30", "#25354b"
WHITE, MUTED, CYAN, GREEN, AMBER, RED = "#edf4ff", "#8d9eb5", "#55d9ef", "#83e3b1", "#ffd080", "#ff8e94"


def wifi_payload(wifi):
    if wifi.get("mode") != "ap" or not wifi.get("ssid"):
        raise ValueError("Hotspot is off")
    security = wifi.get("security")
    if security in ("", "--"):
        kind = "nopass"
    elif security == "wpa-psk":
        kind = "WPA"
    else:
        raise ValueError("QR not supported for this security")
    password = wifi.get("password", "")
    if kind != "nopass" and not password:
        raise ValueError("Hotspot key unavailable")
    def escape(value):
        return "".join("\\" + char if char in '\\;,:"' else char for char in value)
    result = f"WIFI:T:{kind};S:{escape(wifi['ssid'])};"
    if kind != "nopass":
        result += f"P:{escape(password)};"
    return result + ("H:true;" if wifi.get("hidden") else "") + ";"


def qr_image(payload):
    import qrcode
    from PIL import Image
    from qrcode.exceptions import DataOverflowError
    try:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4, box_size=1)
        qr.add_data(payload, optimize=0)
        qr.make(fit=True)
    except DataOverflowError as error:
        raise ValueError("SSID / key too long for LCD") from error
    matrix = qr.get_matrix()
    scale = 128 // len(matrix)
    if scale < 2:
        raise ValueError("SSID / key too long for LCD")
    image = Image.new("RGB", (len(matrix), len(matrix)), "white")
    image.putdata([(0, 0, 0) if cell else (255, 255, 255) for row in matrix for cell in row])
    return image.resize((len(matrix) * scale, len(matrix) * scale), Image.Resampling.NEAREST)


def amount(value, rate=False):
    if value is None:
        return "--"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            number = f"{value:.1f}" if 0 < value < 100 and unit != "B" else f"{value:.0f}"
            return number + " " + unit + ("/s" if rate else "")
        value /= 1024


class Display:
    def __init__(self):
        from PIL import ImageFont
        regular = self.font_path("DejaVuSans.ttf", "Arial.ttf")
        bold = self.font_path("DejaVuSans-Bold.ttf", "Arial Bold.ttf")
        self.fonts = {"tiny": ImageFont.truetype(regular, 9), "small": ImageFont.truetype(regular, 10),
                      "body": ImageFont.truetype(regular, 11), "bold": ImageFont.truetype(bold, 12),
                      "hero": ImageFont.truetype(bold, 24), "number": ImageFont.truetype(bold, 19)}
        self.qr_key, self.qr = None, None
        self.page, self.page_since = None, 0

    @staticmethod
    def font_path(linux, mac):
        for path in (Path("/usr/share/fonts/truetype/dejavu") / linux,
                     Path("/System/Library/Fonts/Supplemental") / mac):
            if path.is_file():
                return str(path)
        raise RuntimeError("Install fonts-dejavu-core for the LCD")

    def label(self, xy, value, font="small", color=WHITE, width=116):
        # Strip control characters, fit actual glyph widths (not character counts).
        value = "".join(char if char.isprintable() else " " for char in str(value))
        face = self.fonts[font]
        if self.draw.textlength(value, font=face) > width:
            while value and self.draw.textlength(value + "…", font=face) > width:
                value = value[:-1]
            value += "…"
        self.draw.text(xy, value, font=face, fill=color, anchor="lt")

    def box(self, bounds, fill=CARD):
        self.draw.rounded_rectangle(bounds, radius=5, fill=fill)

    def chrome(self, page, data):
        self.label((6, 3), {"overview": "OPENSTICK", "isp": "ISP / UPLINK", "transfer": "DATA TRANSFER",
                           "clients": "CLIENTS", "wifi": "WI-FI QR", "network": "NETWORK", "battery": "BATTERY"}[page],
                   "tiny", CYAN, 87)
        voltage = data.get("battery", {}).get("voltage")
        self.label((96, 3), f"{voltage:.2f}V" if voltage else "-- V", "tiny", MUTED, 29)
        # Voltage only: no invented state of charge or charging icon.
        self.draw.line((4, 16, 123, 16), fill=LINE)
        self.draw.line((4, 115, 123, 115), fill=LINE)
        self.label((6, 119), "2x >", "tiny", MUTED, 28)
        for index in range(len(PAGES)):
            x = 47 + index * 6
            self.draw.ellipse((x, 121, x + 2, 123), fill=CYAN if PAGES[index] == page else LINE)
        self.label((106, 119), f"{PAGES.index(page) + 1}/{len(PAGES)}", "tiny", MUTED, 21)

    def uplink_name(self, data):
        interface = data.get("uplink", {}).get("interface")
        if not interface:
            return "No uplink", "OFFLINE"
        wifi, modem = data.get("wifi", {}), data.get("modem", {})
        if interface.startswith(("wwan", "rmnet")):
            return modem.get("name") or "Mobile network", modem.get("technology") or "CELLULAR"
        if interface == wifi.get("interface") and wifi.get("mode") != "ap":
            return wifi.get("ssid") or "Wi-Fi uplink", "WI-FI"
        return interface, "ROUTED"

    def overview(self, data, elapsed):
        name, kind = self.uplink_name(data)
        self.label((6, 22), kind, "tiny", GREEN if kind != "OFFLINE" else AMBER)
        self.label((6, 35), name, "bold")
        wifi = data.get("wifi", {})
        self.label((6, 53), wifi.get("ssid") if wifi.get("mode") == "ap" else "Hotspot off", "small", MUTED)
        self.box((4, 70, 61, 98))
        self.box((65, 70, 123, 98))
        traffic = data.get("traffic", {})
        self.label((9, 74), "DOWN", "tiny", CYAN)
        self.label((70, 74), "UP", "tiny", GREEN)
        self.label((9, 86), amount(traffic.get("rx_rate"), True), "tiny", WHITE, 49)
        self.label((70, 86), amount(traffic.get("tx_rate"), True), "tiny", WHITE, 49)
        clients = str(len(data.get("clients", []))) if data.get("clients_available") else "?"
        voltage = data.get("battery", {}).get("voltage")
        self.label((6, 103), clients + " clients", "tiny", MUTED, 66)
        self.label((86, 103), f"{voltage:.2f}V" if voltage else "-- V", "tiny", AMBER, 37)

    def isp(self, data, elapsed):
        name, kind = self.uplink_name(data)
        self.label((6, 23), name, "bold")
        self.label((6, 42), kind, "number", CYAN)
        modem = data.get("modem", {})
        cellular = (data.get("uplink", {}).get("interface") or "").startswith(("wwan", "rmnet"))
        signal = modem.get("signal") if cellular else None
        self.label((6, 68), "SIGNAL" if cellular else "UPLINK IP", "tiny", MUTED)
        if cellular:
            for index in range(5):
                height = 4 + index * 3
                self.draw.rectangle((7 + index * 9, 98 - height, 12 + index * 9, 98),
                                    fill=CYAN if signal is not None and signal > index * 20 else LINE)
            self.label((62, 83), f"{signal}%" if signal is not None else "--", "bold", WHITE, 61)
        else:
            self.label((6, 84), data.get("uplink", {}).get("ip") or "Unavailable", "small")
        self.label((6, 103), "Roaming" if cellular and modem.get("roaming") else
                   (modem.get("state", "No modem") if cellular else "Route, not internet test"), "tiny", AMBER if modem.get("roaming") else MUTED)

    def transfer(self, data, elapsed):
        traffic = data.get("traffic", {})
        self.label((6, 22), "DOWN", "tiny", CYAN, 40)
        self.label((48, 22), amount(traffic.get("rx_rate"), True), "small", WHITE, 76)
        self.label((6, 38), "UP", "tiny", GREEN, 40)
        self.label((48, 38), amount(traffic.get("tx_rate"), True), "small", WHITE, 76)
        self.box((4, 54, 123, 84))
        history = traffic.get("history", [])
        maximum = max((v for pair in history for v in pair if v is not None), default=1) or 1
        for stream, color in ((0, CYAN), (1, GREEN)):
            previous = None
            for index, pair in enumerate(history):
                if pair[stream] is None:
                    previous = None
                    continue
                point = (7 + index * 113 / 29, 81 - pair[stream] / maximum * 23)
                if previous is not None:
                    self.draw.line((previous, point), fill=color, width=1)
                previous = point
        self.label((6, 89), "D " + amount(traffic.get("rx_total")), "tiny", CYAN, 58)
        self.label((67, 89), "U " + amount(traffic.get("tx_total")), "tiny", GREEN, 57)
        self.label((6, 103), (traffic.get("interface") or "No uplink") + " / iface totals", "tiny", MUTED)

    def clients(self, data, elapsed):
        clients = data.get("clients", [])
        self.label((6, 21), f"{len(clients):02}" if data.get("clients_available") else "--", "hero", CYAN, 44)
        self.label((51, 25), "CONNECTED", "tiny", WHITE, 73)
        self.label((51, 38), "Wi-Fi + LAN", "tiny", MUTED, 73)
        if not clients:
            self.label((6, 63), "No active clients" if data.get("clients_available") else "Client data unavailable", "small", MUTED)
            self.label((6, 82), "Leases alone not counted", "tiny", MUTED)
            return
        start = (int(elapsed // 6) % ((len(clients) + 1) // 2)) * 2
        for index, client in enumerate(clients[start:start + 2]):
            y = 55 + index * 24
            self.label((6, y), client.get("name") or client["mac"], "small", WHITE,
                       91 if client.get("signal") is not None else 116)
            if client.get("signal") is not None:
                self.label((102, y), str(client["signal"]), "tiny", CYAN, 22)
            self.label((6, y + 12), client.get("ip") or "IP pending", "tiny", MUTED, 94)
            self.label((102, y + 12), "WF" if client["kind"] == "Wi-Fi" else "LAN", "tiny", CYAN, 22)
        self.label((6, 105), f"{start + 1}-{min(start + 2, len(clients))} of {len(clients)} / auto 6s", "tiny", MUTED)

    def wifi(self, data, elapsed):
        wifi = data.get("wifi", {})
        try:
            payload = wifi_payload(wifi)
            if payload != self.qr_key:
                self.qr = qr_image(payload)
                self.qr_key = payload
            self.image.paste("white", (0, 0, 128, 128))
            offset = (128 - self.qr.width) // 2
            self.image.paste(self.qr, (offset, offset))
            # Large codes use the entire screen, preserving all four quiet zones.
            if offset >= 12:
                self.label((6, 1), "SCAN TO JOIN WI-FI", "tiny", "black")
                self.label((6, 118), wifi["ssid"], "tiny", "black")
        except (ValueError, ImportError) as error:
            reason = str(error) if not isinstance(error, ImportError) else "Install python3-qrcode"
            self.label((6, 26), "JOIN WI-FI", "bold", CYAN)
            self.label((6, 48), wifi.get("ssid") or "No active hotspot", "small")
            # Wrap errors instead of silently drawing an invalid or tiny code.
            words, lines = reason.split(), [""]
            for word in words:
                if self.draw.textlength((lines[-1] + " " + word).strip(), font=self.fonts["small"]) > 116:
                    lines.append(word)
                else:
                    lines[-1] = (lines[-1] + " " + word).strip()
            for index, line in enumerate(lines[:3]):
                self.label((6, 69 + index * 13), line, "small", AMBER)

    def network(self, data, elapsed):
        wifi, uplink = data.get("wifi", {}), data.get("uplink", {})
        for y, title, value in ((23, "LAN / USB + HOTSPOT", data.get("lan_ip")),
                                (53, "UPLINK / " + (uplink.get("interface") or "NONE"), uplink.get("ip")),
                                (83, "HOTSPOT SSID", wifi.get("ssid") if wifi.get("mode") == "ap" else "Hotspot off")):
            self.label((6, y), title, "tiny", MUTED)
            self.label((6, y + 12), value or "--", "small", CYAN if y == 23 else WHITE)

    def battery(self, data, elapsed):
        battery = data.get("battery", {})
        voltage = battery.get("voltage")
        self.label((6, 23), f"{voltage:.2f} V" if voltage else "-- V", "hero", AMBER)
        self.label((6, 53), "Health: " + (battery.get("health") or "Unknown"), "small")
        self.label((6, 71), "2100 mAh / 7.98 Wh", "small", MUTED)
        self.label((6, 88), "Charge unverified", "small", AMBER)
        self.label((6, 103), "No calibrated percentage", "tiny", MUTED)

    def power_dialog(self, state):
        self.draw.rectangle((0, 0, 127, 127), fill=BG)
        self.draw.ellipse((51, 9, 77, 35), outline=RED, width=2)
        self.draw.rectangle((60, 7, 68, 17), fill=BG)
        self.draw.line((64, 7, 64, 23), fill=RED, width=2)
        self.label((15, 44), "POWER OFF?", "bold", WHITE, 108)
        if state.power_off_pending:
            self.label((14, 71), "Shutting down…", "small", AMBER)
            self.label((9, 99), "Wait before unplugging", "tiny", MUTED)
        elif not state.power_key_released:
            self.label((14, 68), "Release button now", "small", AMBER)
            self.label((9, 91), "Keep holding = forced off", "tiny", MUTED)
        else:
            self.box((6, 65, 121, 85), "#3b202d")
            self.label((15, 71), "2x click: power off", "small", RED)
            self.label((17, 93), "1x click: cancel", "small", WHITE)
            remaining = max(0, int(state.power_deadline - self.now + 0.999))
            self.label((14, 111), "Failed; retry or cancel" if state.power_off_error else f"Cancels in {remaining}s", "tiny", AMBER if state.power_off_error else MUTED)

    def render(self, state, data, now=None):
        from PIL import Image, ImageDraw
        self.now = time.monotonic() if now is None else now
        page = PAGES[state.page]
        if page != self.page:
            self.page, self.page_since = page, self.now
        self.image = Image.new("RGB", (128, 128), BG)
        self.draw = ImageDraw.Draw(self.image)
        if state.power_deadline is not None or state.power_off_pending:
            self.power_dialog(state)
        else:
            self.chrome(page, data)
            getattr(self, page)(data, self.now - self.page_since)
            if data.get("loading"):
                self.draw.rectangle((0, 100, 127, 114), fill=BG)
                self.label((6, 103), "Reading device…", "tiny", AMBER)
            sampled = data.get("sampled_at")
            details = data.get("details_at")
            if (sampled is not None and self.now - sampled > 15) or (details is not None and self.now - details > 40):
                # Never present stale credentials as a current join code.
                if page == "wifi":
                    self.image.paste(BG, (0, 0, 128, 128))
                    self.label((6, 50), "Wi-Fi data expired", "small", AMBER)
                else:
                    self.draw.rectangle((0, 100, 127, 114), fill=BG)
                    self.label((6, 103), "Telemetry delayed", "tiny", AMBER)
        return self.image
