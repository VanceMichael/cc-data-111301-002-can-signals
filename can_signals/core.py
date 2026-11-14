import csv
from pathlib import Path

KINDS = {'data', 'remote', 'error'}


def read_frames(path):
    with Path(path).open(encoding='utf-8', newline='') as stream:
        reader = csv.DictReader(stream)
        required = {'time', 'channel', 'frame_id', 'extended', 'kind', 'data'}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError('帧清单缺少必要列')
        for row in reader:
            payload = bytes.fromhex(row['data'])
            identifier = int(row['frame_id'])
            extended = row['extended'] == '1'
            kind = row['kind']
            if kind not in KINDS:
                raise ValueError('帧类型不合法')
            if identifier < 0 or identifier > (0x1fffffff if extended else 0x7ff) or len(payload) > 64:
                raise ValueError('帧编号或载荷长度不合法')
            yield {'time': float(row['time']), 'channel': row['channel'], 'frame_id': identifier,
                   'extended': extended, 'kind': kind, 'payload': payload}


def inspect_file(path):
    channels = {}
    count = 0
    for frame in read_frames(path):
        count += 1
        channels[frame['channel']] = channels.get(frame['channel'], 0) + 1
    return {'frames': count, 'channels': channels}
