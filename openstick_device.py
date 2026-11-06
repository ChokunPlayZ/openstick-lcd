#!/usr/bin/python3
"""Independent MF800H screen/button daemon. No web server or charger writes."""
import argparse
import array
import fcntl
import json
import logging
import mmap
import os
from pathlib import Path
import selectors
import signal
import socket
import stat
import struct
import subprocess
import time

from lcd_display import Display, PAGES
from lcd_telemetry import Telemetry

LOG = logging.getLogger("openstick-device")
DEFAULTS = {
    "screen_sleep_seconds": 60,
    "double_click_ms": 350,
    "single_click": "display.toggle",
    "double_click": "display.next_page",
}
ACTIONS = ("display.toggle", "display.wake", "display.sleep", "display.next_page",
           "display.previous_page", "none", "hook")
INPUT_EVENT = struct.Struct("@llHHi")
EVIOCGRAB = 0x40044590
EVIOCSCLOCKID = 0x400445a0


def text(path, default=""):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def validate_config(values):
    if not isinstance(values, dict) or set(values) - set(DEFAULTS):
        raise ValueError("Unknown configuration field")
    result = {**DEFAULTS, **values}
    for key, low, high in (("screen_sleep_seconds", 0, 86400), ("double_click_ms", 200, 800)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f"{key} must be an integer from {low} to {high}")
    for key in ("single_click", "double_click"):
        if result[key] not in ACTIONS:
            raise ValueError(f"Unsupported {key} action")
    return result


