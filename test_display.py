import importlib.util
import unittest
from unittest.mock import patch

from lcd_display import Display, PAGES, amount, qr_image, wifi_payload
from lcd_telemetry import empty_snapshot
from openstick_device import DEFAULTS, ScreenState

PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None
QR_AVAILABLE = importlib.util.find_spec("qrcode") is not None
DECODER_AVAILABLE = importlib.util.find_spec("zxingcpp") is not None


class FormattingTests(unittest.TestCase):
    def test_wifi_payload_escaping_hidden_and_open(self):
        wifi = {"mode": "ap", "ssid": ' Cafe: A;B,"C\\ ', "password": 'p:a;s,s"\\',
                "security": "wpa-psk", "hidden": True}
        self.assertEqual(wifi_payload(wifi), 'WIFI:T:WPA;S: Cafe\\: A\\;B\\,\\"C\\\\ ;P:p\\:a\\;s\\,s\\"\\\\;H:true;;')
        self.assertEqual(wifi_payload({"mode": "ap", "ssid": "Guest", "security": "--"}), "WIFI:T:nopass;S:Guest;;")

    def test_never_offer_uplink_credentials_or_invalid_security(self):
        for wifi in ({}, {"mode": "infrastructure", "ssid": "Home", "password": "secret"},
                     {"mode": "ap", "ssid": "Private", "security": "wpa-psk"},
                     {"mode": "ap", "ssid": "Private", "security": "wpa-eap"},
                     {"mode": "ap", "ssid": "Legacy WEP", "security": "none"},
                     {"mode": "ap", "ssid": "Private", "security": "sae", "password": "secret"}):
            with self.subTest(wifi=wifi), self.assertRaises(ValueError):
                wifi_payload(wifi)

    def test_transfer_units_and_unknown(self):
        self.assertEqual(amount(None, True), "--")
        self.assertEqual(amount(0, True), "0 B/s")
        self.assertEqual(amount(2.5 * 1024**2, True), "2.5 MiB/s")
        self.assertEqual(amount(1.7 * 1024**3), "1.7 GiB")


@unittest.skipUnless(PIL_AVAILABLE, "Install Pillow to test LCD rendering")
class RenderingTests(unittest.TestCase):
    def setUp(self):
        from preview import demo_snapshot
        self.data = demo_snapshot()
        self.state = ScreenState(DEFAULTS.copy(), 100)
        self.display = Display()

    def test_all_pages_empty_and_populated_are_rgb_128(self):
        for data in (self.data, empty_snapshot()):
            for index, page in enumerate(PAGES):
                with self.subTest(page=page, populated=data is self.data):
                    self.state.page = index
                    image = self.display.render(self.state, data, 100)
                    self.assertEqual(image.size, (128, 128))
                    self.assertEqual(image.mode, "RGB")
                    self.assertIsNotNone(image.getbbox())

    def test_long_names_and_more_clients_fit_without_errors(self):
        self.data["modem"]["name"] = "Very long network name " * 20
        self.data["wifi"]["ssid"] = "Network " * 20
        self.data["clients"] *= 10
        for index in range(len(PAGES)):
            self.state.page = index
            self.display.render(self.state, self.data, 100)
            self.display.render(self.state, self.data, 107)

    def test_power_prompt_release_confirmation_pending_and_error(self):
        self.state.request_power_off(100)
        first = self.display.render(self.state, self.data, 100).tobytes()
        self.state.power_key_released = True
        second = self.display.render(self.state, self.data, 100).tobytes()
        self.assertNotEqual(first, second)
        self.state.power_off_error = "failed"
        self.assertNotEqual(second, self.display.render(self.state, self.data, 100).tobytes())
        self.state.power_off_pending = True
        self.assertNotEqual(second, self.display.render(self.state, self.data, 100).tobytes())

    @unittest.skipUnless(QR_AVAILABLE, "Install qrcode for QR tests")
    def test_qr_integer_modules_and_quiet_zone(self):
        image = qr_image(wifi_payload(self.data["wifi"]))
        self.assertLessEqual(image.width, 128)
        self.assertEqual(image.width, image.height)
        colors = set(image.getpixel((x, y)) for y in range(image.height) for x in range(image.width))
        self.assertEqual(colors, {(0, 0, 0), (255, 255, 255)})
        # At least four modules at >=2px on every edge, without anti-aliasing.
        for x in range(image.width):
            for y in range(8):
                self.assertEqual(image.getpixel((x, y)), (255, 255, 255))
                self.assertEqual(image.getpixel((x, image.height - 1 - y)), (255, 255, 255))

    @unittest.skipUnless(QR_AVAILABLE and DECODER_AVAILABLE, "Install qrcode and zxing-cpp for scan tests")
    def test_full_screen_qr_decodes_to_exact_escaped_credentials(self):
        import zxingcpp
        self.state.page = PAGES.index("wifi")
        for ssid, password, hidden in (("OpenStick", "demo-only-1234", False),
                                        ('Cafe: A;B,"C\\', 'p:a;s,s"\\', True),
                                        ("旅行ネット", "demo-only-1234", False),
                                        ("Longer network name", "x" * 63, False)):
            self.data["wifi"].update(ssid=ssid, password=password, hidden=hidden)
            image = self.display.render(self.state, self.data, 100)
            result = zxingcpp.read_barcode(image)
            self.assertIsNotNone(result)
            self.assertEqual(result.text, wifi_payload(self.data["wifi"]))

    @unittest.skipUnless(QR_AVAILABLE, "Install qrcode for QR tests")
    def test_oversized_qr_is_rejected_not_drawn_at_one_pixel(self):
        with self.assertRaises(ValueError):
            qr_image("x" * 1000)

    def test_missing_qr_dependency_shows_fallback(self):
        self.state.page = PAGES.index("wifi")
        with patch("lcd_display.qr_image", side_effect=ImportError("qrcode")):
            self.assertEqual(self.display.render(self.state, self.data, 100).size, (128, 128))

    @unittest.skipUnless(QR_AVAILABLE, "Install qrcode for QR tests")
    def test_expired_wifi_telemetry_hides_join_code(self):
        self.state.page = PAGES.index("wifi")
        current = self.display.render(self.state, self.data, 100).tobytes()
        self.assertNotEqual(current, self.display.render(self.state, self.data, 200).tobytes())

    def test_client_subpages_rotate_without_changing_page(self):
        self.state.page = PAGES.index("clients")
        first = self.display.render(self.state, self.data, 100).tobytes()
        self.data["sampled_at"] = self.data["details_at"] = 107
        second = self.display.render(self.state, self.data, 107).tobytes()
        self.assertNotEqual(first, second)
        self.assertEqual(self.state.page, PAGES.index("clients"))
