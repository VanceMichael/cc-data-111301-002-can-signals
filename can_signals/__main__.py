DESCRIPTION = '车辆台架 CAN 记录的字典绑定与信号采集'
SAMPLE = 'fixtures/frames.csv'
SAMPLE_CONFIG = 'fixtures/configurations.json'

import argparse
import json
from pathlib import Path
import tempfile

from .archive import archive_file
from .core import inspect_file
from .store import Store


def demo():
    """完整演练:分批交付、配置变更、未知帧处置、补交字典后的选择性补解码。"""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        store = Store(root / 'signals.db')
        try:
            result = {'原始资料': archive_file(Path(SAMPLE), root / 'raw'),
                      '资料预览': inspect_file(Path(SAMPLE))}
            # 第一段记录到达,登记两个字典版本与设备配置区间
            first = store.add_segment(SAMPLE)
            for name in ('rig-v1.dbc', 'rig-v2.dbc'):
                store.add_dictionary(Path('fixtures') / name)
            store.add_config_file(SAMPLE_CONFIG)
            result['首次交付'] = {'片段': first, '解码': store.decode_pending()}
            # 配置变更前后:同一段原始字节在不同适用区间下的解释
            result['配置变更前后'] = store.reception(channel='can0', t_from=0.0, t_until=1.8)
            result['车速信号检索'] = store.signal_series('speed')
            result['未知帧处置'] = store.unknowns()
            # 第二段记录追加交付;can1 的配置与字典此时仍未到齐
            second = store.add_segment('fixtures/frames-part2.csv')
            result['追加交付'] = {'片段': second, '解码': store.decode_pending()}
            can0_confirmed = store.counts()['channels'].get('can0', {})
            # 配置与字典补交到达:只补解码尚未确认的帧,不重复写入已确认结论
            store.add_config_file('fixtures/configurations-part2.json')
            store.add_dictionary('fixtures/can1.dbc')
            catchup = store.decode_pending()
            frame = store.frame_at('can0', 1.7)
            result['补交字典后'] = {
                '补解码': catchup,
                'can1接收记录': store.reception(channel='can1'),
                '同帧同时有效示例': None if frame is None else store.frame_detail(frame),
                'can0结论未重复写入': can0_confirmed == store.counts()['channels'].get('can0', {}),
            }
            return result
        finally:
            store.close()


def run_store_command(args):
    store = Store(Path(args.store))
    try:
        if args.command == 'ingest':
            return store.add_segment(args.path or SAMPLE)
        if args.command == 'configure':
            return store.add_config_file(args.path or SAMPLE_CONFIG)
        if args.command == 'dictionary':
            if not args.path:
                raise SystemExit('dictionary 命令需要 DBC 文件路径')
            return store.add_dictionary(args.path)
        if args.command == 'decode':
            return store.decode_pending()
        if args.command == 'report':
            return store.reception(channel=args.channel, t_from=args.t_from,
                                   t_until=args.t_until, limit=args.limit)
        if args.command == 'signal':
            if not args.path:
                raise SystemExit('signal 命令需要信号名')
            return store.signal_series(args.path, limit=args.limit)
        if args.command == 'frame':
            if not args.path:
                raise SystemExit('frame 命令需要帧序号')
            return store.frame_detail(int(args.path))
        if args.command == 'unknown':
            return store.unknowns(limit=args.limit)
        raise SystemExit(f'未知命令: {args.command}')
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('command', choices=['inspect', 'capture', 'demo', 'ingest',
                                            'configure', 'dictionary', 'decode',
                                            'report', 'signal', 'frame', 'unknown'])
    parser.add_argument('path', nargs='?', default=None,
                        help='帧清单/配置/字典路径,或 signal 名、frame 序号')
    parser.add_argument('--archive', default='.work/raw')
    parser.add_argument('--store', default='.work/signals.db')
    parser.add_argument('--channel', default=None)
    parser.add_argument('--from', dest='t_from', type=float, default=None)
    parser.add_argument('--until', dest='t_until', type=float, default=None)
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args()
    if args.command == 'inspect':
        result = inspect_file(Path(args.path or SAMPLE))
    elif args.command == 'capture':
        result = archive_file(Path(args.path or SAMPLE), Path(args.archive))
    elif args.command == 'demo':
        result = demo()
    else:
        result = run_store_command(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
