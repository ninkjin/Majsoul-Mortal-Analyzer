import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from majgg_fetcher import majgg_game_to_mjai_events, majgg_game_to_tenhou_summary
from majgg_settlement import _infer_total_han_from_score


class MajggFetcherTest(unittest.TestCase):
    def test_converts_names_tiles_calls_reach_and_hora(self):
        game = {
            "accounts": [
                {"nickname": "east"},
                {"seat": 1, "nickname": "south"},
                {"seat": 2, "nickname": "west"},
                {"seat": 3, "nickname": "north"},
            ],
            "result": {
                "players": [
                    {"partPoint1": 26000},
                    {"seat": 1, "partPoint1": 23000},
                    {"seat": 2, "partPoint1": 25000},
                    {"seat": 3, "partPoint1": 26000},
                ]
            },
            "Rounds": [
                {
                    "scores": [25000, 25000, 25000, 25000],
                    "doras": ["4m"],
                    "tiles0": ["1z", "0m", "7z"],
                    "tiles1": ["2m", "3m", "4m"],
                    "tiles2": ["5p", "5p", "5p"],
                    "tiles3": ["1s", "2s", "3s"],
                    "Tile": [
                        {"tile": "3m", "leftTileCount": 69, "TileType": "Draw"},
                        {"tile": "3m", "isLiqi": True, "TileType": "Discard"},
                        {"seat": 1, "tile": "5p", "leftTileCount": 68, "TileType": "Draw"},
                        {
                            "seat": 1,
                            "tile": "5p",
                            "TileType": "Discard",
                            "operations": [
                                {
                                    "seat": 2,
                                    "operationList": [{"type": 3, "combination": ["5p|5p"]}],
                                }
                            ],
                        },
                        {
                            "seat": 2,
                            "tiles": ["5p", "5p", "5p"],
                            "froms": [2, 2, 1],
                            "TileType": "Call",
                        },
                        {"seat": 2, "tile": "7z", "TileType": "Discard"},
                        {
                            "seat": 3,
                            "tile": "6m",
                            "leftTileCount": 67,
                            "operation": {"seat": 3, "operationList": [{"type": 8}]},
                            "TileType": "Draw",
                        },
                    ],
                }
            ],
        }

        events = majgg_game_to_mjai_events({"Game": game})

        self.assertEqual(
            events[0],
            {"type": "start_game", "names": ["east", "south", "west", "north"], "kyoku_first": 0, "aka_flag": True},
        )
        self.assertEqual(events[1]["bakaze"], "E")
        self.assertEqual(events[1]["dora_marker"], "4m")
        self.assertEqual(events[1]["tehais"][0], ["E", "5mr", "C"])
        self.assertIn({"type": "reach", "actor": 0}, events)
        self.assertIn({"type": "pon", "actor": 2, "target": 1, "pai": "5p", "consumed": ["5p", "5p"]}, events)
        self.assertIn({"type": "hora", "actor": 3, "target": 3, "deltas": [2000, -2000, 0, 1000], "ura_markers": []}, events)
        self.assertEqual(events[-1], {"type": "end_game"})

    def test_writes_json_lines_shape(self):
        events = majgg_game_to_mjai_events({"Game": {"accounts": [], "result": {"players": []}, "Rounds": []}})
        encoded = "\n".join(json.dumps(event, ensure_ascii=False) for event in events)

        self.assertEqual(encoded, '{"type": "start_game", "names": ["0", "1", "2", "3"], "kyoku_first": 0, "aka_flag": true}\n{"type": "end_game"}')

    def test_emits_dealer_fourteenth_starting_tile_as_initial_tsumo(self):
        game = {
            "accounts": [],
            "result": {"players": []},
            "Rounds": [
                {
                    "ju": 2,
                    "scores": [25000, 25000, 25000, 25000],
                    "doras": ["2m"],
                    "tiles0": ["1m"] * 13,
                    "tiles1": ["2m"] * 13,
                    "tiles2": ["3m"] * 13 + ["7m"],
                    "tiles3": ["4m"] * 13,
                    "Tile": [
                        {"seat": 2, "tile": "7m", "TileType": "Discard"},
                    ],
                }
            ],
        }

        events = majgg_game_to_mjai_events({"Game": game})

        self.assertEqual(events[1]["oya"], 2)
        self.assertEqual(len(events[1]["tehais"][2]), 13)
        self.assertEqual(events[2], {"type": "tsumo", "actor": 2, "pai": "7m"})
        self.assertEqual(events[3], {"type": "dahai", "actor": 2, "pai": "7m", "tsumogiri": False})

    def test_builds_tenhou_summary_for_majgg_hora_settlement(self):
        game = {
            "accounts": [
                {"seat": 0, "nickname": "east"},
                {"seat": 1, "nickname": "south"},
                {"seat": 2, "nickname": "west"},
                {"seat": 3, "nickname": "north"},
            ],
            "result": {"players": []},
            "Rounds": [
                {
                    "scores": [25000, 25000, 25000, 25000],
                    "doras": ["2m"],
                    "tiles0": ["1m"] * 13,
                    "tiles1": ["2m"] * 13,
                    "tiles2": ["3m"] * 13,
                    "tiles3": ["4m"] * 13,
                    "Tile": [
                        {
                            "seat": 3,
                            "tile": "9p",
                            "tingpais": [
                                {"tile": "2s", "haveyi": True, "count": 3, "fu": 30, "biaoDoraCount": 1}
                            ],
                            "TileType": "Discard",
                        },
                        {
                            "seat": 1,
                            "tile": "2s",
                            "operations": [{"seat": 3, "operationList": [{"type": 9}]}],
                            "TileType": "Discard",
                        },
                    ],
                },
                {
                    "scores": [25000, 21000, 25000, 29000],
                    "doras": ["3m"],
                    "tiles0": ["1m"] * 13,
                    "tiles1": ["2m"] * 13,
                    "tiles2": ["3m"] * 13,
                    "tiles3": ["4m"] * 13,
                    "Tile": [],
                },
            ],
        }

        summary = majgg_game_to_tenhou_summary({"Game": game})
        result = summary["log"][0][0]

        self.assertEqual(summary["name"], ["east", "south", "west", "north"])
        self.assertEqual(result[0], "和了")
        self.assertEqual(result[1], [0, -4000, 0, 4000])
        self.assertEqual(result[2][0:3], [3, 1, 3])
        self.assertEqual(result[2][3], "30符4番4000点")
        self.assertIn("宝牌(1番)", result[2])
        self.assertIn("手役合计(3番)", result[2])

    def test_calculates_specific_yaku_names_for_closed_ron(self):
        game = {
            "accounts": [],
            "result": {"players": []},
            "Rounds": [
                {
                    "scores": [25000, 25000, 25000, 25000],
                    "doras": ["1z"],
                    "tiles0": ["2m", "3m", "4m", "3m", "4m", "5m", "2p", "3p", "4p", "6p", "6p", "5s", "6s", "1z"],
                    "tiles1": ["1m"] * 13,
                    "tiles2": ["2m"] * 13,
                    "tiles3": ["3m"] * 13,
                    "Tile": [
                        {
                            "seat": 0,
                            "tile": "1z",
                            "isLiqi": True,
                            "TileType": "Discard",
                        },
                        {
                            "seat": 1,
                            "tile": "7s",
                            "operations": [{"seat": 0, "operationList": [{"type": 9}]}],
                            "TileType": "Discard",
                        },
                    ],
                },
                {
                    "scores": [28000, 22000, 25000, 25000],
                    "doras": ["2z"],
                    "tiles0": ["1m"] * 13,
                    "tiles1": ["2m"] * 13,
                    "tiles2": ["3m"] * 13,
                    "tiles3": ["4m"] * 13,
                    "Tile": [],
                },
            ],
        }

        summary = majgg_game_to_tenhou_summary({"Game": game})
        detail = summary["log"][0][0][2]

        self.assertIn("立直(1番)", detail)
        self.assertIn("平和(1番)", detail)
        self.assertIn("断幺九(1番)", detail)

    def test_infers_missing_ura_dora_han_from_tsumo_score(self):
        total_han = _infer_total_han_from_score(
            fu=30,
            deltas=[12000, -6000, -3000, -3000],
            actor=0,
            zimo=True,
            oya=1,
        )

        self.assertEqual(total_han, 6)

    def test_infers_total_han_from_dealer_tsumo_score(self):
        total_han = _infer_total_han_from_score(
            fu=30,
            deltas=[6000, -2000, -2000, -2000],
            actor=0,
            zimo=True,
            oya=0,
        )

        self.assertEqual(total_han, 3)


if __name__ == "__main__":
    unittest.main()
