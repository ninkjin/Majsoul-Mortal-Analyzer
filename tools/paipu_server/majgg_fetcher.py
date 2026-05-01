import json
import urllib.parse
import urllib.request
from pathlib import Path

from majgg_settlement import calculate_round_results


Z_TILE_NAMES = {
    "1z": "E",
    "2z": "S",
    "3z": "W",
    "4z": "N",
    "5z": "P",
    "6z": "F",
    "7z": "C",
}


def fetch_majgg_game(record_uuid, out_path=None):
    url = f"https://maj.gg/api/game/{urllib.parse.quote(record_uuid)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mortal-paipu-server/1.0"})
    with urllib.request.urlopen(req, timeout=30) as res:
        raw = res.read().decode("utf-8")
    data = json.loads(raw)
    if out_path:
        Path(out_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def write_mjai_json_lines(events, out_path):
    path = Path(out_path)
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) for event in events) + "\n",
        encoding="utf-8",
    )
    return path


def majgg_game_to_mjai_file(data, out_path):
    return write_mjai_json_lines(majgg_game_to_mjai_events(data), out_path)


def majgg_game_to_tenhou_summary_file(data, out_path):
    path = Path(out_path)
    path.write_text(
        json.dumps(majgg_game_to_tenhou_summary(data), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def majgg_game_to_tenhou_summary(data):
    game = data.get("Game", data)
    rounds = game.get("Rounds", [])
    final_scores = _final_scores_by_seat(game.get("result", {}).get("players", []))
    log = []
    for index, round_data in enumerate(rounds):
        next_scores = rounds[index + 1].get("scores") if index + 1 < len(rounds) else final_scores
        log.append([_round_result_item(round_data, next_scores)])
    return {
        "ver": "majgg-summary",
        "name": _names_by_seat(game.get("accounts", [])),
        "log": log,
    }


def majgg_game_to_mjai_events(data):
    game = data.get("Game", data)
    events = [
        {
            "type": "start_game",
            "names": _names_by_seat(game.get("accounts", [])),
            "kyoku_first": 0,
            "aka_flag": True,
        }
    ]

    rounds = game.get("Rounds", [])
    final_scores = _final_scores_by_seat(game.get("result", {}).get("players", []))
    for index, round_data in enumerate(rounds):
        next_scores = rounds[index + 1].get("scores") if index + 1 < len(rounds) else final_scores
        events.extend(_round_to_mjai_events(round_data, index, next_scores))

    events.append({"type": "end_game"})
    return events


def _names_by_seat(accounts):
    names = [str(i) for i in range(4)]
    for account in accounts:
        seat = int(account.get("seat", 0))
        if 0 <= seat < 4:
            names[seat] = str(account.get("nickname") or seat)
    return names


def _final_scores_by_seat(players):
    if not players:
        return None
    scores = [0, 0, 0, 0]
    for player in players:
        seat = int(player.get("seat", 0))
        if 0 <= seat < 4:
            scores[seat] = int(player.get("partPoint1", player.get("totalPoint", 0)))
    return scores


def _round_to_mjai_events(round_data, round_index, next_scores):
    chang = int(round_data.get("chang", 0))
    ju = int(round_data.get("ju", round_index % 4))
    honba = int(round_data.get("ben", 0))
    doras = [_tile(tile) for tile in round_data.get("doras", [])]
    current_doras = list(doras)
    scores = [int(score) for score in round_data.get("scores", [25000, 25000, 25000, 25000])]
    oya = ju % 4
    initial_tiles = [[_tile(tile) for tile in round_data.get(f"tiles{seat}", [])] for seat in range(4)]
    events = [
        {
            "type": "start_kyoku",
            "bakaze": "ESWN"[chang] if 0 <= chang < 4 else "E",
            "dora_marker": current_doras[0] if current_doras else "?",
            "kyoku": ju + 1,
            "honba": honba,
            "kyotaku": int(round_data.get("liqibang", 0)),
            "oya": oya,
            "scores": scores,
            "tehais": [tiles[:13] for tiles in initial_tiles],
        }
    ]
    if len(initial_tiles[oya]) > 13:
        events.append({"type": "tsumo", "actor": oya, "pai": initial_tiles[oya][13]})
    reach_actors = set()

    for action in round_data.get("Tile", []):
        seat = _seat(action)
        tile_type = action.get("TileType")
        if tile_type == "Draw":
            events.append({"type": "tsumo", "actor": seat, "pai": _tile(action.get("tile"))})
        elif tile_type == "Discard":
            if action.get("isLiqi"):
                events.append({"type": "reach", "actor": seat})
                reach_actors.add(seat)
            events.extend(_new_dora_events(action, current_doras))
            events.append({
                "type": "dahai",
                "actor": seat,
                "pai": _tile(action.get("tile")),
                "tsumogiri": bool(action.get("moqie")),
            })
        elif tile_type == "Call":
            call = _call_event(action)
            if call:
                events.append(call)
        elif tile_type == "AddKan":
            tile = _tile(action.get("tile"))
            events.append({"type": "kakan", "actor": seat, "pai": tile, "consumed": [tile, tile, tile]})
        else:
            events.extend(_new_dora_events(action, current_doras))

    end_event = _end_event(round_data, scores, next_scores, reach_actors)
    if end_event:
        events.append(end_event)
    events.append({"type": "end_kyoku"})
    return events


def _seat(action):
    return int(action.get("seat", 0))


def _tile(tile):
    if not tile:
        return "?"
    tile = str(tile)
    if tile in Z_TILE_NAMES:
        return Z_TILE_NAMES[tile]
    if tile.startswith("0") and len(tile) == 2:
        return f"5{tile[1]}r"
    return tile


def _new_dora_events(action, current_doras):
    events = []
    doras = [_tile(tile) for tile in action.get("doras", [])]
    while len(doras) > len(current_doras):
        marker = doras[len(current_doras)]
        current_doras.append(marker)
        events.append({"type": "dora", "dora_marker": marker})
    return events


def _call_event(action):
    actor = _seat(action)
    tiles = [_tile(tile) for tile in action.get("tiles", [])]
    froms = [int(item) for item in action.get("froms", [])]
    if not tiles or len(tiles) != len(froms):
        return None
    target_indices = [idx for idx, source in enumerate(froms) if source != actor]
    if not target_indices:
        return None
    target_idx = target_indices[-1]
    target = froms[target_idx]
    pai = tiles[target_idx]
    consumed = [tile for idx, tile in enumerate(tiles) if idx != target_idx]
    if len(tiles) == 3 and _same_base_tile(tiles):
        return {"type": "pon", "actor": actor, "target": target, "pai": pai, "consumed": consumed}
    if len(tiles) == 3:
        return {"type": "chi", "actor": actor, "target": target, "pai": pai, "consumed": consumed}
    if len(tiles) == 4:
        return {"type": "daiminkan", "actor": actor, "target": target, "pai": pai, "consumed": consumed}
    return None


def _same_base_tile(tiles):
    return len({_base_tile(tile) for tile in tiles}) == 1


def _base_tile(tile):
    return tile.replace("r", "") if tile.endswith("r") else tile


def _end_event(round_data, start_scores, next_scores, reach_actors):
    if not next_scores:
        return None
    final_action = _last_meaningful_action(round_data.get("Tile", []))
    if not final_action:
        return {"type": "ryukyoku", "deltas": _score_deltas(start_scores, next_scores, reach_actors)}

    deltas = _score_deltas(start_scores, next_scores, reach_actors)
    if final_action.get("TileType") == "Draw" and _has_operation(final_action, 8):
        actor = _seat(final_action)
        return {"type": "hora", "actor": actor, "target": actor, "deltas": deltas, "ura_markers": []}
    if final_action.get("TileType") == "Discard":
        for operation in final_action.get("operations", []):
            if _has_operation(operation, 9):
                return {
                    "type": "hora",
                    "actor": int(operation.get("seat", 0)),
                    "target": _seat(final_action),
                    "deltas": deltas,
                    "ura_markers": [],
                }
    return {"type": "ryukyoku", "deltas": deltas}


def _round_result_item(round_data, next_scores):
    return calculate_round_results(round_data, next_scores)


def _last_meaningful_action(actions):
    for action in reversed(actions):
        if action.get("TileType") in ("Draw", "Discard"):
            return action
    return actions[-1] if actions else None


def _has_operation(container, operation_type):
    operation_list = container.get("operationList")
    if operation_list is None and isinstance(container.get("operation"), dict):
        operation_list = container["operation"].get("operationList")
    return any(item.get("type") == operation_type for item in operation_list or [])


def _score_deltas(start_scores, next_scores, reach_actors):
    deltas = [int(next_scores[i]) - int(start_scores[i]) for i in range(4)]
    for actor in reach_actors:
        deltas[actor] += 1000
    return deltas
