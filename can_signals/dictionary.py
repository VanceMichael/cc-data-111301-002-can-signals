"""字典(DBC)的登记与载荷解码。

字典以内容 SHA-256 标识身份:同一名字可以登记多个版本,每条解码结论都记录
实际使用的字典名字与版本摘要,试验人员据此区分原始测量变化与解释依据变化。
"""

import hashlib
from pathlib import Path

import cantools


def parse_dictionary(content):
    """把 DBC 内容解析成 (是否扩展帧, 帧号) -> 报文 的索引。

    标准帧与扩展帧的编号可能相同,索引进项必须同时携带扩展位;
    同一份字典里同一 (扩展位, 帧号) 出现两次时拒绝登记,避免解释不唯一。
    """
    database = cantools.database.load_string(content.decode('utf-8'))
    index = {}
    for message in database.messages:
        key = (bool(message.is_extended_frame), message.frame_id)
        if key in index:
            raise ValueError(f'字典内报文编号重复: 帧号 {key[1]} 扩展位 {key[0]}')
        index[key] = message
    return index


def load_dictionary(path):
    """读取 DBC 文件,返回名字、内容摘要与报文索引。"""
    path = Path(path)
    content = path.read_bytes()
    return {
        'name': path.name,
        'sha256': hashlib.sha256(content).hexdigest(),
        'content': content.decode('utf-8'),
        'messages': parse_dictionary(content),
    }


def decode_payload(message, payload):
    """按报文定义解码一帧载荷,返回信号值列表。

    - 大小端、有符号、比例/偏移由 cantools 按 DBC 定义处理;
    - 复用信号只产出当前激活的分支,未激活分支不出现(而不是填零);
    - 无效码(取值表之外、物理值超出声明范围)保留原始码,物理值留空,
      并标记 validity=invalid,绝不用零或截断值顶替。
    """
    raw_values = message.decode(payload, decode_choices=False, scaling=False,
                                allow_excess=True)
    results = []
    for signal in message.signals:
        if signal.name not in raw_values:
            continue  # 未激活的复用分支:不产出任何值
        raw = raw_values[signal.name]
        item = {'signal': signal.name, 'raw': raw, 'unit': signal.unit or ''}
        if signal.choices is not None:
            if raw in signal.choices:
                item.update(physical=None, text=str(signal.choices[raw]),
                            validity='valid', reason='')
            else:
                item.update(physical=None, text=None, validity='invalid',
                            reason='取值代码不在字典定义内')
        else:
            physical = raw * signal.scale + signal.offset
            below = signal.minimum is not None and physical < signal.minimum
            above = signal.maximum is not None and physical > signal.maximum
            if below or above:
                item.update(physical=None, text=None, validity='invalid',
                            reason='物理值超出字典声明范围')
            else:
                item.update(physical=physical, text=None, validity='valid', reason='')
        results.append(item)
    return results
