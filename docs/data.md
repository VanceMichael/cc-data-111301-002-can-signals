# 来源资料约定

frames.csv 的 time 是设备记录的秒数,channel 是总线接口,frame_id 是十进制 CAN 仲裁 ID,extended 为 0 或 1,kind 区分 data、remote 与 error,data 为原始十六进制字节(remote 与 error 帧无载荷)。标准帧与扩展帧的编号可能相同,接收链一律按 (channel, extended, frame_id) 三元组匹配报文。

configurations.json 记录字典适用的左闭右开时间范围 [from, until);null 字典表示来源尚未提供配置,until 为 null 表示没有声明终点。配置按交付批次登记,不允许拿文件末尾的配置解释整段历史;同一时刻被多条配置覆盖且字典不一致时,对应帧按证据不足保留为未解码。

rig-v1.dbc 与 rig-v2.dbc 对 speed 使用不同的比例(0.01 与 0.02),模拟台架固件升级前后的两版字典。can1.dbc 是后补登记的字典:256 报文与 rig-v1 一致,772 报文覆盖大端、有符号比例量与取值表。frames-part2.csv 是第二次追加交付,包含错误帧与无效码取值;configurations-part2.json 是迟到的 can1 配置。

## 帧的处置状态

- `pending`:暂无配置覆盖、配置未提供字典或字典未登记,等待后续交付后补解码;
- `decoded`:已按当时的配置区间与字典版本确认,结论永不重写;
- `unknown`:适用字典中无此报文(原始帧保留可查;字典版本更新后会重试);
- `undecodable`:载荷与报文定义不符(如长度不足),原始帧保留可查;
- `conflict`:配置区间重叠且字典不一致,证据不足,保留未解码;
- `remote` / `error`:远程帧、错误帧无载荷可解码,仅保留原始记录。

每条 decoded 结论都记录字典名字与内容摘要;信号值保留原始码,无效码(取值表之外、物理值超出声明范围)的物理值留空并标记 invalid,未激活的复用分支不产出任何值。
