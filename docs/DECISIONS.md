# WP0 决策记录

这些选择补上 v3 计划里可以两种读法的地方。后包沿用这里的结果，不再另做一套。

## D-WP0-01 身份登记表是显式参数

`item_id`、`claim_id`、`topic_slug`、`claim_slug` 接受可选的 keyword-only `registry`。键按种类分开: `("item", source_id, guid)`、`("claim", ...)`、`("topic", normalized)`、`("claim_slug", topic, claim)`。同一键可以再次计算。不同键撞上同一短摘要时抛 `IdentityCollision`，并且不覆盖原记录。没有全局登记表。

## D-WP0-02 analysis_hash 保留完整正文和完整频道

`analysis_hash` 对完整 `body_plain` 和完整 `channel` 做哈希。`top_comments` 用 `prompt_fields` 的 audience: 前 5 条，每条最多 300 个码位。计数和 `fetched_at` 不进入哈希。截断标记留在 `truncated` 里，不混进可引用正文。`truncated.channel` 记录频道是否被截断。

## D-WP0-03 slug 的编码

`topic_slug` 是规范化主题的 UTF-8 SHA256 前 12 位。`claim_slug` 是规范化主题与规范化 claim key 组成的 JSON 数组的 SHA256 前 12 位。规范 JSON 使用 `sort_keys`、`separators=(",", ":")`、`ensure_ascii=False`、`allow_nan=False`。元组写成数组，布尔保持布尔。

## D-WP0-04 文件源与网络源分开校验

`kind=file` 必须有 `file_format`，URL 只能是 `file:tests/fixtures/<relative>`，路径段不能是空、`.` 或 `..`。其他 kind 的 `file_format` 必须为空，URL 必须已经等于 `canonical_url(url)`。凭据 query 键只在 Source URL 上拒绝，名字大小写不敏感，精确为 `token`、`access_token`、`api_key`、`key`、`password`、`signature`、`sig`。`canonical_url` 不把这些键当跟踪参数删除。主机末尾的点会被去掉。`transcript_hosts` 存小写 IDNA 主机，不带端口、方案或斜线。

## D-WP0-05 空 analysis_hash 只允许停在组装中的 Item

`Item.analysis_hash` 可以为空字符串。`IngestRecord` 拒绝空值，也拒绝与 `analysis_hash(item, asset)` 不一致的值，并拒绝 `asset.item_id != item.id`。`Item.content_hash` 必须已经等于 `content_hash(title, body_raw)`。存进 Item 的标题必须已是单行。直接调用 `content_hash` 时仍把换行收成空格。后包使用 `build_item`、`finalize_item`、`make_ingest_record`、`make_claim`。

## D-WP0-06 记录上的时间必须是规范形式

记录字段里的时间必须已经是 `YYYY-MM-DDTHH:MM:SS.ffffffZ`。`parse_timestamp` 接受带 Z 或偏移的有时区 ISO-8601，拒绝朴素时间。`format_timestamp` 输出上述形式。

## D-WP0-07 异常消息只保留安全 code

消息必须匹配 `^[a-z][a-z0-9_]{0,63}$`。不安全文本换成 `unsafe_error_code`，原文不保留。`RateLimited` 的 host 和 `retry_after_s` 只放在属性上，不进入 `str` 或 `repr`。`GrokUnavailable.usage` 仅在对象带有 Usage 字段时保留，不写入消息。`InvalidBucket` 和 `InvalidStance` 是 `SchemaError` 子类。`Source` 与 `RuntimePolicy` 把 `SchemaError` 转成同 code 的 `ConfigError`。

## D-WP0-08 导入检查覆盖已经存在的模块

`test_import_layers` 用 AST 检查已有模块，并拒绝 `src/second_brain` 下计划外的 `.py`。缺的后包模块在 `cli.py` 出现之前可以不存在。`cli.py` 一旦存在，全清单变成强制，避免 WP14 永远跳过检查。禁止函数内延迟导入和 `import *`。`TYPE_CHECKING` 不是运行时边。空 `__init__.py` 保持空白。运行时导入图必须无环。

## D-WP0-09 setuptools 不锁版本

计划把构建工具版本标为 UNKNOWN。`pyproject.toml` 声明 `setuptools`，不猜一个版本号。

## D-WP0-10 数值、版本和 argv 常量放在 models.py

`SCHEMA_VERSION` 是 `"3"`。`POLICY_TEMPLATE_VERSION` 是 `"classify-v3"`。HTTP 与 grok 的超时、字节上限、退避、User-Agent，以及 `GROK_ARGV_BEFORE_CWD` 的静态参数，都从 `models.py` 导出。`policy_hash` 覆盖模板、偏好、规范化别名对和 schema。后包导入这些名字，不重新手写。

## D-WP0-11 脱敏 URL 使用省略号

`redact_url` 返回 `scheme://host[:port]/…`，省略号是 U+2026。不可解析时返回 `invalid://redacted/…`，不回显原文。`FetchEvent.url_redacted` 必须匹配这个形状。`hash_url` 是原始 URL 的 SHA256。

## D-WP0-12 测试 tripwire 挡住网络和进程入口

`tests/conftest.py` 在收集前拦截 `socket.socket.connect`、`connect_ex`、`socket.create_connection`、`socket.getaddrinfo`、`urllib.request.urlopen`、`OpenerDirector.open`、`subprocess` 的 `Popen/run/call/check_call/check_output`，以及 `os.system` 和 `os.popen`。打开凭据路径也会失败，包括 `*/.grok/auth.json`、`box-secrets.json`、`gateway.json` 和 `chrome-cookie*`。子进程只在 argv0 含斜线、不是符号链接、并且与非符号链接的 `tests/fakes/grok` 为同一文件时放行。该文件由 WP6 建立。`shell=True` 始终拒绝。loopback 不是例外。错误消息只记录入口种类，不包含 URL 或秘密。

## D-WP0-13 gitignore 的运行目录锚定在仓库根

计划要忽略运行时的 `raw/`、`wiki/`、`data/`、`.grok-sandbox/`、`.grok-tmp/`、`.venv/`。不带前导斜线的 `wiki/` 会连 `src/second_brain/wiki` 一起忽略，WP12 的页面模块就无法入库。这些目录规则写成 `/raw/`、`/wiki/`、`/data/`、`/.grok-sandbox/`、`/.grok-tmp/`、`/.venv/`，只匹配仓库根。

## 后包决策

WP1 至 WP14 的决定当时只写在已合并 PR 的正文里。下面按包补记，不改写 D-WP0-01 至 D-WP0-13。PR 已有编号的沿用原号；PR 只按顺序罗列的，按正文顺序编号。

### WP1 Store 与 raw（[PR #10](https://github.com/Dxfory/local-feed-wiki/pull/10)）

## D-WP1-01 入库的 raw 路径

相对 `raw_dir` 保存 `{item_id}/{analysis_hash}.md`，不再加一层 `raw/` 前缀。

## D-WP1-02 raw 字节

正文是 `# raw` 加上标题、纯文本、频道、提示用观众摘录、转写状态、URL 和类型的规范 JSON。不计数字段。相同字节不重写。不同字节抛 `IdentityCollision`，原文件不动。落盘是 fsync 再 `os.link`。

## D-WP1-03 条目问题不入库

`FetchResult.invalid` 统计问题条数。ETag、Last-Modified、`last_fetched_at` 和 `last_http_status` 只在整批没有问题时前进。没有问题的空 200 仍更新验证器。

## D-WP1-04 publication_json

保存路径、已分档引用、`status_after_publish`，以及从 `summary["files"]` 提起的文件校验和。调用方摘要里的键保留，Store 自己的 `changed_topics` 和 `log_pending` 除外。第二次 `mark_publishing` 不替换另一份 publication。

## D-WP1-05 同一 analysis_hash 算未变

计数、日期或 URL 成员变化仍会写入，但同一 `analysis_hash` 计为 `unchanged`。`updated` 只留给分析哈希变化。`fetched_at` 只在内容版本变化时移动。`update_transcript` 不返回 `FetchResult`。

## D-WP1-06 待转写不参与日期搁置

待转写条目即使缺日期或日期在未来，也不进入日期搁置。转写候选包含每一个启用的主项待转写条目，含未来日期。

