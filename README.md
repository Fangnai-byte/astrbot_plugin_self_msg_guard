# astrbot_plugin_self_msg_guard（自消息守卫 / 防自转）

![logo](logo.png)

解决一类很烦的问题：机器人对着自己刚说的话继续回，越回越多，形成「自转」循环。
有的情况是本账号的消息被平台回灌成事件，有的情况是**另一个机器人**引用/复读了本机器人的话，
两个 bot 互相接话，一路转下去。

本插件在事件进入 LLM 之前做判断，比平台自带的 `ignore_bot_self_message` 总开关更精细：
可以只拦重复内容、只拦高频、放行特定前缀、只在某些会话生效，并且可以选择「只静默自动回复，
不掐断整条事件」，避免误伤其它插件。

## 三种模式

| 模式 | 行为 | 适用场景 |
| --- | --- | --- |
| `self` | 只要是本账号发出的消息，一律拦下 | 平台完全不做自消息过滤、循环最严重时 |
| `dedup` | 只拦「与机器人刚发出内容高度相似」的消息 | 平台偶尔回灌，日常最平衡 |
| `rate` | 只拦「本会话机器人短时间发言过量」时的消息 | 群聊活跃、容易聊嗨时 |

## 跨账号回环与静默

- **回环检测**（`dedup_all_senders`，默认开）：非本账号发来的消息里，如果包含机器人
  在 `dedup_window_sec` 内说过的、长度 ≥ `loop_min_len` 的句子，或整句相似度 ≥ `loop_ratio`，
  判定为跨账号回环，静默本会话。群里有别的 AI 角色引用宁宁的话时，这条最有用。
- **引用也算**：被引用的原文不会出现在 `event.message_str` 里，只在消息链的 `Reply`
  组件中（`sender_id` / `sender_nickname` / `message_str` / `chain`）。插件会直接读
  `event.message_obj.message` 取出被引用文本，一并参与比对，所以「引用复读」式互相接话
  同样会被静默；引用的是别人、与本机器人近期发言无关时不会误伤。
- **静默**（`loop_mute_only`，默认开）：只调用 `should_call_llm(False)`，本会话不再自动回复，
  但事件照常往下走，记日志、签到之类的插件不受影响。关掉则直接 `stop_event()` 掐断整条事件。
- **发言过量**（`flood_mute_all_senders`，默认关）：本会话机器人 60 秒内说满 `rate_limit` 条后，
  连别人的消息也一起静默。比较激进，容易让真人在热闹的时候收不到回复，默认不开。
- **豁免名单**（`exempt_sender_ids`）：写进去的 QQ 永远不会被静默，建议把真人好友都填上。

### 对方每轮都 @ 你，但每次都换词

两只 bot 互刷时经常不引用原文，只是换着说法接话，复读判据抓不到。这种情况看节律。

插件会记下「对方说完」到「你回完」这段耗时。机器人一般 10 到 20 秒就回，真人连着三轮都这么快很难。同一账号在 `pingpong_window_sec` 内和你来回够 `pingpong_rounds` 轮，且这几轮往返耗时中位数不超过 `pingpong_gap_sec`，就判成乒乓，静音自动回复，并把这个号记进「学到的对端机器人」。学到之后它再 @ 你、或者跟着说同一句口头禅，都继续静音，不用重新攒轮次。

用群里两只 bot 互刷的真实记录复演过，往返耗时 1 到 44 秒，中位数 10 秒，第 3 轮之后开始静音，第 5 条之后稳定拦住。

判据依赖你回话时 @ 了对方，轮次靠这个攒。只引用不 @ 的话攒不起来，这时看下面这条。

### 只引用、从不在正文里复读

有些 bot 每轮都引用你刚说的话，正文却是自己的新内容。按复读判据它永远算「疑似真人」。

`quote_loop_rounds`（默认 5）管这个。同一账号在 `pingpong_window_sec` 内引用或 @ 你够这么多轮，就学成对端机器人。真人偶尔引用不受影响，连着引用五次的不多。

学成之后默认只静音自动回复，事件继续走。想让整条事件也停下，把 `learned_bots_hard_stop` 打开。

