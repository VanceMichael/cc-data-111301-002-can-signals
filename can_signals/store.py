"""信号接收链的存储与补解码。

设计约束:
- 原始帧(含远程帧、错误帧、未知帧)永久可查,解释结论与原始测量分开存放;
- 一段记录分多次追加交付,按内容摘要去重,重复交付不会重复入账;
- 配置记录给出字典的适用区间 [from, until),不允许拿文件末尾的配置解释整段历史;
  覆盖同一时刻的字典不一致时按证据不足处理,保留未解码帧;
- 已确认(decoded)的结论永不重写;补交配置或字典后,decode_pending 只处理
  尚未确认的帧,实现有选择的补解码;
- 入账、解码、查询全部分批/分页进行,内存占用不随历史帧总量增长。
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .core import read_frames
from .dictionary import decode_payload, load_dictionary, parse_dictionary

BATCH = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    received_at TEXT NOT NULL,
    frames INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY,
    segment INTEGER NOT NULL REFERENCES segments(id),
    seq INTEGER NOT NULL,
    time REAL NOT NULL,
    channel TEXT NOT NULL,
    frame_id INTEGER NOT NULL,
    extended INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload BLOB NOT NULL,
    UNIQUE (segment, seq)
);
CREATE INDEX IF NOT EXISTS frames_lookup ON frames (channel, time);
CREATE TABLE IF NOT EXISTS configs (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    from_time REAL NOT NULL,
    until_time REAL,
    dictionary TEXT,
    source TEXT NOT NULL,
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dictionary_versions (
    name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    content TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (name, sha256)
);
CREATE TABLE IF NOT EXISTS dictionaries (
    name TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes (
    frame INTEGER PRIMARY KEY REFERENCES frames(id),
    status TEXT NOT NULL,
    dictionary TEXT,
    dictionary_sha256 TEXT,
    message TEXT,
    detail TEXT,
    decoded_at TEXT
);
CREATE INDEX IF NOT EXISTS outcomes_status ON outcomes (status);
CREATE TABLE IF NOT EXISTS signal_values (
    frame INTEGER NOT NULL REFERENCES frames(id),
    signal TEXT NOT NULL,
    raw INTEGER NOT NULL,
    physical REAL,
    text TEXT,
    validity TEXT NOT NULL,
    unit TEXT,
    PRIMARY KEY (frame, signal)
);
"""

# 入账时即可确定的处置;data 帧一律先置 pending,等待 decode_pending 确认
INITIAL_OUTCOMES = {
    'data': ('pending', '等待配置或字典'),
    'remote': ('remote', '远程帧无载荷,仅保留原始记录'),
    'error': ('error', '错误帧,仅保留原始记录'),
}

