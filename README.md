# Independent MF800H screen and button daemon

`openstick-device.service` owns the GC9107 framebuffer, backlight and side power
button. It **does not depend on the WebUI**: stop/restart `openstick-webui.service`
and the screen, timer and button macros keep working. The WebUI connects to its
root-only Unix socket, `/run/openstick-device/control.sock`; there is no additional
network listener, cloud dependency or charger-control loop.

Requires the previously installed MF800H LCD DTB and panel firmware, Python 3,
Debian `python3-pil`, `python3-qrcode`, `fonts-dejavu-core`, and systemd. Network
details use the existing `ip`, `nmcli`, `mmcli` and `iw` utilities; missing tools
produce unavailable readings, not fabricated data. It replaces the old
`mf800h-display.service`; the old script/unit and headless DTB remain available
for recovery. Do not run both framebuffer renderers together. This daemon is
specific to the MF800H wiring, not a generic LCD installer for other dongles.

The daemon, tests and `openstick-device.service` live in this standalone
folder, outside both the installer and WebUI. Original LCD firmware and
device-tree work live in [hardware/](hardware/README.md).

Run the daemon's host-side tests from this folder without the WebUI:

```sh
python3 -m unittest discover -s . -p 'test_*.py' -v
```

For the complete renderer and QR scan tests on a development host:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m unittest discover -s . -p 'test_*.py' -v
.venv/bin/python preview.py --output /path/to/lcd-previews
```

Previews use **synthetic** operator, client and Wi-Fi data, never the device or
real credentials. The PNG contact sheet includes all seven screens and the
power-off dialog. Linux uses DejaVu Sans; macOS previews fall back to Arial.
Renderer tests are skipped when Pillow is absent; decoder tests also require
the host-only `zxing-cpp` package. Device runtime does not need that decoder.

## Screens

The 128 × 128 design uses a dark background, cyan/green transfer indicators,
pixel-fitted text, a voltage status bar and seven-page navigation dots:

1. **Overview:** current uplink/provider, hotspot SSID, down/up rates, clients
   and measured battery voltage.
2. **ISP / uplink:** cellular operator, access technology, recent signal quality,
   connection state and roaming. Wi-Fi uplinks show the SSID and address instead.
   A selected route is **not** a verified internet connection; no external IP or
   geolocation service is contacted.
3. **Data transfer:** live receive/transmit rates, a 30-sample sparkline and
   kernel counters for the selected uplink only, avoiding LAN/bridge duplication.
   Rates use B/s, KiB/s or MiB/s; totals are interface counters, **not** a monthly
   allowance or billed usage. A reset/disappearing interface produces unknown
   rates until a second valid sample, rather than a negative spike.
4. **Clients:** associated AP stations plus active LAN neighbors, with DHCP
   hostnames, addresses and Wi-Fi signal in dBm when available. Old leases or
   stale neighbors alone do not count as connected. Two clients are shown at a
   time; further entries rotate every six seconds without changing pages.
5. **Wi-Fi QR:** joins the active local hotspot (open or WPA/WPA2 personal).
   Upstream Wi-Fi credentials are never read for this page. Hidden SSIDs and
   reserved characters are escaped. Enterprise, legacy WEP and WPA3-only modes
   show an explicit unsupported message. The code is black/white with a full
   four-module quiet zone, integer scaling and at least two pixels per module;
   credentials too long to fit show an error rather than an unscannable QR.
   Large codes occupy the entire panel. Treat this page as physical access to
   the hotspot password; no credentials are sent through the control socket or
   logged by the daemon.
6. **Network:** actual LAN bridge address, selected uplink address and AP SSID.
7. **Battery:** voltage, health and design capacity, retaining the explicit
   uncalibrated-percentage / unverified-charging warnings.

Read-only telemetry runs on a background worker: counters/voltage approximately
every two seconds and network/modem/client details approximately every ten
seconds. Subprocesses have deadlines. The button loop never waits for them.
Delayed data is marked; expired Wi-Fi data hides the QR. Screen sleep still
stops framebuffer drawing, without interrupting telemetry or networking.

## Hold to power off

- Hold the side button for **about 1.2 seconds**; the LCD wakes and displays
  **POWER OFF? / Release button now**. **Release immediately.**
- After release, **double-click to confirm** an orderly Linux power-off, or
  **single-click to cancel**. This overrides the configured short-click macros
  only while the prompt is open. The hold itself never runs either macro.
- No response cancels the prompt after **10 seconds**. Input loss also cancels.
  The screen sleep timer is paused during confirmation and restarted on cancel.
- Confirmation executes the fixed `/bin/systemctl --no-block poweroff` command;
  a failed/timed-out request is shown and can be retried or cancelled. The
  control socket cannot open or confirm this physical-button flow.

The **PMIC emergency long-hold action is unchanged** and cannot be cancelled by
this dialog. Do not continue holding while deciding. This is orderly OS
shutdown, not a new battery/charger control or a guarantee that USB power is cut.

## Defaults and configuration

`/etc/openstick-device/config.json` (root-owned, 0600):

```json
{
  "screen_sleep_seconds": 60,
  "double_click_ms": 350,
  "single_click": "display.toggle",
  "double_click": "display.next_page"
}
```

Change these in **Battery & display** in the WebUI. Settings are validated by
both the backend and daemon and saved atomically. Editing the JSON through SSH
requires restarting the daemon. Normal upgrades preserve it. Timeout `0` means
never sleep; valid values are 0–86400 seconds. The double-click window is
200–800 ms. Single-click dispatch waits until that window closes. Double-click
dispatches only the double macro, not the single macro as well. Presses shorter
than 25 ms are ignored as bounce, autorepeat is ignored, and holds of at least
1.2 seconds open the separate power-off confirmation, not short-click macros.
The PMIC's forced long-hold action is **not** changed; release when prompted.

Mappings: `display.toggle`, `display.wake`, `display.sleep`, `display.next_page`,
`display.previous_page`, `none`, or `hook`. Changing pages wakes the screen.
The default double-click advances through all seven pages. Sleep blacks the framebuffer,
switches off the MPP4 backlight and pauses drawing; the panel power rails stay
on. It is **not system suspend** and Wi-Fi/Linux remain running. Status polling
does not wake the screen or reset the sleep countdown.

The daemon exclusively grabs the PMIC `KEY_POWER` input and holds logind's
`handle-power-key` inhibitor while running. The device's existing
`HandlePowerKey=ignore` policy is not changed. The framebuffer virtual console
is detached to prevent a blinking cursor; USB ACM and UART consoles remain.

## Optional custom macros

Select **Custom root-owned script** for a gesture and install the corresponding
executable file:

```text
/etc/openstick-device/macros/single-click
/etc/openstick-device/macros/double-click
```

Directory and files must be root-owned, not symlinks and not group/world-writable;
use mode 0700. Each file needs a shebang, for example:

```sh
#!/bin/sh
logger -t openstick-button "Custom side-button macro ran"
```

These are deliberately root-capable scripts. Only trusted administrators should
edit them. A macro is invoked by its fixed path, not by interpolating HTTP input
into a shell. `OPENSTICK_GESTURE` is set to `single_click` or `double_click`.
Only one custom macro can run at a time, with a 10-second limit; its process
group is terminated on exit/timeout. Standard output is discarded and nonzero
exit/timeout/missing-script errors appear in WebUI diagnostics and the journal.
The daemon's read-only systemd filesystem restrictions apply to hooks too;
ordinary direct writes outside `/etc/openstick-device` and its runtime directory
are not supported. Network changes through system services may disconnect you;
the safe defaults do not alter networking, charging or device power state.

## WebUI battery module

`GET /api/hardware` is authenticated and combines read-only BMS sysfs telemetry
with the daemon status. Changes use the existing CSRF-protected, confirmed,
audited job mechanism (`hardware.configure` and `hardware.screen`). The WebUI
does not directly open the framebuffer or input device and never starts the
daemon as a child process. A missing/stopped daemon is an explicit status error,
not a reason to invent successful control results. Battery voltage remains
available independently of the daemon.

Battery label: 3.8 V nominal, 2100 mAh, 7.98 Wh, 4.35 V maximum. That maximum is
design metadata, **not** a newly programmed charger setpoint. Percentage is
explicitly marked uncalibrated; BMS `Discharging` lacks a charger-supplier link
and is not treated as measured current direction. Active charging remains
unverified and no charge-limit controls or charging-register writes are added.

## Standalone installation

No WebUI source/build/deploy is required. For a fresh headless device, first
read [hardware/README.md](hardware/README.md) to install the matching LCD DTB
and panel firmware. Never apply that DTB to another kernel or board revision.

Copy this folder to the MF800H. On the device:

```sh
sudo apt-get install python3-pil python3-qrcode fonts-dejavu-core iw
sudo sh install.sh
sudo systemctl status openstick-device
sudo journalctl -u openstick-device -n 100 --no-pager
```

The installer checks MF800H identity, framebuffer/backlight, Python libraries,
fonts and systemd. It saves any previous service/runtime files in a new dated
`/var/backups/openstick-device-*` directory, preserves `/etc/openstick-device/`
configuration/macros, stops/disables the old renderer, and installs all three
Python modules together. It only restarts the LCD service; it does not flash,
edit boot/network/charging settings, install the WebUI, or reboot.

Runtime: `/usr/local/lib/openstick-device/`; docs:
`/usr/local/share/openstick-device/`; config: `/etc/openstick-device/`;
unit: `/etc/systemd/system/openstick-device.service`.

To disable the daemon: `sudo systemctl disable --now openstick-device`.
To revert to the old renderer, enable `mf800h-display` only if its original
script/unit still exist; never run both renderers together. For headless
boot recovery restore your own saved extlinux configuration as described in
the hardware guide. WebUI controls report unavailable when the daemon is stopped.

See [RELEASE.md](RELEASE.md) for publication/license gates. Host tests are not
a new native/hardware test; charging and percentage remain unverified.
