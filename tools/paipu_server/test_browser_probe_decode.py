import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_probe import decode_lq_frame


class BrowserProbeDecodeTest(unittest.TestCase):
    def test_decodes_lq_wrapper_name_from_websocket_payload(self):
        payload = "AgEAChsubHEuUm91dGUucmVxdWVzdENvbm5lY3Rpb24SEhABGgdyb3V0ZS0yIM/sg/LdMw=="

        decoded = decode_lq_frame(payload)

        self.assertEqual(decoded["kind"], 2)
        self.assertEqual(decoded["request_id"], 1)
        self.assertEqual(decoded["name"], ".lq.Route.requestConnection")
        self.assertEqual(decoded["dataLength"], 18)
        self.assertTrue(base64.b64decode(decoded["dataSample"]))


if __name__ == "__main__":
    unittest.main()
