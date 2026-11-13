# 来源资料约定

frames.csv 的 time 是设备记录的秒数，channel 是总线接口，frame_id 是十进制 CAN 仲裁 ID，extended 为 0 或 1，kind 区分 data 与 remote，data 为原始十六进制字节。configurations.json 记录字典适用的左闭右开时间范围；null 字典表示来源尚未提供配置，until 为 null 表示没有声明终点。两个 DBC 对 speed 使用不同的比例。
