# 第二大脑搭建计划 v3：冻结契约与并行实施手册

日期：2026-11-10。适用目录是仓库根。本文件是实施契约。编写代码时使用的模型不是分类时调用的模型。

本次只阅读本地材料、撰写计划，没有安装、联网、生成业务代码或运行真实 grok。本文中的代码块是待实现的接口规范，不是已经实现的代码；本文列出的 pytest 是第 4 步必须实现并运行的验收，不表示现在已经通过。

## S01 目标、材料依据与全局边界

### 1.1 产品目标

为操作者从零搭建独立的本机个人信息分拣器。读取公开博客 RSS/Atom、播客 RSS、YouTube 频道 Atom，主动抓更新；处理文字、已有播客文稿或可获取的纯文本 transcript、视频元数据及有限评论文字；每次运行将尚未成功归档的更新按主题分为「必读 / 推荐阅读 / 不推荐」。同主题提供带证据的多源对照；同一命题出现不同来源的多数和少数立场时，输出可核查的非共识候选。

“每次登录”在 MVP 中指手动执行 `second-brain run`，没有账号登录系统。阅读器是本地静态 HTML，知识层是 Markdown，持久状态是 SQLite。非共识只描述本库材料的分布，不代表市场共识，不判断投资收益，不生成买卖、仓位、目标价或交易指令。HTML 和每份机会页固定显示：**这是多篇材料之间的句子对照，不是买卖建议。**

### 1.2 已读材料与事实优先级

计划对照的是当时的本地材料。私人调研笔记不进入可发布的工作树。`reports/` 已查看，为空。

外部事实仅转述这些材料的记录，**本阶段没有重新核实**。冲突时采用 `REVIEW_GROK_CN.md` 的较后复核结论，同时保留“未确认”；仓库热度、账户状态、价格、版本、配额都不能当成当前实时事实。本文新定的长度、批次数、文件格式、退避参数和测试数据均是**工程设计值或合成夹具**，不是平台保证或操作者已经选择的产品偏好。需要新增外部知识时写 `UNKNOWN`，不补查网络。

一份未确认的第三方视频和口述项目名仍未对上公开仓库。Karpathy [gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) 是方法说明，不能据此认定某个软件仓库就是视频原版。采用 raw、wiki、schema 三层思想，用自己的话写规则，不 vendoring gist 全文。

### 1.3 硬约束

- 不读取、引用、复制或改动其他产品的代码。只在本项目实施。从零搭，不 fork、不 clone 外部仓库，不复制任何外部实现。
- 不复制 GPL、AGPL 或缺完整许可证的仓库代码。自有代码使用 MIT，版权持有人写 second-brain contributors，不在源文件里写 SPDX 头。
- Python + 标准库 SQLite；运行依赖只用 feedparser、PyYAML，测试用 pytest。不引入 Web 框架、向量库、Tauri、Torch、Whisper、yt-dlp、常驻服务、账号系统或监听端口。
- 不下载音频或视频画面，不逐帧，不调用 ffmpeg、Whisper、STT。没有现成文稿的播客保持 `pending_transcript`，不送模型、不投票。
- 不请求 `api.bilibili.com`，不实现 Bilibili 频道适配器，不登录视频网站，不使用 Cookie，不调用 YouTube Data API。
- 测试零出网，禁止真实 grok，禁止下载模型。真实网络与模型默认关闭，只有明确启用且符合 S08、S12 的已有授权条件才可启动。第 4 步的离线开发验收不以真实服务为前提。
- 不读、不打印 `~/.grok/auth.json`、`box-secrets.json`、`gateway.json`、`chrome-cookie*` 或任何凭据值。不从其他项目复制配置。应用不自行登录、刷新、购买、升级、切换计费路径。
- 目录本来就不是 Git 仓库；不 `git init`、`git commit`、`git push`。本文“合并”指调度员验收工作包并接纳文件，不要求 Git 操作。
- 第 3 步只写本文件与 `CHANGES_V2_TO_V3_CN.md`；其他材料只读。后文目录、安装示例和任务均是第 4 步的设计，本阶段不执行。

## S02 架构、技术栈与选择理由

数据流固定为：配置校验 → HTTP/fixture → 纯解析 → 单源事务摄入与 raw 版本 → 待处理队列 → 分批分类 → 当前有效材料的多源对照与非共识 → 原子发布本次阅读页 → 更新归档状态。

运行时单进程、单写入者；多 agent 并行指开发并行，不是多个进程同时写同一数据库。纯解析、分类校验、非共识与页面渲染不直接访问网络。HTTP 只由 `HttpClient` 执行；LLM 只由 `llm/grok_cli.py` 执行；SQL 只由 `Store` 执行；wiki 文件只由 `WikiPublisher` 写。编排层负责连接这些端口。

