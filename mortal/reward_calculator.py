import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import torch
    import numpy as np
except ModuleNotFoundError:
    torch = None
    np = None


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


async def download_majsoul_tenhou_log(url, out_file, username=None, password=None):
    try:
        import aiohttp
        from ms.base import MSRPCChannel
        from ms.rpc import Lobby
        from tensoul import MajsoulPaipuDownloader
    except ImportError as exc:
        raise RuntimeError(
            'missing dependency "tensoul"; install it with: pip install tensoul'
        ) from exc

    record_uuid, _ = parse_majsoul_paipu_url(url)
    username = username or os.environ.get('MAJSOUL_USERNAME')
    password = password or os.environ.get('MAJSOUL_PASSWORD')
    if not username or not password:
        raise ValueError(
            'Mahjong Soul login is required. Pass --username/--password or set '
            'MAJSOUL_USERNAME and MAJSOUL_PASSWORD.'
        )

    downloader = MajsoulPaipuDownloader()

    async def fetch_json_with_retry(session, request_url, label, attempts=3):
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                async with session.get(request_url) as res:
                    res.raise_for_status()
                    return await res.json()
            except (asyncio.TimeoutError, OSError, aiohttp.ClientError) as exc:
                last_error = exc
                print(
                    f'{label} failed ({attempt}/{attempts}): '
                    f'{type(exc).__name__}: {exc}',
                    file=sys.stderr,
                    flush=True,
                )
                if attempt < attempts:
                    await asyncio.sleep(attempt)
        raise RuntimeError(
            f'连接雀魂服务器超时：{label}。请稍后重试，或检查网络/代理。'
        ) from last_error

    async def connect_with_current_config():
        print('connecting to Mahjong Soul...', file=sys.stderr, flush=True)
        timeout = aiohttp.ClientTimeout(total=20, connect=10, sock_connect=10, sock_read=10)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            version_res = await fetch_json_with_retry(
                session,
                f'{downloader.MS_HOST}/1/version.json',
                '读取雀魂版本信息',
            )
            downloader.version = version_res['version']
            downloader.version_to_force = downloader.version.replace('.w', '')
            print(f'version: {downloader.version}', file=sys.stderr, flush=True)

            config_url = f'{downloader.MS_HOST}/1/v{downloader.version}/config.json'
            config = await fetch_json_with_retry(session, config_url, '读取雀魂网关配置')
            gateway_group = config['ip'][0]
            if 'region_urls' in gateway_group:
                routes = [item['url'] for item in gateway_group['region_urls']]
            else:
                routes = [item['url'] for item in gateway_group['gateways']]

            last_error = None
            for route_url in routes:
                try:
                    print(f'route: {route_url}', file=sys.stderr, flush=True)
                    gateway_url = (
                        f'{route_url}/api/clientgate/routes'
                        f'?platform=Web&version={downloader.version}&lang=chs&randv=1'
                    )
                    routes_res = await fetch_json_with_retry(
                        session,
                        gateway_url,
                        f'读取雀魂网关线路 {route_url}',
                        attempts=2,
                    )
                    downloader.endpoint = _endpoint_from_clientgate_routes(routes_res)
                    print(f'endpoint: {downloader.endpoint}', file=sys.stderr, flush=True)
                    break
                except Exception as exc:
                    last_error = exc
                    print(
                        f'route failed: {type(exc).__name__}: {exc}',
                        file=sys.stderr,
                        flush=True,
                    )
            else:
                raise RuntimeError('无法检测雀魂网关线路，请稍后重试或切换 maj.gg 免登录获取。') from last_error

        downloader.channel = MSRPCChannel(downloader.endpoint)
        downloader.lobby = Lobby(downloader.channel)
        try:
            await asyncio.wait_for(downloader.channel.connect(downloader.MS_HOST), timeout=30)
        except asyncio.TimeoutError as exc:
            raise RuntimeError('连接雀魂网关超时，请稍后重试或检查网络/代理。') from exc

    downloader._connect = connect_with_current_config
    await asyncio.wait_for(downloader.start(), timeout=90)
    try:
        print('logging in...', file=sys.stderr, flush=True)
        await asyncio.wait_for(downloader.login(username, password), timeout=30)
        print(f'downloading record {record_uuid}...', file=sys.stderr, flush=True)
        log = await asyncio.wait_for(downloader.download(record_uuid), timeout=60)
    finally:
        await downloader.close()

    out_path = Path(out_file)
    print(f'writing tenhou log: {out_path}', file=sys.stderr, flush=True)
    out_path.write_text(
        json.dumps(log, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    return out_path


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
    subprocess.run(
        [
            str(reviewer_exe),
            '--no-review',
            '--in-file',
            str(in_file),
            '--player-id',
            str(player_id),
            '--mjai-out',
            str(out_path),
        ],
        check=True,
    )
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