## D-WP1-07 完成发布

`finish_publication` 使用已存的 `status_after_publish`。游标只在成功时前进到该次 `started_at`。不再符合发布条件的引用跳过。第二次完成不改写 `finished_at`。

## D-WP1-08 失败与恢复

对处于 publishing 的运行调用 `fail_run` 时保留状态和 publication 快照。恢复只把 `running` 标失败，留下 `publishing`，不删条目或分类，并把 `started` 的模型调用改成 `interrupted_unknown`，不动用量列。

## D-WP1-09 已就绪转写的保留

下一次 feed 条目还不是 ready、且转写 URL 未变时，保留已存的就绪转写。URL 变了又没有新的就绪转写时，正文清空为 pending，旧 raw 文件仍留着。Feed 摄入不清除 `transcript_last_attempt_at`。Content-Type 参数去掉；非法类型保留旧值。

## D-WP1-10 符号链接

raw 目录、条目目录或目标文件是符号链接时抛 `StorageError("symlink_escape")`。

## D-WP1-11 变化主题

记在当前运行上。没有活动运行时先放进 `changed_topics_pending`，下次 `begin_run` 合并。调用方摘要不能覆盖它们。

## D-WP1-12 日志待写

完成、失败，以及对 running 的恢复会置 `log_pending`。`mark_log_written` 清除它。

## D-WP1-13 批次原子性

非成功批次拒绝分类和命题。成功批次先检查每个哈希，再在一个事务里替换该条目的分类和命题。引文子串检查留在 WP9。评论下标必须落在提示用观众摘录里。

## D-WP1-14 重复 guid

同一批次里重复 guid 抛 `SchemaError("duplicate_guid")`。一个 feed 里的条目按 guid 顺序应用。

## D-WP1-15 别名项

别名项不写 raw、不做分类。提升为主项时，缺的那一版才写 raw。旧 raw 字节永不删除。

## D-WP1-16 首次回填下界

`initial_since_at` 在源 id 的生命周期内是 `now - backfill_days`。以后改 `backfill_days` 不移动它。新 guid 严格早于下界则跳过且不入库。等于下界的入库。已知 guid 忽略下界。

## D-WP1-17 304 与验证器

304 更新 `last_fetched_at` 和状态，只在响应头出现时替换对应验证器。200 缺某个头时，该验证器存 None。

## D-WP1-18 模型调用终态

一次调用从 `started` 就地变成终态。再写另一个终态抛 `StorageError("llm_event_final")`。用量 NULL 保持 NULL。也可以直接插入一条新的终态事件。

## D-WP1-19 当前与未发布查询

要求启用的主项、`published_at <= now`、转写不是 pending，且分类哈希等于条目哈希。`ClassifiedItem.url` 是规范 URL。评论是读取时算出的提示用观众摘录。分类队列忽略两个游标。

## D-WP1-20 known_claim_keys 不用时钟

包含启用的、`published_at` 非空的当前命题，含未来日期。带日期的其他查询仍使用注入的 `now`。

## D-WP1-21 fetch_log 的运行可空

`fetch_log.source_id` 是可空外键。`begin_run` 期间的事件记下该 run id。没有活动运行时存 NULL，并且不出现在 `events_for_run`。

## D-WP1-22 第二次 begin_run

同一连接上再次 `begin_run` 抛 `StorageError("run_active")`。另一个连接可以开始另一次运行。恢复不清除仍处于 publishing 的活动 id。

## D-WP1-23 停用的源

`ingest_feed` 拒绝停用的源。更新源配置不重置验证器或 `initial_since_at`。

## D-WP1-24 set_meta 的保护键

不能写 `schema_version`、`last_classified_at` 或 `changed_topics_pending`。

## D-WP1-25 URL 移动与降级

替换分类会清掉 `published_run_id`。同一哈希的 URL 移动若仍是主项，保留分类并记下主题。降级则删除分类。

## D-WP1-26 归档主项

一个规范组里没有启用成员时，保留一个归档主项，并退出输出查询。

## D-WP1-27 raw 写在事务里

修订行只在字节已经存在之后插入，所以 raw 文件写在 SQLite 事务内。本模块不做网络、模型或页面写入。

## D-WP1-28 Store 与协议

`Store` 在结构上实现 `HttpStateStore`，不继承该 Protocol。

## D-WP1-29 发布资格

引用只在条目仍是启用主项、转写不是 pending、且 `published_at <= finished_at` 时标成已发布。

## D-WP1-30 只改计数

只改计数，或只改提示用观众摘录之外的评论文字，会更新 asset，不清分类、不刷新 `fetched_at`、不重写 raw。

## D-WP1-31 旧库可以开 WAL

连接时可以给旧文件打开 WAL，这可能改写数据库头。它不改表 SQL 或行。`migrate` 仍然拒绝未知 schema。

### WP2 公网 URL 与 HTTP（[PR #8](https://github.com/Dxfory/local-feed-wiki/pull/8)）

## D-WP2-01 最多三跳

`HTTP_MAX_REDIRECTS` 把链限制为三次请求。第四跳不发送。

## D-WP2-02 守卫不做 DNS

`guard_public_url` 不解析 DNS，也不是 `canonical_url`。它拒绝非全球单播，包括组播，以及 `is_global` 为真时的 IPv6 站点本地地址。IPv4 映射地址按内嵌 IPv4 判断。`ipaddress` 拒绝的短写、十六进制、八进制和十进制形式是 `ambiguous_address`。`10.example.com` 仍允许。

## D-WP2-03 主机比较

比较时折叠大小写、去掉末尾点并做 IDNA。`localhost` 及其子域、`metadata.google.internal` 及其子域拒绝。字面反斜杠在 `urlsplit` 之前拒绝。C0、DEL 和空白是 `control_character`。主机名里的百分号是 `invalid_host`，不再解码。

## D-WP2-04 Bilibili

三个 `BILIBILI_HOSTS` 在任何端口上按精确主机、在进入传输前拒绝。`policy_interval` 对这些名字仍返回 `max(default, 2)`，但不因此允许抓取。子域不在名单里。

## D-WP2-05 秘密 query

拒绝发生在 `HttpClient.fetch`，不在 `guard_public_url`，并使用 WP0 的辅助函数。userinfo 和秘密 query 同时存在时先报告 userinfo。

## D-WP2-06 allowed_hosts

`None` 表示只做公网 URL 守卫。比较用去掉端口的规范主机名。非法条目忽略。字符串不会被当成字符序列迭代。

## D-WP2-07 社区错误码

200 正文的 JSON 若带社区错误码，走同一套退避，并且先于非 XML 拒绝。Feed JSON 会读，以便看见该码。转写 JSON 在读正文前拒绝。检测只用合成载荷。

## D-WP2-08 读正文之前

Content-Length 超过 2 MiB、非 identity 的 Content-Encoding、不允许的媒体类型，都在读正文前拒绝。流仍计数，超过 2 MiB 即停。DTD 和实体声明是字节扫描，不开主机熔断。

## D-WP2-09 熔断

除 403 和 408 以外的 4xx 不增加熔断计数。403 和 429 抛 `RateLimited`。408、5xx，以及 DNS、连接或超时失败会增加计数。已经阻断的主机不再增加。200 和 304 清计数。重定向只更新 `last_request_at`。

## D-WP2-10 条件验证器与请求头

验证器留在同一限速主机上，主机变化时丢弃。从不发送 Authorization 和 Cookie。标准库传输是一次 GET，经 `http.client`：不跟随重定向，没有 CookieJar，不继承环境代理。测试注入连接器或连接工厂。TLS 验证保持开启。

## D-WP2-11 Retry-After

很长的 Retry-After 不会被缩短到 60 或 900 秒。从第三次失败起本地下限是 900 秒，然后取该下限和响应头中较晚的一个。失败计数在 `2**failures` 之前增加。非法或已过去的日期只用本地退避。日期溢出存 `datetime.max` UTC。

## D-WP2-12 限速主机键

忽略 scheme 和默认端口，保留非默认端口，做 IDNA 并去掉末尾点。IPv6 使用带方括号的压缩形式。

## D-WP2-13 媒体扩展名

