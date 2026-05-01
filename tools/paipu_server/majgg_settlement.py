from collections import Counter

from mahjong.constants import EAST, NORTH, SOUTH, WEST
from mahjong.hand_calculating.hand import HandCalculator
from mahjong.hand_calculating.hand_config import HandConfig, OptionalRules
from mahjong.meld import Meld
from mahjong.tile import TilesConverter


WIND_CONSTANTS = [EAST, SOUTH, WEST, NORTH]

YAKU_NAMES = {
    "Riichi": "立直",
    "Menzen Tsumo": "门前清自摸和",
    "Tanyao": "断幺九",
    "Pinfu": "平和",
    "Iipeiko": "一杯口",
    "Yakuhai (seat wind east)": "役牌 自风东",
    "Yakuhai (seat wind south)": "役牌 自风南",
    "Yakuhai (seat wind west)": "役牌 自风西",
    "Yakuhai (seat wind north)": "役牌 自风北",
    "Yakuhai (round wind east)": "役牌 场风东",
    "Yakuhai (round wind south)": "役牌 场风南",
    "Yakuhai (round wind west)": "役牌 场风西",
    "Yakuhai (round wind north)": "役牌 场风北",
    "Yakuhai (haku)": "役牌 白",
    "Yakuhai (hatsu)": "役牌 发",
    "Yakuhai (chun)": "役牌 中",
    "Ippatsu": "一发",
    "Rinshan Kaihou": "岭上开花",
    "Chankan": "抢杠",
    "Haitei Raoyue": "海底摸月",
    "Houtei Raoyui": "河底捞鱼",
    "Chitoitsu": "七对子",
    "Toitoi": "对对和",
    "Sanankou": "三暗刻",
    "Sanshoku Doujun": "三色同顺",
    "Sanshoku Doukou": "三色同刻",
    "Ittsu": "一气通贯",
    "Honroto": "混老头",
    "Shosangen": "小三元",
    "Honitsu": "混一色",
    "Junchan": "纯全带幺九",
    "Ryanpeiko": "二杯口",
    "Chinitsu": "清一色",
    "Dora": "宝牌",
    "Aka Dora": "红宝牌",
    "Ura Dora": "里宝牌",
}


class TileAllocator:
    def __init__(self):
        self.counts = Counter()

    def array(self, tiles):
        return [self.one(tile) for tile in tiles]

    def one(self, tile):
        base = _tile_34_index(tile)
        if _is_red(tile):
            tile_id = {4: 16, 13: 52, 22: 88}[base]
        else:
            tile_id = base * 4
            if base in (4, 13, 22):
                tile_id += 1
            tile_id += self.counts[base]
        self.counts[base] += 1
        return tile_id


def calculate_round_results(round_data, next_scores):
    start_scores = [int(score) for score in round_data.get("scores", [25000, 25000, 25000, 25000])]
    deltas = [int(next_scores[i]) - int(start_scores[i]) for i in range(4)] if next_scores else [0, 0, 0, 0]
    context = _build_context(round_data)
    result = _find_win_result(round_data, context, deltas)
    if result:
        return result
    return ["流局", deltas]


def _build_context(round_data):
    oya = int(round_data.get("ju", 0)) % 4
    hands = [[_tile(tile) for tile in round_data.get(f"tiles{seat}", [])] for seat in range(4)]
    melds = [[] for _ in range(4)]
    reach = set()
    ippatsu = set()
    latest_tingpais = {}
    rinshan = set()
    actions = round_data.get("Tile", [])

    return {
        "oya": oya,
        "honba": int(round_data.get("ben", 0) or 0),
        "round_wind": WIND_CONSTANTS[int(round_data.get("chang", 0)) % 4],
        "hands": hands,
        "melds": melds,
        "reach": reach,
        "ippatsu": ippatsu,
        "latest_tingpais": latest_tingpais,
        "rinshan": rinshan,
        "actions": actions,
        "current_action": None,
        "last_draw_left_count": None,
        "dora_indicators": [_tile(tile) for tile in round_data.get("doras", [])],
    }