## 配置项

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | true | 总开关 |
| `mode` | string | self | `self` / `dedup` / `rate` |
| `allow_prefixes` | list | `["!self"]` | 自己消息以此前缀开头则放行 |
| `session_whitelist` | list | `[]` | 只在这些会话生效（支持统一会话 ID 或其中的数字 ID） |
| `apply_group` / `apply_private` | bool | true / false | 是否作用于群聊 / 私聊 |
| `block_llm_for_self_msg` | bool | true | 放行的自己消息是否禁用默认 LLM 请求 |
| `dedup_window_sec` | int | 120 | 重复/回环比对的时间窗口 |
| `dedup_min_len` | int | 4 | 参与相似度比对的最短长度 |
| `dedup_ratio` | float | 0.9 | 自消息重复判定阈值 |
| `dedup_all_senders` | bool | true | 是否对别人的消息也做回环比对 |
| `loop_mute_only` | bool | true | 静默时只掐自动回复，不掐事件 |
| `loop_min_len` | int | 6 | 回环检测的最短匹配长度 |
| `loop_ratio` | float | 0.55 | 回环检测相似度阈值 |
| `pingpong_rounds` | int | 3 | 来回多少轮算乒乓 |
| `pingpong_window_sec` | int | 120 | 轮次与往返耗时的统计窗口 |
| `pingpong_ignore_echo` | bool | true | 群聊里不要求正文复读，只看轮次和耗时 |
| `pingpong_gap_sec` | float | 25 | 往返耗时中位数上限，0 表示不看耗时 |
| `quote_loop_rounds` | int | 5 | 只引用不复读的账号，引够几次就学成对端机器人，0 关闭 |
| `flood_mute_all_senders` | bool | false | 机器人发言过量时，别人的消息也静默 |
| `exempt_sender_ids` | list | `[]` | 永不静默的发送者 QQ |
| `peer_bot_ids` | list | `[]` | 写明的对端机器人 QQ，它们的引用回环会被主动刹车 |
| `learned_bots_hard_stop` | bool | false | 自动学到的对端机器人要不要也刹车。默认只静音自动回复，避免误伤真人 |
| `learned_bot_ttl_sec` | int | 1800 | 自动学到的账号多久失效，误学会自己过期 |
| `rate_limit` / `rate_window_sec` | int | 5 / 60 | 频率判定：窗口内超过这么多条即视为循环 |
| `log_blocked` / `verbose_log` | bool | true / false | 拦截日志 |

## 命令

| 命令 | 权限 | 说明 |
| --- | --- | --- |
| `/自转守卫` | 管理员 | 查看当前配置与拦截统计 |
| `/自转守卫日志` | 管理员 | 最近 10 条拦截记录 |
| `/自转守卫测试 文本` | 管理员 | 只判断不拦截 |
| `/自转守卫清空` | 管理员 | 清空记录与统计 |
| `/自转守卫帮助` | 所有人 | 命令说明 |

## 和平台面板开关的关系

面板 `platform_settings.ignore_bot_self_message`（默认 false）是最粗的一层，一开就完全不处理
自消息。两者可以二选一，也可以叠加：

- 只想解决自消息回灌：面板开关打开即可，本插件可只留跨账号回环检测。
- 想保留「自己发的指令照常生效」、或者需要防跨账号循环：本插件更合适，模式建议 `rate`。
- 两者叠加时，本插件的回环检测仍然有效，因为跨账号消息不受面板开关影响。

## 说明

- 判定用的记录只在内存里，重启即清空；插件不联网，也不主动收集隐私信息。
- 唯一会写盘的是调试开关：`verbose_log` 打开时，判定现场会写到 `data/plugin_data/astrbot_plugin_self_msg_guard/self_guard_debug.jsonl`，内容包含会话文本与发送者号码，排查完请关掉这个开关并删掉文件。
- 判定全部基于 `time.time()` 与 `difflib.SequenceMatcher`，无额外依赖。
- 需要 AstrBot `>= v4.12.0`。

## 更新日志

- **v0.1.3**
  - 修复：`verbose_log` 打开时调试文件写到 AstrBot 的 `data/` 根目录，不符合「持久化数据放 `data/plugin_data/<plugin_name>/`」的规范。现在写到 `data/plugin_data/astrbot_plugin_self_msg_guard/self_guard_debug.jsonl`。
  - 文档：README 原先写「不落盘、不收集隐私信息」，与行为不符，已改成如实描述。
- **v0.1.1**
  - 修复：节流原先排在作用域与豁免判断之前，导致豁免用户、未启用范围的会话
    也会被「本次先停」牵连；现改为先过范围与豁免，再判断节流。
  - 修复：节流由「按会话」改为「按会话 + 发送者」计数，群里第二个人正常说话
    不会再被连带掐掉，同一个人跨群也不会互相干扰。
  - 优化：节流记录超过阈值时自动清理过期项，避免内存随会话数量增长。
- **v0.1.0** 首版。