已知媒体扩展名在请求前对所有用途拒绝。转写没有 Content-Type 时，只接受 `.txt`、`.vtt`、`.srt`，正文必须是无 NUL 的 UTF-8。明确的音频、视频、图像、HTML、JSON 或 PDF 类型即使 URL 以 `.txt` 结尾也拒绝。

## D-WP2-14 每次拒绝和每一跳都写 FetchEvent

事件只存 `redact_url` 和 `hash_url`。返回的 `HttpResponse` 可以带 feed 正文，事件不带。注入的时钟在按主机等待期间不前进时，客户端抛 `clock_stalled`，不提前发送。

### WP3 文字解析（[PR #2](https://github.com/Dxfory/local-feed-wiki/pull/2)）

## D-WP3-01 返回正文和固定形态

返回字符串只来自 `html`。`title` 不拼接，也不替换空正文。空片段返回 `("", "text")`。第二项永远是 `"text"`。视频调用方可以复用纯文本，不能把这个形态当成视频条目。

## D-WP3-02 标题只校验

非 str 或含 NUL 的 `title` 抛 `SchemaError("invalid_title")`。`html` 同样情况抛 `SchemaError("invalid_text")`。不强制转换。标题里的换行留给 `build_item` 的 `single_line`。异常文本只有安全码。

## D-WP3-03 一次 html.parser

解析器是 `HTMLParser(convert_charrefs=True, scripting=False)`。实体只解码一次，没有第二次 `html.unescape`。`&lt;script&gt;` 保持可见文本。`title` 和 `textarea` 是 RCDATA。`&#0;` 变成 U+FFFD。正文不做 NFKC，也不做 casefold。不接受 `scripting` 的解释器只传 `convert_charrefs=True`（3.13.10 与 3.14.1 起才有该参数）。

## D-WP3-04 省略哪些区域

`script` 和 `style` 省略，含 `svg:style` 这类带命名空间的名字，与 `type` 无关。`iframe`、`noframes`、`noembed`、`xmp`、`plaintext` 也省略。元素在解析器报告的第一个结束标签处结束。属性、注释、文档类型、处理指令和未知声明不复制、不抓取。文档类型内部子集不展开；解析器可能把子集尾部 `]>` 当成数据，这段保留。`noscript` 保留，其中的真实 `script` 仍删除。`template` 文本保留。不把整个 `head` 丢掉。图片的 alt 不取，图注文本保留。

## D-WP3-05 块级标签先变成空白，再折叠

块级和换行标签插入换行。行内标签不插入：`H<span>2</span>O` 仍是 `H2O`。去掉行内 script 或 style 不插入空格。`str.split()` 把各种 Unicode 空白折叠成一个 ASCII 空格并去掉两端。`<pre>` 同样折叠。连续 `<br>` 不保留空行。

### WP4 播客文稿（[PR #4](https://github.com/Dxfory/local-feed-wiki/pull/4)）

## D-WP4-01 空文本是 pending，损坏结构是 invalid_transcript

`None`、空串、空白和只有 BOM 的值，对每种接受的格式都返回空正文和 `pending_transcript`。只有签名、只有 NOTE、只有时间轴或解开后只剩空白的字幕同样 pending。结构损坏拒绝整份，没有半截正文。

## D-WP4-02 格式名只有 plain、vtt、srt

先 strip 再 casefold。`text/vtt` 这类媒体类型抛 `invalid_transcript_format`。空文本也先检查格式名。不嗅探：含 `WEBVTT` 的纯文本仍按字面保留。

## D-WP4-03 字幕只去掉控制行

标识符和序号行去掉。时间轴及其设置去掉。NOTE、STYLE、REGION 块去掉。关键词是第一个记号且不区分大小写，所以 `NOTEWORTHY` 仍是提示 id。开头 `WEBVTT` 块里的 `Kind` 和 `X-TIMESTAMP-MAP` 丢弃。该头部里的箭头算损坏。签名区分大小写，且必须是第一行。前导 BOM 去掉。`WEBVTT` 前的空行算损坏。SRT 可以以空行开始，也可以没有序号。提示按文件顺序合并，重复句子保留。空白折叠成一个空格。提示文本做 HTML 反转义。`<v>` 和 `<i>` 保留。纯文本不反转义。

## D-WP4-04 时间轴只检查形状

`-->` 两侧要有 ASCII 空格或制表符，小数恰好三位，点或逗号都可以。VTT 允许 `MM:SS` 或带小时。SRT 要求小时，小时位数不限。`00:99` 仍是时间轴。台词里可以出现 `-->`。时间之前最多一行标识。两行标识算损坏。

## D-WP4-05 不读取 audio_url 和 transcript_url

它们留在冻结签名里。本函数不打开、不抄进正文，也不因为地址存在而改变空白文本的状态。不启动程序。

## D-WP4-06 title 检查后忽略

必须是不含 NUL 的 str，否则 `invalid_title`。空标题允许。不抄进 `body_plain`。标题换行留给摄入。

## D-WP4-07 非法文稿值是 invalid_text

非字符串（含 bytes）不解码，也不当成 pending。原始 NUL 是 `invalid_text`。异常文本只有错误码。

## D-WP4-08 这里不截断

1500 字的提示上限留在 `prompt_fields`。本解析器从不返回 `not_applicable`。

## D-WP4-09 样例是内联字符串

`tests/fixtures/podcast/` 属于 WP10。本包不往那里加文件。

### WP5 视频元数据（[PR #3](https://github.com/Dxfory/local-feed-wiki/pull/3)）

## D-WP5-01 封闭载荷

未知键抛 `SchemaError("invalid_field")`。不接受 `guid`、`url`、`published_at`、`description`、`views`、`likeCount`、`starRating`、`transcript_status`。不把 `starRating` 抄成 `like_count`，不把 `views` 抄成 `view_count`，不从 `len(comments)` 推导 `comment_count`。真正的 `0` 保持 `0`。只有缺失或 null 变成 `None`。

## D-WP5-02 本解析器不截断

300 字上限由 WP0 的 `prompt_fields` 以后应用。这里保留前 `PROMPT_COMMENT_MAX`（5）条非空评论的全文，不限制标题或频道长度。只含空白的评论跳过。保留的评论不 strip。

## D-WP5-03 标题走 single_line

使用 WP0 的 `single_line`。频道里的换行保留。`title` 和 `channel` 是必需字符串。`comments` 省略或 null 变成 `()`。字符串、非序列或非字符串元素抛 `SchemaError("invalid_comments")`。不迭代生成器。结果的转写状态永远是 `not_applicable`。

## D-WP5-04 什么算本地媒体路径

后缀只认 `.mp4`、`.jpg`、`.png`、`.webp`，不区分大小写。去掉空白并剥掉一层匹配的引号或反引号后，`file:`、`~/`、盘符、斜杠或反斜杠路径，以及没有空白的单个记号，算本地路径。句子里只是提到后缀则保留且不打开。`http://` 和 `https://` 保留且不抓取。前导 `//` 当成协议相对 URL。频道使用同一检查。

## D-WP5-05 媒体字段按出现拒绝

非空 `frame_paths` 抛 `FramesNotAllowed`。空列表、空元组、null 或缺少该键表示没有帧。其他类型（含 `""`）抛 `SchemaError("invalid_frame_paths")`。`thumbnail`、`frame`、`video_path`、`audio_path`、`poster`、`enclosure` 等名单上的键即使值为空也抛 `FramesNotAllowed`。名字不区分大小写。帧拒绝先于未知键错误。异常文本只有安全码。

### WP6 grok 适配器与离线假程序（[PR #9](https://github.com/Dxfory/local-feed-wiki/pull/9)）

## D-WP6-01 假程序是文件

`tests/fakes/grok` 是 tripwire 允许的可执行文件，不是包目录。D-WP0-12 要求它是非符号链接文件。

## D-WP6-02 负例路径挪到 not-grok

文件一旦存在，允许名单就会放行对它的 `subprocess.run`，原来的负例无法再失败。负例改为 `tests/fakes/not-grok`，仍然拒绝。`tests/conftest.py` 没有改，允许名单没有放宽。

## D-WP6-03 两个只给假程序的环境变量