def save_config(path, config):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(config, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class ClickDetector:
    """Classify short clicks and hold/release. A double never emits a single."""
    def __init__(self, window=0.35):
        self.window = window
        self.down = None
        self.pending = None
        self.held = False

    def tick(self, now):
        if self.down is not None and not self.held and now - self.down >= 1.2:
            self.held = True
            self.pending = None
            return ["hold"]
        if self.pending is not None and self.down is None and now >= self.pending + self.window:
            self.pending = None
            return ["single_click"]
        return []

    def event(self, value, now):
        result = self.tick(now)
        if value == 1 and self.down is None:
            self.down = now
            self.held = False
        elif value == 0 and self.down is not None:
            duration = now - self.down
            self.down = None
            if duration >= 1.2:
                self.pending = None
                self.held = False
                return result + ["hold_release"]  # Never also emit a short click.
            if duration < 0.025:
                return result  # Contact bounce.
            if self.pending is not None and now - self.pending <= self.window:
                self.pending = None
                result.append("double_click")
            else:
                if self.pending is not None:
                    result.append("single_click")
                self.pending = now
        return result


class ScreenState:
    def __init__(self, config, now):
        self.config = config
        self.awake = True
        self.page = 0
        self.activity = now
        self.last_gesture = ""
        self.clicks = {"single_click": 0, "double_click": 0}
        self.power_deadline = None
        self.power_key_released = False
        self.power_off_pending = False
        self.power_off_error = ""

    def request_power_off(self, now):
        if self.power_off_pending:
            return
        self.power_deadline = now + 10
        self.power_key_released = False
        self.power_off_error = ""
        self.awake = True
        self.activity = now

    def cancel_power_off(self, now):
        self.power_deadline = None
        self.power_key_released = False
        self.power_off_error = ""
        self.activity = now

    def action(self, action, now):
        if action not in ACTIONS:
            raise ValueError("Unsupported display action")
        if action == "display.toggle":
            self.awake = not self.awake
        elif action == "display.sleep":
            self.awake = False
        elif action == "display.wake":
            self.awake = True
        elif action in ("display.next_page", "display.previous_page"):
            self.page = (self.page + (1 if action == "display.next_page" else -1)) % len(PAGES)
            self.awake = True
        if action != "none":
            self.activity = now

    def tick(self, now):
        if self.power_off_pending:
            self.awake = True
            return
        if self.power_deadline is not None:
            if now < self.power_deadline:
                self.awake = True
                return
            self.cancel_power_off(now)
        timeout = self.config["screen_sleep_seconds"]
        if self.awake and timeout and now - self.activity >= timeout:
            self.awake = False

    def status(self, now):
        timeout = self.config["screen_sleep_seconds"]
        return {"config": self.config, "awake": self.awake, "page": PAGES[self.page],
                "pages": list(PAGES), "power_confirmation": self.power_deadline is not None,
                "power_off_pending": self.power_off_pending, "power_off_error": self.power_off_error,
                "sleep_in_seconds": max(0, round(timeout - (now - self.activity)))
                if timeout and self.awake and self.power_deadline is None and not self.power_off_pending else None,
                "last_gesture": self.last_gesture, "clicks": self.clicks.copy()}


class Framebuffer:
    def __init__(self):
        candidates = [fb for fb in Path("/sys/class/graphics").glob("fb[0-9]*")
                      if text(fb / "virtual_size") == "128,128"]
        if len(candidates) != 1:
            raise RuntimeError("Expected one MF800H 128x128 framebuffer")
        for console in Path("/sys/class/vtconsole").glob("vtcon[0-9]*"):
            if "frame buffer" in text(console / "name") and text(console / "bind") == "1":
                (console / "bind").write_text("0\n")
        self.device = "/dev/" + candidates[0].name
        self.fd = os.open(self.device, os.O_RDWR)
        var = array.array("I", [0] * 40)
        fcntl.ioctl(self.fd, 0x4600, var, True)
        self.width, self.height, _, virtual_height, self.xoff, self.yoff, self.bpp = var[:7]
        self.stride = struct.unpack_from("I", fcntl.ioctl(self.fd, 0x4602, bytes(80)), 48)[0]
        if self.width != 128 or self.height != 128 or self.bpp != 32 or tuple(var[i] for i in (8, 11, 14)) != (16, 8, 0):
            raise RuntimeError("Unsupported framebuffer layout")
        self.memory = mmap.mmap(self.fd, self.stride * virtual_height)
        self.led = Path("/sys/class/leds/mf800h:lcd/brightness")
        if not self.led.exists():
            raise RuntimeError("MF800H backlight is unavailable")
        self.display = Display()
        self.telemetry = Telemetry()
        self.telemetry.start()
        self.was_awake = None
        LOG.info("Rendering on %s, 128x128, no framebuffer console", self.device)

    def put(self, image):
        pixels = image.tobytes("raw", "BGRX")
        for row in range(128):
            offset = (row + self.yoff) * self.stride + self.xoff * 4
            self.memory[offset:offset + 512] = pixels[row * 512:(row + 1) * 512]

    def render(self, state):
        from PIL import Image
        if not state.awake:
            if self.was_awake is not False:
                self.led.write_text("0\n")
                self.put(Image.new("RGB", (128, 128), "black"))
            self.was_awake = False
            return
        self.put(self.display.render(state, self.telemetry.snapshot()))
        if self.was_awake is not True:
            self.led.write_text("1\n")
        self.was_awake = True

    def close(self):
        self.telemetry.close()
        self.led.write_text("0\n")
        self.memory.close()
        os.close(self.fd)


class Daemon:
    def __init__(self, config_path, socket_path, renderer):
        self.config_path = Path(config_path)
        self.socket_path = Path(socket_path)
        if self.config_path.exists():
            config = validate_config(json.loads(self.config_path.read_text()))
        else:
            config = DEFAULTS.copy()
            save_config(self.config_path, config)
        self.state = ScreenState(config, time.monotonic())
        self.detector = ClickDetector(config["double_click_ms"] / 1000)
        self.renderer = renderer
        self.selector = selectors.DefaultSelector()
        self.input_fd = None
        self.input_path = ""
        self.input_buffer = b""
        self.input_dropped = False
        self.input_error = "Power key not opened yet"
        self.hook = None
        self.hook_deadline = 0
        self.last_macro_error = ""
        self.running = True
        self.listener = None
        self.poweroff = None
        self.poweroff_deadline = 0

    def status(self):
        return {**self.state.status(time.monotonic()), "input_device": self.input_path,
                "button_available": self.input_fd is not None, "button_error": self.input_error,
                "framebuffer": self.renderer.device, "macro_running": self.hook is not None,
                "last_macro_error": self.last_macro_error}

    def dispatch(self, request):
        if not isinstance(request, dict):
            raise ValueError("Expected a JSON object")
        operation = request.get("op")
        if operation == "status":
            pass  # Polling must not wake the screen or extend its timeout.
        elif operation == "configure":
            config = validate_config(request.get("config"))
            save_config(self.config_path, config)
            self.state.config = config
            self.state.activity = time.monotonic()
            self.detector.window = config["double_click_ms"] / 1000
        elif operation in ACTIONS and operation not in ("hook", "none"):
            self.state.action(operation, time.monotonic())
            self.renderer.render(self.state)
        else:
            raise ValueError("Unknown operation")
        return self.status()

    def start_hook(self, gesture):
        if self.hook is not None:
            raise ValueError("A button macro is already running")
        directory = self.config_path.parent / "macros"
        path = directory / gesture.replace("_", "-")
        # Hooks are deliberate root scripts, never shell text from an HTTP request.
        for candidate in (directory, path):
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError("Macro and its directory must be root-owned, non-symlink and not group/world writable")
        if not stat.S_ISREG(path.stat().st_mode) or not os.access(path, os.X_OK):
            raise ValueError("Macro must be an executable regular file")
        self.hook = subprocess.Popen([str(path)], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                                          "OPENSTICK_GESTURE": gesture}, start_new_session=True)
        self.hook_deadline = time.monotonic() + 10

    def gesture(self, gesture):
        now = time.monotonic()
        self.state.tick(now)
        if gesture == "hold":
            self.state.request_power_off(now)
        elif gesture == "hold_release":
            if self.state.power_deadline is not None:
                self.state.power_key_released = True
                self.state.power_deadline = now + 10
        elif self.state.power_off_pending:
            return
        elif self.state.power_deadline is not None:
            # Confirmation always overrides configured macros, including hooks.
            if self.state.power_key_released:
                if gesture == "single_click":
                    self.state.cancel_power_off(now)
                elif gesture == "double_click":
                    self.start_poweroff(now)
        else:
            self.macro(gesture)
            return
        self.renderer.render(self.state)

    def start_poweroff(self, now):
        try:
            # A fixed, non-shell command, reachable only through the physical
            # hold/release/double-click flow; never via the control socket.
            self.poweroff = subprocess.Popen(["/bin/systemctl", "--no-block", "poweroff"],
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL)
            self.poweroff_deadline = now + 5
            self.state.power_off_pending = True
            self.state.power_off_error = ""
            LOG.info("Physical button confirmed orderly power-off")
        except OSError:
            self.state.power_off_error = "Unable to request system power-off"
            self.state.power_deadline = now + 10
            LOG.warning(self.state.power_off_error)

    def check_poweroff(self, now):
        if self.poweroff is None:
            return
        code = self.poweroff.poll()
        if code is None and now < self.poweroff_deadline:
            return
        if code is None:
            self.poweroff.kill()
        self.poweroff.wait()
        self.poweroff = None
        if code != 0:
            self.state.power_off_pending = False
            self.state.power_off_error = "System power-off request failed"
            self.state.power_deadline = now + 10
            LOG.warning(self.state.power_off_error)
        self.renderer.render(self.state)

    def macro(self, gesture):
        self.state.clicks[gesture] += 1
        self.state.last_gesture = gesture
        action = self.state.config[gesture]
        LOG.info("Button %s -> %s", gesture, action)
        self.last_macro_error = ""
        try:
            self.state.action(action, time.monotonic())
            if action == "hook":
                self.start_hook(gesture)
        except (OSError, ValueError) as error:
            self.last_macro_error = str(error)
            LOG.warning("Button macro: %s", error)
        self.renderer.render(self.state)

    def connect_input(self):
        for event in sorted(Path("/sys/class/input").glob("event*")):
            if text(event / "device/name") not in ("pm8941_pwrkey", "pm8916_pwrkey"):
                continue
            fd = None
            try:
                fd = os.open("/dev/input/" + event.name, os.O_RDONLY | os.O_NONBLOCK)
                fcntl.ioctl(fd, EVIOCSCLOCKID, struct.pack("i", time.CLOCK_MONOTONIC))
                fcntl.ioctl(fd, EVIOCGRAB, 1)
                self.selector.register(fd, selectors.EVENT_READ, "input")
                self.input_fd = fd
                self.input_path = "/dev/input/" + event.name
                self.input_error = ""
                LOG.info("Exclusively grabbed power button %s", self.input_path)
                return
            except OSError as error:
                if fd is not None:
                    os.close(fd)
                self.input_error = str(error)
        if not self.input_error:
            self.input_error = "PMIC power-key device is unavailable"

    def read_input(self):
        try:
            block = os.read(self.input_fd, INPUT_EVENT.size * 32)
            if not block:
                raise OSError("Power-key input disconnected")
            self.input_buffer += block
            while len(self.input_buffer) >= INPUT_EVENT.size:
                seconds, microseconds, kind, code, value = INPUT_EVENT.unpack(self.input_buffer[:INPUT_EVENT.size])
                self.input_buffer = self.input_buffer[INPUT_EVENT.size:]
                if kind == 0 and code == 3:  # SYN_DROPPED: discard incomplete gestures.
                    self.detector = ClickDetector(self.state.config["double_click_ms"] / 1000)
                    self.state.cancel_power_off(time.monotonic())
                    self.input_dropped = True
                elif self.input_dropped:
                    if kind == 0 and code == 0:
                        self.input_dropped = False
                elif kind == 1 and code == 116:  # EV_KEY, KEY_POWER; ignore autorepeat.
                    for gesture in self.detector.event(value, seconds + microseconds / 1e6):
                        self.gesture(gesture)
        except BlockingIOError:
            pass
        except OSError as error:
            LOG.warning("Power key: %s", error)
            self.input_error = str(error)
            self.selector.unregister(self.input_fd)
            os.close(self.input_fd)
            self.input_fd = None
            self.input_path = ""
            self.input_buffer = b""
            self.input_dropped = False
            self.detector = ClickDetector(self.state.config["double_click_ms"] / 1000)
            self.state.cancel_power_off(time.monotonic())

    def serve_request(self):
        connection, _ = self.listener.accept()
        with connection:
            connection.settimeout(0.2)
            try:
                data = b""
                while b"\n" not in data and len(data) <= 4096:
                    block = connection.recv(4097 - len(data))
                    if not block:
                        break
                    data += block
                if len(data) > 4096 or b"\n" not in data:
                    raise ValueError("Request must be a JSON line of at most 4096 bytes")
                request = json.loads(data.split(b"\n", 1)[0])
                response = {"ok": True, "state": self.dispatch(request)}
            except (OSError, ValueError, TypeError) as error:
                response = {"ok": False, "error": str(error)}
            try:
                connection.sendall(json.dumps(response).encode() + b"\n")
            except OSError:
                pass

    def run(self):
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = os.open(self.socket_path.parent / "daemon.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if self.socket_path.exists():
            if not stat.S_ISSOCK(self.socket_path.lstat().st_mode):
                raise RuntimeError("Refusing to replace a non-socket control path")
            self.socket_path.unlink()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self.listener.listen(8)
        self.listener.setblocking(False)
        self.selector.register(self.listener, selectors.EVENT_READ, "socket")
        next_input, next_render = 0, 0
        try:
            while self.running:
                now = time.monotonic()
                if self.input_fd is None and now >= next_input:
                    self.connect_input()
                    next_input = now + 3
                # Drain timestamped input before expiring a pending single-click.
                for key, _ in self.selector.select(0.05):
                    if key.data == "input":
                        self.read_input()
                    else:
                        self.serve_request()
                now = time.monotonic()
                for gesture in self.detector.tick(now):
                    self.gesture(gesture)
                previous = self.state.awake
                power_prompt = self.state.power_deadline
                self.state.tick(now)
                self.check_poweroff(now)
                if previous != self.state.awake or power_prompt != self.state.power_deadline or now >= next_render:
                    self.renderer.render(self.state)
                    next_render = now + (0.2 if self.state.power_deadline is not None else 2)
                if self.hook is not None:
                    exitcode = self.hook.poll()
                    if now >= self.hook_deadline or exitcode is not None:
                        # Kill any background descendants too; hooks must be bounded.
                        try:
                            os.killpg(self.hook.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        self.hook.wait()
                        if exitcode is None or exitcode != 0:
                            self.last_macro_error = "Macro timed out" if exitcode is None else f"Macro exited {exitcode}"
                            LOG.warning(self.last_macro_error)
                        self.hook = None
        finally:
            if self.poweroff is not None:
                if self.poweroff.poll() is None:
                    self.poweroff.kill()
                self.poweroff.wait()
            if self.hook is not None:
                try:
                    os.killpg(self.hook.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.hook.wait()
            if self.input_fd is not None:
                os.close(self.input_fd)
            self.selector.close()
            self.listener.close()
            self.socket_path.unlink(missing_ok=True)
            self.renderer.close()
            os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/openstick-device/config.json")
    parser.add_argument("--socket", default="/run/openstick-device/control.sock")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    daemon = Daemon(args.config, args.socket, Framebuffer())
    def stop(_signum, _frame):
        daemon.running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    daemon.run()


if __name__ == "__main__":
    main()
