# Mac 本地部署与启动

此目录基于上游 `cyber-xiaoya/cyber-xiaoya-live` 的 commit `1c6b4f2`，增加 Mac 启动器和远程 TTS 适配。当前默认方案是 **本地桥 + BreezeBlue 官方托管语音 API + Vidu S2 组件版画面**。Mac 只运行网页和桥接程序，无需 Windows、WSL 或本机 NVIDIA GPU；官方语音模式不依赖原来的云实例或 SSH 通道。

## 打开与退出

在 Finder 双击根目录的 **启动-大肥鱼.command**，保留打开的终端窗口。舞台地址为 <http://127.0.0.1:8795/stage>，服务仅监听 `127.0.0.1`。也可在项目根目录运行：

```sh
.venv/bin/python -u tools/run_local.py
```

启动器优先检查本项目是否已经运行：已运行则只打开现有页面，不重新启动或接管服务；若 `8795` 被其他服务占用则报错。未运行时，启动器只从本地配置提取 `tts.engine` 来决定依赖，不输出配置内容，不读取 Key 文件。默认 `breezeblue` 跳过所有 SSH 和 `17860` 接口检查；`omnivoice` 同样不自动建立 SSH；仅旧版 `breeze` 会处理 SSH 网关。

双击启动和打开网页不会自动创建 Vidu 付费会话，也不会发布公开直播。要请求数字人画面，需在舞台页手动点击 **开始连线**。结束后先点网页 **断开**，再回终端按 **Ctrl+C**。启动器只停止自己创建的本地服务；使用旧网关时，也只关闭自己创建的 SSH 通道。已经存在的服务和通道不受接管。

## 官方 BreezeBlue API 与配置