def _find_win_result(round_data, context, deltas):
    final_index = _last_meaningful_action_index(context["actions"])
    for index, action in enumerate(context["actions"]):
        tile_type = action.get("TileType")
        seat = _seat(action)
        if tile_type == "Draw":
            context["current_action"] = action
            context["last_draw_left_count"] = action.get("leftTileCount")
            tile = _tile(action.get("tile"))
            context["hands"][seat].append(tile)
            if index == final_index and _has_operation(action, 8):
                detail = _winning_tingpai_detail(context["latest_tingpais"], seat, tile, zimo=True)
                return _hora_result(seat, seat, tile, deltas, detail, context, zimo=True)
        elif tile_type == "Discard":
            context["current_action"] = action
            tile = _tile(action.get("tile"))
            win_ops = _winning_operations(action)
            if index == final_index and win_ops:
                op = win_ops[0]
                actor = int(op.get("seat", 0))
                detail = _winning_tingpai_detail(context["latest_tingpais"], actor, tile, zimo=False)
                return _hora_result(actor, seat, tile, deltas, detail, context, zimo=False)
            if action.get("isLiqi"):
                context["reach"].add(seat)
                context["ippatsu"].add(seat)
            if action.get("tingpais"):
                context["latest_tingpais"][seat] = action["tingpais"]
            _remove_tile(context["hands"][seat], tile)
            if seat in context["ippatsu"] and not action.get("isLiqi"):
                context["ippatsu"].discard(seat)
        elif tile_type == "Call":
            _apply_call(context, action)
            context["ippatsu"].clear()
            context["rinshan"].clear()
        elif tile_type == "AddKan":
            context["current_action"] = action
            tile = _tile(action.get("tile"))
            win_ops = _winning_operations(action)
            if index == final_index and win_ops:
                op = win_ops[0]
                actor = int(op.get("seat", 0))
                detail = _winning_tingpai_detail(context["latest_tingpais"], actor, tile, zimo=False)
                return _hora_result(actor, seat, tile, deltas, detail, context, zimo=False, chankan=True)
            context["rinshan"].add(seat)
            context["ippatsu"].clear()
    return None


def _last_meaningful_action_index(actions):
    for index in range(len(actions) - 1, -1, -1):
        if actions[index].get("TileType") in ("Draw", "Discard", "AddKan"):
            return index
    return -1


def _hora_result(actor, target, win_tile, deltas, detail, context, zimo, chankan=False):
    hand_tiles = list(context["hands"][actor])
    if not zimo:
        hand_tiles.append(win_tile)
    calc = HandCalculator()
    allocator = TileAllocator()
    tiles_136 = allocator.array(hand_tiles)
    if zimo:
        matching = {_tile_34_index(win_tile) * 4 + i for i in range(4)}
        win_tile_136 = next((tile for tile in reversed(tiles_136) if tile in matching), tiles_136[-1])
    else:
        win_tile_136 = tiles_136[-1]
    melds = [_meld_to_mahjong(meld, allocator) for meld in context["melds"][actor]]
    dora_indicators = allocator.array(context["dora_indicators"])
    config = HandConfig(
        is_tsumo=zimo,
        is_riichi=actor in context["reach"],
        is_ippatsu=actor in context["ippatsu"],
        is_rinshan=actor in context["rinshan"] and zimo,
        is_chankan=chankan,
        is_haitei=zimo and _is_last_draw(context),
        is_houtei=(not zimo) and _is_last_discard(context),
        player_wind=WIND_CONSTANTS[(actor - context["oya"]) % 4],
        round_wind=context["round_wind"],
        options=OptionalRules(has_aka_dora=True),
    )
    response = calc.estimate_hand_value(
        tiles_136,
        win_tile_136,
        melds=melds,
        dora_indicators=dora_indicators,
        config=config,
    )
    yaku_lines = _response_yaku_lines(response)
    han = int(detail.get("countZimo" if zimo and detail.get("countZimo") is not None else "count") or response.han or 0)
    dora = int(detail.get("biaoDoraCount") or 0)
    fu = int(detail.get("fuZimo" if zimo and detail.get("fuZimo") is not None else "fu") or response.fu or 0)
    score_han = _infer_total_han_from_score(fu, deltas, actor, zimo, context["oya"], target, context["honba"])
    total_han = max(int(response.han or 0), han + dora, score_han)
    if response.error:
        yaku_lines = _fallback_yaku_lines(detail, total_han)
    else:
        missing = max(0, total_han - int(response.han or 0))
        if missing and actor in context["reach"]:
            yaku_lines.append(f"里宝牌({missing}番)")
        elif missing:
            yaku_lines.append(f"其他役/宝牌({missing}番)")
    return ["和了", deltas, [actor, target, actor, _point_text(fu, total_han, max(0, deltas[actor])), *yaku_lines]]


def _apply_call(context, action):
    actor = _seat(action)
    tiles = [_tile(tile) for tile in action.get("tiles", [])]
    froms = [int(item) for item in action.get("froms", [])]
    if not tiles or len(tiles) != len(froms):
        return
    consumed = [tile for tile, source in zip(tiles, froms) if source == actor]
    called = [tile for tile, source in zip(tiles, froms) if source != actor]
    for tile in consumed:
        _remove_tile(context["hands"][actor], tile)
    meld_type = Meld.PON if len({_base_tile(tile) for tile in tiles}) == 1 else Meld.CHI
    context["melds"][actor].append({"type": meld_type, "tiles": tiles, "called": called[0] if called else tiles[-1]})