`SECOND_BRAIN_GROK_SCENARIO` 选择场景。`SECOND_BRAIN_GROK_ARGV_LOG` 在测试临时路径追加一行 JSON。它们都不是 CLI 旗标。`complete()` 不读 `SECOND_BRAIN_GROK`，也不回退到 `本机绝对路径`。

## D-WP6-04 沙箱目录名

`cwd` 必须叫 `.grok-sandbox`，`temp_dir` 必须叫 `.grok-tmp`，可以位于测试临时目录。适配器不创建沙箱，不删除别人的文件。项目根检查用 `Path(__file__).resolve().parents[3]`，因为本模块不能导入 `config`。

## D-WP6-05 complete 不做在线门

在线 `RuntimePolicy` 门留在 WP13/WP14。启动前拒绝 PATH 查找、符号链接二进制、非空或名字错误的 cwd，以及 `grok-4.6` 以外的模型。

## D-WP6-06 billing_path 按原样保存

含签名默认值 `unknown`。unknown 不阻止启动，也不改写成 `test` 或免费金额。缺少 `total_cost_usd` 保持 `None`。数字金额拒绝，不转成字符串。JSON `null` 用量变成 `None`。字符串 `"0"` 是真零。

## D-WP6-07 子进程环境

去掉任意大小写的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY`，以及以 `_API_KEY`、`_API_BASE`、`_BASE_URL` 结尾的名字。所有计费路径都去掉，含 `api`。去掉的值不打印。

## D-WP6-08 structured_output

`null` 或 `""` 落到 `text`。其他非对象不落。`{}` 就是载荷。

## D-WP6-09 退出与停止原因

非零退出是 `nonzero_exit`，除非 `type` 为 `"error"`，那是 `cli_error`，即使退出码是 0。只有本来成功、且 `stopReason` 存在又不是恰好 `end_turn` 时才是 `unexpected_stop_reason`。超时和输出上限丢弃缓冲，不返回用量，不重试。上限按流计算。恰好 `GROK_OUTPUT_LIMIT_BYTES` 接受，再多一字节是 `output_limit`。读取用有界 `Popen`，不用 `communicate()`。

## D-WP6-10 非法载荷停止调用

`CompletionResult` 存不下的载荷（布尔、浮点或 NUL）变成 `invalid_payload` 并停止。没有第二种返回形状。

## D-WP6-11 假程序的默认演示

默认分类主题是 `离线`，桶是 `recommend`，理由恰好是 `离线合成结果`，`claims` 是 `[]`。短评是固定离线句子；只有观众摘录非空才加观众句。仅当整个文本被一对 ` ``` ` 或 ` ```json ` 围住时去掉围栏。不剥 BOM。重复键和 `NaN`/`Infinity` 使整次解码失败。

## D-WP6-12 假程序自己的 tripwire

它拦截 socket、DNS、urllib 和 subprocess。某个包装缺失时，探针报告该入口未拦截，并且不真正调用下去。

## D-WP6-13 不另写调用日志

WP6 自己不写调用日志。测试记录只有假程序的 argv 日志，保存 argv、cwd、模式和哈希，不保存标准输出、标准错误或秘密。`GrokUnavailable` 的消息只有安全码。

## D-WP6-14 稳定错误码

`invalid_prompt`、`invalid_schema`、`invalid_model`、`invalid_timeout`、`invalid_billing_path`、`invalid_binary`、`invalid_sandbox`、`sandbox_not_empty`、`invalid_temp_dir`、`spawn_failed`、`timeout`、`output_limit`、`invalid_json`、`invalid_envelope`、`invalid_structured_output`、`invalid_payload`、`invalid_usage`、`cli_error`、`nonzero_exit`、`unexpected_stop_reason`。计划点名的只有 `unexpected_stop_reason`。

### WP7 非共识计票（[PR #5](https://github.com/Dxfory/local-feed-wiki/pull/5)）

## D-WP7-01 规范主题和命题键

发出的 `topic` 和 `claim_key` 是 `normalize_key` / `resolve_alias(..., {})` 的形式，只要仍符合冻结长度。计划示例 `机构A|需求同比增长|2030年度|业务B` 因此存成 `机构a|需求同比增长|2030年度|业务b`。casefold 后主题超过 80 或命题键超过 200 时，保留仍放得下的、字典序最小的原始表面。

## D-WP7-02 计数是排除之后剩下的

`n_items`、`n_sources`、`n_for`、`n_against` 统计去掉冲突来源之后的命题。一个来源两种立场、又没有其他有效来源时，状态是 `conflicted`，四个计数都是 0，该来源列入 `excluded_source_ids`。

## D-WP7-03 状态顺序

没有有效来源是 `conflicted`。否则少于 3 条是 `insufficient`，少于 2 个来源是 `single_source`，只有一侧是 `one_sided`，来源票相等是 `disputed`，严格多数是 `opportunity`。冲突来源旁边还剩两条时仍是 `insufficient`。不用冲突来源内部较大的一侧凑多数。

## D-WP7-04 unknown 是整个片段

`normalize_key(segment) == "unknown"` 时该片段省略。`UNKNOWN`、带空格的 `UNKNOWN` 和全角 `UNKNOWN` 省略。`未知`、`unknowns` 以及只含这些字母的片段仍计入。被省略的键不产生评估，也不产生机会。六个冻结状态没有第七个标签。

## D-WP7-05 评论先于冲突检查移除

`evidence_kind=comment` 在冲突检测和投票之前离开输入。正文 `for` 上的评论 `against` 不排除该来源，也不加一票。标题证据与正文一起投票。

## D-WP7-06 别名留在调用方

`find_minority` 保持冻结的单参数签名。调用方先做 `KeyPolicy` 和 `resolve_alias`。别名环从 `KeyPolicy` 抛 `ConfigError("alias_cycle")`。

## D-WP7-07 句子和证据 id

少数句是少数侧上 `(item_id, claim.id)` 最小的那条 `sentence`。同条目、同方向的命题仍可被选中。它们仍只贡献一个条目 id，并与该来源的其余命题合成一票。`majority_item_ids` 和 `minority_item_ids` 列出剩下的每个条目 id，已排序。

## D-WP7-08 额外黄金用例

夹具包含 S10 的纯函数用例，另加 `four_against_one_for`、`repeated_direction_one_vote`、`partial_conflict_insufficient`。洗牌种子是 `0, 1, 2, 3, 4`，用在基线、来源刷屏和票数持平上。相等比较的是完整 `ConsensusResult` 和每个字段的规范 JSON。

### WP8 多源短评（[PR #7](https://github.com/Dxfory/local-feed-wiki/pull/7)）

## D-WP8-01 至少两个不同条目才有表

一个条目不成表。同一来源的两条是 `single_source`。

## D-WP8-02 分组键

分组键是 `normalize_key(topic)`。放得进 `TOPIC_CHARS` 时 `TopicReview.topic` 用该键，否则用字典序最小的原始主题。行保留自己的主题文本和 `policy_hash`。

## D-WP8-03 重复条目

同一主题里重复的条目 id 合并。保留按 `source_id`、条目 id、`analysis_hash` 排序后的第一条。

## D-WP8-04 行序

确定性行按 `source_id` 再按条目 id。提示行按最新 `published_at` 在前，缺日期在后，时间相同则条目 id 升序。`build_topic_reviews` 按规范键排序主题。`enrich_topic_reviews` 保持调用方的评审顺序和同一个 `rows` 元组。

## D-WP8-05 changed_topics

可以匹配原始字符串或 `normalize_key`，对象是评审主题或任一行的主题。参数必须是字符串的 `frozenset`。

## D-WP8-06 回调缺失与预算

`complete_fn is None` 关闭每一个多源主题，无论是否变化。单源保持 `single_source`。本包不读 `RuntimePolicy`。回调抛 `BudgetExceeded` 时，当前批和之后每个变化的多源主题标 `budget_skipped`，不再调用。

## D-WP8-07 失败范围

`GrokUnavailable` 使当前批和之后每个变化的多源主题失败，不再调用。schema 非法的载荷只失败该批，后面的批继续。新载荷未被接受时丢掉先前句子。行不清空。

## D-WP8-08 未变化的多源主题

保持 `not_requested`，句子为空，不复用旧段落。`summary_note("not_requested")` 给发布器返回 `本次未请求短评`。这句话不存成 `ReviewSentence`。

## D-WP8-09 放不下两个来源

`TopicReview` 没有错误字段。提示无法在 32000 字内保留两个来源时，`summary_status=failed`，句子为空，不调用模型。这条路径叫 `INPUT_TOO_LARGE`。

## D-WP8-10 提示形状

提示只是 `{task: "review-v3", instructions, topics}` 的 `canonical_json`。每条有 `id`、`source_id`、`title`、`bucket`、`reason`、`claims`、`audience`。不发送 URL。命题保留 `evidence_kind` 和 `comment_index`，不再检查引文。标题、理由、命题和评论不第二次截断。

## D-WP8-11 选择上限

最多保留 `REVIEW_ROWS_MAX` 个来源和行：每个来源最新的一行，最多八个来源，然后再补行直到八行。两个上限用同一个常量。

## D-WP8-12 超过 32000 字

按规范 JSON 的 `len` 计算。先从批尾丢掉主题，再丢掉不是该来源所剩最新行的最旧行，再丢掉最旧的所剩来源。绝不低于两个来源。一批也会在重复主题字符串之前停下，以便回复能精确匹配。

## D-WP8-13 模型输出

必须是没有额外键的 JSON 列表和对象。主题字符串必须等于请求集合。每个主题 2 到 8 句，至少一句 `consensus` 和一句 `difference`。`item_ids` 非空、唯一，且属于实际送出的行。送出的行里若有评论，至少一句 `audience` 必须引用带评论的送出行；否则拒绝观众句。一句里 `。．｡？?！!` 至多出现一个。连续两个以上 ASCII 句点是省略号，数字之间的点是小数，两者都不算这个记号。

## D-WP8-14 空评论与中文标签

空评论保持空元组。缺观众摘录时的中文标签和中文桶名属于 wiki 发布器。

## D-WP8-15 schema 从源码树读取

`schema.json` 读模块旁边的文件。`pyproject.toml` 已冻结，所以没有改 package data。

### WP9 分档（[PR #6](https://github.com/Dxfory/local-feed-wiki/pull/6)）

## D-WP9-01 提示就是一个规范 JSON

对象含 `task`、`instructions`、`preferences`、`topic_aliases`、`claim_aliases`、`known_claim_keys`、`items`。对象外没有文字。`--- audience ---` 和禁止执行数据中命令的说明在 `instructions` 里。`items` 是未改动的 `prompt_fields` 对象。

## D-WP9-02 comment_index

`comment` 证据的下标必须在 `0 .. len(audience) - 1`。`audience` 是 `prompt_fields` 已经截断的列表，最多 5 条、每条最多 300。上限不是 `Asset.comment_count`，也不是原始 `top_comments` 长度。本包不构造 `ClassifiedItem`，也不再次截断评论。

## D-WP9-03 引文

折叠 Unicode 空白并 strip 之后，要求连续子串。不做 casefold，不做 NFKC。存下的引文是模型字符串。合法来源只有截断后的提示 `title`、`body` 或 `audience[index]`。

## D-WP9-04 主题和命题键

存储前经过 `resolve_alias`。原始字符串和解析后的值都必须放进字符上限（80 和 200）。解析后的命题键必须有四段非空片段。规范成 `unknown` 的片段为了显示而保留，本包不丢掉它。

## D-WP9-05 提示里的别名图

两侧都做 `normalize_key` 之后是一跳对象。解析模型输出时仍跟随链。

## D-WP9-06 已知命题键

丢掉空值和长于 200 的值，去重，排序，再留 32 个。超长键不截断，也不占名额。

## D-WP9-07 调用前的防御性跳过

重复项（不要求 asset）、`pending_transcript`、缺少 `published_at`、`published_at` 晚于 `now`。`published_at == now` 和空正文仍可进入。已有分类的条目这里不过滤，回填下界也不再应用；两者都归调用方。

## D-WP9-08 analysis_hash 必须相符

合格条目必须已经带有 `analysis_hash(item, asset)`。不符在调用前抛 `SchemaError`。`run_id`、`classified_at`、`analysis_hash`、`policy_hash` 从调用上下文复制。模型对象上的同名键是额外字段，整批失败。

## D-WP9-09 排序和批次

排序是 `(fetched_at, item_id)`。第一次调用前计划好每一批，每批最多 8 条。提示超过 32000 字时从尾部减条目直到放下。单条仍放不下则抛 `SchemaError("prompt_too_long")` 且不调用模型。批 id 是 `b0`、`b1`……偏好长于 4000 字时，在准备条目前抛 `ConfigError`。

## D-WP9-10 命题重复

超过 5 个命题对象即使内容重复也使该批失败。合法条目内部，重复的命题 id 保留第一次。

## D-WP9-11 稳定错误码

schema 错误使用 `SchemaError` / `InvalidBucket` / `InvalidStance` 的稳定码。之后不可用的批次复用 `GrokUnavailable` 的码。预算跳过使用 `budget_exceeded`。其他异常继续抛出。

### WP10 RSS 与转写水合（[PR #11](https://github.com/Dxfory/local-feed-wiki/pull/11)）

## D-WP10-01 文档、条目和 304

不是良构 XML，或含 DTD、`ENTITY` 声明时，在 feedparser 之前抛 `FetchError`（`invalid_feed` 或 `xml_dtd`）。一条坏条目变成 `EntryIssue`，不丢掉兄弟。验证器只在存储批次没有问题时移动。304 走 `Store.accept_not_modified`，不走 `persist_feed`，因为 `ingest_feed` 拒绝 200 以外的状态。304 省略 ETag 或 Last-Modified 时保留旧值。

## D-WP10-02 guid、链接和日期

缺 guid 时用规范的公开链接。标题从不当 guid。有 guid 但没有公开链接是 `invalid_url`，不把 guid 当成链接。相对链接只对网络 `Source.url` 解析。文件源要求条目 URL 已是绝对 http(s)。解析不出的日期存 `published_at` None 并搁置。未来日期原样保存。

## D-WP10-03 文章 HTML

正文 HTML 取第一个非空内容块（`content:encoded` 或 Atom `content`），否则 `summary` 或 `description`。`body_raw` 保留该 HTML。`body_plain` 是 `parse_text`。形态跟随源种类。文字条目即使有 enclosure 也保持 `not_applicable`；enclosure 仍可存成 `audio_url`。文字 feed 忽略 `podcast:transcript`。它们的频道留空，避免博客标题变化重写每条分析哈希。

## D-WP10-04 播客注释不进正文

非空的 `podcast:transcript` 字符数据，或 `podcast_json` 的转写字符串，经过 `parse_podcast` 后可以就绪。否则条目保持 pending，`body_plain` 为空。坏的 enclosure URL 省略。非法转写 URL 使该条目失败。多个转写元素时，内联文本优先，然后是 `text/plain`、`text/vtt`、`application/x-subrip`（或 `.txt` / `.vtt` / `.srt`）。feedparser 6.0.14 只保留最后一个 `podcast:transcript`，所以由 ElementTree 收集。MIME 参数去掉，转写 URL 以规范形式保存。播客频道是 feed 标题。

## D-WP10-05 解析入口

`parse_rss` 只接受 `rss` 和 `podcast_rss`。`youtube_rss` 留在 WP11。`parse_local_records` 只接受 `podcast_json` 和 `video_json`。未知 RSS 元素忽略。未知 JSON 字段拒绝该条目。顶层不是 `{"items": ...}`、重复键或 NaN/Infinity 时，整份文档是 `invalid_payload`。输入是 UTF-8，正文超过 2 MiB 拒绝。JSON 日期是字符串但不是时间戳时搁置为 None；非字符串日期是 `invalid_timestamp`。日期从已存的 `published_parsed` 和 `updated_parsed` 读取。只有 `updated` 的 Atom 条目使用该时刻。

## D-WP10-06 hydrate_transcripts

返回变为就绪的条数，加上 `EntryIssue`。候选按 `item_id` 排序。失败计入 `limit`。超过上限的其余条目保持 pending，不记录尝试。`file:` 转写只对 `kind=file`、且通过夹具 URI 形状检查后读取。网络条目指向夹具时，在读取器运行前抛 `file_transcript_forbidden`。`http is None` 对 http(s) URL 表示 `offline_transcript`。允许的主机是 `transcript_hosts` 加上订阅主机。只有存储类型是 `text/plain`、`text/vtt` 或 `application/x-subrip`，或路径以 `.txt`、`.vtt`、`.srt` 结尾时才抓取。过了这道门之后，`HttpClient` 仍拒绝音频、HTML、JSON、PDF 和媒体扩展名。304 的转写响应不编造正文（`transcript_not_modified`）。

订阅主机在当时按 feed URL 的主机加入名单。本轮复核发现 IPv6 字面量的括号形式与 `HttpClient` 不一致，修正见文末 D-REV-01，不改写本条的原始决定。

## D-WP10-07 计数必须一致

ElementTree 是良构性闸门。它的 `item`/`entry` 计数与 feedparser 不一致时，文档以 `invalid_feed` 失败，避免配错节点。一批里同一 guid 的第一份保留，后面的是 `duplicate_guid`。

## D-WP10-08 视频 JSON

走 `parse_video`。`FramesNotAllowed` 变成该条目的 `frames_not_allowed`，不使兄弟失败。

### WP11 YouTube Atom（[PR #12](https://github.com/Dxfory/local-feed-wiki/pull/12)）

## D-WP11-01 用 ElementTree，不用 feedparser

feedparser 在导入允许名单上，但它的严格解析会退回宽松解析，并可能为非法 XML 返回部分条目。它还按文档前缀键入 YouTube 命名空间，而且 `feedparser.parse` 可以打开文件名或 URL。ElementTree 让损坏文档失败，按命名空间 URI 取 `yt:videoId` 和 `media:*`，并且只接收字节。

## D-WP11-02 三层失败

非法 XML、非 Atom 根或 DTD 抛 `FetchError`（`invalid_feed` 或 `dtd_forbidden`），不返回半截批次。一条坏条目变成一条 `EntryIssue`，feed 的其余部分继续。调用方错误（错误的源种类、非字节、非规范的 `now`）抛 `SchemaError`，不解析。

## D-WP11-03 DTD 扫描

`<!DOCTYPE` 和 `<!ENTITY` 在解析前拒绝，含 UTF-16 文档，因此内部实体不展开。同一段文字出现在注释或 CDATA 里时是描述文字，不是声明。

## D-WP11-04 问题身份

`entry_key_hash` 是 `kind`、源 id 和一个值用换行连接后的 SHA-256。种类依次是 `video`（视频 id）、`link`（原始 href）、`atom`（atom id）、`index`（从 0 起的条目下标）。问题只存该摘要和一个错误码。

## D-WP11-05 一条问题，按第一项失败的检查

顺序是视频 id、链接、标题、播放量，然后是 `frames_not_allowed`、不安全 URL 或 schema 错误。视频 id 缺失时，即使链接也缺失，仍报告视频 id。

## D-WP11-06 用哪条链接

缺少 `rel` 视为 `alternate`。优先 `text/html`、`application/xhtml+xml` 或没有类型；否则用另一条 alternate。`rel=self` 是 feed，不是视频。`media:content`、`media:thumbnail`、`media:player` 不是链接，也不读取。不从 `videoId` 拼出观看 URL。

## D-WP11-07 相对链接

只对 `youtube_rss` 的源 URL 做 `urljoin`，然后 `guard_public_url` 和 `canonical_url`。`file` 源必须已经带绝对的公开 http(s) URL。条目保留该绝对 href；`build_item` 另行保存规范形式。

## D-WP11-08 文本和频道

正文只取第一条 `media:description`，不取 `atom:summary`。空描述保留。标题是 `atom:title`，然后 `media:title`；空白标题是 `invalid_title`。频道是条目作者，否则 feed 作者，否则空。源标题不当频道。

## D-WP11-09 命名空间和计数

YouTube 元素使用 `http://www.youtube.com/xml/schemas/2015`，与前缀无关。Media RSS 接受有或没有末尾斜杠。空白或缺失的 `views` 是 `None`。其他非十进制记号以 `invalid_count` 拒绝该条目。

