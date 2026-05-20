import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch

import prelude
from common import filtered_trimmed_lines
from config import config
from engine import MortalEngine
from libriichi.mjai import Bot
from model import Brain, DQN


def main():
    player_id = int(sys.argv[1])
    review_mode = os.environ.get('MORTAL_REVIEW_MODE', '0') == '1'
    if review_mode:
        raise RuntimeError('mapping review mode output is not supported')

    device = torch.device('cpu')
    state_file = os.environ.get('MORTAL_MODEL_PATH') or config['control']['state_file']
    state = torch.load(
        state_file,
        weights_only=True,
        map_location=torch.device('cpu'),
    )
    cfg = state['config']
    version = cfg['control'].get('version', 1)
    mortal = Brain(
        version=version,
        num_blocks=cfg['resnet']['num_blocks'],
        conv_channels=cfg['resnet']['conv_channels'],
    ).eval()
    dqn = DQN(version=version).eval()
    mortal.load_state_dict(state['mortal'])
    dqn.load_state_dict(state['current_dqn'])

    engine = MortalEngine(
        mortal,
        dqn,
        version=version,
        is_oracle=False,
        device=device,
        enable_amp=False,
        enable_quick_eval=True,
        enable_rule_based_agari_guard=True,
        name='mortal',
    )
    bot = Bot(engine, player_id)

    for event_index, line in enumerate(filtered_trimmed_lines(sys.stdin)):
        try:
            reaction = bot.react(line)
            if reaction:
                print(json.dumps({
                    'event_index': event_index,
                    'event': json.loads(line),
                    'reaction': json.loads(reaction),
                }, ensure_ascii=False), flush=True)
        except RuntimeError as e:
            # 状态同步失败时跳过该事件，继续处理后续事件
            print(json.dumps({
                'event_index': event_index,
                'event': json.loads(line),
                'reaction': {'type': 'none', 'error': str(e)},
            }, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
