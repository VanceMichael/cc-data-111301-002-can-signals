# 车辆台架 CAN 记录的字典绑定与信号资料工具

这个 Python 项目管理台架 CAN 记录的接收链:原始帧归档与清点、按设备配置区间绑定字典、把数据帧解码成结构化信号值。原始记录与解释结论分开存放——同一段原始字节在配置变更前后会得出不同的物理值,每条结论都记录所用字典的名字与内容摘要,试验人员可以分清原始测量变化与解释依据变化。

`can_signals/core.py` 处理帧清单解析与预览,`archive.py` 保存原文件,`dictionary.py` 登记 DBC 并解码载荷,`store.py` 维护 SQLite 接收库(原始帧、配置区间、字典版本、解码结论),`fixtures/` 保存本地业务演练资料,`tests/` 检查已有行为。

## 环境

Python 3.11.2。依赖及其间接依赖固定在 `requirements.txt`。安装只在环境准备阶段执行:

```sh
python3 -m pip install -r requirements.txt
```

## 运行

```sh
python3 -m can_signals inspect fixtures/frames.csv
python3 -m can_signals capture fixtures/frames.csv --archive .work/raw
```

归档返回 SHA-256、字节数及原文件名;文件名不参与内容身份。预览只说明单份来源的基本结构,不代表关联资料已经齐全。输入不可识别时命令失败,不产生空的成功结果。

## 接收链

一段记录会分几次追加交付,配置与字典可能后到。接收库默认在 `.work/signals.db`:

```sh
python3 -m can_signals ingest fixtures/frames.csv          # 追加一段交付(按内容去重)
python3 -m can_signals dictionary fixtures/rig-v1.dbc      # 登记字典版本
python3 -m can_signals configure fixtures/configurations.json  # 登记配置区间
python3 -m can_signals decode                              # 确认可解码的帧,补解码待定的帧
```

查询与追溯:

```sh
python3 -m can_signals report --channel can0 --from 0 --until 2   # 接收记录(含解释依据)
python3 -m can_signals signal speed                # 按信号检索取值序列
python3 -m can_signals frame 6                     # 单帧:同时有效的值与未激活分支
python3 -m can_signals unknown                     # 未解码帧的处置清单
```

行为约定:

- 原始帧(含远程帧、错误帧、未知帧)永久可查,载荷原样保存;
- 字典的适用区间由设备配置记录给出([from, until) 左闭右开),不会拿文件末尾的配置解释整段历史;边界证据不足时保留未解码帧;
- 解码覆盖大小端、有符号比例量、无效码与复用信号;未激活的复用分支不产出值,无效码保留原始码且物理值留空;
- 已确认的结论永不重写;补交配置或字典后,`decode` 只处理尚未确认的帧,不会把其他通道已经确认的结果重复写入;
- 入账、解码、查询全部分批进行,内存占用不随历史帧总量增长;`report`/`signal`/`unknown` 用 `--limit` 分页。

## 开发检查

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q can_signals tests
```

## 本地演示

```sh
python3 -m can_signals demo
```

演示实际读取来源资料,走完"首次交付 → 配置变更前后对照 → 未知帧处置 → 追加交付 → 补交字典后选择性补解码"的完整链路,打印各阶段结果,使用临时目录并自动清理。所有示例身份与业务数值均为虚构,不需要外部账号。