## D-WP11-10 日期和重复

缺少或解析不出的 `published` 让 `published_at` 不设置，条目仍返回。未来和很旧的日期保留为规范 UTC。`updated` 不是发布时间，也不用 `fetched_at` 代替。本解析器不做回填或跨源去重。重复视频 id 保留实际建成的第一条；更早被拒绝的副本不挡住后来的有效副本（`duplicate_guid`）。记录保持文档顺序。

### WP12 本地 wiki（[PR #13](https://github.com/Dxfory/local-feed-wiki/pull/13)）

## D-WP12-01 同一目录里的 slug 碰撞

不同的完整键在同一目录共享一个 12 位十六进制 slug 时，在任何暂存提交之前抛 `IdentityCollision`。主题 slug 和命题 slug 是两套登记，所以 `topics/` 和 `opportunities/` 里相同摘要不是文件碰撞。最终文件字节与清单不符时抛 `PublishError("hash_mismatch")`，并留下作为证据。

## D-WP12-02 Publication 路径

最终路径是 `digest/<run_id>.html` 和 `snapshots/<run_id>/manifest.json`。清单的 `files` 是摘要、主题页和机会页的 `{path, sha256}`。清单不为自己做哈希。

## D-WP12-03 索引状态

`unclassified` 非空时索引状态是 `部分完成`，否则是 `已发布`。待转写、搁置日期和非正常短评不改变它。`stage` 没有运行状态，也没有插入或更新计数，所以页面不印 `版本更新次数`。