| 项目 | 实施约束 | 材料依据与证据范围 |
|---|---|---|
| Python | `requires-python >=3.13`，命令使用 `python3` | v2/评审记录本机 3.13.5；本步未重新检测版本 |
| SQLite | 标准库 `sqlite3`，外键、WAL | 评审记录 3.46.1；实际运行时若功能不可用则报配置错误 |
| feedparser | `==6.0.14`，BSD-2-Clause | [既有版本页](https://pypi.org/project/feedparser/6.0.14/)；[材料引用的 LICENSE](https://raw.githubusercontent.com/kurtmckee/feedparser/develop/LICENSE) |
| PyYAML | `==6.0.3`，MIT；仅 `safe_load` | [既有版本页](https://pypi.org/project/PyYAML/6.0.3/) |
| pytest | `==9.2.0`，MIT，dev extra | [既有版本页](https://pypi.org/project/pytest/9.2.0/) |
| grok CLI | 默认路径 `SECOND_BRAIN_GROK`；仅离线假实现参与验收 | 评审记录 1.0.46；[headless 文档](https://docs.x.ai/build/cli/headless-scripting)；当前行为未实测 |
| ffmpeg | 禁止调用 | 评审记录本机 7.1.5；存在不构成使用授权 |

打包采用 `pyproject.toml` 的 setuptools backend，构建工具版本 `UNKNOWN`，不是新增运行依赖；不得在测试中安装或升级。环境没有依赖时如实报告，不能把联网安装放进测试。可以通过 `PYTHONPATH=src python3 -m second_brain.cli` 运行，入口同时声明 `second-brain = second_brain.cli:main`。

保留从零搭的原因：`nashsu/llm_wiki` 是 GPL 桌面 wiki，`TrendRadar` 是 GPL 订阅热点工具，`mstick3/brain` 虽接近需求但绑定其他模型与较重转写链，`open-notebook` 偏资料问答，`lucasastorian/llmwiki` 偏剪藏与定时维护。它们均不完整覆盖本目标。许可证取材料中 LICENSE 正文结论，不把 GitHub `NOASSERTION` 当许可证名称；所有候选只作能力参考。[nashsu](https://github.com/nashsu/llm_wiki/blob/main/LICENSE)、[TrendRadar](https://github.com/sansan0/TrendRadar/blob/master/LICENSE)、[brain](https://github.com/mstick3/brain)、[open-notebook](https://github.com/lfnovo/open-notebook)、[lucasastorian](https://github.com/lucasastorian/llmwiki)。

`sdyckjq-lab/llm-wiki-skill`、`NicholasSpisak/second-brain`、`zhiwehu/second-brain` 在材料中缺完整 LICENSE，故不复制；`khoj` 的 AGPL 代码不并入；WeKnora 在材料中超过既有 200MB clone 上限且有第三方许可例外，本项目根本不 clone。星数、推送时间和其他备选详情仅作历史调研，见 `REPOS_CN.md`；元数据漂移不改变本选择。

## S03 需求到验收的追踪表

以下是先于工作包编写建立的「需求 → 契约 → 工作包 → 验收 → 待决」对照。测试 node 名是合同，第 4 步不得删除测试以迁就实现。

| ID | 需求与边界 | 契约章节 | 工作包 | 最低验收节点 | 待决 |
|---|---|---|---|---|---|
| R01 | 博客/播客/视频公开订阅，固定首次回填范围 | S04、S05、S07 | WP2、WP10、WP11 | `test_backfill_cutoff_is_fixed` | Q01、Q09 |
| R02 | 文字、现成文稿、文本 transcript；音频 pending | S07 | WP3、WP4、WP10 | `test_transcript_retry_after_feed_304` | Q07 |
| R03 | 视频元数据与最多 5 条评论；没有评论不伪造 | S07、S08 | WP5、WP9、WP11 | `test_comments_reach_prompt_not_votes` | Q05 |
| R04 | 增量三分档、改稿重分档、失败可恢复 | S05、S11 | WP1、WP9、WP13 | `test_publish_failure_replays_without_llm` | Q02 |
| R05 | 多源可读对照及失败降级 | S09 | WP8、WP12 | `test_review_failure_preserves_rows` | Q10 |
| R06 | 同命题、按来源计票、黄金集可证伪 | S10 | WP7 | `test_source_repetition_cannot_create_majority` | Q11 |
| R07 | CLI 信封、低 effort、空 cwd、工具关闭策略 | S08 | WP6 | `test_plain_payload_is_rejected` | Q03 |
| R08 | 分计费路径、usage、不自动重试、调用预算 | S08 | WP6、WP13、WP14 | `test_budget_stops_before_spawn` | Q04 |
| R09 | URL/重定向守卫、限流、日志脱敏 | S06 | WP2、WP14 | `test_redirect_to_private_is_blocked` | Q12 |
| R10 | 全测试禁止网络及真实服务 | S12、S14 | WP0、WP14 | `test_network_tripwire` | 无 |
| R11 | 冻结接口、无环依赖、文件独占 | S04、S13 | WP0 至 WP14 | `test_import_layers` | 无 |
| R12 | 本地 HTML、三分档、来源可查 | S09、S11、S12 | WP12 至 WP14 | `test_digest_escapes_untrusted_text` | 无 |
| R13 | 独立从零搭、许可、MVP 范围 | S01、S02、S15 | WP0、WP14 | `test_scope_contract` | Q06、Q08 |

## S04 冻结目录、类型、错误与端口

### 4.1 文件布局与写入所有者

```text
仓库根目录/
  已有计划、评审、提示、research_notes/       只读
  AGENTS.md .gitignore pyproject.toml        WP0
  README.md                                WP14（WP0 不建占位）
  config/
    sources.example.yaml                   WP0
    preferences.example.md                 WP0
    topic_aliases.example.yaml              WP0
    runtime.example.yaml                   WP0
  src/second_brain/
    __init__.py models.py errors.py ports.py WP0
    store.py storage/raw.py                 WP1
    urlguard.py http.py                     WP2
    parse/text.py                          WP3
    parse/podcast.py                       WP4
    parse/video.py                         WP5
    llm/grok_cli.py                        WP6
    consensus/minority.py                  WP7
    review/multi_source.py review/schema.json WP8
    classify/buckets.py classify/schema.json WP9
    sources/rss.py sources/ingest.py        WP10
    sources/youtube.py                     WP11
    wiki/compile.py                        WP12
    run.py                                WP13
    config.py cli.py                       WP14
  tests/
    conftest.py test_models.py test_boundaries.py WP0
    test_store.py test_http.py test_text.py test_podcast.py test_video.py
    test_grok_cli.py test_minority.py test_review.py test_buckets.py
    test_rss.py test_youtube.py test_wiki.py test_run.py test_cli.py
    fakes/grok                             WP6
    fixtures/{rss,podcast,video,youtube,consensus,review}/
  raw/<item_id>/<analysis_hash>.md          运行时，WP1 的 RawArchive 唯一写
  data/second_brain.sqlite                 运行时，Store 唯一写
  data/run.lock                           运行时，WP13 持有，不删除
  .grok-sandbox/                          运行时空 cwd
  .grok-tmp/                              运行时临时 prompt/schema
  wiki/
    index.md log.md
    digest/<run_id>.html
    snapshots/<run_id>/{manifest.json,topics/,opportunities/}
```

所有包目录的空 `__init__.py` 由 WP0 一次建立。运行时的 `raw/`、`wiki/`、`data/`、`.grok-sandbox/`、`.grok-tmp/`、`.venv/`、真实 `config/{sources.yaml,preferences.md,topic_aliases.yaml,runtime.yaml}` 列入 `.gitignore`，即使当前没有 Git 也保留该规则。夹具子目录独占：rss 与 podcast 为 WP10，video 为 WP5，youtube 为 WP11，consensus 为 WP7，review 为 WP8；其他测试在 `tmp_path` 构造数据。

### 4.2 基础值与哈希

以下类型均在 `models.py` 定义为 `@dataclass(frozen=True)`，集合默认 tuple；序列化只用 JSON 基础类型，不 pickle。表列与模型字段对应，显式标为派生字段的除外。

```python
Modality = Literal["text", "podcast", "video"]
Bucket = Literal["must", "recommend", "skip"]
Stance = Literal["for", "against"]
SourceKind = Literal["rss", "youtube_rss", "podcast_rss", "file"]
FeedFormat = Literal["rss", "podcast_rss", "youtube_rss", "video_json", "podcast_json"]
TranscriptStatus = Literal["ready", "pending_transcript", "not_applicable"]
RunStatus = Literal["running", "publishing", "success", "partial", "failed"]
EvidenceKind = Literal["title", "body", "comment"]
AssessmentStatus = Literal["opportunity", "insufficient", "single_source",
                           "disputed", "one_sided", "conflicted"]

def item_id(source_id: str, guid: str) -> str: ...
def canonical_url(url: str) -> str: ...
def normalize_key(text: str) -> str: ...
def resolve_alias(key: str, aliases: Mapping[str, str]) -> str: ...
def content_hash(title: str, body_raw: str) -> str: ...
def analysis_hash(item: Item, asset: Asset) -> str: ...
def claim_id(claim: Claim) -> str: ...
def topic_slug(topic: str) -> str: ...
def claim_slug(topic: str, claim_key: str) -> str: ...
```

- `item_id = sha256((source_id + "\n" + guid).encode()).hexdigest()[:32]`；拒绝 source_id/guid 中的换行、NUL 和空值，避免拼接歧义。source_id 限 ASCII 字母、数字、`_`、`-`，长度 1 至 64，此为工程值。哈希撞到不同原始键时报 `IdentityCollision`，不能覆盖。
- `canonical_url` 首先要求 http/https、有效 host、无 userinfo，再小写/IDNA 化 host，移除 fragment、默认端口、查询键 `utm_*`/`fbclid`/`gclid`（键名比较不区分大小写），保留其他参数的顺序、路径大小写和尾斜线。非法条目链接返回 SchemaError，由 parser 记 EntryIssue。不做 HTTP 抓取、跨域跳转推断或把 http 合并成 https。`guard_public_url` 是独立入口校验，不能由 canonicalization 替代。
- `normalize_key` = Unicode NFKC → strip → casefold → 连续空白压成单空格。不是去掉全部词间空格；不自动翻译或把语义相近命题合并。alias 先按同样规则规范化，再沿映射求终点；循环、空目标、相同输入有不同目标时报 `ConfigError`。
- `content_hash` 保留 v2 的 `sha256(title + "\n" + body_raw)`，用 UTF-8，完整 64 位 hex；title 先统一为无换行的单行文字。`analysis_hash` 为固定 JSON 编码（排序键、紧凑分隔、UTF-8）的 `{content_hash, body_plain, modality, channel, transcript_status, transcript_url, transcript_type, top_comments}` 的 SHA256；评论用 S08 中真正会入 prompt 的截断版本。播放/点赞计数和 fetched_at 不在此哈希中，计数刷新不触发重分档。
- `claim_id` 对 `{item_id,analysis_hash,topic,claim_key,stance,evidence_kind,comment_index,evidence_quote}` 固定 JSON 求 SHA256 前 32 位。重复同一证据不增加票数。
- `topic_slug=sha256(normalize_key(topic))[:12]`；`claim_slug=sha256(JSON([topic,claim_key]))[:12]`。后者包含规范化后的主题，修复跨主题相同 key 的覆盖。manifest 保存完整键，发现短 slug 碰撞必须报错，禁止覆盖。
- 时间输入须为有时区 ISO-8601，内部统一 UTC，持久化固定为 `YYYY-MM-DDTHH:MM:SS.ffffffZ`。外部无日期记 None，不偷用抓取时间冒充发布时间。`run_id` 由注入的 ID 工厂产生，生产用 UUID hex，测试给固定值；不单靠秒级时间当唯一键。

### 4.3 数据记录的完整字段

下列 `?` 表示 `T | None`，未列默认值的字段必须显式提供；这是一份字段清单，不允许实现者自行换返回形状。

| 类型 | 冻结字段 |
|---|---|
| `Source` | `id:str, kind:SourceKind, url:str, title:str, backfill_days:int=14, file_format:FeedFormat?=None, transcript_hosts:tuple[str,...]=()` |
| `SourceState` | `source_id:str, initial_since_at:str, etag:str?, last_modified:str?, last_fetched_at:str?, last_http_status:int?` |
| `Item` | `id:str, source_id:str, guid:str, url:str, canonical_url:str, title:str, published_at:str?, fetched_at:str, content_hash:str, analysis_hash:str, modality:Modality, body_raw:str, body_plain:str, duplicate_of:str?=None` |
| `Asset` | `item_id:str, transcript_status:TranscriptStatus, channel:str="", audio_url:str?=None, transcript_url:str?=None, transcript_type:str?=None, transcript_etag:str?=None, transcript_last_modified:str?=None, transcript_last_attempt_at:str?=None, like_count:int?=None, comment_count:int?=None, view_count:int?=None, top_comments:tuple[str,...]=()` |
| `IngestRecord` | `item:Item, asset:Asset`；哈希由 parser 组装后统一计算，Store 再验证，不信任调用方传入的哈希 |
| `EntryIssue` | `entry_key_hash:str, code:str`；不放原文或原始 URL |
| `FeedBatch` | `records:tuple[IngestRecord,...], issues:tuple[EntryIssue,...]=()` |
| `FetchResult` | `inserted:int, updated:int, unchanged:int, skipped_old:int=0, skipped_dup:int=0, held_date:int=0, invalid:int=0`；前三者互斥，后四者为额外诊断，duplicate 也可能计入 inserted |
| `ParsedPodcast` | `body_plain:str, transcript_status:TranscriptStatus` |
| `VideoMeta` | `title:str, channel:str, like_count:int?, comment_count:int?, view_count:int?, comments:tuple[str,...], transcript_status:Literal["not_applicable"]="not_applicable"` |
| `Classification` | `item_id:str, run_id:str, topic:str, bucket:Bucket, reason:str, analysis_hash:str, policy_hash:str, classified_at:str, published_run_id:str?=None` |
| `Claim` | `id:str, item_id:str, source_id:str, analysis_hash:str, topic:str, claim_key:str, sentence:str, stance:Stance, evidence_kind:EvidenceKind, evidence_quote:str, comment_index:int?=None` |
| `ClassifiedItem` | `id:str, source_id:str, topic:str, url:str, title:str, bucket:Bucket, reason:str, published_at:str?, analysis_hash:str, policy_hash:str, comments:tuple[str,...]=(), claims:tuple[Claim,...]=()`；Store 视图联结有效 claims，供短评引用 |
| `Usage` | `input_tokens:int?, output_tokens:int?, reasoning_tokens:int?, cache_read_input_tokens:int?, cache_creation_input_tokens:int?, total_cost_usd:str?`；金额用十进制字符串，不用浮点，不存在不填零 |
| `CompletionResult` | `payload:dict, usage:Usage, stop_reason:str?, billing_path:Literal["test","session","api","unknown"]` |
| `BatchOutcome` | `batch_id:str, item_ids:tuple[str,...], status:Literal["success","invalid","unavailable","budget_skipped"], error_code:str?` |
| `ClassificationResult` | `classifications:tuple[Classification,...], claims:tuple[Claim,...], batches:tuple[BatchOutcome,...]` |
| `Opportunity` | `topic:str, claim_key:str, minority_sentence:str, minority_stance:Stance, minority_item_ids:tuple[str,...], majority_item_ids:tuple[str,...], minority_source_ids:tuple[str,...], majority_source_ids:tuple[str,...], majority_count:int, minority_count:int, n_sources:int, falsify_hint:str`；count 计来源票，非文章数 |
| `ClaimAssessment` | `topic:str, claim_key:str, status:AssessmentStatus, n_items:int, n_sources:int, n_for:int, n_against:int, excluded_source_ids:tuple[str,...]` |
| `ConsensusResult` | `opportunities:tuple[Opportunity,...], assessments:tuple[ClaimAssessment,...]` |
| `ReviewSentence` | `kind:Literal["consensus","difference","audience","limit"], text:str, item_ids:tuple[str,...]` |
| `TopicReview` | `topic:str, rows:tuple[ClassifiedItem,...], sentences:tuple[ReviewSentence,...], summary_status:Literal["ok","disabled","failed","budget_skipped","single_source","not_requested"]` |
| `HttpResponse` | `status:int, body:bytes, headers:Mapping[str,str], url:str`；headers 键统一小写，ETag、Content-Type、Retry-After 从此读取 |
| `RateState` | `host:str, blocked_until:str?, consecutive_failures:int=0, last_request_at:str?=None` |
| `FetchEvent` | `source_id:str?, host:str, url_redacted:str, url_hash:str, status:int?, at:str, error_code:str?, retry_after_s:float?` |
| `LlmEvent` | `call_id:str, run_id:str, purpose:str, at:str, status:str, error_code:str?, billing_path:str, usage:Usage?` |
| `Publication` | `run_id:str, html_path:str, manifest_path:str, classified_refs:tuple[tuple[str,str],...]`；refs 为 item_id、analysis_hash |
| `RunReport` | `run_id:str, status:RunStatus, inserted:int, updated:int, classified:int, pending:int, deferred:int, failed_source_ids:tuple[str,...], failed_item_ids:tuple[str,...], html_path:str?, warnings:tuple[str,...]` |

`RuntimePolicy` 冻结为：`offline:bool=True, live_authorized:bool=False, billing_path:test|session|api|unknown="test", billing_path_verified:bool=False, tool_policy_verified:bool=False, review_llm:bool=True, max_classify_batches:int=3, max_review_batches:int=1, max_transcript_fetches:int=20`。这些是可配置的工程预算，不是已批准的付费额度。`KeyPolicy` 含 `topic_aliases:Mapping[str,str]`、`claim_aliases:Mapping[str,str]`，默认空。`AppConfig` 含 `sources:tuple[Source,...], preferences:str, keys:KeyPolicy, runtime:RuntimePolicy`。

运行预算和backfill_days必须为非负整数且不接受bool；预算0表示该类本次不发起调用，短评关闭优先于其预算。所有bool字段只接受真正布尔值，不能把字符串"false"当真。JSON/SQLite转换层不得改变这些含义。

所有字符截断使用同一个 WP0 工具 `prompt_fields(item:Item, asset:Asset)->dict`，返回 `{id,source_id,title,modality,body,channel,audience,truncated}`。body是body_plain的截断文本，audience是截断后的评论数组；truncated对象分别记录title/body布尔值和每条audience布尔数组，标记不混入可引用正文。channel最多300字符，超出也在truncated中标记。analysis_hash、RawArchive 与 prompt 中的评论使用同一 audience 值，分类器不得二次拼接未截断内容。parser可以暂以空analysis_hash构造Item，再用该纯函数/哈希计算并replace为最终IngestRecord；空hash永远不能提交Store。

### 4.4 错误与控制流

`errors.py` 冻结以下异常。消息只含固定说明和 code，不包含正文、stderr、凭据或 URL query。所有网络/模型错误都要转成这些类型，禁止 `except Exception: pass`。

| 异常 | 基类与固定属性 | 调用方行为 |
|---|---|---|
| `ConfigError` | ValueError；`code:str` | 启动前退出，未发请求 |
| `UnsafeUrl` | ValueError；`code:str` | 拒绝该源或 transcript；日志只写安全码 |
| `FramesNotAllowed` | ValueError；`code="frames_not_allowed"` | 拒绝条目，不读取媒体 |
| `SchemaError` | ValueError；`code:str` | 单条摄入拒绝或整分类批失败，依发生层处理 |
| `InvalidBucket` / `InvalidStance` | SchemaError 子类 | 分档整批拒绝，不偷改成 skip |
| `IdentityCollision` | RuntimeError；`code="identity_collision"` | 停止本次写入，保留现存数据 |
| `FetchError` | RuntimeError；`code:str, status:int?` | 当前源失败，其余源继续 |
| `RateLimited` | FetchError；`host:str,retry_after_s:float` | 该 host 跳过，不在本次 run 立即重试 |
| `GrokUnavailable` | RuntimeError；`code:str,usage:Usage?=None` | 停止本次后续模型调用，保留未完成队列；异常可携带已清洗的usage用于记账 |
| `BudgetExceeded` | RuntimeError；`purpose:str` | 不启动进程，余项记 deferred |
| `StorageError` / `PublishError` | RuntimeError；`code:str` | 不推进归档游标；可重放已存分档 |
| `ConcurrentRun` | RuntimeError；`code="run_locked"` | 未抓取即退出，不破坏已有锁 |

分类 schema 失败仅影响该批，后续批继续；grok 不可用、认证/额度类失败或超时不盲目重试。S11 冻结 `run_once` 如何把这些情况映射到报告和退出码。

### 4.5 端口和关键函数签名

`ports.py` 只依赖 models/errors/标准库，用 `Protocol` 冻结以下端口。WP0 提供定义和合成 stub 测试，后包实现，不通过导入彼此实现来共享接口。

下列代码块的Protocol类归ports.py；其余def是各所有者模块的公开签名索引，不在ports.py另建业务占位实现。Store等具体类的类型注解只出现在其消费者模块，不能为了解析注解让ports反向导入Store。模型与签名实现使用延迟注解处理前向引用，不能因此引入运行时依赖环。

```python
class Clock(Protocol):
    def now(self) -> datetime: ...  # aware UTC
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...

class HttpTransport(Protocol):
    def request(self, url: str, *, headers: Mapping[str, str], timeout_s: int,
                max_bytes: int, purpose: str) -> HttpResponse: ...
    # 单跳、不自动重定向；必须在读 body 前检查类型/大小，见 S06

class HttpStateStore(Protocol):
    def get_rate_state(self, host: str) -> RateState: ...
    def put_rate_state(self, state: RateState) -> None: ...
    def append_fetch_event(self, event: FetchEvent) -> None: ...

class CompletionFn(Protocol):
    def __call__(self, prompt: str, *, schema: dict, purpose: str) -> CompletionResult: ...

class FixtureReader(Protocol):
    def __call__(self, relative_uri: str) -> bytes: ...

def parse_text(title: str, html: str) -> tuple[str, Literal["text"]]: ...
def parse_podcast(title: str, transcript: str | None, audio_url: str | None,
                  transcript_url: str | None = None, *, transcript_format: str = "plain") -> ParsedPodcast: ...
def parse_video(payload: Mapping[str, object]) -> VideoMeta: ...
def parse_rss(source: Source, xml_bytes: bytes, *, now: str) -> FeedBatch: ...
def parse_youtube_feed(source: Source, xml_bytes: bytes, *, now: str) -> FeedBatch: ...
def parse_local_records(source: Source, payload: bytes, *, now: str) -> FeedBatch: ...

def persist_feed(store: Store, source: Source, batch: FeedBatch, *, now: str,
                 raw_dir: Path, response: HttpResponse | None) -> FetchResult: ...
def hydrate_transcripts(store: Store, *, http: HttpClient | None,
                        fixture_reader: FixtureReader, sources: tuple[Source, ...],
                        now: str, raw_dir: Path, limit: int) -> tuple[int, tuple[EntryIssue, ...]]: ...

def classify_items(items: Sequence[Item], assets: Mapping[str, Asset], *,
                   run_id: str, now: str, preferences: str, keys: KeyPolicy,
                   known_claim_keys: tuple[str, ...], complete_fn: CompletionFn) -> ClassificationResult: ...
def build_topic_reviews(items: Sequence[ClassifiedItem]) -> tuple[TopicReview, ...]: ...
def enrich_topic_reviews(reviews: Sequence[TopicReview], *, changed_topics: frozenset[str],
                         complete_fn: CompletionFn | None) -> tuple[TopicReview, ...]: ...
def find_minority(claims: Sequence[Claim]) -> ConsensusResult: ...

def complete(prompt: str, *, binary: str, model: str = "grok-4.6", schema: dict,
             cwd: Path, temp_dir: Path, timeout_s: int = 120,
             billing_path: str = "unknown") -> CompletionResult: ...

def run_once(store: Store, *, config: AppConfig, clock: Clock, run_id_factory: Callable[[], str],
             complete_fn: CompletionFn, wiki_dir: Path, raw_dir: Path,
             lock_path: Path, http: HttpClient | None, fixture_reader: FixtureReader) -> RunReport: ...
def load_config(config_dir: Path, *, offline: bool) -> AppConfig: ...
def main(argv: Sequence[str] | None = None) -> int: ...
```

`HttpClient(transport:HttpTransport, clock:Clock, state_store:HttpStateStore, *, min_interval_s:float=1.0)` 的唯一取数方法是 `fetch(url:str, *, source_id:str|None=None, etag:str|None=None, last_modified:str|None=None, purpose:Literal["feed","transcript"]="feed", allowed_hosts:frozenset[str]|None=None)->HttpResponse`。必须提供 state_store，不能用可选 store 绕过限流持久化。

`WikiPublisher(wiki_dir:Path)` 提供 `stage(...)->Publication`、`publish(publication:Publication)->None`、`append_events(run_id:str, events:Sequence[LlmEvent|FetchEvent])->None`。`stage` 的完整参数为 `run_id, now, delta:Sequence[ClassifiedItem], reviews:Sequence[TopicReview], consensus:ConsensusResult, pending:Sequence[Item], unclassified:Sequence[Item], held_date:Sequence[Item], warnings:Sequence[str]`，均为 keyword-only。只有它可以写 wiki；WP8 不再直接写 Markdown，WP13 不追加他包的页面。

所有构造、函数和 Schema 的实现以本节及后文约束共同为准。需要变更必须交 WP0 所有者登记旧值、新值、受影响测试，再同步所有消费者；禁止悄悄增加第二套 `VideoMeta`、临时 tuple 返回值或跨包私有 SQL。

## S05 数据库、增量、去重和可恢复归档

### 5.1 SQLite 逻辑 Schema

WP1 是全部 DDL、迁移和 SQL 的唯一所有者。空库创建 `schema_version="3"`；当前目录没有需要迁移的已实现 v2 数据库。发现非空未知版本时抛 `ConfigError(code="unsupported_schema")`，不得 DROP、清空或猜迁移。迁移函数重复运行幂等。

每个连接执行并检查 `PRAGMA foreign_keys=ON`、`journal_mode=WAL`，设置 `busy_timeout=5000` 毫秒，这是工程值。网络、LLM 和页面写入期间不持有 SQLite 写事务。字段类型：UTC/枚举/JSON/hash 为 TEXT，计数/布尔为 INTEGER；JSON 由标准库编码/校验，不依赖额外 SQLite 扩展。

| 表 | 列、约束与索引 |
|---|---|
| `meta` | `key TEXT PRIMARY KEY,value TEXT`；`schema_version`、可空 `last_classified_at` |
| `sources` | Source 全字段，`transcript_hosts_json` 替代 tuple；SourceState 除 source_id 的全部字段；`enabled INTEGER NOT NULL`。id 主键，kind/backfill_days/布尔有 CHECK |
| `items` | Item 全字段；id 主键，source_id 外键，`UNIQUE(source_id,guid)`、`UNIQUE(id,source_id)`；duplicate_of 自外键，禁止自指。canonical_url 非空；fetched_at、canonical_url 索引 |
| `items` 的辅助约束 | `UNIQUE(canonical_url) WHERE duplicate_of IS NULL`；相同 canonical_url 可以留多条来源记录，但只有一个主条目；所有 duplicate 指向该组主条目，不允许链或环 |
| `assets` | Asset 全字段，item_id 主键/外键，comments 存 `top_comments_json`；计数 nullable 且非负；每个 Item 必须有 Asset |
| `classifications` | Classification 全字段；item_id 主键/外键，run_id 与 published_run_id 外键到 runs；bucket CHECK；`published_run_id`、run_id 索引 |
| `claims` | Claim 全字段；id 主键，`(item_id,source_id)` 外键到 items；stance/evidence_kind CHECK；索引 `(topic,claim_key)`、item_id |
| `runs` | `id PRIMARY KEY,started_at,since_at,finished_at,status,publication_json,summary_json`；status CHECK；publication_json 为恢复发布所需相对路径、文件校验和、分类引用，不含凭据 |
| `run_items` | `run_id,item_id,analysis_hash,state,error_code`；前三列联合主键，前两列外键；state 取 `selected/classified/invalid/unavailable/deferred/published`，不是分类历史副本 |
| `raw_revisions` | `item_id,analysis_hash,path,sha256,created_at`；前两列主键，path UNIQUE；用于核对不可变 raw 与当前版本的对应 |
| `fetch_log` | `id INTEGER PRIMARY KEY` + FetchEvent 全字段，source_id nullable 外键；不用原始 URL 存 `url` |
| `rate_limits` | RateState 全字段，host 主键；失败次数非负 |
| `llm_calls` | LlmEvent 全字段，call_id 主键，run_id 外键；Usage 展平为 nullable 列；同 call_id 不重复计费或追加日志 |

Item 与 Asset 同事务写入；分档和该条全部 claims 同事务替换。未知 bucket/stance、NaN、负计数、布尔冒充整数、重复 id、孤儿外键均拒绝。`get_current_claims()` 只返回主条目、启用来源、日期有效、文稿可用、classification.analysis_hash 等于 item.analysis_hash 的 claims，不把陈旧结果喂给算法。

save_batch把success映射为run_items.classified，invalid/unavailable保持同名，budget_skipped映射deferred。finish_publication仅把本次run对应引用置published（旧未发布分类补发时可新增本次published引用），不重写其他run的失败经历；是否尚未发布始终以classifications.published_run_id为准。

### 5.2 Store 完整公共接口

除 S04 的 HttpStateStore 三个方法外，冻结为：

```python
Store.connect(path: str) -> Store
Store.migrate() -> None
Store.close() -> None
Store.get_meta(key: str) -> str | None
Store.set_meta(key: str, value: str) -> None
Store.sync_sources(sources: Sequence[Source], *, now: str, raw_dir: Path) -> None
Store.get_source_state(source_id: str) -> SourceState
Store.ingest_feed(source: Source, batch: FeedBatch, *, now: str, raw_dir: Path,
                  response: HttpResponse | None) -> FetchResult
Store.accept_not_modified(source_id: str, *, now: str, response: HttpResponse) -> None
Store.transcript_candidates(*, now: str) -> tuple[IngestRecord, ...]
Store.update_transcript(item_id: str, *, expected_hash: str, text: str,
                        response: HttpResponse, now: str, raw_dir: Path) -> None
Store.record_transcript_attempt(item_id: str, *, now: str) -> None
Store.items_for_classify(*, now: str) -> tuple[Item, ...]
Store.assets_for(item_ids: Sequence[str]) -> dict[str, Asset]
Store.pending_transcript_items(*, now: str) -> tuple[Item, ...]
Store.held_date_items(*, now: str) -> tuple[Item, ...]
Store.begin_run(run_id: str, *, started_at: str) -> None
Store.select_run_items(run_id: str, items: Sequence[Item]) -> None
Store.save_batch(outcome: BatchOutcome, classifications: Sequence[Classification],
                 claims: Sequence[Claim], *, run_id: str) -> None
Store.get_current_classified(*, now: str) -> tuple[ClassifiedItem, ...]
Store.get_current_claims(*, now: str) -> tuple[Claim, ...]
Store.get_unpublished_classified(*, now: str) -> tuple[ClassifiedItem, ...]
Store.get_changed_topics(run_id: str) -> frozenset[str]
Store.known_claim_keys() -> tuple[str, ...]
Store.append_llm_event(event: LlmEvent) -> None
Store.events_for_run(run_id: str) -> tuple[LlmEvent | FetchEvent, ...]
Store.mark_publishing(publication: Publication, *, status_after_publish: RunStatus,
                      summary: Mapping[str, object]) -> None
Store.finish_publication(publication: Publication, *, finished_at: str) -> None
Store.unfinished_publications() -> tuple[Publication, ...]
Store.fail_run(run_id: str, *, finished_at: str, code: str) -> None
Store.recover_interrupted_runs(*, now: str) -> None
Store.pending_log_runs() -> tuple[str, ...]
Store.mark_log_written(run_id: str) -> None
```

`events_for_run` 的 FetchEvent 关联由 `fetch_log` 增加 `run_id` nullable 外键完成。Store本身实现HttpStateStore；begin_run在当前连接绑定active_run_id，append_fetch_event自动附上它，close时清理；没有begin_run的HTTP单元测试可为空。CLI与run集成测试必须把同一个Store注入HttpClient，不能使用另一个不记日志的state stub。`get_changed_topics` 从本次版本失效、资格变化（含日期/主项/启用状态）和分档写入登记获取，登记存 runs.summary_json 的 `changed_topics`，仅 Store 修改。`get_unpublished_classified` 必须含之前 run 已分类但未发布的当前版本，不能只查本次 run_id。`get_current_classified` 的comments必须是prompt_fields的audience，claims必须是同版本已验证记录，不让短评重新读取未经截断的评论。

`RawArchive(raw_dir:Path).put(record:IngestRecord)->tuple[str,str]` 返回相对路径和 SHA256。Store 在数据库提交前调用它，WP10/11/13 不自己写 raw。

### 5.3 首次回填和两个游标

`sources.initial_since_at` 在首次注册时固定为 `now-backfill_days`，默认14天。后续不滚动这个下界，避免本地时间过滤丢掉停机后仍由feed返回的中间更新。新的guid：发布时间早于该下界才skipped_old；已知guid的改稿不受回填下界限制。配置中的backfill_days改动不追溯重置已注册源下界；更改范围属Q09，不能隐式重新灌入历史。源是否还返回停机期间全部旧条目为UNKNOWN，本期不实现历史补抓，不能承诺feed未返回内容的完整性。

外部发布时间缺失或无法解析时仍保存条目，`published_at=None`，放「日期待确认」，不分类。未来时间的条目同样暂缓，待后续 run 的 now 到达再进入队列，不改写其发布时间。这样既不把未知日期伪装为“刚发布”，也不永久丢数据。合法发布时间在首次范围内的空正文文字/视频可以按标题分档，但必须提示证据不足且不凭空生成 claims。

每源 ETag/Last-Modified/last_fetched_at 管已成功摄入的 feed。200 必须解析及数据库提交成功后才换 validator；格式错误、部分条目错误时保留旧 validator，使下次可重取。304 不解析空 body，不清 validator，无新正文；之前待转写、待分类、待发布的队列仍继续。304 中缺失的新头不能覆盖旧值为 None。

全局 `last_classified_at` 是**完整成功归档**的审计时间，不是 SQL 过滤条件。筛选依据是“启用源 + 当前主条目 + 日期有效 + 非 pending + 当前 analysis_hash 没有有效分档”。来源抓取失败、任何应分类条目失败/预算延后或发布失败，本次不得推进它。pending、日期待确认本身不阻断成功，页面必须单列；短评失败也不阻断。成功分档但尚未发布的条目，走发布补偿，不重新调用模型。

### 5.4 去重与版本失效

1. 同源同 guid 定位同 Item；没有 guid 用规范化后的公开 link，二者都缺失则拒绝并记 EntryIssue。放弃标题作为 guid 回退，避免同名节目互相覆盖。
2. 跨源相同 canonical_url 留 alias Item，`duplicate_of` 指向首次成功入库的主条目。源按 source_id、条目按 guid 排序摄入，保证同一输入顺序可复现。重复项不分类、不投票、不写第二份 raw。
3. 主条目的当前 URL 改到另一组或来源停用时，原组从仍启用的别名中取 item_id 最小者升为主条目，其他指针直接重指；必要时创建其 raw 版本。新组已有主条目则原条目降为 alias，删除其分档/claims，登记受影响主题。事务中先解除旧主的唯一索引占位，再提升新主；不能生成 self-reference。没有启用成员的组只存档，不参与输出。
4. `content_hash`、正文或入 prompt 评论/文稿状态变化使 analysis_hash 改变，删除当前分档/claims，登记旧主题失效；即使 fetched_at 小于全局游标，也必须重新分档。仅 views/like/comment_count 刷新更新 Asset，不重跑模型。
5. 同 guid 的 feed 不得用旧的节目简介覆盖已下载的 transcript。候选只是 pending/简介而已存 ready、transcript URL 未变时，保留成功文稿再计算哈希；候选本身明确提供 ready 文稿时按新文稿更新。transcript URL 改变且无明确新文稿则清现有文稿、置 pending，旧 raw 版本保留。没有更换 URL 的 ready transcript 暂不定期强制重拉，见 Q07。
6. URL 不同的转载、同作者多账号和共同引用一个来源，不能凭 canonical URL 自动识别。来源票只是 source_id 票，不声称统计独立；该限制进入机会页。后续人工来源分组属于 Q11，本期不加向量去重。

`fetched_at` 记录当前内容版本首次成功摄入时刻；unchanged 不刷新它，计数变化也不刷新。日期、URL、source enabled 等资格变化由 Store 重新判断，不依赖这个字段捕捉。

### 5.5 raw 与崩溃恢复

`raw/<item_id>/<analysis_hash>.md` 永不覆盖。内容是确定性序列化的标题、正文、频道、入 prompt 评论与 transcript 来源标记，不含会独立变化的互动计数；同路径重写前只核对字节，完全相同则不写。先用同目录临时文件写全并 fsync，再原子放置最终路径；若已存在不同字节报 IdentityCollision。目录或目标为逃逸 symlink 则拒绝。

文件成功、SQLite 事务失败可能留下尚未引用的 raw，这是允许的只追加孤儿，不自动删除。重试按同 hash 复用。SQLite 只能提交已经存在且校验通过的 raw_revisions。重复摄入不改已有 raw 的字节和 mtime。分档历史不新增一张完整历史表，保留 run_id、run_items 的状态和发布快照；当前行因改稿可替换，但旧快照标明当时版本，不能当成当前观点。

## S06 HTTP、限流、失败、URL 与隐私

### 6.1 URL 与传输边界

`guard_public_url(url)->None` 拒绝非 http/https、缺 host、userinfo、控制字符、非法端口、反斜线混淆、`localhost` 及其子域、`metadata.google.internal`。host 比较前大小写折叠并去末尾点。字面 IPv4/IPv6 用 `ipaddress` 判断，拒绝任何非全局单播地址，包括 v2 的私网/loopback/link-local、IPv6 ULA/link-local/unspecified、多播、IPv4-mapped 私网。拒绝解析器不能标准解释的纯数字/十六进制/短 IPv4 表达，不能把 `127.1` 当普通域名。`https://10.example.com/feed` 仍允许通过语法检查。

保留 v2 的范围：**本期不进行 DNS 解析验证或 IP 固定连接**。因此这不是完整 SSRF 防护，不能阻止公网形状域名解析到内网或 DNS rebinding。生产只接受操作者明确配置的公开源，不提供任意 URL 接口。动态 transcript 只允许源 host 与 `Source.transcript_hosts` 列出的 host；空列表表示只允许 feed host，不自动信任 feed 给出的任意外域。此限制和剩余风险必须写入 README，Q12 决定后续是否扩大边界。

生产 urllib transport 必须关闭自动重定向和环境代理继承，不使用 CookieJar，发固定 User-Agent `SecondBrain/0.3 (local; personal)`、`Accept-Encoding: identity`。`api.bilibili.com` 在进入 transport 前无条件拒绝。只用 GET；Feed parser 永远接收 bytes，不能把 URL 交给 feedparser 自动取网。

重定向由 HttpClient 显式处理，最多 3 跳（工程值），每跳重新做 URL、allowed_hosts 和 host 限流检查；相对 Location 用当前 URL 解析。HTTPS 降级 HTTP、循环和缺 Location 拒绝。跨 host 不转发 ETag/Last-Modified 等条件头。不发 Authorization/Cookie，自然也不得跨域转发。最终响应 URL 与每一跳都记录脱敏事件；不能只在取完后检查最终 URL。

请求超时 20 秒；feed 和 transcript 的最大 body 都为 2 MiB，这是内存/带宽保护值，不是平台限额。读取分块累加计数，声明 Content-Length 超限可提前拒绝，不信任缺失或伪造的头。拒绝非 identity 压缩编码，避免无限解压；XML 出现 DTD/ENTITY 声明直接拒绝，不解析外部实体。feed 只收 XML 候选；transcript 在读 body 前检查 S07 类型，媒体 Content-Type 立即拒绝，不落媒体文件。

### 6.2 host 状态机

所有调度时间使用注入 Clock，测试不用真实 sleep。每 host 最小间隔默认 1 秒；为 `api.bilibili.com`、`www.bilibili.com`、`api.bilibili.tv` 保留 2 秒策略表，仅供未来复用，**不解除本期 Bilibili 禁止请求规则**。这些秒数是本地策略，不是官方 QPS。

- `blocked_until > now`：返回 RateLimited，transport 调用次数为零。未 blocked 时，等待到 last_request_at + interval，再发请求。
- 200、304：失败次数归零并清 blocked_until；last_request_at 持久化。
- 429/403：失败次数先加一，解析 Retry-After 的非负秒数或 HTTP 日期；无效则 `min(60, 2**failures)` 秒。blocked_until 取退避时间与服务端 Retry-After 的较晚值；不能把长 Retry-After 裁成更短。连续第 3 次失败及以后至少熔断 900 秒。
- 408、5xx、DNS/连接/超时错误：同样累加失败与本地退避，跨三次的熔断规则相同。其他 4xx 本次源失败，不在 run 内重试；单独记录诊断。
- Bilibili 社区错误码 `{-412,-799,-509,-352,-503}` 仅由纯函数 `rate_limit_code(payload)->bool` 检测，其结果走相同限流状态机。该检测器用合成 JSON 验收，不为此请求真实 Bilibili。官方业务码承诺、QPS 仍未确认。材料引用：[社区整理](https://github.com/fython/BilibiliAPIDocs)、[开放平台](https://open.bilibili.com/)。
- 同一次 run 对同一失败请求不自动重试，不因 403 切 IP、Cookie 或其他账号。A host 失败不阻断 B host；下次 run 从持久状态继续。

禁止记录完整响应 body 或原始异常文本。FetchEvent 包含 at、host、status、安全 code、retry_after_s、脱敏 URL 和 URL 哈希；原始 URL 只存在本地受保护的源/条目数据。`url_redacted` 固定为 `scheme://host[:port]/…`，不含路径与 query；调试定位依赖 source_id 与 url_hash。query 中出现 `token/access_token/api_key/key/password/signature/sig` 等凭据键时配置即拒绝，这不能保证识别所有私有 URL，源必须是公开内容。

### 6.3 文件读取、凭据与 prompt 注入

`kind=file` 是独立夹具入口，必须给 file_format；URL 仅允许 `file:tests/fixtures/<relative>`。`FixtureReader` resolve 后必须位于本项目 tests/fixtures 内，拒绝绝对路径、`..` 逃逸、网络 file host 和 symlink 逃逸。不让 HTTP 层接受 file scheme。真实个人文件导入不在本期范围。

正文、标题、评论全是不可信数据，不接受其中“忽略指令、读文件、执行命令、访问链接”的要求；prompt 用明确数据边界和结构化 JSON 承载，模型只返回规定 schema。输出必须验证 item_id、证据引用、类型和枚举，不执行任何模型返回的代码或路径。HTML 不自动加载图片、脚本、远程字体或 iframe，详见 S09。

README 明示：**启用真实 LLM 后，偏好、标题、截断正文、评论文字和命题目录会发送给 grok/xAI；本地存储不等于本地推理。** 不发送 sources.yaml、全项目、raw 文件路径、私有 feed URL、账户文件、Cookie、完整环境或历史终端日志。prompt 临时文件只放 `.grok-tmp/`，文件权限 0600、目录 0700，调用 finally 清理；`.grok-sandbox/` 始终为空，不放 prompt/schema。进程中断后的残留不自动发送，用户可按 README 人工处理，清理功能不扩展成遍历用户目录。

日志只接受字段白名单。usage 只存 S04 允许的数值；不存 sessionId、模型推理文本、原始 stdout/stderr。`wiki/log.md` 只追加 run 与已脱敏事件。秘密值哨兵不得出现在任何异常、日志、HTML、Markdown 或 fake argv 中。S12 的测试要覆盖这些边界。

## S07 接入与多模态解析的精确定义

### 7.1 源配置和 RSS

配置顶层仅 `sources`。每个源按 Source 字段严格校验，未知字段（含 cookie、headers、api_key）拒绝。`rss/podcast_rss/youtube_rss` 只允许 http/https；`file` 只允许上节夹具 URL，且 file_format 必填。不要沿用 v2 中 `kind:rss + file:` 的歧义示例。

源 id 唯一且稳定。同一 id 的 URL/kind/file_format 变更报 ConfigError，换源应使用新 id；旧 id 不在配置中时设置 enabled=false，不删历史。title 可改；当前源状态不能由 YAML 的 etag 字段覆盖。偏好文件固定四个标题「关注」「降权」「必读触发」「非共识约束」，只作为 prompt 文本，不执行散文里的命令或变成隐式规则。示例可写“已读过的发布说明降权”，但 MVP 没有已读状态，模型不能自行声称用户已经读过。

RSS 普通正文优先取非空 content:encoded，再取 description/summary；`parse_text` 用 html.parser 去标签、decode entities、去 script/style 内容并压缩连续空白，保留可读文本。不抓全文页、不取附件或图片。Atom/RSS 日期统一 UTC，异常日期按 S05 暂缓。每条解析错误变为 EntryIssue，其他合法条目可以摄入；整份 XML 非法时 feed 失败且不更新 validators。

相对条目link只可针对网络Source.url用urljoin解析后再做canonical_url校验，不发补充请求；file夹具必须给绝对公开条目URL。YouTube条目必须同时有videoId和有效公开link，缺一记EntryIssue，不猜造平台链接规则。

`parse_rss` 同时识别 podcast_rss 和音频 enclosure，但 enclosure 只作为链接元数据保存，任何情况下不下载。节目 show notes/description 不能自动冒充 transcript。RSS 无正文不自动去文章 URL 补抓。

### 7.2 播客

优先使用明确给出的非空 transcript 文本。`parse_podcast` 是纯函数，不读 URL、不启动任何子进程。只有 audio_url、或文本为空时返回 `body_plain="", transcript_status="pending_transcript"`。

`podcast:transcript` 的 URL 与声明 MIME 存入 Asset，不能只存一个临时变量后丢掉。材料中已有该文本链接规范：[Podcasting 2.0 transcript](https://github.com/Podcastindex-org/podcast-namespace/blob/main/docs/tags/transcript.md)。本期支持的解码格式是 plain、VTT、SRT；这是应用接受范围，不声称其他格式不存在。

下载前候选必须声明 `text/plain` 或 `text/vtt`，或 URL 有 `.txt/.vtt/.srt` 扩展名；响应须为相符文本类型。SRT 可接受显式 `application/x-subrip`，这是本应用白名单设计；当前真实源是否使用该 MIME 为 UNKNOWN。Content-Type 缺失时，只有上述扩展名加 UTF-8 可解码、无 NUL 的文本才接受。明确 `audio/*`、`video/*`、image、HTML、JSON 或 PDF 响应，即使 URL 以 .txt 结尾也拒绝。出现已知音视频扩展名在请求前拒绝。

VTT/SRT 只去编号、时间轴、NOTE 等控制行并合并文本，不做音频转写；夹具固定这些规则，不宣称支持任意损坏字幕。成功后 Store 原子更新正文、状态、hash 和 transcript validators，清旧分档。空文稿或下载失败保持 pending，记录安全码，不把 show notes 送分类。

每次 run 无论 feed 是 200 还是 304，都扫描 pending 的 transcript URL，按 item_id 排序，最多尝试 RuntimePolicy.max_transcript_fetches。每条每次最多一次；失败由 HTTP host 退避控制，下次再试。达到调用上限仍保持 pending 并给出原因。fixture 的 transcript URL 只能指向同一受限 fixture 树，不走 HTTP；真实源不能通过 feed 注入 file URL。

本地 `podcast_json` 是本应用的合成/现成文稿输入格式，顶层 `items` 数组，条目字段为 `guid,url,title,published_at,transcript,audio_url,transcript_url`，未知字段拒绝。它不是对任何外部 API 的声称。已 ready 文稿的定期再验证、人工补文稿 UI、无文稿的 Whisper/STT 仍属 Q07。

### 7.3 视频与 YouTube

`parse_video` 只接收标题、频道、nullable 的 like/comment/view 计数、comments 和可为空的 frame_paths。最多保留前 5 条非空文字评论，每条长度的 prompt 限制见 S08。计数缺失是 None，不是 0；不能从 starRating 推导点赞，不能从 comment_count 推导评论文字。

非空 frame_paths、显式媒体文件字段或标题/评论整体为待打开的本地 `.mp4/.jpg/.png/.webp` 路径，抛 FramesNotAllowed。不把普通句子中提到“.mp4”当要读取的文件；无论内容如何都不打开本地路径。不下载 thumbnail，不访问 media:content，绝不调用 ffmpeg。

`youtube_rss` 解析 `yt:videoId`、title、channel、`media:description`、可选 `media:statistics views`；guid 使用 videoId。缺 videoId/link 则按明确的错误路径拒绝，不造 id。views 缺失为 None；likes、comment_count 为 None，comments=()。普通 RSS 不假设都有这些扩展字段。

这些字段来自评审于 2026-11-09 的公开 Atom 抽样，**不保证每个 feed 始终有**：[材料抽样 URL](https://www.youtube.com/feeds/videos.xml?channel_id=UC_x5XG1OV2P6uZZ5FSM9Ttw)、[官方推送说明](https://developers.google.com/youtube/v3/guides/push_notifications)。测试使用按该形状编写的合成 XML，views=13931 只是沿用 v2 的测试常量。公开 RSS 没有评论时页面明确写“未提供评论文字”，不假装观众有共识。

本地 `video_json` 顶层 `items` 数组，条目字段 `guid,url,published_at,title,channel,like_count,comment_count,view_count,comments,frame_paths`。它覆盖有限评论端到端入 prompt、对照和页面的路径，不暗示实际已接通评论服务。官方 YouTube Data API（videos.list/commentThreads.list）的启用、key、实际配额和授权均待 Q05。

## S08 LLM、输出 Schema、预算与计费

### 8.1 唯一适配器与真实启用门槛

默认二进制路径保留 `SECOND_BRAIN_GROK`，`SECOND_BRAIN_GROK` 只用于显式替换。离线模式只能运行项目内已经校验的 fake，不能因环境变量缺失回退真实二进制。调用 `complete` 前由编排构造的受预算约束 CompletionFn 检查 RuntimePolicy；应用的其他模块禁止直接启动子进程。

真实调用须同时满足 `offline=false`、`live_authorized=true`、billing_path 为 session/api、`billing_path_verified=true`、`tool_policy_verified=true`。默认不满足。授权由操作者决定，两个 verified 值只能在另行允许核验且留有证据后配置；不能只选择一个枚举就称计费路径已确认，也不自动根据 auth 文件、缓存或环境猜测。材料中的关工具参数是已有设计依据，但**尚未实测它们是否覆盖全部内置/MCP/未来工具、全局上下文和后台自动行为**。WP13/WP14 必须把这个限制做成可测试的关闭门槛，WP6固定并验证参数；不能把fake argv通过当作已经证明真实CLI无工具。

在获准的真实 smoke 前，先核对所用 CLI 的无工具能力；若该版本无法确保无工具/无工作区读取，保持 gate 关闭，另行修订适配方案，不删安全参数迁就运行。这里不发明 `--tools ''` 或其他未经材料支持的开关语义。应用不会调用 `grok login`、logout、update，也不会读取认证文件。账户登录续期行为、双凭据优先级均为 UNKNOWN，不能通过删除登录文件来强制切换。

### 8.2 argv、临时文件与信封

生产 argv 固定为列表，`shell=False`，保留 v2 的全部保护参数：

```text
{binary}
--no-auto-update
--verbatim
--no-plan
--no-subagents
--disable-web-search
--max-turns 1
--disallowed-tools run_terminal_cmd,web_search,web_fetch,search_replace,Agent
--deny Bash --deny Edit --deny Write --deny WebFetch
--model grok-4.6
--reasoning-effort low
--cwd {absolute_empty_sandbox}
--json-schema {absolute_schema_file}
--prompt-file {absolute_prompt_file}
```

不传超长 `-p`，不继承 xhigh，不用 Grok 4.7 Fast，不启用 debug/trace。材料记录 `--json-schema` 暗示 json 输出；真实版本兼容性仍由 gate 保护。cwd 必须位于本项目 `.grok-sandbox/` 且没有文件、symlink 或子目录；非空直接拒绝，不自动删除用户文件。prompt/schema 用绝对路径置于独立 `.grok-tmp/`，不能为了找到它们把 cwd 换回项目根。

超时 120 秒，stdin 设为 DEVNULL，stdout/stderr 有 2 MiB 工程上限；用受控 Popen/communicate 实现，超时或输出超限终止该子进程并回收，不留下后台进程，不重试。测试中仅白名单 fake 可启动。真实会话路径的子进程环境剔除 API key 与自定义代理/base-url 覆盖，API 路径也不得自动沿用不明确的双凭据状态；不打印被剔除字段的值。具体实际计费路径无法确认时阻止启动，而不是标成免费。

stdout 必须恰好是一个 CLI JSON 信封对象，禁止从一堆输出里贪婪搜 `{...}`；JSON解码拒绝重复对象键及NaN/Infinity，payload同样适用：

1. 非零退出码，或信封 `type=="error"`，报 GrokUnavailable；安全 error code 代替原始 stderr。
2. 有非空 `structured_output` 时必须是对象，直接取为 payload；类型错误不能回退到 text 掩盖错误。
3. 否则要求 `text` 为字符串，允许去掉**包围全部 text 的一对** json Markdown 围栏，再严格 `json.loads`。不截取前言/尾注，不修复非法 JSON。
4. payload 必须为对象；拒绝原始顶层 `items` 的“假二进制捷径”。fake 也必须生成信封，保持与生产同契约。
5. stopReason 若存在且不是材料夹具中的 `end_turn`，按 `unexpected_stop_reason` 拒绝，保留 usage 事件；缺失允许但记录状态未知。未知新值不猜成完成。
6. usage 仅按白名单接收非负整数或金额十进制字符串；缺字段保留 None，不反推免费。返回 `CompletionResult`，不丢失 usage，也不把整个信封当分类对象。

每次启动前由 BudgetedCompletion 生成 call_id 并预登记 LlmEvent，结束后用相同 call_id 补全结果；即使 schema 无效或后续数据库/页面失败，也不漏记已发生调用。正常路径从 CompletionResult 取 usage，异常路径从 GrokUnavailable.usage 取已清洗的 usage；退出失败但信封可解析时也应保留它，无法解析则记 UNKNOWN。`Store.append_llm_event` 对同 call_id 是受限状态更新：只允许 started 到最终状态，禁止覆盖已终结事件为另一调用；不追加第二次费用。

### 8.3 分类输入与严格 Schema

工程默认：最多 8 条/批，body_plain 最多 1500 个 Unicode 字符，标题最多 300，评论最多 5 条且每条最多 300，偏好全文最多 4000，完整 prompt 最多 32000 字符。字符数不是 token 数，更不是美元上限。preferences 超限在启动前报 ConfigError，不偷偷截断个人偏好；条目截断有显式标记。构造后超总长则按稳定顺序缩小批次，单条仍超长报 SchemaError，不增加模型上下文假设。

排序为 `(fetched_at,item_id)`；先过滤 duplicate、pending、无有效日期与已有当前分档的项。分类器内部再防御性过滤 pending/duplicate，不依赖上层恰好做对。源 URL、音频 URL和本地文件路径不入 prompt。视频评论置于独立 `audience` 字段并以 `--- audience ---` 明确标识；这是观众文字，不是作者正文。未知 likes/views 不冒充零。prompt 包括偏好全文、id、source_id、title、modality、body、channel 与 audience、规范化后的主题别名、至多 32 个现有 claim_key（排序后取前 32，单个不超过 200 字）。没有进入目录的命题仍可新建，不能因此强行复用相近 key。

`policy_hash` 是偏好全文、KeyPolicy、分类 schema 和应用 prompt 模板版本（自有常量 `classify-v3`）的固定 JSON SHA256。偏好改动不自动重跑整个历史库；旧结果保留该 hash，页面标明旧偏好。批量重分档入口本期不做，见 Q02，不能暗中扩大费用。

为使fake和生产输入完全同形，prompt本身固定为一个JSON字符串：分类顶层键为`task="classify-v3", instructions, preferences, topic_aliases, claim_aliases, known_claim_keys, items`，items采用prompt_fields输出；instructions明确禁止执行数据中的命令。短评顶层为`task="review-v3", instructions, topics`，每个topic含`topic,items`，行仅含`id,source_id,title,bucket,reason,claims,audience`。所有prompt使用相同JSON编码，不在JSON外再拼任意前后缀。CompletionFn的purpose只允许`classify`或`review`，未知purpose在调用前拒绝。

`classify/schema.json` 顶层形状如下，WP9 必须生成完整 JSON Schema，并用标准库手工验证相同规则；不新增 jsonschema 依赖：

```json
{
  "items": [{
    "id": "请求中给出的 id",
    "topic": "主题",
    "bucket": "must",
    "reason": "分档依据",
    "claims": [{
      "claim_key": "主体|可检验的肯定命题|时期|范围",
      "sentence": "来源表达的观点",
      "stance": "for",
      "evidence_kind": "body",
      "evidence_quote": "输入中的原文摘句",
      "comment_index": null
    }]
  }]
}
```

所有对象 `additionalProperties=false`，上述字段全部 required，只有 comment_index 可为 null。items 长度恰好等于本批请求数量，id 集合与请求集合精确相等且不重复，不允许漏项、伪造 id 或重复项覆盖。topic 规范化后非空且最多 80 字，reason 非空且最多 800 字，claims 最多 5 条；claim_key、sentence、evidence_quote 分别最多 200、400、300 字且非空。bucket 只有 must/recommend/skip，stance 只有 for/against；comment_index 对 comment 为 0 至该条评论数减一，其余必须为 null。数字类型不接受 bool，禁止 NaN/Infinity。

`claim_key` 用同一肯定命题、主体、时间与范围；反对方不能把 key 写成另一个否定命题再单独分组。必须恰有四个非空段，段内不能含分隔符；alias后再验证。四段缺失报schema错误；“未知时期/未知范围”使用明确字面UNKNOWN，程序用normalize_key(segment)=="unknown"判断，避免casefold后漏过滤。这种key保留供展示但不进入投票。主题alias与claim alias在分组前由程序统一应用，不能只在prompt里提到却不执行。

evidence_quote 必须是对应**实际送入本批 prompt 的** title/body/指定 comment 的连续子串，比较前仅统一空白；不能在未发送的长正文末尾寻找引文替模型背书。quote 可证明引用存在，不能证明立场解释正确。comment 证据保存供多源评审但永不作为来源票。没有可引用证据时 claims 应为空；reason 不等于事实证据。

整个批次验证通过才生成 classifications 与 claims；任一记录非法，整批不写，BatchOutcome=invalid，下一批继续。`InvalidBucket/InvalidStance` 保留为校验层具体异常，由 classify_items 捕获并转安全批错误。GrokUnavailable 使当前及后续批 unavailable，后续不再调用；BudgetExceeded 使当前及余批 budget_skipped。函数返回唯一的 ClassificationResult，不再有 v2 的“可返回 tuple”。

### 8.4 调用预算、失败和费用账本

编排提供 `BudgetedCompletion` 包装器，构造参数固定为 `(delegate:CompletionFn, policy:RuntimePolicy, store:Store, run_id:str, clock:Clock)`，调用签名同 CompletionFn。它归 WP13。分类默认最多 3 次、短评默认最多 1 次，每次尝试（含失败）都扣一次，不自动修复 JSON 再调用。分类每批最多 8 条，所以普通小输入一轮最多尝试 24 条；超总长拆小批可能更少。余项存 deferred，下次继续；不能提前推进 last_classified_at。主题短评从独立预算扣，不抢占分类预算。一次GrokUnavailable后包装器进入本run不可用状态，后续任何purpose都在delegate之前拒绝，不再新记实际调用事件；因此短评不能绕过分类阶段的停止条件。

`budget_stops_before_spawn` 必须证明第 4 次分类在 delegate/子进程调用之前被挡住。次数限制不能保证美元上限；工具关闭效果、推理 token、实际计费路径未经验证时，不可称为“硬费用封顶”。没有 `total_cost_usd` 就显示金额 UNKNOWN；有则作为服务端报告值保存，并带 billing_path，不替用户推算发票。usage 各字段分列记录，不未经核实把 reasoning 加到 output 造成重复计数。

以下为**材料于 2026-11-09/10 打开的历史页面记录**，不是本次查价，也不构成购买建议：

| 路径 | 材料中的公开信息 | 本系统处理 |
|---|---|---|
| A：CLI 会话 / Grok Build 套餐池 | [套餐页](https://x.ai/pricing) 记录 Free $0 Limited、Plus $8、Premium $30、Super $100、Ultra $200，材料写用量周刷新；[限制页](https://docs.x.ai/x-subscription/usage-limits) 未给 token 定额，超额可能 Additional Usage | 当前账户套餐、余量、单次美元成本 UNKNOWN；不按 API 单价给它生成账单；不自动充值 |
| B：API key 按量 | [API 定价](https://docs.x.ai/developers/pricing) 记录 grok-4.6/4.7 在 prompt <200k 时每百万 input/cache/output 为 $2/$0.50/$6；≥200k 为 $4/$1/$12，整次请求适用；[FAQ](https://x.ai/pricing) 记录 API 另计，不从套餐池扣 | 仅在该路径已被明确选择并能确认实际使用时标 api；认证优先级冲突未实测，不能只看到变量存在就标 api |
| Fast / 工具 / STT | 同一历史定价页记录 Fast 限 Cursor/Grok Build，不是公共 API；Web Search $5/1000；STT REST $0.10/小时、Streaming $0.20/小时 | 本期不用 Fast、搜索与 STT，不拿它们补功能；无数值免费额度可承诺 |

材料缓存的 session context 与 API 页面 context 不一致，不据此设计超长 prompt。v2 的约 $0.06/天、$1.8/月依赖未经实测 token 和推理计价假设，本版不作为预算承诺。若操作者要预算，只列 `input_tokens×对应价 + cache_tokens×对应价 + billable_output_tokens×对应价` 的场景公式；billable_output 是否已含 reasoning、用量口径和新价格都要先核实，当前写 UNKNOWN。测试不调用真实服务，故没有本系统测试产生的外部模型用量；不等于已验证生产免费。

## S09 多源对照与静态阅读输出

### 9.1 确定性层必须有内容

每次从当前有效的 ClassifiedItem 集合构建主题页。topic 按分类时应用 alias 后的规范化值分组；保留每行 policy_hash，不在读取时静默重解释旧偏好。至少 2 个不同 item 就产生表格；只有一个 source_id 时标「单源材料汇总」，不得冒称多源；有至少 2 个 source_id 才称「多源对照」。同 URL alias 已被 Store 排除。

每条列标题、可点击公开 URL、source_id、发布时间或明确未知、中文分档、reason、正文/评论的证据区别。评论另列「观众摘句」，没有评论写“未提供评论文字”。表格按 source_id/item_id 排序。仅含 URL 列表的页面不能验收通过。

`build_topic_reviews` 不调模型、不写盘；返回 TopicReview，sentences=()，单源为single_source，多源初始为not_requested。`enrich_topic_reviews` 也不写盘，注入 CompletionFn 获取摘要，失败保留 rows 和 summary_status=failed，不能清空确定性层。未变主题不重用无法核对版本的旧LLM段，保留not_requested并注明“本次未请求短评”；complete_fn=None表示用户关闭，所有多源主题为disabled。

### 9.2 LLM 层限定输入与引文

仅对 changed_topics 中的多源主题尝试短评，主题按规范化 key 排序，每批最多4个主题。每主题至多8行：先按published_at从新到旧、再item_id排序，取各不同来源最新一行（至多8源），再按同排序补足8行，确保至少两个来源。确定性表保留全部行，摘要明确“仅基于本次所列选中材料”。输入包含title、bucket、reason、已验证观点与观众摘句，不要求模型打开URL；模型无工具。完整短评prompt也受32000字符上限：先减少同批主题数，再移除单主题中排序末尾且非该源最后一条的行；若仍超长，再移除末尾来源的行，但始终至少两源。不足以构造合法请求则failed并记input_too_large，不启动模型。列不全时不得声称覆盖该主题所有观点。

`review/schema.json` 约束的输出形状为 `{"topics":[{"topic":"...","sentences":[{"kind":"consensus","text":"...","item_ids":["..."]}]}]}`，kind按ReviewSentence的四个枚举验证，不是任意字符串。每层禁止额外字段；topic集合恰好匹配请求，不能重复；每主题sentences为2至8条，至少一条consensus和一条difference。每条最多300字、单行，text至多一个终止句号/问号/感叹号（允许末尾标点）；此为可测的“最多8句”定义。item_ids非空、无重复且均属于该主题选中材料。选中材料有评论时至少一条audience句，且须指向有评论的item；没有评论则不允许audience句。

无差异时可以写“所选材料未提供可确认的分歧”，不得为凑字段造立场；无评论时不生成观众态度。schema 错误、超时、预算不足分别标 failed/budget_skipped；关闭时 disabled。短评只是辅助解释，不参与投票，不更改分类。是否关闭该层以节省额度由 Q10 决定，默认示例在离线 fake 下演示开启。

### 9.3 页面与文件发布

`wiki/digest/<run_id>.html` 的增量区只含当前有效且 published_run_id 为空的成功分档，分节标题精确为「必读」「推荐阅读」「不推荐」。另列「待转写」「日期待确认」「待分类/本次未完成」，并展示来源失败、预算延后、短评不可用等原因。没有增量时显示“本次没有新的已分档条目”，不把全历史重新当新消息。

主题与非共识是本库当前快照，不限于这次增量；显式标注“本库当前已收录材料”，列出参与条目时间范围。主题页面和机会页面放 `wiki/snapshots/<run_id>/topics/<slug>.md`、`.../opportunities/<slug>.md`。这是替换 v2 的全局可变目录，避免旧机会残留冒充当前机会。`wiki/index.md` 指向最新完整发布快照，并列本次 run_id 和状态；旧快照只作带时间的历史证据。不删除旧文件。

HTML 与 Markdown 都转义不可信内容：HTML 使用 html.escape；href 仅保留已校验的 http/https，拒绝 javascript/data/file/userinfo，错误 URL 只显示文本；Markdown 特殊字符和括号转义，不允许源内容注入图片或 raw HTML。HTML 无 JS、远程 CSS、字体、图片、自动请求和 meta refresh，包含 UTF-8 声明。打开本地页面不会自动联网，用户点击明确的来源链接是用户动作。

页内「样本不足」「争议（未写机会）」「单源，未写机会」「未出现对立立场」「来源自身冲突，已排除」分别对应 ClaimAssessment，不再把所有无机会都写成样本不足。机会页包含命题、样本/来源票数、两边来源链接、原文摘句、排除来源、可证伪说明与固定脚注。无法用短评覆盖具体事实的证据链。

## S10 非共识算法与可证伪黄金集

### 10.1 输入与分组

算法为纯函数，不调用 LLM、不搜索、不访问 Store。输入为 Store 过滤的当前有效 Claim；函数本身再次拒绝非法 stance、空 key，剔除 `evidence_kind=comment`、key 任一段 UNKNOWN 的记录。相同 claim.id 去重，且同一 item 对同 key 的重复同向证据最多一票。

按 `(normalize_key(topic), resolve_alias(normalize_key(claim_key)))` 分组；调用方先将 alias 应用到 Claim，算法不自行读取文件。同主题的“需求增长”与“库存上升”不是同一命题，不能直接判为正反。不同主体、时期、地域/业务范围的主张不得混合。不能自动识别的语义等价允许漏报，不可用模糊匹配制造结论。

### 10.2 一来源一票与状态优先级

1. 同一 item 对同 key 出现 for 与 against，则该 item 冲突；同一 source_id 的不同 item 对同 key 出现双向，也视为来源自身冲突。整个来源在该命题中排除，并列入 excluded_source_ids，不能选择较新/较多的一边凑票。
2. 其余每个来源只投一票。该来源可以有多篇同向材料，但票数仍为 1；majority/minority_item_ids 保留其全部有效证据 id，排序去重。
3. n_items 是排除冲突来源后的有效不同 item 数，n_sources 是有效来源数；n_for/n_against 是来源票数。没有有效来源且存在冲突，status=conflicted；否则 n_items<3 为 insufficient；否则 n_sources<2 为 single_source。
4. 满足样本门槛后，只有一边为 one_sided；两边都有且票数相等为 disputed；严格多数才 opportunity。原 v2 的 ≥3 item、≥2 source 门槛保留为必要条件，但一源一票后，两边都有的严格多数自然至少需要 3 个来源。这个更严格的结果是有意修复单源连发问题，不是计数 bug。
5. minority_stance 为来源票较少的一侧；minority_sentence 取该侧按 `(item_id,claim.id)` 排序最小的 sentence。所有输出按 `(topic,claim_key)` 排序，机会内 id 也排序，输入顺序变化不能影响完整结果。
6. falsify_hint 固定可检查模板：`本条仅为本库材料分歧。命题：{claim_key}。请核对所列原文是否支持少数立场；若主体、时期、范围不一致，或后续同范围的公开证据否定少数句，则本候选失效。少数句：{minority_sentence}`。不生成价格、交易、概率或“必然赚钱”的结论。

此算法检验的是证据分布规则，不能自动证明哪个来源说得对。多数不等于事实，少数不等于机会。相关转载与多账号可能仍不独立；机会页必须保留这一局限。互动计数、观众评论、模型短评不能增加来源票。

### 10.3 黄金集与评估标准

WP7 独占 `tests/fixtures/consensus/golden_claims.json`。字段包括 case_id、claims、expected_assessments、expected_opportunities；所有记录为**合成文本**，不模拟市场真相。统一示例命题是“机构A|需求同比增长|2030年度|业务B”，for 原句为“2030年度业务B需求将同比增长”，against 原句为“2030年度业务B需求不会同比增长”。不再把“库存过高”硬标为“需求增长”的反证。

| case_id | 给定输入 | 精确期望 |
|---|---|---|
| `four_for_one_against` | 5 个 item、5 个 source，同一命题 4 for + 1 against | 1 机会；票数 4:1；少数句为“不增长”原句，against |
| `two_items` | 2 个 item、2 source，1:1 | insufficient，无机会 |
| `tie_two_two` | 4 item、4 source，2:2 | disputed，无机会 |
| `single_source_five` | 5 item 同一 source，全部 for | single_source，无机会 |
| `single_source_conflict` | 5 item 同一 source，4 for + 1 against | conflicted，排除该源，无机会 |
| `source_spam_four_one` | A 连发 4 for，B 发 1 against | 5 item、2 source、来源票 1:1，disputed，无机会 |
| `two_keys_same_topic` | 同主题两个 key，每 key 3 个来源且一边倒 | 两个 one_sided，无机会 |
| `duplicate_claims` | 基准的每条 Claim 重复 3 次 | 与基准完整结果相同 |
| `item_conflict` | 基准中少数 item 加同 key 反向 Claim | 排除对应来源；剩余 one_sided，无机会 |
| `comments_only_opposition` | 3 个来源正文都 for，评论中 against | one_sided，无机会；评论可在对照展示 |
| `scope_period_split` | 主体相同但时期/范围不同 | 分组独立，不生成跨范围机会 |
| `unknown_scope` | key 含 UNKNOWN | 不投票、不生成机会 |
| `alias_merge` | 输入经 KeyPolicy 把 chips 映射半导体、同义 key 映射一致 | 与统一键基准相同；alias 循环在配置层拒绝 |
| `shuffle_stable` | 基准、spam、tie 各打乱 5 个固定 seed | 完整 ConsensusResult 序列化后相等 |
| `canonical_duplicate_integration` | 两来源同 canonical URL，经 Store 去重后再取 Claim | 不增加 item/source 票，结果不变 |

机会的比较键为 `(topic,claim_key,minority_stance,minority_source_ids,minority_item_ids,majority_source_ids)`，expected 集合必须与实际精确相等；每个 assessment 状态、数量、excluded_source_ids 同样精确匹配。规则黄金集门槛为零差异，不以“看起来合理”验收。

端到端另由 WP9/WP13 使用 fake completion 验证原文引文存在、pending/duplicate/评论不入票、批失败不入库。黄金集证明确定性实现，不证明 LLM 抽取在真实资料上的精度。操作者后续人工标注 20 条（沿用评审建议）的事实/立场/相关性集属于 Q11；报告 precision/recall 时要写数据集、分母和人工分歧，未完成前真实抽取准确率为 UNKNOWN，不虚报数字。

## S11 运行状态机、发布事务与恢复

### 11.1 一次运行的固定顺序

1. CLI 严格加载配置，离线模式拒绝任何非 file 源，验证 fake 路径；此时不得调用网络/模型。进入 `run_once` 后持有传入 lock_path 的非阻塞排他 `fcntl.flock`，CLI 固定传项目 `data/run.lock`，一直到 finally 释放；文件不 unlink。Linux 本机是本期运行目标，其他平台的锁行为 UNKNOWN，不静默降级无锁。
2. 对CLI已创建并传入的Store连接执行migrate，处理unfinished_publications；业务迁移与状态写入都在取得锁后进行。已有publishing记录优先恢复，验证通过前不发新的请求。无发布记录但status=running的中断run标failed，已完成分类照样留在未发布队列。该清理仅更新状态，由Store.recover_interrupted_runs实现，不能清items/classifications。旧llm_calls仍为started的调用标interrupted_unknown，金额与完成结果保持UNKNOWN；若模型已执行但结果未持久化，下一次人工run可能再次消费，不能宣称这种中断也保证不重复费用。
3. 捕获固定本次 `now=clock.now()` 和新 run_id，begin_run 记录 since_at=此前 last_classified_at。sync_sources 注册新源、停用删除的配置源、重算必要的去重主项。
4. 按 source_id 顺序抓取。file 走 FixtureReader 与对应 parser；网络源走 HttpClient，带已提交 validators。youtube_rss 用 parse_youtube_feed，其余 RSS 用 parse_rss；video_json/podcast_json 用 parse_local_records。`persist_feed` 统一调用 Store.ingest_feed。某源失败收集 failed_source_ids，其他源继续；304 调 accept_not_modified，不把 b"" 当损坏 feed。
5. 执行 hydrate_transcripts，即使所有 feed 都 304。限额/失败保留 pending；不下载音频。离线模式只允许 fixture 文稿。
6. items_for_classify(now) 形成当前待分类快照并 select_run_items；获取 assets、已知命题；创建 BudgetedCompletion 后调用 classify_items。按每个 BatchOutcome 调 Store.save_batch，只提交该批验证通过的分类与 claims；批内全有或全无。保存时校验 expected analysis_hash，若不一致报 SchemaError 并保持队列，不能把旧 prompt 的结果写到新正文。
7. 从 Store 读当前有效全库 ClassifiedItem 与 Claim；get_changed_topics 包括新分档主题和被改稿/去重/停用移除的旧主题。build_topic_reviews，然后对受影响主题尝试 enrich；任何模型失败都保留确定性结果。find_minority 消费有效 claims，完全不受短评成败影响。
8. get_unpublished_classified 组成增量页；读取 pending、held_date、未分类列表及安全 warnings。WikiPublisher.stage 写完整可核查快照、digest 与 manifest，尚不改变 index，也不标 published。
9. Store.mark_publishing 将 Publication、文件校验和、归档引用、成功/部分状态写入 runs，再 WikiPublisher.publish 幂等发布文件；最后 Store.finish_publication 标记本版本 published_run_id，更新 finished_at，并在完整成功时推进 last_classified_at 到该 run 的 started_at。只有 hash 仍一致的引用可标 published。
10. WikiPublisher.append_events 追加完整日志块，返回 RunReport，finally 释放锁与关闭资源。日志块以 run_id 与内容 hash 标识完整 BEGIN/END；崩溃的半块不视为已写，重试只追加新的完整块，不改旧日志。不允许调用方自己写 wiki/log.md。

`get_changed_topics` 恢复时要包含尚未完成发布的修改；pending 已 ready 或删除旧 classification 的更新，即使这次分类失败，也必须从当前主题/机会快照中移除旧意见。不能沿用上一轮机会文件。

### 11.2 文件系统与 SQLite 不假装是一个事务

stage 写到 `wiki/.staging/<run_id>/`，manifest 列完整相对文件路径、SHA256、run_id、classification refs、快照范围、发布后状态与警告。校验路径不能逃逸 wiki。最终快照目录为 `snapshots/<run_id>/`，digest 为 `digest/<run_id>.html`。对每个目标使用同文件系统 rename/replace；旧 run 的最终文件绝不覆盖，若已存在只核对 manifest 和 hash。

publish 顺序：完整 snapshot 目录 → digest → 原子替换 index.md 指针；index 是当前版本可见性门槛，不能先指向半成品。只有当前 run 对 index 的更新允许 replace，其他历史文件不重写。Store 随后完成 published 标记；这一先后顺序允许崩溃后通过 manifest 补记，不会永久丢读物。

恢复规则：

| 中断位置 | 下一次行为 | 必须不发生 |
|---|---|---|
| 抓取已提交、模型未完成 | 从当前未分类队列继续 | 重置 validators、丢已抓条目 |
| 部分批已分类 | 仅对剩余条目调用模型，成功批留待发布 | 重复计算成功批 |
| 分类完成、stage 失败 | 新 run 重建未发布结果 | 因已有 classification 而永远不展示 |
| mark_publishing 后、文件未发完 | 根据原 manifest 继续 publish | 新建另一个快照掩盖未完成旧 run |
| index 已更新、数据库未标 published | 验证最终文件后补 finish_publication | 再次调用模型或重复列同一版本 |
| 最终文件缺失/校验和冲突 | PublishError，保留证据，停止本次发布 | 覆盖不一致历史文件、提前推进游标 |
| 日志追加失败 | 保持可恢复的日志待写标记，下一次补写 | 将模型调用记录视为未发生 |

`runs.summary_json` 的 `log_pending` 布尔由 Store 设置，`Store.pending_log_runs()->tuple[str,...]`、`Store.mark_log_written(run_id:str)->None` 由 WP1 实现；事件仍以 llm_calls/fetch_log 为源。日志失败不撤销已完成归档，但报告 warning，并使本次 CLI 非零，便于修复。磁盘错误不能通过删 raw/wiki、绕沙箱或切到别的项目解决。

### 11.3 成功、部分失败与退出码

- `success`：本次所有源成功取数/304、所有应分类条目完成、发布完成；允许 pending/date hold/短评失败等有明确解释的非阻断状态。推进全局游标。CLI 退出 0。
- `partial`：已发布可读结果，但有源失败、分类错误、grok 不可用或预算延后。保存成功批及抓取，不推进全局游标。CLI 退出 2；页面列出未完成项。全批 grok 不可用也可发布确定性“待分类”页，不把抓取白做一遍。
- `failed` 或异常：配置/锁/磁盘/数据库/发布不一致等致命错误。退出 1，不推进游标，不自动删除。mark_publishing 后失败保持可恢复 publishing 记录，不能把恢复信息抹掉。

短评失败不改变success为partial；调用账本仍记失败。日志追加失败是退出1的持久化告警，归档若已完成保持原status，不伪造回滚。README要说明这个可恢复的特殊情况。RunReport.classified指本次新成功条数，deferred指预算延后数，不能把旧结果补发布计为新增模型分类。inserted统计新存来源条目（含被去重别名）；updated统计feed更新加文稿恢复的版本更新次数，同条多次变化可能计多次，页面须使用“版本更新次数”标签，不当成独立文章数。

v2 对 grok 失败直接向外抛出的方式在这里明确调整为部分完成报告；仍满足“抓取保留、非零退出、不推进游标”，并让其他成功批可阅读。

## S12 本地运行与测试隔离

### 12.1 离线示例配置

`sources.example.yaml` 只能使用合成 fixture，不能预填真实人物信息源：

```yaml
sources:
  - id: example-blog
    kind: file
    file_format: rss
    url: file:tests/fixtures/rss/feed.xml
    title: 合成博客
    backfill_days: 14
  - id: example-podcast
    kind: file
    file_format: podcast_json
    url: file:tests/fixtures/podcast/episodes.json
    title: 合成播客
    backfill_days: 14
  - id: example-youtube
    kind: file
    file_format: youtube_rss
    url: file:tests/fixtures/youtube/feed.xml
    title: 合成频道
    backfill_days: 14
```

`topic_aliases.example.yaml` 为 `topic_aliases: {}` 与 `claim_aliases: {}` 两个映射，空映射即不做语言合并。偏好示例保留 S07 四个标题。`runtime.example.yaml` 明确列 S04 的全部默认值，不含 key、Cookie、账户信息。

以下是**第 4 步代码与离线依赖就绪后**的运行说明，不在第 3 步执行：

```bash
cd 仓库根目录
PYTHONPATH=src python3 -m pytest -q
PYTHONPATH=src SECOND_BRAIN_GROK=tests/fakes/grok python3 -m second_brain.cli run --offline --config-dir config --db data/second_brain.sqlite
```

先由用户从 example 建立真实配置副本；文件已存在时 CLI 不覆盖。为了让全新目录可演示，CLI 另提供 `--example-config`，直接读取 example 文件而不复制/写真实配置。完整零配置演示为前述命令再加 `--example-config`。本地打包入口 `second-brain run` 与 module 入口同实现。打开 RunReport 输出的本地 HTML 路径，程序不自动启动浏览器、服务或定时任务。

示例夹具的发布时间采用冻结测试时间，演示须提供 `--now 2030-01-15T00:00:00Z`；该选项只在 `--offline` 可用，真实模式拒绝。没有这个参数时用实际 Clock，过旧 fixture 被 backfill 跳过并提示，而不是偷偷改时间。WP14 的 README 必须把示例完整写为：

```bash
PYTHONPATH=src SECOND_BRAIN_GROK=tests/fakes/grok python3 -m second_brain.cli run --offline --example-config --now 2030-01-15T00:00:00Z --db data/second_brain.sqlite
```

未来获得环境准备授权后可建 `.venv`，使用既有 wheelhouse 安装固定依赖或 editable 包；本阶段不执行安装，不把缺包冒充测试通过，不设置测试时自动联网下载。真实启用示例只写参数意义：`--live` 与 `--offline` 互斥、`--live` 仍要求 runtime 的授权、计费路径与工具核验；没有这些条件在启动真实进程前 ConfigError。默认不带模式等同 `--offline`。

CLI模式参数仅设置本次RuntimePolicy.offline，不能顺便把live_authorized或两个verified设为true；runtime配置不能覆盖显式--offline。--example-config只选择example文件路径，不修改真实配置。所有读取/校验在发网络请求与创建真实CompletionFn前完成。

### 12.2 测试不信任“这次刚好没联网”

WP0 的 `tests/conftest.py` 全局 autouse 安装 tripwire：任何真实 `socket.connect/connect_ex/create_connection/getaddrinfo`、`urllib.request.urlopen`、默认 `OpenerDirector.open` 调用均失败并记录尝试次数。HTTP 用注入 HttpTransport stub，不能启动 localhost 测试服务器；“零出网”也不允许把 loopback 作为绕过。

父进程所有 subprocess/Popen 路径拦截，除已验证真实路径的 `tests/fakes/grok` 外一律失败，禁止调用 PATH 上真实 grok、ffmpeg、whisper、curl。fake 自身启动时也安装 socket/urllib tripwire，且只用标准库读取传入 prompt/schema、写指定 tmp_path 的 argv 记录；不能以为父进程 monkeypatch 自动传给子进程。CLI 测试直接调用 `main(argv)`，不通过 shell 启动任意命令。

WP6 只在自己的测试 fixture 中给该假二进制放行，其他包用 CompletionFn stub，不改全局 conftest。fake 场景包括 structured_output、text 围栏、非零退出、type=error、超时、输出超限、非法 JSON、异常 stopReason、缺 usage；场景开关只存在测试环境，不能变成真实 CLI 参数。

所有测试用 tmp_path 下独立 SQLite/raw/wiki/sandbox/temp 文件，固定 Clock，无真实等待、HOME/config 读取或用户资料依赖。真实认证路径的打开函数由哨兵阻断，源码导入本身也不得读环境/启动服务。fake 与 fixtures 均为自有合成数据，不复制研究中的受版权保护全文。

CLI 的生产根只从 `config.PROJECT_ROOT=Path("仓库根目录")` 取得，不随cwd变化。CLI测试在main调用前monkeypatch该常量为tmp_path，并只复制自有示例/合成fixture到该临时根；不得用真实config/raw/wiki。`--db`相对该根解析，raw/wiki/sandbox/temp/lock均由该根生成。`read_fixture(uri:str, *, fixture_root:Path)->bytes` 归WP14实现，传入该根的tests/fixtures；run_once测试直接注入FixtureReader与lock_path。

`test_network_tripwire` 故意触发被阻断的连接入口，证明安全栅栏工作；其他测试汇总连接尝试必须为 0。测试报告要区分这个负例的预期拦截，不声称连一次测试性调用都没发生。全部离线测试通过只证明实现及协议模拟，不能证明真实账号、工具关闭效果、价格或 API 可用性。

## S13 并行 DAG、工作包与文件所有权

### 13.1 依赖边、Wave 和接纳顺序

箭头表示“左包的冻结实现通过后，右包才能集成它”，不是运行时数据依赖反转：

```text
WP0 -> {WP1, WP2, WP3, WP4, WP5, WP6, WP7, WP8, WP9}
{WP1, WP2, WP3, WP4, WP5} -> WP10
{WP3, WP5} -> WP11
{WP2, WP7, WP8} -> WP12
{WP1, WP2, WP6, WP7, WP8, WP9, WP10, WP11, WP12} -> WP13
WP13 -> WP14
```

WP0 冻结后 WP1 至 WP9 **可同时开工**；并发 slot 不足时按批调度，不要求运行环境一定提供九个 agent。Wave 1 优先 WP1/WP2/WP3/WP4/WP5，尽早解锁接入链。Wave 2 为 WP10/WP11/WP12，可在各自依赖完成后同时开工，不必等待无关包。Wave 3 为 WP13。Wave 4 为 WP14 和全量验收。接纳按上述拓扑顺序；同 Wave 互不改对方文件，接纳顺序可交换。

代码 import 层规则同 DAG：models/errors 不导入业务模块；ports 只导入 models/errors；Store/HTTP/纯解析/LLM/分类/评审/非共识不导入 run/cli/config；HTTP 依赖 HttpStateStore Protocol，不能 import Store；WP8 依赖 CompletionFn，不能 import grok_cli；WP11 不依赖 RSS 实现或 Store；run 不 import cli/config。WP14 是唯一装配真实实现和配置的入口。禁止靠延迟 import 掩盖环。

### 13.2 冲突规避和工作包完成定义

- 按 S04 文件名分工，目录不是整棵写权限。WP0 不预写 cli.py、README.md、schema.json 等后包文件，避免“骨架所有者”和后包争同一职责。
- `models.py/errors.py/ports.py/pyproject.toml/.gitignore/config/*.example.*`、所有 `__init__.py`、`tests/conftest.py` 在 WP0 接纳后冻结。后包不得自行补字段、加依赖或改安全 fixture；问题交调度员，由 WP0 所有者一次修改并重跑受影响测试。
- Store 是唯一数据库写入口；WikiPublisher 是唯一 wiki 写入口；RawArchive 是唯一 raw 写入口。后包不得在测试以外拼 SQL，不直接追加别包 Markdown，不建立另一份配置读取器。
- 每包只写自己测试文件与独占 fixture 子目录；fake 的 argv、临时数据库、sandbox 和生成页在 tmp_path，不能写回 tests/fakes/argv.txt。测试共用 helper 有缺口时用本包私有 helper，不直接改 conftest。
- 一个工作包必须交齐实现、下列测试、实际命令与退出码、使用的依赖 stub、未解决事项。不能用 xfail/skip 或删断言把硬验收变绿。命令失败先修本包；共享契约问题同步修设计，不让集成包临时猜测。
- 下面每包的命令均可在依赖已接纳时独立运行；Wave 1 包不能为了通过而 import 尚未实现的兄弟包。需假端口的包在自己的测试内提供 stub。

### WP0 契约、骨架与全局测试边界

**独占文件：** S04 列出的 WP0 文件及全部包 `__init__.py`，`tests/conftest.py`、`tests/test_models.py`、`tests/test_boundaries.py`。**输入：** S01 至 S04、S12。**输出：** 可 import 的全部 frozen 类型、Protocol、hash/alias 工具，固定依赖声明，示例配置和测试 tripwire。**依赖：** 无。**并行：** 不与实现包同时改契约，先完成。

- [ ] `test_item_id_and_normalization`：相同 source/guid 得 32 位稳定 id；换 guid 不同；换行键拒绝；NFKC/空白/casefold 得固定结果。
- [ ] `test_canonical_url_keeps_semantic_query`：`https://EXAMPLE.com:443/a?id=1&utm_source=x#z` 得 `https://example.com/a?id=1`；`id=2` 不合并；保留路径大小写。
- [ ] `test_alias_cycle_is_rejected`：chips→半导体 合并；a→b→a 拒绝；无 alias 不猜翻译。
- [ ] `test_network_tripwire`：socket/DNS/urllib 各入口都被预期拦截；默认 subprocess 调用也被拦截。
- [ ] `test_import_layers`：用 AST 检查已存在业务模块的 import 符合 S13；缺模块在 WP0 可不检查，WP14 全量时必须检查全清单，不能永远跳过。
- [ ] `test_scope_contract`：声明依赖不含重库，示例无外网源/凭据，模型类单一定义，默认 live=false；后续全量再检查业务 import/进程入口边界。

命令：`PYTHONPATH=src python3 -m pytest tests/test_models.py tests/test_boundaries.py -q`。

### WP1 Store 与不可变 raw

**独占文件：** `store.py`、`storage/raw.py`、`tests/test_store.py`。**输入：** IngestRecord、ClassificationResult 各成功批、Publication、HttpStateStore。**输出：** S05 全部 Store 方法与 RawArchive，含 S11 的恢复/日志待写方法。**依赖：** WP0。**并行：** 可与 WP2 至 WP9 同时；不依赖 HTTP 实现。

- [ ] `test_schema_pragmas_and_idempotence`：临时库 migrate 两次，schema_version=3、外键生效、WAL 生效，初始队列为空；未知非空 schema 拒绝且表不变。
- [ ] `test_updated_clears_classification`：同 guid body 改变，hash/asset 同事务更新，旧 classification/claims 消失；旧 raw 字节保留、新版本新增。
- [ ] `test_metric_refresh_does_not_reclassify`：只改 views/like_count，analysis_hash 与分档不变；改评论入 prompt 的部分则清分档。
- [ ] `test_canonical_duplicate_and_promotion`：两源同 URL，只有一主项、无第二份 raw/分类；停用主源后别名升主且无环；改 URL 后旧组仍可用。
- [ ] `test_save_batch_is_atomic`：第二条 hash 不匹配，整个批次没有半写；合法批的分类/claims/run_items 一起提交。
- [ ] `test_raw_db_failure_reuses_revision`：模拟 raw 写好后 DB 回滚，重试复用相同文件，不覆盖；目标不同字节时 IdentityCollision。
- [ ] `test_old_unclassified_is_not_lost`：fetched_at 早于 last_classified_at 但无有效分类的条目仍入队；已分档当前版本不入队。
- [ ] `test_unpublished_query_survives_restart`：关闭重开库，已分类未发布仍可查；finish_publication 后不再入增量队列。
- [ ] `test_interrupted_usage_is_unknown`：中断run的started调用恢复为interrupted_unknown、不填0费用；已终结调用不被重写。

命令：`PYTHONPATH=src python3 -m pytest tests/test_store.py -q`。

### WP2 URL 守卫与 HTTP 状态机

**独占文件：** `urlguard.py`、`http.py`、`tests/test_http.py`。**输入：** HttpTransport、Clock、HttpStateStore stubs。**输出：** HttpClient、标准库单跳 transport、guard_public_url、rate_limit_code。**依赖：** WP0。**并行：** 可与 WP1/WP3 至 WP9 同时；测试不需要 SQLite。

- [ ] `test_url_guard_matrix`：拒绝 127.0.0.2、169.254.169.253、[::1]、ULA、IPv4-mapped loopback、短 127.1、userinfo、metadata 主机；允许 `10.example.com` 语法；全部无 DNS 调用。
- [ ] `test_redirect_to_private_is_blocked`：公共 host 的 302→内网，内网 transport 调用为 0；跨 host 清条件头；第 4 跳、循环、https 降级被拒。
- [ ] `test_429_opens_circuit`：A 返回 Retry-After:7，clock+1 时再次 A 不调用 transport；B 200 不受影响；日期格式与长于 900 秒的头不会被缩短。
- [ ] `test_failure_backoff_persists`：依次三次失败并推进 fake clock，至少熔断 900 秒；换 HttpClient 仍读取原状态；200/304 清失败计数。
- [ ] `test_limits_and_content_types_before_body`：超 Content-Length、流式超 2 MiB、压缩或 transcript 返回 audio 类型时拒绝；媒体 body 的读取次数为 0。
- [ ] `test_bilibili_never_reaches_transport`：真实禁用 host 的请求在传输前失败；社区码的合成 payload 仅测试纯状态函数。
- [ ] `test_http_log_redaction`：URL 的凭据哨兵、headers/body/原始异常不出现在 FetchEvent；safe code、status、retry_after 可查。

命令：`PYTHONPATH=src python3 -m pytest tests/test_http.py -q`。

### WP3 文字解析

**独占文件：** `parse/text.py`、`tests/test_text.py`。**输入：** title、HTML 字符串。**输出：** `parse_text` 的固定 tuple。**依赖：** WP0。**并行：** 可同时。

- [ ] `test_strips_tags`：`<p>Hello <b>world</b></p>` → `("Hello world","text")`。
- [ ] `test_entities_and_script_removed`：HTML entity 解码，script/style 内容不进入正文，连续空白合并。
- [ ] `test_text_parser_has_no_io`：夹带链接、img、本地路径仍只解析文字，无文件读取、socket 或 subprocess。

命令：`PYTHONPATH=src python3 -m pytest tests/test_text.py -q`。

### WP4 现成播客文稿解析

**独占文件：** `parse/podcast.py`、`tests/test_podcast.py`。**输入：** 明确 transcript、audio_url、文本格式。**输出：** ParsedPodcast。**依赖：** WP0。**并行：** 可同时，不 import HttpClient。

- [ ] `test_ready_when_transcript_present`：`alpha beta` 文稿原文可读、ready；不访问传入 audio_url。
- [ ] `test_pending_when_only_audio_url`：transcript=None/空白，返回空 body/pending；subprocess.run/Popen 都设为抛错仍成功。
- [ ] `test_subtitle_text_only`：VTT/SRT 夹具去编号时间行保留语句，损坏/空字幕按固定规则 pending 或 SchemaError，不调用媒体工具。

命令：`PYTHONPATH=src python3 -m pytest tests/test_podcast.py -q`。

### WP5 视频纯元数据

**独占文件：** `parse/video.py`、`tests/test_video.py`、`tests/fixtures/video/`。**输入：** 本地合成 payload。**输出：** 单一 VideoMeta。**依赖：** WP0。**并行：** 可同时。

- [ ] `test_keeps_five_comments`：like=10、comment=4、view=100、6 条评论，保留前 5；缺计数保持 None。
- [ ] `test_rejects_frames`：非空 frame_paths、本地媒体路径拒绝，临时目录无媒体文件；普通句子提到扩展名不触发读取。
- [ ] `test_invalid_metadata_types`：负数、bool 计数、非字符串评论拒绝；不自行推导点赞或评论数。

命令：`PYTHONPATH=src python3 -m pytest tests/test_video.py -q`。

### WP6 grok 适配器与 fake

**独占文件：** `llm/grok_cli.py`、`tests/fakes/grok`、`tests/test_grok_cli.py`。**输入：** prompt/schema/binary/空 cwd/temp_dir。**输出：** CompletionResult 或安全 GrokUnavailable。**依赖：** WP0。**并行：** 可同时。

- [ ] `test_parses_envelope_text_and_structured`：两层 text JSON、完整围栏、structured_output 均可得 payload，usage 不丢失；优先 structured_output，错误类型不能悄悄 fallback。
- [ ] `test_plain_payload_is_rejected`：直接stdout `{"items":[]}`、多对象、前后说明、数组、重复JSON对象键、NaN均拒绝；错误信封即使exit=0仍失败。
- [ ] `test_argv_locks_cost_and_tools`：逐项检查 S08 完整 argv、low、max-turns=1、全部 deny/disallowed/no-*、绝对 prompt/schema 路径，cwd 空且不等于项目根；不包含正文或凭据。
- [ ] `test_timeout_output_cap_and_cleanup`：fake 超时/超输出，子进程回收、临时文件清理、sandbox 空，无自动重试。
- [ ] `test_usage_and_error_redaction`：缺 total_cost 显示 None；非零/非法 stopReason 只返回安全码与可用 usage，日志无 stdout/stderr 中的秘密哨兵。
- [ ] `test_fake_is_offline_and_supports_demo`：fake 自装网络 tripwire；读结构化 prompt 生成合法分类/主题信封，默认分类 recommend、claims=[]、reason 标“离线合成结果”；支持测试场景，不需改真实 CLI 参数。

命令：`PYTHONPATH=src python3 -m pytest tests/test_grok_cli.py -q`。真实工具策略 gate 的启动校验另由 WP14 测试，不能通过本包 fake 测试自动置 true。

### WP7 非共识纯函数

**独占文件：** `consensus/minority.py`、`tests/test_minority.py`、`tests/fixtures/consensus/`。**输入：** 当前有效 Claim。**输出：** ConsensusResult。**依赖：** WP0。**并行：** 可同时。

- [ ] `test_golden_claims`：参数化运行 S10 所有纯函数 case，精确比较机会集合及每个 assessment；canonical_duplicate_integration 留给 WP13。
- [ ] `test_source_repetition_cannot_create_majority`：A 四篇对 B 一篇仍 disputed；同源双向整源排除；评论不能增加来源票。
- [ ] `test_shuffle_full_result_stable`：固定 seeds 下完整结果、排序、少数句不变。
- [ ] `test_slug_uses_topic_and_claim`：不同主题相同claim_key得到不同文件键，同键规范化后结果稳定；碰撞时不覆盖文件的验收由WP12承担。

命令：`PYTHONPATH=src python3 -m pytest tests/test_minority.py -q`。

### WP8 多源对照和可选短评

**独占文件：** `review/multi_source.py`、`review/schema.json`、`tests/test_review.py`、`tests/fixtures/review/`。**输入：** ClassifiedItem、changed_topics、CompletionFn stub。**输出：** TopicReview tuple；不写任何 wiki 文件。**依赖：** WP0。**并行：** 可同时。

- [ ] `test_review_contains_both_sources_and_reasons`：两源两项得到表格行含 URL、title、bucket、reason、评论；单源两项标 single_source。
- [ ] `test_review_failure_preserves_rows`：stub 超时/非法 topic/越界 item_id，rows 完全不变，summary_status=failed。
- [ ] `test_review_schema_bounds`：9句、缺consensus/difference、无来源句、有评论却遗漏audience、无评论却输出audience、请求外topic拒绝；合法2至8句接受。
- [ ] `test_review_batches_and_input_scope`：5个changed主题只以每批≤4个发送且prompt≤32000字符，缩行仍至少两源；BudgetExceeded后剩余budget_skipped；未变主题not_requested且不调用，确定性页仍返回。

命令：`PYTHONPATH=src python3 -m pytest tests/test_review.py -q`。

### WP9 分批三分档与证据校验

**独占文件：** `classify/buckets.py`、`classify/schema.json`、`tests/test_buckets.py`。**输入：** S04 classify_items 完整参数与 stub。**输出：** ClassificationResult，含失败批诊断。**依赖：** WP0。**并行：** 可同时，不 import WP6。

- [ ] `test_maps_three_buckets`：合法三枚举生成精确结果、run_id/hash/policy_hash 全部由调用上下文填入，不信任模型自报。
- [ ] `test_exact_ids_and_batch_atomicity`：漏 id、重复 id、未知 id、later bucket、非法 stance、额外字段均整批 invalid；没有半批分类结果。
- [ ] `test_batches_by_eight`：9 条短输入得到 2 次调用；第 1 批 schema 错而第 2 批合法，仍返回第 2 批成功结果。
- [ ] `test_prompt_bounds_and_quote_validation`：长 body 截到1500，截断之外的 quote 拒绝；超长偏好拒绝；总长约束缩批，不溢出。
- [ ] `test_comments_reach_prompt_not_votes`：视频评论出现在 audience；Claim 保留 comment 类型且 index 合法，不把其改成body证据；真正“不投票”的跨包断言由WP13承担。
- [ ] `test_pending_and_duplicates_skipped`：这些项不出现在 prompt；无有效输入则调用次数 0。
- [ ] `test_grok_unavailable_stops_later_calls`：首次 unavailable 后余批有状态但没有额外调用；预算类错误同样不绕过。

命令：`PYTHONPATH=src python3 -m pytest tests/test_buckets.py -q`。本包不 import WP7，评论“不投票”的实际跨模块验收归 WP13，WP9 只确保 evidence_kind 被保留。

### WP10 RSS、统一摄入与文稿获取

**独占文件：** `sources/rss.py`、`sources/ingest.py`、`tests/test_rss.py`、`tests/fixtures/rss/`、`tests/fixtures/podcast/`。**输入：** bytes、Source、Store、HttpClient、FixtureReader。**输出：** parse_rss、parse_local_records、persist_feed、hydrate_transcripts；摄入全部落 Store，不写私有 SQL/raw。**依赖：** WP1/WP2/WP3/WP4/WP5。**并行：** 可与 WP11/WP12 同时。

- [ ] `test_second_fetch_inserts_only_new_items`：a/b/c 三项且 a 已存在相同 hash，inserted=2、unchanged=1，a raw 字节和 mtime 不变。
- [ ] `test_backfill_cutoff_is_fixed`：首次20天前条目跳过、14天边界接受；停机20天后发现初始下界内的新 guid 仍收；已知旧 guid 改稿仍更新。
- [ ] `test_missing_future_date_and_guid`：无日期/未来日期保存但暂缓；仅 link 可稳定生成 guid；只有 title 拒绝而不是覆盖同名项。
- [ ] `test_podcast_does_not_treat_notes_as_transcript`：show notes+音频仍 pending/body_plain空；明确 transcript ready，不请求音频。
- [ ] `test_transcript_retry_after_feed_304`：首轮文稿503/pending，下一轮 feed304 文稿200文本，变 ready 并清旧分档；第三轮不重复下载 ready 文稿。
- [ ] `test_transcript_mime_and_host_guard`：.txt 返回audio、HTML、未列 host、file逃逸均拒绝；合法 VTT/SRT/plain 成功；上限后仍 pending。
- [ ] `test_invalid_feed_preserves_validators`：损坏 XML/DTD 拒绝；部分非法条目可保存合法项但不替换 feed validators；304 不丢 ETag。

命令：`PYTHONPATH=src python3 -m pytest tests/test_rss.py -q`。

### WP11 YouTube Atom 纯解析

**独占文件：** `sources/youtube.py`、`tests/test_youtube.py`、`tests/fixtures/youtube/`。**输入：** XML bytes、Source、now。**输出：** FeedBatch，与 RSS 共用 IngestRecord。**依赖：** WP3/WP5（均已依赖 WP0）。**并行：** 可与 WP10/WP12 同时；不重复实现 Store/去重/HTTP。

- [ ] `test_youtube_views_without_likes`：合成 Atom views=13931，like_count/comment_count=None，comments=()，modality=video。
- [ ] `test_youtube_missing_stats_and_namespaces`：缺 statistics 保留 None；namespace 正常解析；缺稳定键返回 EntryIssue。
- [ ] `test_youtube_never_fetches_media`：包含 thumbnail/media:content，所有网络和媒体 IO 次数仍为 0；不把网页播放 URL 误当要下载的帧。

命令：`PYTHONPATH=src python3 -m pytest tests/test_youtube.py -q`。

### WP12 WikiPublisher、阅读页与原子发布

**独占文件：** `wiki/compile.py`、`tests/test_wiki.py`。**输入：** Publication 所需完整视图数据、ConsensusResult、TopicReview。**输出：** S04 三个发布方法及快照/manifest/digest/index/log。**依赖：** WP2/WP7/WP8。**并行：** 可与 WP10/WP11 同时；不 import Store/run。

- [ ] `test_html_labels_and_pending`：中文三标题、待转写、日期待确认、未分类区、脚注均存在；空增量不展示全历史为新增。
- [ ] `test_assessment_labels_are_distinct`：2项→样本不足；2:2→争议；单源/一边倒/冲突文案各自正确；这些新快照无机会文件。
- [ ] `test_digest_escapes_untrusted_text`：title/comment/reason 中 script、恶意链接、Markdown图片转义，无自动远程资源，合法来源链接保留。
- [ ] `test_snapshot_removes_stale_current_opportunity`：首轮有机会，次轮变争议；index 指新快照且新快照无该机会；旧页仅历史可见，不删除。
- [ ] `test_publish_idempotence_and_collision`：同 Publication 再发无变化；不同完整键短 slug 撞车报错；已有最终文件 hash 不同不能覆盖。
- [ ] `test_index_never_points_to_partial_snapshot`：模拟每个 rename 前后异常，index 要么旧完整版本，要么新完整版本，不指缺文件。
- [ ] `test_append_log_once_per_complete_block`：相同完整 run block 不重复追加，半块恢复只追加完整块，无凭据/正文 dump。

命令：`PYTHONPATH=src python3 -m pytest tests/test_wiki.py -q`。

### WP13 编排、预算与恢复集成

**独占文件：** `run.py`、`tests/test_run.py`。**输入：** AppConfig、所有已验收端口、Clock/fake。**输出：** run_once、BudgetedCompletion、进程锁装配、RunReport。**依赖：** WP1/WP2/WP6/WP7/WP8/WP9/WP10/WP11/WP12。**并行：** 等完整链就绪；不改任何上游实现文件。

- [ ] `test_classifies_only_unclassified`：已有有效旧项和两个新项，complete 只收到新 id；同 guid 改稿即使早于游标也送入。
- [ ] `test_skips_pending_transcript`：pending 不分类不投票；HTML 单列；音频/ffmpeg 从未调用。
- [ ] `test_grok_failure_keeps_items_and_cursor`：fake unavailable，抓取和既有成功批保留，游标不变、报告partial、退出非零；不真实重试。
- [ ] `test_rate_limit_skips_one_source`：A429、B fixture成功，B可归档，source失败可见，全局游标不推进。
- [ ] `test_budget_stops_before_spawn`：需4个分类批时仅3次delegate调用，余项deferred；下次仅处理剩余，不重复成功项。
- [ ] `test_publish_failure_replays_without_llm`：分类已存而stage失败，重启后仅补发，模型调用次数不增加。
- [ ] `test_recover_after_index_before_db_commit`：模拟index已更新、finish未执行，下一次校验manifest补记，增量不重复。
- [ ] `test_canonical_duplicate_integration`：跨源同URL只一份当前分档/投票；alias提升不留旧claims。
- [ ] `test_comments_and_review_end_to_end`：本地视频5评论→prompt→对照→页面，观众证据不制造作者多数；短评失败确定性页仍发布。
- [ ] `test_two_runs_are_mutually_exclusive`：第二个锁申请失败，HTTP/LLM调用均0；释放后可运行，锁文件不被删除。
- [ ] `test_log_failure_is_recoverable`：日志失败不重复模型记账，pending标记可补写，发布游标不被伪回滚。

命令：`PYTHONPATH=src python3 -m pytest tests/test_run.py -q`。

### WP14 配置、CLI、README 与总验收

**独占文件：** `config.py`、`cli.py`、`README.md`、`tests/test_cli.py`。**输入：** S04 AppConfig/Source/RuntimePolicy 和 run_once。**输出：** 严格配置加载、模块/脚本入口、离线示例、故障/费用/隐私说明。**依赖：** WP13 及其传递依赖。**并行：** 集成最后进行，不回头修改 WP0 文件。

- [ ] `test_config_strict_and_fixture_boundary`：重复source_id、cookie字段、别名环、未知format、symlink逃逸、私密query拒绝；合法file配置可运行。
- [ ] `test_example_cli_runs_offline`：S12完整演示参数在tmp_path得到三分档HTML与摘要，第二轮无新分类；不覆盖既有真实配置。
- [ ] `test_live_requires_authorization_and_tool_verification`：未确认计费/工具或offline下指定真实grok，subprocess调用0、清晰ConfigError；不检查auth内容、不自动登录。
- [ ] `test_cli_exit_codes`：完整0、可读部分2、配置/锁/发布致命1；错误消息无正文/凭据；无子命令打印帮助并0退出。
- [ ] `test_config_and_output_secret_sentinel`：注入凭据哨兵至不允许的字段、fake错误输出，所有文件/日志/异常均不回显；仅被拒绝而非进入prompt。
- [ ] README 写完整运行命令、冻结时间、依赖就绪前提、未知计费、隐私、部分失败/恢复、无文稿/无评论限制、无自动全文抓取、Q事项和本计划链接。
- [ ] `test_import_layers` 与 `test_scope_contract` 全模块检查，无缺失模块、上层反向导入、未批准依赖或额外子进程入口。
- [ ] 执行全量 pytest，逐条对应 S14；记录真实退出码与测试数，不能写“应该通过”。

命令：`PYTHONPATH=src python3 -m pytest tests/test_cli.py tests/test_boundaries.py -q`，随后 `PYTHONPATH=src python3 -m pytest -q`。除有新变更/失败外不反复扩大无关测试，不为了验收联网或调用真实 grok。

## S14 总验收清单

前 16 项保留并细化 v2 的总验收，后续项补实施裂缝。每条只有在对应 pytest 真实通过后才能勾选；此文档阶段全部保持待执行。

| 勾选 | ID | 给定输入与必须观察到的结果 | pytest 证据 |
|---|---|---|---|
| [ ] | A01 | RSS a已入库，再摄入a/b/c只新增2，a raw不变 | `tests/test_rss.py::test_second_fetch_inserts_only_new_items` |
| [ ] | A02 | 同guid正文变更，旧分类/claims清除，新raw新增、旧raw保留 | `tests/test_store.py::test_updated_clears_classification` |
| [ ] | A03 | loopback/link-local/IPv6/metadata被拒，不发请求不插item | `tests/test_http.py::test_url_guard_matrix`、`tests/test_cli.py::test_config_strict_and_fixture_boundary` |
| [ ] | A04 | 文字有title/body_plain、modality=text、script不进正文 | `tests/test_text.py::test_strips_tags`、`test_entities_and_script_removed` |
| [ ] | A05 | 明确文稿ready，没有音频下载 | `tests/test_podcast.py::test_ready_when_transcript_present` |
| [ ] | A06 | 只有音频pending、body空，无ffmpeg/grok | `tests/test_podcast.py::test_pending_when_only_audio_url`、`tests/test_run.py::test_skips_pending_transcript` |
| [ ] | A07 | 视频保留5评论和nullable计数，媒体路径拒绝且无文件 | `tests/test_video.py::test_keeps_five_comments`、`test_rejects_frames` |
| [ ] | A08 | YouTube Atom解析views，不虚构likes/comments，不抓thumbnail | `tests/test_youtube.py::test_youtube_views_without_likes`、`test_youtube_never_fetches_media` |
| [ ] | A09 | 只分档当前无有效分类版本，不用时间游标漏旧未分类项 | `tests/test_run.py::test_classifies_only_unclassified`、`tests/test_store.py::test_old_unclassified_is_not_lost` |
| [ ] | A10 | 桶仅三枚举，HTML精确中文标题 | `tests/test_buckets.py::test_maps_three_buckets`、`tests/test_wiki.py::test_html_labels_and_pending` |
| [ ] | A11 | 同主题两源页面有URL、标题、reason和评论信息 | `tests/test_review.py::test_review_contains_both_sources_and_reasons` |
| [ ] | A12 | 4:1独立来源有少数句，2项不足、2:2争议均无机会 | `tests/test_minority.py::test_golden_claims`、`tests/test_wiki.py::test_assessment_labels_are_distinct` |
| [ ] | A13 | grok失败抓取保留、游标不变、CLI非零 | `tests/test_run.py::test_grok_failure_keeps_items_and_cursor`、`tests/test_cli.py::test_cli_exit_codes` |
| [ ] | A14 | fake信封正确解析，argv全保护项存在，plain payload拒绝 | `tests/test_grok_cli.py::test_parses_envelope_text_and_structured`、`test_argv_locks_cost_and_tools`、`test_plain_payload_is_rejected` |
| [ ] | A15 | A429不阻止B，host状态跨实例持久化 | `tests/test_run.py::test_rate_limit_skips_one_source`、`tests/test_http.py::test_failure_backoff_persists` |
| [ ] | A16 | 全测试无真实网络/模型/媒体进程，tripwire能挡住负例 | `tests/test_boundaries.py::test_network_tripwire` 与全局autouse汇总 |
| [ ] | A17 | 固定回填下界、不丢停机间更新，缺日期暂缓 | `tests/test_rss.py::test_backfill_cutoff_is_fixed`、`test_missing_future_date_and_guid` |
| [ ] | A18 | feed304后pending文稿仍能恢复，类型/host守卫生效 | `tests/test_rss.py::test_transcript_retry_after_feed_304`、`test_transcript_mime_and_host_guard` |
| [ ] | A19 | canonical重复不多分类/多投票，主项变更不留孤儿 | `tests/test_store.py::test_canonical_duplicate_and_promotion`、`tests/test_run.py::test_canonical_duplicate_integration` |
| [ ] | A20 | 9条分两批，坏批不污染好批，id/quote严格校验 | `tests/test_buckets.py::test_batches_by_eight`、`test_exact_ids_and_batch_atomicity`、`test_prompt_bounds_and_quote_validation` |
| [ ] | A21 | A源连发不造多数；评论不入票；shuffle不变 | `tests/test_minority.py::test_source_repetition_cannot_create_majority`、`test_shuffle_full_result_stable` |
| [ ] | A22 | 短评失败仍有可读确定性页，引用不越界 | `tests/test_review.py::test_review_failure_preserves_rows`、`test_review_schema_bounds` |
| [ ] | A23 | 分类完成但页面失败，下次补发不重调模型 | `tests/test_run.py::test_publish_failure_replays_without_llm`、`test_recover_after_index_before_db_commit` |
| [ ] | A24 | 旧机会不留在当前快照，半发布不影响index有效性 | `tests/test_wiki.py::test_snapshot_removes_stale_current_opportunity`、`test_index_never_points_to_partial_snapshot` |
| [ ] | A25 | 第4次分类在启动前被预算阻止，缺usage金额为UNKNOWN | `tests/test_run.py::test_budget_stops_before_spawn`、`tests/test_grok_cli.py::test_usage_and_error_redaction` |
| [ ] | A26 | 日志/页面/异常无秘密哨兵，恶意HTML不能执行 | `tests/test_cli.py::test_config_and_output_secret_sentinel`、`tests/test_wiki.py::test_digest_escapes_untrusted_text` |
| [ ] | A27 | 外键/WAL/事务/未知schema行为正确 | `tests/test_store.py::test_schema_pragmas_and_idempotence`、`test_save_batch_is_atomic` |
| [ ] | A28 | 并发run第二个锁失败且零取数，日志失败可补 | `tests/test_run.py::test_two_runs_are_mutually_exclusive`、`test_log_failure_is_recoverable` |
| [ ] | A29 | 离线CLI新目录完整演示，live缺核验不启动 | `tests/test_cli.py::test_example_cli_runs_offline`、`test_live_requires_authorization_and_tool_verification` |
| [ ] | A30 | 全模块无依赖环、文件角色单一、scope未扩张 | `tests/test_boundaries.py::test_import_layers`、`test_scope_contract` 与调度员文件清单核对 |

每包的其他测试仍是必需验收，表中列的是总链路代表点，不是允许省略的“选做”。最后报告应附命令、测试数、退出码、网络/真实进程尝试计数（除tripwire负例）、fixture时间与生成HTML路径。没有真实外部 smoke 时必须明说，不编造线上可用结论。

## S15 本阶段明确不做

- 不把未确认的第三方视频或同名仓库当成必须复制的原版。
- 不 fork、不 clone、不复制外部源码或 gist 全文。许可证是 MIT。不把本仓库改成公开。
- 不登录 Bilibili、抖音、视频号、小红书；不使用 Cookie、不调 api.bilibili.com、不绕限流。host策略测试不等于平台适配。
- 不安装或调用 Whisper、yt-dlp、ffmpeg、云 STT，不下载音频/画面，不逐帧。无文稿播客功能明确降级为 pending，不声称已完成音频理解。
- 不调 YouTube Data API，不凭公开RSS补出点赞/评论，不用抓网页评论替代未授权API。
- 不抓文章全文、付费墙、图片OCR、PDF或浏览器私人历史；不自动导入任意本地文件。
- 不加向量检索、图数据库、Web框架、端口、账号、通知机器人或常驻守护进程。cron 由操作者后续自行决定，不创建系统定时任务。
- 不把 LLM 对照当事实核验，不把来源票当市场共识，不生成投资建议。
- 不自动重新分档全部历史、不自动扩大回填、不自动充值/换模型/升推理档/重试付费调用。
- 不迁移未知已有数据库，不清理历史raw或快照；保留期限、备份和清理另议。
- 本次第3步不写实现、不安装、不联网、不执行pytest业务测试，只产出两份计划文档。

## S16 实施风险与缓解

| 风险 | 后果 | 本版措施与验证 | 仍存限制 |
|---|---|---|---|
| CLI信封/stopReason/参数漂移 | 错解析或无声截断 | WP6严格两层解析、unknown拒绝、完整fake矩阵 | 真实版本行为UNKNOWN，Q03 |
| 禁用列表不能覆盖工具/全局上下文 | 费用或隐私外溢 | 低effort、空cwd、全部既有禁用参数、live gate默认关 | fake不能证明真实隔离；核验失败不能启用 |
| 认证优先级冲突/登录续期 | 走错计费路径或阻塞 | 不读auth、不自动login，不明路径拒绝 | 账户余量和刷新成功与否UNKNOWN，Q04 |
| reasoning与输出量无法硬封顶 | 实际费用超估算 | 次数/字符预算、无自动重试、usage账本、金额缺失UNKNOWN | 不承诺日/月成本或免费额度 |
| YouTube缺views/评论 | 空值被当0或造观众态度 | nullable字段、无评论文案、API关闭 | API数据需Q05 |
| 播客无文稿或transcript错误 | 浪费模型调用、伪转写 | pending排除、304独立重试、MIME/host守卫 | 音频理解需Q07 |
| SSRF重定向/混淆IP | 访问内网 | 每跳guard、CIDR/IPv6、限host、禁代理 | DNS rebinding未解决，不能接任意外部URL |
| 429/403/服务故障 | 连续撞站或全局中断 | 持久host退避、Retry-After、三次熔断、异host隔离 | 本地参数不是官方SLA |
| 来源注入/模型伪造证据 | 错事实/危险输出 | 无工具、strict schema、id等集、原文子串、HTML转义 | 原文存在不保证模型解释正确 |
| feed未返回停机期间的全部内容 | 更新覆盖不完整 | 固定首次下界不再本地误过滤，展示本次抓取时间和来源失败 | 远端历史保留范围UNKNOWN，本期不补抓历史全文 |
| 不同命题混合/单源刷屏 | 假非共识 | 主题+命题范围、来源一票、冲突排除、黄金集 | 语义等价和来源独立性仍可能误判，Q11 |
| 跨源去重误合并或遗漏转载 | 漏证据/重复意见 | 保守URL规范化、别名可追溯、主项重选 | 不做语义去重，不同URL转载仍可能重复 |
| 文件发布与数据库中断 | 丢读物、重复花费、旧机会残留 | 不可变raw/快照、manifest、未发布队列、恢复测试 | 磁盘空间不足要停止并报告，不删资料救场 |
| 多agent改共享文件 | 接口漂移与集成冲突 | WP0独占冻结、文件清单、DAG与AST检查 | 调度员必须执行所有者规则 |
| 依赖版本/安装包不可用 | 无法运行pytest | 沿用材料固定版本，缺包如实报告 | 本步未检查安装状态；不自动联网换版本 |
| 版权/个人资料/日志泄漏 | 无意传播原文或账户信息 | 本地受保护目录、示例合成、日志白名单、不公开默认 | 保留期限、公开边界由Q08/Q12决定 |

## S17 待操作者决定与开放问题

这些事项没有被本计划替操作者批准。**都不阻塞第4步的离线实现和合成验收**；其中 Q01/Q03/Q04 是启用真实服务前必须补齐的条件。工程兼容性核验应由实施者给证据，不要求操作者猜 CLI 技术细节。

| 编号 | 需要决定或补证据的事项 | 未决定时行为 |
|---|---|---|
| Q01 | 真实博客/播客RSS与YouTube channel_id名单；transcript允许外域 | 仅fixture，不联网 |
| Q02 | 真实偏好、必读触发、旧偏好是否批量重分档、是否需要已读状态 | 四标题合成偏好；不宣称知道已读；不重跑历史 |
| Q03 | 是否授权一次真实grok smoke；所用CLI是否确能无工具、无仓库上下文；失败时是否允许登录 | live gate关闭；不调用、不登录；协议兼容性UNKNOWN |
| Q04 | 订阅池还是API；实际账户余量、Additional Usage控制、双凭据优先级与使用预算 | 测试路径；无美元承诺、不读凭据、不买额度 |
| Q05 | 是否后续使用YouTube Data API key与授权官方计数/评论请求 | 只公开RSS字段与合成评论；本期API不实现 |
| Q06 | 是否提供目标视频或置顶 URL | 原版仓库继续未确认；底座仍从零搭 |
| Q07 | 无文稿是否未来安装Whisper或用付费STT；ready文稿是否周期复查、人工补录方式 | 不安装、不转写；pending保留；ready不强制重拉。材料的Whisper磁盘/VRAM不是同一个指标，实际下载量UNKNOWN |
| Q08 | 自有代码许可证、是否公开、可公开哪些原文/报告 | MIT，见 LICENSE。本仓库不改为公开。不复制外部源码 |
| Q09 | 默认14天回填是否调整、未知日期如何人工确认、已有源是否回填历史 | 固定首次下界，未知/未来日期暂缓；无自动回填 |
| Q10 | 是否关闭LLM主题短评、允许的分类/短评调用次数与偏好长度 | 使用明确工程默认；超额排队，短评可降级 |
| Q11 | 人工20条抽取黄金集、来源独立分组、命题别名、比较时间窗 | 仅合成规则测试；当前全库且命题注明时期/范围；实际抽取准确率UNKNOWN |
| Q12 | 公开源内容发给xAI的接受范围、私人源策略、DNS防护升级、保留期/备份/定时 | 仅公开源；所有真实发送须Q03/Q04；不扩URL能力、不自动清理/建cron |

历史配额只作背景：评审记录 [YouTube 配额页](https://developers.google.com/youtube/v3/determine_quota_cost) 当时写新项目 search.list 100 次、videos.insert 100 次、其余合计10000 units/day；本期不调用，当前实际项目配额 UNKNOWN，不能把这个数当已获授权的额度。历史 [Whisper README](https://raw.githubusercontent.com/openai/whisper/main/README.md) 给的是不同模型 VRAM 示例，`mstick3/brain` README 的首次约1.5GB是作者环境说法，不能当本机安装承诺。

## S18 实施交接门槛

1. 先读本文件和 `CHANGES_V2_TO_V3_CN.md`。已有提示/评审/研究笔记保持只读；不要读取其他项目或凭据，不初始化Git。缺能力用安全错误报告，不能自行扩大安装/联网/账号范围。
2. 把 WP0 单独交一个 agent，产出模型、端口、测试安全边界与示例。逐条对照 S04/S05/S08/S11 的所有补充签名，合成一个完整接口版本；这是实现冻结文本，不允许重新发明字段。WP0 测试通过后调度员接纳。
3. 按可用并发槽位派 WP1至WP9，每条派工消息写明独占文件、依赖、公共接口和独立测试命令。并发数量是环境资源问题，不改DAG；不强制安装未提供的 superpowers skill。
4. 依赖就绪就启动 WP10/WP11/WP12。若遇接口缺失，暂停受影响包的接口使用，交WP0所有者一次修正并同步，不让每包加自己的字段；无关包继续。
5. 接纳WP13，先跑失败/恢复/去重/预算集成，再跑正常run。所有HTTP和LLM仍是fake，不用真实调用“证明集成”。
6. 接纳WP14，完整跑本地CLI示例与全量pytest，检查S14全部条目，打开本地生成HTML核对标签、链接、评论和未完成提示；本地打开不加载远程资源。
7. 交给审阅者验收实现、文件清单、测试命令/结果、已知限制与Q清单。没有真实服务授权时交付“离线实现通过”，不得自称已接入操作者真实账号或线上抓取验证通过。

本版的设计门槛是：需求映射齐全、所有跨包接口只有一个定义、DAG无环、每包文件独占且有独立可跑的验收、失败路径和未知外部事实均显式。满足这些条件即可进入第4步离线实施；第4步完成门槛是S14和每包测试实际通过。真实服务启用是另一个门槛，不能用计划完成或fake测试替代。
