import argparse
import asyncio
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

try:
    import torch
    import numpy as np
except ModuleNotFoundError:
    torch = None
    np = None


REVIEWER_TIMEOUT_SECONDS = 120


def parse_majsoul_paipu_url(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    paipu = query.get('paipu', [None])[0]
    if not paipu:
        raise ValueError('missing "paipu" query parameter')

    record_uuid, _, account_part = paipu.partition('_')
    if not record_uuid:
        raise ValueError('empty Mahjong Soul record uuid')

    account_id = account_part[1:] if account_part.startswith('a') else account_part
    return record_uuid, account_id or None


def _endpoint_from_clientgate_routes(payload):
    routes = payload.get('data', {}).get('routes', [])
    if not routes:
        raise RuntimeError(f'Cannot detect endpoint. Response: {payload}')

    ordered = sorted(
        routes,
        key=lambda item: (
            item.get('state') != 'idle',
            item.get('level', 999),
            item.get('order', 999),
        ),
    )
    route = ordered[0]
    domain = route.get('domain')
    if not domain:
        raise RuntimeError(f'Cannot detect endpoint. Response: {payload}')

    scheme = 'wss' if route.get('ssl', True) else 'ws'
    return f'{scheme}://{domain}/gateway'


async def download_majsoul_tenhou_log(
    url, out_file, username=None, password=None, status_callback=None,
):
    record_uuid, _ = parse_majsoul_paipu_url(url)
    username = username or os.environ.get('MAJSOUL_USERNAME')
    password = password or os.environ.get('MAJSOUL_PASSWORD')
    if not username or not password:
        raise ValueError(
            'Mahjong Soul login is required. Pass --username/--password or set '
            'MAJSOUL_USERNAME and MAJSOUL_PASSWORD.'
        )

    module_path = Path(__file__).with_name('tensoul_fetcher.py')
    spec = importlib.util.spec_from_file_location('mortal_tensoul_fetcher', module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError('无法加载本地牌谱获取组件。')
    fetcher = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(fetcher)
    except ImportError as exc:
        raise RuntimeError('本地获取依赖不完整，请运行“检查环境.cmd”修复。') from exc
    return await fetcher.download(
        record_uuid, out_file, username, password, status_callback=status_callback,
    )


def tenhou_log_to_mjai_log(in_file, out_file, player_id=0, reviewer_exe=None):
    root = Path(__file__).resolve().parents[1]
    reviewer_exe = Path(reviewer_exe) if reviewer_exe else (
        root / '.tools' / 'mjai-reviewer' / 'target' / 'release' / 'mjai-reviewer.exe'
    )
    if not reviewer_exe.exists():
        raise FileNotFoundError(
            f'mjai-reviewer was not found at {reviewer_exe}. '
            'Build it first or pass --reviewer-exe.'
        )

    out_path = Path(out_file)
    print(f'converting to mjai log: {out_path}', file=sys.stderr, flush=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = out_path.with_name(f'.{out_path.name}.{uuid4().hex}.tmp')
    try:
        try:
            subprocess.run(
                [
                    str(reviewer_exe),
                    '--no-review',
                    '--in-file',
                    str(in_file),
                    '--player-id',
                    str(player_id),
                    '--mjai-out',
                    str(temporary),
                ],
                check=True,
                timeout=REVIEWER_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('牌谱转换超时，请检查输入牌谱是否完整。') from exc
        temporary.replace(out_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return out_path


class RewardCalculator:
    def __init__(self, grp=None, pts=None, uniform_init=False):
        if torch is None or np is None:
            raise RuntimeError('RewardCalculator requires torch and numpy')

        self.device = torch.device('cpu')
        self.grp = grp.to(self.device).eval()
        self.uniform_init = uniform_init

        pts = pts or [3, 1, -1, -3]
        self.pts = torch.tensor(pts, dtype=torch.float64, device=self.device)

    def calc_grp(self, grp_feature):
        seq = list(map(
            lambda idx: torch.as_tensor(grp_feature[:idx+1], device=self.device),
            range(len(grp_feature)),
        ))

        with torch.inference_mode():
            logits = self.grp(seq)
        matrix = self.grp.calc_matrix(logits)
        return matrix

    def calc_rank_prob(self, player_id, grp_feature, rank_by_player):
        matrix = self.calc_grp(grp_feature)

        final_ranking = torch.zeros((1, 4), device=self.device)
        final_ranking[0, rank_by_player[player_id]] = 1.
        rank_prob = torch.cat((matrix[:, player_id], final_ranking))
        if self.uniform_init:
            rank_prob[0, :] = 1 / 4
        return rank_prob

    def calc_delta_pt(self, player_id, grp_feature, rank_by_player):
        rank_prob = self.calc_rank_prob(player_id, grp_feature, rank_by_player)
        exp_pts = rank_prob @ self.pts
        reward = exp_pts[1:] - exp_pts[:-1]
        return reward.cpu().numpy()

    def calc_delta_points(self, player_id, grp_feature, final_scores):
        seq = np.concatenate((grp_feature[:, 3 + player_id] * 1e4, [final_scores[player_id]]))
        delta_points = seq[1:] - seq[:-1]
        return delta_points


def main():
    parser = argparse.ArgumentParser(
        description='Download a Mahjong Soul paipu URL as tenhou.net/6 JSON.'
    )
    parser.add_argument('url', help='Mahjong Soul paipu URL')
    parser.add_argument(
        '-o',
        '--out',
        default='log.json',
        help='final mjai JSON-lines output file',
    )
    parser.add_argument(
        '--tenhou-out',
        default='majsoul-tenhou.json',
        help='intermediate tenhou.net/6 JSON output file',
    )
    parser.add_argument(
        '-a',
        '--player-id',
        default=0,
        type=int,
        choices=range(4),
        help='target player id, 0-3',
    )
    parser.add_argument('--username')
    parser.add_argument('--password')
    parser.add_argument('--reviewer-exe')
    args = parser.parse_args()

    tenhou_path = asyncio.run(download_majsoul_tenhou_log(
        args.url,
        args.tenhou_out,
        username=args.username,
        password=args.password,
    ))
    out_path = tenhou_log_to_mjai_log(
        tenhou_path,
        args.out,
        player_id=args.player_id,
        reviewer_exe=args.reviewer_exe,
    )
    print(f'wrote {out_path}')


if __name__ == '__main__':
    main()