## D-WP12-04 日期标签

搁置日期的行显示 `日期待确认`，不印 `published_at` 或 `fetched_at`。没有时间的未分类行显示 `发布时间未知`。

## D-WP12-05 摘要分桶

桶只列增量。空增量在三个桶里都写 `本次没有新的已分档条目`。增量非空但某个桶空时写 `本节没有新的条目`。空队列和空评估列表写 `无`。标题顺序是必读、推荐阅读、不推荐、待转写、日期待确认、待分类/本次未完成、非共识、说明、脚注。

## D-WP12-06 href

只有 `canonical_url(url) == url`、`guard_public_url` 成功，且 URL 不含空白、引号、方括号或反斜杠时，才保留 HTML `href`。其他 URL 是转义后的文本。Markdown 先做 HTML 转义再做反斜杠转义；`|` 只在表格单元格里转义。

## D-WP12-07 机会页

只来自 `ConsensusResult.opportunities`。状态 `opportunity` 的标签是 `少数候选`。发布的命题键是该 `Opportunity` 上已经选定的键。`find_minority` 在符合四段规则时返回规范键，清单存的是该字符串。

## D-WP12-08 classified_refs

只来自增量，按 `(item_id, analysis_hash)` 排序。主题页的时间范围是该主题的评审行。摘要和清单的范围是增量加上评审行。

## D-WP12-09 重复检查

重复条目 id、重复评审主题、重复评估或机会键、评审行的主题不能规范成评审主题，以及同一 id 出现在增量和评审里但分析哈希、标题或 URL 不同，都抛 `SchemaError`。警告长于 500 字或含 U+0020 以下字符时抛 `SchemaError("invalid_text")`。

## D-WP12-10 不导入评审模块

`compile.py` 不导入 `review.multi_source` 或 `consensus.minority`。它渲染收到的 `TopicReview` 和 `ConsensusResult`。`本次未请求短评` 是计划中的说明。`仅基于本次所列选中材料` 只在 `sentences` 非空时出现。

## D-WP12-11 固定脚注

非共识页只对照库内句子的分布。它不是市场结论，也不是买卖、仓位或目标价。多数不等于事实，少数不等于机会。相关转载与多账号可能仍不独立。无法用短评覆盖具体事实的证据链。

## D-WP12-12 发布顺序

`os.rename` 发布快照目录和摘要。`os.replace` 发布 `index.md`。失败的调用留下先前的索引；若索引替换本身已经完成，索引指向新的完整版本。旧快照目录永不删除。再次发布相同字节不重写最终文件。

## D-WP12-13 日志块

一块是白名单事件字段的一行规范 JSON，夹在匹配的 BEGIN/END 标记之间。身份是 `(run_id, sha256)`，哈希覆盖含末尾换行的那一行。不完整的尾部留在原地。缺少 `total_cost_usd` 时写成字符串 `UNKNOWN`。令牌 null 保持 null。