实际设置位于根目录 `config.json`；若设置了 `VIDU_CONF`，应用与启动器使用该变量指定的文件。当前 TTS 选择为 `tts.engine=breezeblue`，官方 API base 为 `https://api.breeze.blue/v1`，私有 Key 文件为 `assets/local/breezeblue.key`。Key 由后端读取用于鉴权，不能放到浏览器脚本或分享的配置中。[官方 API 说明](https://docs.breezeblue.ai/api-reference/introduction)

Vidu Key 仍在本地 `config.json`，DeepSeek Key 文件仍为 `assets/local/deepseek.key`。更换 TTS 不修改或删除这些设置，也不改变 Vidu 负责画面、DeepSeek 负责对话的分工。

聊天和弹幕回复默认启用 `tts.breezeblue_streaming=true`：LLM 先生成一段简短回答，再调用 `POST /v1/text-to-speech/{voice_id}/stream?output_format=pcm`，增量接收并按实时节奏向 Vidu 发送 PCM16LE / 24000 Hz / 单声道音频，不必等待整段语音下载完毕。该模式没有实现 LLM 文字到 TTS 的增量输入。打断时停止向 Vidu 发送，已发出的上游合成请求仍排空并释放锁，避免新旧合成重叠。预览、礼物感谢及旧适配器继续使用整句接口。[输出格式](https://docs.breezeblue.ai/concepts/output-format)、[官方流式接口](https://docs.breezeblue.ai/api-reference/text-to-speech/stream-text-to-speech)

## 保留 Breeze02 音色

原始 Breeze02 保留为逻辑音色 `breeze02`。用户已授权把原始 Breeze02 参考音频上传到官方服务，生成克隆预览并保存为私有音色；官方上传、保存和真实试听均已完成。保存的 `voice_id` 为 `voc_yap4983vwy8k`，对应 `assets/local/breezeblue-voices/breeze02/breezeblue.json`；后续每句直接复用，不必重复上传或重新设计。当前舞台按用户后续选择使用 `breeze_3stpk98ss4hq`（官方 `voc_3stpk98ss4hq`）。

原始资料保留在 `assets/local/voices/breeze02/`：`ref_audio.wav` 是选中的原始参考，`breeze.json` 是旧网关参数。原始设计 seed 为 `202`，人物提示词为：

> 标准普通话。十八岁的少女音，甜软清亮、带一点奶气，容易气鼓鼓地撒娇，委屈时会带哭腔，傲娇嘴硬

官方克隆流程是上传单个音频到 `/v1/voice-previews/clone`，再保存所得预览。本次指定中文分析时遇到官方 503；核对余额未变、无已保存重复音色后，改用官方支持的自动语言识别成功，再以 `language_code=zh` 保存。官方会处理参考片段并转写；保存后的 TTS 使用该参考及其文字，不是把生成的预览音频重新当参考。30.48 秒、约 1.46 MB 的原始 WAV 符合上传要求，无需为了 30 秒边界自行裁切。[克隆指南](https://docs.breezeblue.ai/guides/voice-clone)

## Vidu 画面与更换形象

Vidu 仍单独负责数字人画面，创建入口保持 `/live/s_avatar/component`，模型 `vidu-s2`，RTC 使用托管模式。更换语音 API 不会使 Vidu 形象处理状态自动恢复。

当前默认形象是用户新提供的蓝发写实近景图，已上传并创建为 `1013629607527587840`，云端状态为 `success`。原图和创建回执分别保存在 `assets/local/avatars/whale-closeup-0f758a050bd4.png`、`assets/local/avatars/vidu-0f758a050bd4.json`。旧的两张动漫形象曾停留在 `created`，直接创建会话返回 `avatar is still processing`；这个历史问题不适用于现在的新形象。

新形象已通过真实组件会话、WebSocket `conn_init_ack`、DeepSeek → 官方 Breeze02 → Vidu PCM 及网页 RTC 播放验证。网页视频为 1364×768、`readyState=4`、未暂停且未静音；两次读数的播放时间从 21.167 秒推进到 27.747 秒，静态预览图和入会遮罩均已隐藏。验收画面为 `assets/local/checks/new-avatar-live-proof.png`；会话记录为 `new-avatar-live-e2e.json` 和 `new-avatar-rtc-check.json`。测试已挂断，云端回报 `ended`；正式单场上限已恢复为 600 秒。

本次同时修复 AliRTC Web SDK 7.1.9 的网页接入：用实际支持的 `publishLocalAudioStream(false)` / `publishLocalVideoStream(false)` 关闭本机采集，只订阅数字人；视频只绑定 camera 轨道，避免再被不存在的 screen 轨道覆盖；会话结束后旧入会 Promise 不再覆写界面状态。连接日志使用 SDK 实际的 `connectionStatusChange` 事件。

离线回归命令 `node tools/test_stage_rtc.cjs`，3 项通过：入会前禁采集、只绑定 camera、挂断期间旧入会和延迟回调不会恢复已结束画面。30 项 Python 测试及网页脚本语法检查也通过。

本地图保留为 `assets/local/whale.png` 和传输副本 `assets/local/whale-transfer.jpg`。更换时在控制台选择或上传新的单人图并启用；仅替换磁盘 PNG 不会更新已保存的形象设置。官方允许 `image_uri`（公网 URL、平台上传地址或带 MIME 前缀的 base64 data URI）与 `avatar_id` 二选一，无须强制先建资产。PNG/JPG/JPEG/WEBP 的 base64 解码后文件须小于 20M。更换后先断开当前会话，再重新连线。[Vidu 组件版参数](https://platform.vidu.cn/vidu-stream/doc/s2-avatar/component/parameters)

## 本地弹幕源（8757）

已接入本机 `dyhub` 的统一事件 WebSocket：`ws://127.0.0.1:8757/ws?roomId=566705425536&types=chat,gift`。配置保存在 `config.json` 的 `danmaku_source` 段；启动本地桥时自动订阅，退出时关闭订阅，连接断开后按退避间隔重连。修改该段后需重启本地桥。

聊天沿用现有弹幕回复和动作识别流程，礼物沿用合并感谢流程。控制帧不进入对话；事件 ID 去重，连击礼物忽略进度帧，最终累计数量只交付新增部分。数字人未连线时只接收并计数，不积压弹幕、不触发 LLM/TTS，不自动创建 Vidu 会话。

舞台顶栏显示订阅状态，详细状态和收取/排队/闲置丢弃计数见 `GET /api/danmaku/source`（同时包含在 `/api/health`）。WebSocket 已连接和房间正在采集是两个独立状态：采集器的 `__connected` 也可能带 `room.status=closed`，此时页面显示“采集已关闭”，不能据此宣称已收到真实弹幕。

本地采集服务需先在 `127.0.0.1:8757` 运行；桥只订阅用户指定房间，不接管或关闭采集器本身。连击数量按采集器提供的累计字段归一，单价字段 `diamondCount` 乘以本次增量数量后送入感谢队列。礼物字段映射及重连/去重通过离线测试，真实收礼仍需在正在开播且采集有效的房间验收。

本次部署已刷新指定房间的旧关闭连接，采集器恢复 `live`。新版本桥实际收到至少两条聊天，数字人空闲时回执为 `idle`，没有启动 Vidu 或触发语音。40 项 Python 测试与 3 项 RTC 前端测试通过；真实礼物本轮尚未收到。运行回执为 `assets/local/checks/danmaku-source-verification.json`，舞台截图为 `assets/local/checks/danmaku-connected-proof.png`。

## VTuber 互动配置（2026-10-30）

当前人物 prompt 保存为 `assets/local/personas/bigfish-vtuber.txt`，已写入 `llm.system_prompt`；改版前的设定保留在 `bigfish-before-vtuber.txt`。保留成年鲸鱼娘、长公主、嘴硬心软和爱吃白饭的特点，默认一到两句接话，避免客服开场、照搬段子、假装看见游戏画面或宣称动作已经完成。运行时补充实际动作匹配开关；礼物感谢使用同一人设，不再硬编码“小雅”。

本地弹幕配置改为只回答不复读，节拍从 20 秒改为 3.5 秒，首轮收集窗口为 200 毫秒。每轮最多收集 4 条，但只明确回应一个目标，其余作为背景。消息 ID、观众 ID、房间和原始时间贯穿队列与回复记录；同一 ID 不重复入队，同一观众相同内容在 20 秒内去重，不合并不同观众的同文消息。超过 20 秒的弹幕跳过，等待音频权、LLM 或首段 TTS 后还会检查新鲜度，避免过期才开口。已发送的完整回答若在 45 秒内再次生成相同文本则跳过；只有完整发送的语句写入对话历史，取消的未播内容不进入记忆。该节拍不代表端到端延迟，Vidu 仍有自己的渲染与播放缓冲。

顶栏区分“正在准备回复”“语音发送中”，并显示本轮回应对象；悬停可查看对应弹幕和收到消息至首段音频成功发送的耗时。`/api/live/status` 的 `response.turn`、`reply_recent` 分别记录本轮与最近回合的排队、LLM、TTS 首包和发送结果。音频发送完毕不等于已收到远端播放完成确认。TTS 失败或空音频不再生成假播报字幕；动作排队仅标 `queued`，实际 type9 下发后才记 `fire`，界面含义为“已下发”，不能表示动作已完成。

修复了首轮回复误杀：看门狗过去用初始心跳 0 与系统时间相减，可能把刚开始合成的任务判定为卡死；现在每个持有者以独立单调时钟计时，真实发送 PCM 成功后才更新心跳。同一持有者重入不延长超时，真正 40 秒停滞仍会取消。

参考一手实现的状态区分、播放确认和短回合设计：[AITuberKit 消息接口](https://docs.aituberkit.com/en/guide/other/message-receiver)、[Open-LLM-VTuber 回合完成逻辑](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber/blob/main/src/open_llm_vtuber/conversations/conversation_utils.py#L149)。本项目仍使用 Vidu 画面与 BreezeBlue 声音，没有接入 Live2D。

离线验证共 66 项 Python 测试、3 项 RTC 测试通过。真实对话及礼物文案生成记录在 `assets/local/checks/vtuber-dialogue-smoke.json`；短时数字人验证记录为 `vtuber-live-verification.json`，截图为 `vtuber-live-proof.png`。

后续 v5 修复针对延迟、串弹幕和重复回复：113 项 Python 测试、3 项 RTC 测试通过，另完成网页脚本语法与 diff 检查。限时实机中，测试弹幕收到至首段 PCM 成功发送为 2256.6 毫秒（排队 201.8、LLM 1095.6、TTS 首包 953.8 毫秒）；上一轮同一测试话题约 8.6 秒开始发送。两次独立测试的回复文本和上游负载不同，此数值不是稳定延迟保证，也不含 Vidu 后续渲染与播放缓冲。同 ID 重发、同用户换 ID 同文重发均返回 `duplicate`，仅一次回复完整发送了 3.28 秒音频。网页 RTC 为 1364×768、`readyState=4`，播放时间 10.07→32.13 秒，声音未静音。会话已结束，单场上限恢复 600 秒。

此次实机使用本地注入的测试弹幕，外部采集器当时显示 `closed`，没有把合成测试当成真实直播弹幕验收。随后刷新用户指定房间的关闭连接，采集恢复 `live`，实际收到聊天并按空闲策略只计数、不触发语音。回执 `assets/local/checks/realtime-live-verification.json`，实机画面 `realtime-live-proof.png`、最终待连线画面 `realtime-ready-proof.png`；单独的官方流式探测记录为 `breeze-stream-probe.json`。

## 计费与诊断

启动器不调用语音或对话生成接口。BreezeBlue 在克隆预览、TTS 等生成时计费，保存音色占用账户音色槽位；DeepSeek 对话另行计费。Vidu 按云端数字人会话规则计费，关闭网页不等于断开会话。[BreezeBlue 费用说明](https://docs.breezeblue.ai/concepts/pricing)

本地桥日志位于 `logs/bridge.log`。此机 Vidu 的 HTTP 与组件 WebSocket 使用直连设置 `vidu.use_proxy=false`；官方 BreezeBlue 的网络连通性和 Key 权限需单独验收，不能沿用旧 SSH 网关测试的结论。

已安装依赖快照为 `requirements.mac.lock.txt`。重新安装可使用：

```sh
UV_CACHE_DIR=/private/tmp/cyber-xiaoya-uv-cache uv pip install --python .venv/bin/python -r requirements.mac.lock.txt
```

## 可选：保留旧 Breeze SSH 网关

原云端网关及其文件没有删除。仅在本机主动把 `tts.engine` 改回 `breeze` 时，启动器才复用或创建 `127.0.0.1:17860 → 远端 127.0.0.1:7860` 的 SSH 通道，连接为 `root@cpod-1x371za4lojo.podtcp.compshare.cn:26571`。它不会影响默认官方 API 模式。

旧网关模式需要实例运行：停机后先在云平台恢复原实例，等服务自启动约 2 分钟，再启动本地程序；必要时在远端查看 `tail -50f /root/wan/log.txt`。若需要新建通道，SSH 会在终端交互提示密码，脚本不保存密码。

旧通道使用项目 `assets/local/known_hosts`、`StrictHostKeyChecking=accept-new`、保活和独立控制 socket；退出仅关闭本次创建的通道。既有 `17860` 服务异常时会报错，不覆盖或关闭未知进程。实例身份变化时应先核对主机指纹，再更新项目 known_hosts。启动器不自动开云实例，不修改远端部署。

## 官方接口真实验收（2026-10-30）

默认 `Breeze02` 已通过当前控制台调用官方 API 合成成功：HTTP 200，客户端耗时 5.56 秒，音频时长 6.96 秒，24kHz / mono / PCM16，非静音。`/api/health` 显示 `tts_engine=breezeblue`、官方 base；网页显示 Breeze02 官方已保存，另有账户原来的两条音色可手动选择。公开设置接口未返回 BreezeBlue 或 Vidu Key 明文，配置、备份和 Key 文件均为 0600。30 项本地测试通过，Mac 启动器实际启动日志确认不检查 SSH 或 17860。

验收音频：`assets/local/checks/breezeblue-breeze02-preview.wav`；结构化记录：`assets/local/checks/breezeblue-breeze02-preview.json`；克隆与保存回执：`assets/local/checks/breezeblue-clone.json`。Vidu 新形象的实际画面与端到端验证见上文。
