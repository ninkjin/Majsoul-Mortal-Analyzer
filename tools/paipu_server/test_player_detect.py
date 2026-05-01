import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import account_id_from_paipu, resolve_player_id


class PlayerDetectTest(unittest.TestCase):
    def test_decodes_account_id_from_paipu_suffix(self):
        paipu = "260213-b409422a-54ad-4699-a9fe-23f9ed4c2390_a48969976"

        self.assertEqual(account_id_from_paipu(paipu), 18920167)

    def test_auto_detects_majgg_seat_from_paipu_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "game.source.json"
            source.write_text(json.dumps({
                "Game": {
                    "accounts": [
                        {"seat": 1, "accountId": 14150605, "nickname": "other"},
                        {"accountId": 18920167, "nickname": "lastkasd"},
                    ]
                }
            }), encoding="utf-8")

            self.assertEqual(
                resolve_player_id(
                    source,
                    "auto",
                    "",
                    "260213-b409422a-54ad-4699-a9fe-23f9ed4c2390_a48969976",
                ),
                0,
            )

    def test_detects_majgg_seat_by_nickname(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "game.source.json"
            source.write_text(json.dumps({
                "Game": {
                    "accounts": [
                        {"seat": 1, "nickname": "other"},
                        {"seat": 0, "nickname": "lastkasd"},
                    ]
                }
            }), encoding="utf-8")

            self.assertEqual(resolve_player_id(source, "auto", "lastkasd"), 0)

    def test_detects_tenhou_name_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "game.tenhou.json"
            source.write_text(json.dumps({"name": ["a", "b", "lastkasd", "d"]}), encoding="utf-8")

            self.assertEqual(resolve_player_id(source, "auto", "lastkasd"), 2)

    def test_manual_player_id_still_works(self):
        self.assertEqual(resolve_player_id(Path("missing.json"), "3", ""), 3)


if __name__ == "__main__":
    unittest.main()