## D-WP12-14 发布 IO

rename、replace 或 fsync 期间的 `OSError` 变成 `PublishError("publish_io")`，消息不含路径。会逃出 wiki 的符号链接是 `PublishError("symlink_escape")`。

### WP13 一次运行（[PR #14](https://github.com/Dxfory/local-feed-wiki/pull/14)）

## D-WP13-01 PublishError 的时机

`stage` 在 `mark_publishing` 之前抛 `PublishError` 时，返回 `RunReport(status="failed")`，已保存的分类保持未发布。下次运行补发它们，不再调用模型。`mark_publishing` 之后的失败仍然抛出，`fail_run` 留下 publishing 行，下次完成该清单，不开始第二份快照。

## D-WP13-02 退出码不属于 run_once

`partial` 和 `failed` 是 WP14 映射到退出码 2 和 1 的状态。日志追加失败保持归档状态和游标，加上警告 `日志尚未写入`，并留在 `pending_log_runs`，直到以后某次追加完整的一块。

## D-WP13-03 没有委派就不插入 llm_calls

预算用尽，或 `GrokUnavailable` 之后的任何用途，只要没有调用委托，就不插入行。已经委派的调用先插入 `started`，再按同一 `call_id` 更新，含清理后的 `GrokUnavailable.usage`。

## D-WP13-04 清单文件

`mark_publishing` 存下的文件列表是已暂存清单的 `files` 数组。检查这些哈希的是 `publish`。

## D-WP13-05 HTTP 304

304 是成功抓取。空正文不解析，验证器保留，已经有当前分类的条目不再送给模型。

## D-WP13-06 规范主项

主项是 Store 选出的那个条目 id。集成测试读该行。去掉它的来源会提升别名，并在给提升后的条目分类之前删除旧命题。

## D-WP13-07 短评 schema 失败

短评载荷未通过 schema 时，不把已完成的归档从 `success` 改成 `partial`。确定性主题页仍然发布，并写 `短评不可用`。

## D-WP13-08 崩溃模拟

“索引已更新、完成未执行”通过在 `publish` 返回后让 `finish_publication` 抛一次来模拟。`run_once` 没有额外的崩溃钩子。

### WP14 配置、CLI 与 README（[PR #15](https://github.com/Dxfory/local-feed-wiki/pull/15)）

## D-WP14-01 离线是强制的

默认模式和显式 `--offline` 都迫使 `RuntimePolicy.offline=True`。运行时文件不能把它改回去。`--live` 只设置 `offline=False`；它不设置 `live_authorized` 或任一核验旗标。

## D-WP14-02 example 配置只读

`--example-config` 读取 `config/*.example.*`，从不写入。它和 `--config-dir` 同时出现是 `conflicting_config`。

## D-WP14-03 路径

省略 `--db` 时是 `data/second_brain.sqlite`，相对 `PROJECT_ROOT` 解析。绝对路径和 `..` 拒绝。`raw/`、`wiki/`、`.grok-sandbox/`、`.grok-tmp/` 固定在该根下。WP14 当时把锁写成 `data/second_brain.lock`。复核改为计划中的 `data/run.lock`，见 D-REV-02。数据库名不变。锁不跟随自定义数据库名。

## D-WP14-04 --now

接受带时区的 ISO-8601，含演示形式 `2030-01-15T00:00:00Z`。时钟发出带微秒的规范时间戳。它和 `--live` 一起使用时拒绝。

## D-WP14-05 没有子命令

打印两行并退出 0：一行带占位符（`DIR`、`PATH`、`TIMESTAMP`）的用法，然后是 S12 的离线演示命令。`--help` 和 `run --help` 打印同一段。帮助里没有文章正文、凭据或已加载的配置值。未知命令仍以 `unknown_command` 退出 1。`-h` 仍是 `unknown_argument`。

## D-WP14-06 离线二进制

离线模式要求 `SECOND_BRAIN_GROK` 与 `PROJECT_ROOT/tests/fakes/grok` 是同一个文件。符号链接拒绝。未设置时不回退到 `本机绝对路径`。该默认路径只在在线门通过、且变量未设置时使用。

## D-WP14-07 缺少夹具

缺少夹具文件使该源失败，仍可以发布可读的部分运行（退出 2）。符号链接或 `..` 逃逸是 `ConfigError`，不读取外面的文件。

## D-WP14-08 日志警告迫使 CLI 退出 1

运行警告 `日志尚未写入` 使 CLI 退出 1。`RunReport` 状态保持 `run_once` 返回的值。

## D-WP14-09 load_config 增加可选 example

`load_config(config_dir, *, offline, example=False)` 保留冻结的必需参数，并增加可选 `example`，因此只有一个加载器。

## D-WP14-10 非文件 URL 在加载时守卫

非文件 URL 在配置加载时经过 `guard_public_url` 和 Bilibili 主机名单，此时还没有 HTTP 客户端。

## D-WP14-11 输出

成功和部分运行在标准输出打印一行状态。失败时标准错误只有安全码，没有文章正文，没有凭据值。

### 本轮复核

## D-REV-01 IPv6 订阅主机与守卫使用同一主机名

WP10 的允许名单是 `transcript_hosts` 加上订阅主机。`urlsplit().hostname` 会去掉 IPv6 字面量的方括号，而 `HttpClient` 比较的是 `parse_public_url().hostname`，形式为 `[2001:db8::1]`。同一公网 IPv6 主机上的转写因此被判 `host_not_allowed`。`sources/ingest.py` 改为用 `parse_public_url` 取订阅主机。不是公网 http(s) 的订阅 URL 不加入名单；列出的 `transcript_hosts` 仍然有效，客户端仍拒绝该次抓取并写日志。`tests/conftest.py` 与 `pyproject.toml` 没有改。

## D-REV-02 CLI 锁文件改为 data/run.lock

计划 S04 和 S11 写 CLI 固定传 `data/run.lock`，并且 finally 只释放、不 unlink。WP14 用了 `data/second_brain.lock`。CLI、断言该路径的测试和 README 改为 `data/run.lock`。数据库仍是 `data/second_brain.sqlite`。`run_once` 继续由调用方传入路径，释放时只 `flock` 解锁并关闭描述符，不删除文件。

### 跟进：打包、CI 与冒烟

## D-FOLLOW-01 两份 schema 放进 wheel

`classify/schema.json` 和 `review/schema.json` 原来不在 wheel 里。加载器已经用 `Path(__file__).with_name("schema.json")`，所以只把这两个相对路径写进 `tool.setuptools.package-data`。不改 `requires-python`，不加运行依赖，不改加载代码。

## D-FOLLOW-02 冒烟脚本不改在线授权

当时 CLI 的根固定是 `config.PROJECT_ROOT`。`--db` 搬不走 `raw/`、`wiki/` 和 `data/run.lock`。`scripts/live_smoke.sh` 在调用 `second_brain.cli.main` 之前把 `config.PROJECT_ROOT` 指到临时目录，从而沿用现有 CLI，而不是重写流水线。D-PUB-01 增加了 `--root` 和 `SECOND_BRAIN_ROOT`。冒烟脚本仍可在调用前赋值 `config.PROJECT_ROOT`。

离线，或 `SECOND_BRAIN_GROK` 与 `tests/fakes/grok` 是同一个文件时，脚本拒绝其他二进制。`--live` 必须同时带 `--budget` 和 `--max-items`，并且两者都只能是 1、2 或 3。条目上限靠临时目录里的合成 RSS 条数，不抓取公网订阅。在线模式只在 `config/runtime.yaml` 已经写明授权、`session` 或 `api`、以及两项核验时才调用 CLI；脚本不把这些旗标改成 true，也不读取 `auth.json`。这不证明 Q03 或 Q04。

## D-FOLLOW-03 页面仍不印版本更新次数

`updated` 统计这次运行里的 feed 更新和文稿恢复。同一条可以计多次。这个整数在 `run.py` 里累加，放进 `RunReport`，不是 `WikiPublisher.stage` 的参数。`stage` 收到的 `ClassifiedItem` 和 `Item` 没有变化次数，并且重复 id 会抛 `SchemaError`，所以快照里看不出“同一条变了几次”。