def _meld_to_mahjong(meld, allocator):
    tiles = allocator.array(meld["tiles"])
    called = allocator.one(meld.get("called") or meld["tiles"][-1])
    return Meld(meld_type=meld["type"], tiles=tiles, opened=True, called_tile=called)


def _response_yaku_lines(response):
    lines = []
    for yaku in response.yaku or []:
        han = yaku.han_open if getattr(yaku, "is_open", False) else yaku.han_closed
        if not han:
            han = yaku.han_closed or yaku.han_open
        lines.append(f"{YAKU_NAMES.get(yaku.name, yaku.name)}({han}番)")
    return lines


def _fallback_yaku_lines(detail, total_han):
    dora = int(detail.get("biaoDoraCount") or 0)
    lines = []
    if dora:
        lines.append(f"宝牌({dora}番)")
    hand_han = max(0, int(total_han or 0) - dora)
    if hand_han:
        lines.append(f"手役合计({hand_han}番)")
    return lines or ["役种详情不可用"]


def _infer_total_han_from_score(fu, deltas, actor, zimo, oya, target=None, honba=0):
    try:
        fu = int(fu)
        actor = int(actor)
        oya = int(oya)
        honba = int(honba or 0)
        deltas = [int(delta) for delta in deltas]
    except (TypeError, ValueError):
        return 0
    if fu <= 0 or len(deltas) < 4:
        return 0
    for han in range(1, 14):
        if _score_matches(fu, han, deltas, actor, zimo, oya, target, honba):
            return han
    return 0


def _score_matches(fu, han, deltas, actor, zimo, oya, target, honba):
    base = _base_points(fu, han)
    if zimo:
        expected_losses = []
        for seat in range(4):
            if seat == actor:
                continue
            if actor == oya or seat == oya:
                payment = _ceil100(base * 2) + 100 * honba
            else:
                payment = _ceil100(base) + 100 * honba
            expected_losses.append((seat, payment))
        return all(deltas[seat] == -payment for seat, payment in expected_losses)

    if target is None:
        return False
    try:
        target = int(target)
    except (TypeError, ValueError):
        return False
    multiplier = 6 if actor == oya else 4
    payment = _ceil100(base * multiplier) + 300 * honba
    return deltas[target] == -payment


def _base_points(fu, han):
    if han >= 13:
        return 8000
    if han >= 11:
        return 6000
    if han >= 8:
        return 4000
    if han >= 6:
        return 3000
    if han >= 5:
        return 2000
    return min(2000, fu * (2 ** (han + 2)))


def _ceil100(value):
    return ((int(value) + 99) // 100) * 100


def _winning_operations(action):
    return [op for op in action.get("operations", []) if _has_operation(op, 9)]


def _winning_tingpai_detail(latest_tingpais, actor, win_tile, zimo=False):
    for detail in latest_tingpais.get(actor, []):
        if _tile(detail.get("tile")) == win_tile:
            return detail
    return {}


def _has_operation(container, operation_type):
    operation_list = container.get("operationList")
    if operation_list is None and isinstance(container.get("operation"), dict):
        operation_list = container["operation"].get("operationList")
    return any(item.get("type") == operation_type for item in operation_list or [])


def _remove_tile(hand, tile):
    normalized = _base_tile(tile)
    for idx, item in enumerate(hand):
        if _base_tile(item) == normalized:
            return hand.pop(idx)
    return None


def _seat(action):
    return int(action.get("seat", 0))


def _tile(tile):
    if not tile:
        return "?"
    tile = str(tile)
    return {"1z": "E", "2z": "S", "3z": "W", "4z": "N", "5z": "P", "6z": "F", "7z": "C"}.get(tile, tile)


def _tile_34_index(tile):
    tile = _base_tile(tile)
    if tile in ("E", "S", "W", "N", "P", "F", "C"):
        return 27 + ["E", "S", "W", "N", "P", "F", "C"].index(tile)
    number = int(tile[0])
    suit = tile[1]
    offset = {"m": 0, "p": 9, "s": 18}[suit]
    return offset + number - 1


def _base_tile(tile):
    tile = str(tile)
    if tile.startswith("0"):
        return f"5{tile[1]}"
    if tile.endswith("r"):
        return tile[:-1]
    return tile


def _is_red(tile):
    tile = str(tile)
    return tile.startswith("0") or tile.endswith("r")


def _point_text(fu, han, gain):
    if fu and han:
        return f"{fu}符{han}番{gain}点"
    if han:
        return f"{han}番{gain}点"
    return f"{gain}点"


def _is_last_draw(context):
    action = context.get("current_action") or {}
    try:
        return int(action.get("leftTileCount")) == 0
    except (TypeError, ValueError):
        return False


def _is_last_discard(context):
    try:
        return int(context.get("last_draw_left_count")) == 0
    except (TypeError, ValueError):
        return False
