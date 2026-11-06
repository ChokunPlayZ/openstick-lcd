#!/usr/bin/env python3
"""Render synthetic LCD previews; never connects to hardware or a real network."""
import argparse
from pathlib import Path

from lcd_display import Display, PAGES
from lcd_telemetry import empty_snapshot
from openstick_device import DEFAULTS, ScreenState


def demo_snapshot():
    return {**empty_snapshot(), "loading": False, "sampled_at": 100, "details_at": 100,
            "uplink": {"interface": "wwan0", "ip": "10.42.18.7", "gateway": "10.42.18.1"},
            "modem": {"name": "Northstar Mobile", "technology": "LTE", "signal": 78,
                      "state": "connected", "roaming": False},
            "wifi": {"mode": "ap", "ssid": "OpenStick", "security": "wpa-psk", "password": "demo-only-1234",
                     "interface": "wlan0", "hidden": False},
            "clients_available": True,
            "clients": [{"name": "My phone", "mac": "02:00:00:00:00:01", "ip": "192.168.100.21", "kind": "Wi-Fi", "signal": -42},
                        {"name": "Laptop", "mac": "02:00:00:00:00:02", "ip": "192.168.100.22", "kind": "LAN", "signal": None},
                        {"name": "Tablet", "mac": "02:00:00:00:00:03", "ip": "192.168.100.23", "kind": "Wi-Fi", "signal": -57}],
            "traffic": {"interface": "wwan0", "rx_rate": 2.4 * 1024**2, "tx_rate": 186 * 1024,
                        "rx_total": 1.7 * 1024**3, "tx_total": 238 * 1024**2,
                        "history": [(1024**2 * (0.8 + ((i * 7) % 13) / 7), 1024**2 * (0.1 + i % 7 / 20)) for i in range(30)]},
            "battery": {"voltage": 4.12, "health": "Good"}, "lan_ip": "192.168.100.1"}


def main():
    from PIL import Image, ImageDraw
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Directory for PNGs with demo-only data")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    display, data = Display(), demo_snapshot()
    state = ScreenState(DEFAULTS.copy(), 100)
    frames = []
    for index, page in enumerate(PAGES):
        state.page = index
        frames.append((page, display.render(state, data, 100).copy()))
    state.request_power_off(100)
    frames.append(("power-release", display.render(state, data, 100).copy()))
    state.power_key_released = True
    frames.append(("power-confirm", display.render(state, data, 100).copy()))
    state.power_off_pending = True
    frames.append(("power-off", display.render(state, data, 100).copy()))
    columns, scale = 5, 3
    sheet = Image.new("RGB", (columns * 408, ((len(frames) + columns - 1) // columns) * 432), "#202938")
    draw = ImageDraw.Draw(sheet)
    for index, (name, image) in enumerate(frames):
        image.save(args.output / (name + ".png"))
        x, y = (index % columns) * 408 + 12, (index // columns) * 432 + 12
        sheet.paste(image.resize((128 * scale, 128 * scale), Image.Resampling.NEAREST), (x, y))
        draw.text((x, y + 393), name, font=display.fonts["bold"], fill="white")
    sheet.save(args.output / "preview.png")
    print(args.output / "preview.png")


if __name__ == "__main__":
    main()