从 `WikiPublisher` 读 `Store` 要新增一条被冻结允许名单禁止的导入。`raw_revisions` 按 `(item_id, analysis_hash)` 各记一行，包含首次入库，也不等于这次的 `updated`。两种做法都会变成另一个数。因此页面不加「版本更新次数」。标准输出的 `updated=` 保留。

### 操作者于 2026-11-11 的决定

下面四条是操作者在 2026-11-11 12:03（Asia/Shanghai）作出的决定。只追加，不改写 D-WP0-01 至 D-WP14-11，也不改写 D-REV 与 D-FOLLOW。

## D-DX-01 Q03 授权一次真实冒烟

允许一次真实 grok 冒烟。范围是 1 个来源，最多 3 条。失败时不要自动重新登录。

操作步骤写在 README「真实 grok 冒烟」。`config/runtime.example.yaml` 仍是离线示例。冒烟形状在 `config/runtime.live-smoke.example.yaml`，复制为不入库的 `config/runtime.yaml` 之后，`scripts/live_smoke.sh --live --budget 3 --max-items 3` 才读得到授权。脚本自己不把 `live_authorized` 或两项核验改成 true。仓库里没有登录命令。

## D-DX-02 Q04 只用现有订阅额度

只用已经有的订阅额度。计费路径是 `session`。不使用 API。不购买额外用量。

`scripts/live_smoke.sh` 和 CLI 仍把 `billing_path: api` 当成可以放行的已核验路径。这次不改代码。操作者必须让 `config/runtime.yaml` 保持 `session`。子进程会丢掉以 `_API_KEY`、`_API_BASE`、`_BASE_URL` 结尾的环境变量，这不代替上述文件里的路径。

## D-DX-03 Q12 只决定发送范围

公开源和操作者的私人笔记都可以发给 xAI。DNS 防护、保留期、备份和定时仍开放。

这次冒烟命令仍只准备临时目录里的一条合成 RSS 和示例偏好，不读取真实订阅，也不读取私人笔记。

## D-DX-04 Q01 仍开放

订阅名单仍不写入。订阅名单仍由操作者自行写入，不进仓库。

## D-REV-03 grok 1.0.46 去掉不受支持的 --no-auto-update

`GROK_ARGV_BEFORE_CWD` 仍包含 `--no-auto-update`。`models.py` 没有改。grok CLI 1.0.46 的 `grok --help` 没有这个旗标；自动更新是单独的 `update` 子命令。在线调用先执行不带其它参数的 `grok --help`，工作目录是 `.grok-tmp`，不消耗模型额度，也不往空的 `.grok-sandbox` 里写文件。帮助文本列出 `--no-auto-update` 时原样传入，以免旧版本在调用期间自动更新。没有列出时从实际 argv 去掉。不传入 `--auto-update`，也不调用 `update`。

其余冻结安全旗标一个都不删。帮助文本缺任何一个将要传入的旗标时，错误码是 `unsupported_flag`，不启动模型。帮助进程本身失败时错误码是 `help_check_failed`，同样不启动模型。与 `tests/fakes/grok` 同一文件的离线二进制不跑这次检查，argv 仍是冻结常量。

模型进程非零退出时，只把标准错误的第一行经过脱敏后写入 `llm_calls.error_detail`。脱敏替换看起来像令牌或密钥的片段，并替换掉 `Authorization` 的整段值（包括 `Basic` 后面的编码）以及引号中的完整口令，不留下后半段。其余标准错误丢弃，不写入异常消息，也不写入 wiki 日志。异常消息仍只有安全码。D-WP6-09 对标准错误其余部分的丢弃不变。已有 schema 3 的数据库在 `migrate()` 时补上这一列，不提升 `SCHEMA_VERSION`。

## D-REV-04 `.json` 的 `--prompt-file` 写成 ACP 内容块

`grok --help` 的说明来自 clap。`crates/codegen/xai-grok-pager/src/app/cli.rs` 写着：`--prompt-file <PATH>` 是 “Single-turn prompt from a file.”，`--prompt-json <JSON>` 是 “Single-turn prompt as JSON content blocks.”。来源：https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/src/app/cli.rs

解析在 `headless/cli.rs`。`.json` 按内容块解析，其它扩展名按纯文本。JSON 可以是数组 `[...]`，也可以是 `{"type":"acp","content":[...]}`。对象没有字符串 `type` 时的错误就是 run5 的第一行：`JSON object must have a "type" field (e.g., {"type": "acp", "content": [...]})`。`content` 反序列化成 `Vec<acp::ContentBlock>`。来源：https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/src/headless/cli.rs 。用户指南同一规则，并写明 headless 不把管道里的 stdin 当作提示；纯文本例子是 `grok --prompt-file ./prompt.txt`。来源：https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md

文本块的字段来自 grok 所钉的 agent-client-protocol 0.10.4。`ContentBlock` 用 `type` 区分，`text` 变体必有字符串 `text`。协议页的例子是 `{"type":"text","text":"What's the weather like today?"}`。来源：https://github.com/agentclientprotocol/agent-client-protocol/blob/v0.10.4/schema/schema.json 与 https://agentclientprotocol.com/protocol/content 。grok-build 的工作区 `Cargo.toml` 把 `agent-client-protocol` 钉在 0.10.4。

因此 `.json` 提示文件写成 `{"type":"acp","content":[{"type":"text","text":<原来的提示>}]}`。提示字符串只放在 `text` 里，不写 `annotations` 或 `_meta`。argv、`--help` 自检、脱敏后的 `error_detail` 和符号链接拒绝都不改。不传 `--auto-update`，也不调用 `update`。

没有改成 `.txt` 或 stdin。1.0.46 已经因为后缀 `.json` 走了内容块解析，run5 的报错就是这条路径；换扩展名会换成另一个解析器。stdin 不是 headless 的提示入口，子进程仍然把 stdin 接到 `DEVNULL`。裸数组同样合法，假二进制也接受。写入选用错误信息自己举的 `{"type":"acp","content":[...]}` 包装。

### 可发布工作树

下面四条是为可发布的当前树作出的决定。不改写更早的决定编号。

## D-PUB-01 根目录的优先级

`--root` 优先。否则使用非空的 `SECOND_BRAIN_ROOT`。两者都没有时使用 `config.PROJECT_ROOT`，默认是包含 `src/second_brain` 的检出目录，不跟随当前工作目录。相对路径先按当前工作目录拼成绝对路径，不解析符号链接；根本身是符号链接则拒绝，错误码 `invalid_root`。空串、未去空白和反斜杠同样拒绝。

环境变量只在 `cli.py` 读取。`config.py` 不读环境。冒烟脚本和测试仍可在调用前替换 `config.PROJECT_ROOT`。

## D-PUB-02 在线二进制没有本机默认路径

`SECOND_BRAIN_GROK` 未设置时，离线和在线都拒绝，错误码 `grok_binary_required`。不再探测某个用户主目录下的可执行文件。离线仍然要求该路径与检出里的 `tests/fakes/grok` 是同一个文件，并拒绝符号链接。在线要求它是普通文件。授权、计费路径和两项核验一个都不放宽。

## D-PUB-03 示例预算按大约 21 个源

`RuntimePolicy` 的代码默认仍是分类 3 批、短评 1 批。`config/runtime.example.yaml` 写成分类 21、短评 8、文稿 20。分类每批最多 8 条，21 批可以给大约 21 个源各留一批。短评每批最多 4 个主题，8 批是 32 个主题。这些数字是调用次数，不是金额。`examples/offline/runtime.yaml` 保持 3 和 1，供合成演示。不在 `run.py` 里按源数量自动放大，以免改变已经断言预算耗尽的测试。

## D-PUB-04 公开示例源与离线夹具分开

`config/sources.example.yaml` 只放四个公开 https 源：Hacker News、BBC World、Planet Money、Google Developers 的 YouTube Atom。`config/sources.yaml` 继续被忽略，不入库。离线运行拒绝非 file 源，所以合成演示放在 `examples/offline/`，用 `run --offline --config-dir examples/offline`。`--example-config` 仍只读 `config/*.example.*`，不会把示例写成真实配置。