# decode_pending 会重新评估的状态;decoded/remote/error/conflict 是终态,绝不重写
RETRYABLE = ('pending', 'unknown', 'undecodable')


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _round(value):
    return None if value is None else round(value, 6)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path))
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    def close(self):
        self.connection.close()

    # ------------------------------------------------------------------ 入账

    def add_segment(self, source):
        """追加一段交付记录;按内容摘要去重,重复交付返回已有片段。"""
        source = Path(source)
        digest = hashlib.sha256()
        with source.open('rb') as stream:
            while block := stream.read(65536):
                digest.update(block)
        sha256 = digest.hexdigest()
        row = self.connection.execute(
            'SELECT id, frames FROM segments WHERE sha256 = ?', (sha256,)).fetchone()
        if row is not None:
            return {'segment': row['id'], 'sha256': sha256, 'source': source.name,
                    'frames': row['frames'], 'duplicate': True}
        cursor = self.connection.cursor()
        cursor.execute(
            'INSERT INTO segments (sha256, source, received_at, frames) VALUES (?, ?, ?, 0)',
            (sha256, source.name, _now()))
        segment = cursor.lastrowid
        count = 0
        batch = []
        for frame in read_frames(source):
            batch.append(frame)
            if len(batch) >= BATCH:
                count += self._flush_frames(segment, count, batch)
                batch.clear()
        if batch:
            count += self._flush_frames(segment, count, batch)
        cursor.execute('UPDATE segments SET frames = ? WHERE id = ?', (count, segment))
        self.connection.commit()
        return {'segment': segment, 'sha256': sha256, 'source': source.name,
                'frames': count, 'duplicate': False}

    def _flush_frames(self, segment, first_seq, batch):
        cursor = self.connection.cursor()
        cursor.executemany(
            'INSERT INTO frames (segment, seq, time, channel, frame_id, extended, kind, payload)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            [(segment, first_seq + offset, frame['time'], frame['channel'],
              frame['frame_id'], 1 if frame['extended'] else 0, frame['kind'],
              frame['payload']) for offset, frame in enumerate(batch)])
        rows = self.connection.execute(
            'SELECT id, kind FROM frames WHERE segment = ? AND seq >= ? AND seq < ? ORDER BY seq',
            (segment, first_seq, first_seq + len(batch))).fetchall()
        cursor.executemany(
            'INSERT INTO outcomes (frame, status, detail) VALUES (?, ?, ?)',
            [(row['id'], *INITIAL_OUTCOMES[row['kind']]) for row in rows])
        return len(batch)

    # ------------------------------------------------------------ 配置与字典

    def add_config_file(self, source):
        """登记设备配置记录;相同区间重复登记会被忽略。"""
        source = Path(source)
        records = json.loads(source.read_text(encoding='utf-8'))
        if not isinstance(records, list):
            raise ValueError('配置记录必须是数组')
        added = skipped = 0
        for record in records:
            channel = str(record['channel'])
            start = float(record['from'])
            until = record.get('until')
            until = None if until is None else float(until)
            dictionary = record.get('dictionary')
            if until is not None and until <= start:
                raise ValueError('配置区间终点必须大于起点')
            row = self.connection.execute(
                'SELECT id FROM configs WHERE channel = ? AND from_time = ?'
                ' AND until_time IS ? AND dictionary IS ?',
                (channel, start, until, dictionary)).fetchone()
            if row is not None:
                skipped += 1
                continue
            self.connection.execute(
                'INSERT INTO configs (channel, from_time, until_time, dictionary, source,'
                ' registered_at) VALUES (?, ?, ?, ?, ?, ?)',
                (channel, start, until, dictionary, source.name, _now()))
            added += 1
        self.connection.commit()
        return {'source': source.name, 'added': added, 'skipped': skipped}

    def add_dictionary(self, source):
        """登记字典版本并置为该名字的当前版本;历史版本保留供追溯。"""
        info = load_dictionary(source)
        self.connection.execute(
            'INSERT OR IGNORE INTO dictionary_versions (name, sha256, content, registered_at)'
            ' VALUES (?, ?, ?, ?)',
            (info['name'], info['sha256'], info['content'], _now()))
        self.connection.execute(
            'INSERT INTO dictionaries (name, sha256) VALUES (?, ?)'
            ' ON CONFLICT(name) DO UPDATE SET sha256 = excluded.sha256',
            (info['name'], info['sha256']))
        self.connection.commit()
        return {'name': info['name'], 'sha256': info['sha256'],
                'messages': len(info['messages'])}

    # ---------------------------------------------------------------- 补解码

    def decode_pending(self):
        """只处理尚未确认的帧;已确认结论与远程/错误帧的处置保持不变。

        unknown/undecodable 在适用字典版本更新后按新版本重试;配置无覆盖、
        字典未提供或未登记的帧保持 pending,等待后续交付。
        """
        stats = {'decoded': 0, 'unknown': 0, 'undecodable': 0, 'conflict': 0}
        parsed = {}
        last = -1
        while True:
            page = self.connection.execute(
                'SELECT f.id, f.time, f.channel, f.frame_id, f.extended, f.payload,'
                ' o.status, o.dictionary_sha256'
                ' FROM outcomes o JOIN frames f ON f.id = o.frame'
                ' WHERE o.status IN (?, ?, ?) AND f.id > ? ORDER BY f.id LIMIT ?',
                (*RETRYABLE, last, BATCH)).fetchall()
            if not page:
                break
            with self.connection:
                for row in page:
                    outcome, values = self._decide(row, parsed)
                    if outcome is None:
                        continue
                    self.connection.execute(
                        'UPDATE outcomes SET status = ?, dictionary = ?,'
                        ' dictionary_sha256 = ?, message = ?, detail = ?, decoded_at = ?'
                        ' WHERE frame = ?',
                        (outcome['status'], outcome['dictionary'],
                         outcome['dictionary_sha256'], outcome['message'],
                         outcome['detail'], _now(), row['id']))
                    if values:
                        self.connection.executemany(
                            'INSERT INTO signal_values'
                            ' (frame, signal, raw, physical, text, validity, unit)'
                            ' VALUES (?, ?, ?, ?, ?, ?, ?)',
                            [(row['id'], item['signal'], item['raw'], item['physical'],
                              item['text'], item['validity'], item['unit'])
                             for item in values])
                    stats[outcome['status']] += 1
            last = page[-1]['id']
        stats['still_pending'] = self.connection.execute(
            "SELECT COUNT(*) AS c FROM outcomes WHERE status = 'pending'").fetchone()['c']
        return stats

    def _decide(self, row, parsed):
        covering = self.connection.execute(
            'SELECT dictionary FROM configs WHERE channel = ? AND from_time <= ?'
            ' AND (until_time IS NULL OR ? < until_time)',
            (row['channel'], row['time'], row['time'])).fetchall()
        if not covering:
            return None, None  # 无配置覆盖该时刻:证据不足,保持待解码
        names = {item['dictionary'] for item in covering
                 if item['dictionary'] is not None}
        if not names:
            return None, None  # 配置声明字典尚未提供:保持待解码
        if len(names) > 1:
            return {'status': 'conflict', 'dictionary': None, 'dictionary_sha256': None,
                    'message': None,
                    'detail': '配置区间重叠且字典不一致: ' + ', '.join(sorted(names))}, None
        name = names.pop()
        current = self.connection.execute(
            'SELECT sha256 FROM dictionaries WHERE name = ?', (name,)).fetchone()
        if current is None:
            return None, None  # 字典未登记:保持待解码
        sha256 = current['sha256']
        if row['status'] != 'pending' and row['dictionary_sha256'] == sha256:
            return None, None  # 同一字典版本已有结论,不重复判定
        if sha256 not in parsed:
            content = self.connection.execute(
                'SELECT content FROM dictionary_versions WHERE name = ? AND sha256 = ?',
                (name, sha256)).fetchone()
            parsed[sha256] = parse_dictionary(content['content'].encode('utf-8'))
        message = parsed[sha256].get((bool(row['extended']), row['frame_id']))
        base = {'dictionary': name, 'dictionary_sha256': sha256}
        if message is None:
            return {**base, 'status': 'unknown', 'message': None,
                    'detail': '适用字典中无此报文(通道/扩展位/帧号无匹配)'}, None
        try:
            values = decode_payload(message, bytes(row['payload']))
        except Exception as error:  # 载荷长度不足等,保留原始帧待查
            return {**base, 'status': 'undecodable', 'message': message.name,
                    'detail': f'载荷无法按报文 {message.name} 解码: {error}'}, None
        return {**base, 'status': 'decoded', 'message': message.name,
                'detail': ''}, values

    # ------------------------------------------------------------------ 查询

    def reception(self, channel=None, t_from=None, t_until=None, limit=100):
        """接收记录:按时间列出帧、处置状态、解释依据与同帧同时有效的信号值。"""
        sql = ('SELECT f.id, f.time, f.channel, f.frame_id, f.extended, f.kind, f.payload,'
               ' o.status, o.dictionary, o.dictionary_sha256, o.message, o.detail'
               ' FROM frames f JOIN outcomes o ON o.frame = f.id')
        clauses, params = [], []
        if channel is not None:
            clauses.append('f.channel = ?')
            params.append(channel)
        if t_from is not None:
            clauses.append('f.time >= ?')
            params.append(t_from)
        if t_until is not None:
            clauses.append('f.time < ?')
            params.append(t_until)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY f.time, f.channel, f.id LIMIT ?'
        params.append(limit + 1)
        rows = self.connection.execute(sql, params).fetchall()
        return {'frames': [self._frame_record(row) for row in rows[:limit]],
                'truncated': len(rows) > limit}

    def signal_series(self, signal, limit=100):
        """按信号检索:每个取值都带上当时的解释依据(字典名字与版本)。"""
        rows = self.connection.execute(
            'SELECT f.time, f.channel, v.raw, v.physical, v.text, v.validity, v.unit,'
            ' o.dictionary, o.dictionary_sha256'
            ' FROM signal_values v'
            ' JOIN frames f ON f.id = v.frame'
            ' JOIN outcomes o ON o.frame = v.frame'
            ' WHERE v.signal = ? ORDER BY f.time, f.channel, f.id LIMIT ?',
            (signal, limit + 1)).fetchall()
        points = [{'time': row['time'], 'channel': row['channel'], 'raw': row['raw'],
                   'physical': _round(row['physical']), 'text': row['text'],
                   'validity': row['validity'], 'unit': row['unit'],
                   'dictionary': row['dictionary'],
                   'dictionary_sha256': row['dictionary_sha256']} for row in rows[:limit]]
        return {'signal': signal, 'points': points, 'truncated': len(rows) > limit}

    def frame_detail(self, frame):
        """单帧视图:原始载荷、解释依据、同时有效的值与未激活的复用分支。"""
        row = self.connection.execute(
            'SELECT f.id, f.time, f.channel, f.frame_id, f.extended, f.kind, f.payload,'
            ' o.status, o.dictionary, o.dictionary_sha256, o.message, o.detail'
            ' FROM frames f JOIN outcomes o ON o.frame = f.id WHERE f.id = ?',
            (frame,)).fetchone()
        if row is None:
            raise ValueError(f'帧不存在: {frame}')
        record = self._frame_record(row)
        if row['status'] == 'decoded':
            content = self.connection.execute(
                'SELECT content FROM dictionary_versions WHERE name = ? AND sha256 = ?',
                (row['dictionary'], row['dictionary_sha256'])).fetchone()
            index = parse_dictionary(content['content'].encode('utf-8'))
            message = index[(bool(row['extended']), row['frame_id'])]
            active = set(record['values'])
            record['inactive_signals'] = [signal.name for signal in message.signals
                                          if signal.name not in active]
        return record

    def frame_at(self, channel, time):
        row = self.connection.execute(
            'SELECT id FROM frames WHERE channel = ? AND time = ? ORDER BY id LIMIT 1',
            (channel, time)).fetchone()
        return None if row is None else row['id']

    def unknowns(self, limit=100):
        """未解码帧的处置清单:每一帧都保留原始记录并说明原因。"""
        rows = self.connection.execute(
            'SELECT f.id, f.time, f.channel, f.frame_id, f.extended, f.kind, f.payload,'
            ' o.status, o.dictionary, o.dictionary_sha256, o.message, o.detail'
            " FROM frames f JOIN outcomes o ON o.frame = f.id"
            " WHERE o.status != 'decoded' ORDER BY f.time, f.channel, f.id LIMIT ?",
            (limit + 1,)).fetchall()
        return {'frames': [self._frame_record(row) for row in rows[:limit]],
                'truncated': len(rows) > limit}

    def counts(self):
        status = {row['status']: row['c'] for row in self.connection.execute(
            'SELECT status, COUNT(*) AS c FROM outcomes GROUP BY status')}
        channels = {}
        for row in self.connection.execute(
                "SELECT f.channel, COUNT(*) AS c FROM frames f JOIN outcomes o"
                " ON o.frame = f.id WHERE o.status = 'decoded' GROUP BY f.channel"):
            channels.setdefault(row['channel'], {})['decoded'] = row['c']
        for row in self.connection.execute(
                'SELECT f.channel, COUNT(*) AS c FROM signal_values v JOIN frames f'
                ' ON f.id = v.frame GROUP BY f.channel'):
            channels.setdefault(row['channel'], {})['values'] = row['c']
        frames = self.connection.execute('SELECT COUNT(*) AS c FROM frames').fetchone()['c']
        return {'frames': frames, 'status': status, 'channels': channels}

    def _frame_record(self, row):
        record = {'frame': row['id'], 'time': row['time'], 'channel': row['channel'],
                  'frame_id': row['frame_id'], 'extended': bool(row['extended']),
                  'kind': row['kind'], 'data': bytes(row['payload']).hex(),
                  'status': row['status'], 'detail': row['detail']}
        if row['dictionary'] is not None:
            record['dictionary'] = row['dictionary']
            record['dictionary_sha256'] = row['dictionary_sha256']
        if row['message'] is not None:
            record['message'] = row['message']
        if row['status'] == 'decoded':
            record['values'] = {item['signal']: self._value_record(item)
                                for item in self._values_of(row['id'])}
        return record

    def _values_of(self, frame):
        return self.connection.execute(
            'SELECT signal, raw, physical, text, validity, unit FROM signal_values'
            ' WHERE frame = ? ORDER BY signal', (frame,)).fetchall()

    @staticmethod
    def _value_record(row):
        return {'raw': row['raw'], 'physical': _round(row['physical']),
                'text': row['text'], 'validity': row['validity'], 'unit': row['unit']}
