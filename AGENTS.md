# AGENTS.md — Decis 工程约束

Decis 是"一个 API 跑所有轻量决策模型"的推理服务框架。它把 jev / TypeSafe System One 的线格式实现一次，把各家开源决策模型（kev、Laya 等）作为可插拔引擎接进来。

本文件是**在这个仓库里工作的契约**。它写给 AI agent，也写给人类。规则不是建议，是约束；违反约束的改动即使"能跑"也不接受。

> **当前状态**：契约层（`schema.py` / `render.py` / `answers.py` / `errors.py` / `auth.py`）、引擎抽象、
> `paths.py` 权重解析、`hub.py`（Hub 选择与 ModelScope 回退）、`scheduler.py`、`config.py`、`cli.py`
> （含 `decis download`）、`docker/Dockerfile` 与 CI 都已实现，三个真实模型家族跑在同一个契约后面：
> `uv sync --extra laya && uv run decis serve --engine laya-multilingual`、
> `uv sync --extra kev && uv run decis serve --engine kev-0.8b`、
> `uv sync --extra jeff && uv run decis serve --engine jeff-qwen3.5-0.8b`。
> **未实现**：kev 的 prefix 缓存路径、`/metrics`；`docs/design.md` 里那批只在设计稿里出现的旋钮
> （`DECIS_BATCH_MODE`、`DECIS_BATCH_MAX_WAIT_MS`、`DECIS_BATCH_MAX_SIZE`、`DECIS_MAX_QUEUE`、
> `DECIS_MAX_QUESTIONS`、`DECIS_MAX_STATE_CHARS`、`DECIS_CORS_ORIGINS`、`DECIS_HTTP_WORKERS`、
> `DECIS_ENGINE_WORKERS`、`DECIS_RATE_LIMIT_RPM`、`DECIS_RATE_LIMIT_TOKENS_PER_S`）没有任何代码读，别照它们写文档。`docs/design.md §11` 的目录树是
> 目标结构，其中未出现的文件即尚未实现的部分。
>
> **引擎注册表**：出厂六个——`laya`、`laya-multilingual`、`laya-typed-decisions`、`kev-0.8b`、
> `jeff-qwen3.5-0.8b`、`jeff-gemma4-e2b`。其中两个需要第二个仓库：`kev-0.8b` 是适配器 + Qwen3.5-0.8B
> 基座（见 `paths.BaseModel`），而两个 Jeff 是**全权重微调**，一个目录里就是全部，`bases` 为空。
> **注册表里没有测试替身**：无权重的确定性测试替身住在 `tests/fixture_engine.py`，由 `tests/conftest.py`
> 以 id `stub` 只在测试进程里注册，出厂镜像永远不会用它作答。
>
> **镜像**：`.github/workflows/docker-build.yml` 按引擎构建、推送到 Docker Hub 的**一个**仓库
> `chaitin/decis`，**引擎就是 tag**（`chaitin/decis:laya-multilingual`）。命名空间来自工作流的
> `IMAGE_NAMESPACE`（默认 `chaitin`，仓库变量 `DOCKERHUB_NAMESPACE` 可覆盖），**不从
> `DOCKERHUB_USERNAME` 推导**——那是登录身份，不是发布目标。tag 规则：**内置权重的 baked 变体只在
> release tag（`<engine>-v1.2.0`）或明确要求它的手动 dispatch 时构建**，默认分支与 feature 分支只发
> 不带权重的 `-runtime` 变体（默认分支用移动名 `<engine>-runtime`，其他分支用
> `<engine>-runtime-sha-<7>`）；release 额外把不带后缀的 `<engine>` / `<engine>-runtime` 指向刚发布的
> 镜像，`laya-multilingual` 的 baked 变体另外拿裸 `latest`（随 release 移动，绝不给别的引擎或
> `-runtime` 镜像）；`variants: baked|both` 的手动 dispatch 在默认分支上同样会移动这些别名与 `latest`
> ——它是 release 的手动替身，于是 `:latest` 与 `:laya-multilingual` 不会各自漂移。任何分支推送都不
> 下载权重。
> `playground` 是唯一的非引擎镜像（release 为 `playground-<version>`）。多架构（amd64 + arm64 原生
> runner）、带 SBOM 与 provenance；只有 Docker Hub 一个 registry，**GHCR 不再推送**。
> tag 全表在 `docs/deployment.md`，规则的理由在工作流的注释里。
>
> **本地编排**：`docker-compose.yml` 是部署文件，用 profile 选引擎，**profile 名 == 引擎 id ==
> image tag == `--engine` == `DECIS_DEFAULT_ENGINE`**。权重在构建期写入镜像的 `DECIS_MODEL_DIR=/models`，
> **不许往那里挂卷**（挂上去会盖掉镜像里已有的权重，容器转去联网下载而不报错）。compose 层的探针打
> `/readyz`（该层不会因探针失败重启容器，所以 `--wait` 是真的就绪等待），镜像自带的 `HEALTHCHECK`
> 仍是 `/healthz`，给编排器用。`docker-compose.override.yml` 靠**文件名**被 Compose 自动叠上，
> 于是源码目录里的裸 `docker compose up` 用本仓库源码构建 `decis-local:*`，部署只拷基文件。
>
> **容器内已实测的边界**：内置权重的引擎镜像与 playground 镜像都从 `chaitin/decis` 拉下来跑过——
> 前者在 `HTTP_PROXY`/`HTTPS_PROXY` 指向一个没有服务监听的端口时从 `/models/...` 加载、零下载，并作出完整契约响应
> （无凭证 403、错 key 401）。**未验证**：`-runtime` 变体只验到能构建、能合并、体积对（没拉下来跑过）、
> kev 的容器内冷启动、kev 的 compose 路径。两个 Jeff 镜像在 2026-09-30 第一次构建并推送成功，
> 体积已从 registry manifest 读出并记进 `docs/deployment.md`，但**同样没拉下来跑过**——它们的
> 容器内冷启动不在上面那条"已实测"里。报镜像相关的结论不要超出这个范围。

---

## 1. 必读文档

| 文档 | 作用 | 什么时候必须读 |
|---|---|---|
| [`docs/api-compatibility.md`](docs/api-compatibility.md) | 对外线格式的**唯一事实来源**，含证据等级（L > S > A > B > C > D） | 任何涉及请求/响应字段的改动 |
| [`docs/api.md`](docs/api.md) | 面向调用方的 API 参考：端点、原语、错误码、容量上限（双语） | 改端点、错误码、容量校验或 `decis` 命名空间时 |
| [`docs/schema/`](docs/schema/) | 由 `src/decis/schema.py` **生成**的 JSON Schema + OpenAPI（`export.py --check` 进 CI） | 改任何线格式模型时；不得手改生成的 JSON |
| [`docs/design.md`](docs/design.md) | 架构、抽象、并发、打包方案 | 任何新增模块或引擎的改动 |
| [`docs/design-review.md`](docs/design-review.md) | 对本设计的**自我审查**：已修正的缺陷、方法论局限、尚未验证的假设 | 动手实现前；以及任何"这个设计是不是已经想清楚了"的疑问 |
| [`docs/feasibility.md`](docs/feasibility.md) | 调查证据、实测数字、风险登记 | 讨论性能预期或选型时 |
| [`docs/contract/`](docs/contract/) | 官方 OpenAPI 快照（L0 测试基准）+ 线上观测原始记录（L 级证据） | 任何契约相关改动；**改前必须跑一次线上差分** |

面向用户的双语指南（`docs/getting-started.md`、`configuration.md`、`deployment.md`、`api.md`、
`engines.md`、`playground.md`、`performance.md`，各有 `.zh-CN.md` 孪生）是**产品的一部分**：
改一种语言就必须改另一种，`tests/test_docs.py` 盯着文件对是否存在、互链、以及相对链接 /
`DECIS_*` 变量 / 代码块语言 / 生成标记在两侧是否一致。

冲突时优先级：`api-compatibility.md` > `design.md` > 其余。

**`design-review.md` 不是历史文档，是活文档。** 它列的"尚未验证"清单在对应验证完成前一直有效。

**M5 已经有数据了（§4-M5），结论是否定**：跨请求批处理不提升吞吐。所以原来那条"不得承诺 QPS"的理由消失了，
但**新理由接上**：现在可以报的是一条**负结论**加一个**实测的吞吐上界**（单进程 24 线程，`laya-multilingual`，
CPU，约 1.2 项/秒，只有 16 个生成项、合成批的串行路径），**不得**把它包装成"批处理带来的高 QPS"，
也**不得**把它外推到 GPU、kev 或真实并发负载——那三样都没有数据。

---

## 2. 唯一事实来源（One canonical home）

每个概念只能有一个实现处。**发现第二处实现就是 bug**，即使两处当前行为相同。

| 概念 | 唯一所在 | 禁止 |
|---|---|---|
| 各层共享的领域类型（`Option`/`PreparedQuestion`/`PreparedRequest`/`ProbDist`） | `src/decis/domain.py` | 在 `schema.py`/`render.py` 里另定义一份；`domain.py` **不得 import 包内任何模块** |
| `state`/`instructions`/`criteria` → 可读文本（任意 JSON 的扁平化） | `src/decis/render.py` | 引擎各自实现 JSON 扁平化；引擎各自做分隔符转义 |
| 「调用方原始 JSON 原样交给需要它的引擎」 | `domain.PreparedQuestion.raw` / `PreparedRequest.raw_state` / `WorkItem.raw_state`，由 `render.py` 填 | 引擎自己去解析请求体；把扁平化后的文本当成 `raw`（Jeff 的提示词是 `json.dumps`，传入错误的文本不会报错，只是答案变差） |
| `--model-path ENGINE=PATH` 的语法与别名规范化 | `src/decis/cli.py: _parse_model_paths`（规范化后写进 `Settings.model_paths`） | 引擎自己解析命令行参数；用把引擎 id 编进**变量名**的方式做覆盖（点号在变量名里没有表示法，见 §9） |
| 第三方 vendored 副本的字节 | `src/decis/engines/_kev_vendor/`、`src/decis/engines/_jeff_vendor/`，各自带 `VENDOR.md` | 就地改 vendored 代码；re-vendor 时不一起改 `VENDOR.md` / `NOTICE` / 守卫里的 sha256 |
| 「某个引擎需要哪个 Python 版本」 | `src/decis/engines/registry.py: EngineSpec.python_min`（`jeff.MIN_PYTHON` 是同一数字给 `load()` 用的那份，`pyproject.toml` 的 marker 是给包管理器的那份） | 三处各写一个数字而不加守卫；把"vendored 代码需要更新的语法"报成"缺依赖"（Jeff 要 3.12，`design-review.md §2-D31`；守卫 `tests/test_engines_jeff.py` 把三处钉在一起，并用 `ast.parse(feature_version=…)` 把 floor **从 vendored 源码推出来**） |
| 把渲染片段排成**某个引擎自己的序列** | 该引擎（并优先用它上游库的函数，如 Laya 的 `build_sequence`） | 在 `render.py` 里重写某个模型的序列格式——那是对上游内部的复制，保证会漂移（`design.md §4.1` 的 Stage 1 修正） |
| `noul` 的选项名 `"false"/"true"` | `src/decis/render.py: noul_options` | 任何地方写字面量 `Option("false", …)` |
| 「这个请求会消耗多少 token」（容量校验的**测量**） | 各引擎的 `DecisionEngine.measure` | 用 `len(text)//4` 估算一个会截断的引擎；在 `render.py` 里猜某个模型的 head 开销 |
| 概率分布 → `Noul`/`Choice`/`Score` answer | `src/decis/answers.py` | 引擎返回线格式 answer |
| `confidence` 计算 | `src/decis/answers.py` | 引擎各自算 confidence 并直接透出 |
| question 的 wire key（`"false"/"true"`、选项名、`"0".."n-1"`） | `src/decis/answers.py: question_keys` | 任何地方重复这份规则 |
| 线格式 Pydantic 模型 | `src/decis/schema.py` | 路由里零散定义 model |
| 请求容量校验的**策略**（选项数、token 预算、错误形状） | `src/decis/schema.py: validate_capacity` | 让引擎截断后静默给出劣化答案 |
| upstream 的 head 预算换算（`budgeted_head`） | `src/decis/engines/laya.py` | 在别处再算一遍这个不截断条件 |
| 异常 → 契约错误响应（状态码、`detail` 多态形状） | `src/decis/errors.py` | 路由里直接 `raise HTTPException` 拼 body |
| Bearer 校验、常数时间比较、401/403 分工 | `src/decis/auth.py` | 在路由或中间件里各写一份鉴权 |
| `x-typesafe-request-id` 生成与请求日志 | `src/decis/observability.py` | 各处在响应上手写这个 header |
| 单请求等待预算（取锁上限、429 的退避值） | `src/decis/scheduler.py: InProcessScheduler.run` | 在路由或 `config.py` 里再判一次超时；把阻塞函数写成 `async def` 路由 |
| 引擎加载状态（idle/loading/ready/failed 与失败原因） | `src/decis/scheduler.py: LoadStatus` | 在 CLI/路由里各写一份"就绪"判断；用 `ready` 一个布尔表示"为什么不能服务" |
| 权重路径解析、完整性判定、下载清单、"本机缺哪个模块" | `src/decis/paths.py` | 引擎自己决定去哪找权重；引擎自己调 `snapshot_download` |
| 「一个 checkpoint 需要哪些仓库」（适配器 + 它适配的基座） | `src/decis/paths.py: BaseModel` / `WeightSpec.bases` | 引擎自己下载基座；把基座写成引擎里第二个硬编码 repo id |
| 「网络取权重时问哪个 Hub、pin 能不能兑现」 | `src/decis/hub.py`: `choose` / `Selection.pinned` / `download` | 引擎或 CLI 自己判断『连得上吗』再挑客户端；把 `WeightSpec.revision` 转交给不支持 commit 的客户端（ModelScope 收到 sha 会**静默成功且一个文件都不下**，`design-review.md §2-D32`）；在 `hub.py` 之外提 `snapshot_download` / `huggingface_hub` / `modelscope`（守卫 `tests/test_conventions.py::test_only_hub_names_a_hub_client`）；把 repo id（尤其 `f"{repo_id}@{revision}"` 这种字串）交给**会自己联网**的上游 loader——vendored 的 `resolve_run` 拿到它就自己调 Hugging Face 客户端，于是 `DECIS_HUB`/`--hub` 被绕过（`design-review.md §2-D33`；守卫 `tests/test_engines_kev.py::test_the_vendored_loader_is_handed_a_directory_not_a_hub_id` 与 `test_conventions.py::test_engines_do_not_build_a_hub_id_string`） |
| 「本机有没有装某个模块」 | `src/decis/paths.py: module_available` | 在别处再写一遍 `importlib.util.find_spec`（少写那个 `except` 就会把『父包不存在』报成已安装；守卫 `test_module_presence_has_one_home`） |
| `(引擎, 设备) → dtype`、以及"能跑但性能已知很差"的组合 | `src/decis/engines/registry.py: DTYPE_DEFAULTS` / `DEGRADED` | 引擎自己判断 dtype；全局统一一个 dtype（kev 在 CPU 上 bf16 比 fp32 慢 83 倍） |
| `DECIS_DEVICE` 的合法取值、「这台机器能用哪个设备」、「不设时用哪个」，以及「明明有 GPU 为什么在 CPU 上服务」 | `src/decis/engines/devices.py: DEVICES` / `ACCELERATOR_ORDER` / `available_devices` / `best_device` / `requested_device` / `torch_build` / `nvidia_gpus` / `accelerator_advice` / `warn_if_accelerator_is_idle` | 引擎自己写设备回退（`settings.device or "cpu"` 曾让 kev 在 Apple 芯片上跑 CPU，实测慢 15 倍）；在别处再判断一次"CUDA/MPS 可用吗"；或在 `devices.py` 之外再拼一次 `nvidia-smi` 查询——PyPI 的 Windows torch 是 CPU-only 构建，`torch.cuda.is_available()` 分不开"没有 GPU"和"这个 wheel 用不了 GPU"，所以这一问只能有一处（`design-review.md §2-D36`） |
| 「某个 primitive 的选项在提示里长什么样」 | 各引擎自己的 record 构造 | 让 `render.py` 决定——kev 的 noul 是 `no`/`yes`、score 是裸层级文本，与 Decis 的 `Option.name` 不同（`design-review.md §2-D9`） |
| 「这个引擎**现在**能不能跑」的分类（依赖 + 权重） | `src/decis/engines/registry.py: status` | 在 CLI 或路由里各写一份"就绪"判断；把"注册了"当成"能跑"报给用户 |
| 引擎 id → 实现的映射 | `src/decis/engines/registry.py` | `if engine == "..."` 散落在业务代码里 |
| 环境变量（含代理绕过名单的规范化） | `src/decis/config.py: normalize_proxy_environment` / `ensure_loopback_bypass` | `os.environ` 出现在其他模块；在别处再解析一遍绕过名单（`[::1]` 会让 httpx 在**构造客户端**时就抛 `InvalidURL`，于是每一次下载都失败，见 `design-review.md §2-D25`） |
| playground 页面的调色板 / 字体 / 组件样式 | `playground/web/theme.css` | 页面在自己的 `<style>` 里再抄一套颜色或按钮样式（`tests/test_playground.py` 盯着） |
| playground 的界面语言（检测、切换、顶栏文案） | `playground/web/i18n.js` | 页面自己实现语言检测或切换；把要发给模型的 `state`/`instructions`/`criteria` 翻译掉——那是 API 的语言，也是上面那些 token 数字量出来的那份字符串 |
| 三个小游戏共用的界面外壳（手动/AI 开关、推理面板、最近一次调用的控制台、快捷键、引擎状态灯、开始/重置按钮） | `playground/web/game.js`（`window.GameShell`） | 页面各自实现模式开关、自己轮询引擎、自己算延迟与吞吐；页面里再抄一份"最近一次调用"的面板（`tests/test_playground.py` 盯着） |
| 「怎么构建、怎么起服务」的快捷方式 | `Makefile`（目标全部转调 Compose） | 在 Makefile 里重写镜像 tag / 引擎 id / 构建参数（归 compose 与工作流）；在文档里写一个不存在的 `make` 目标（`tests/test_makefile.py` 两条都盯着） |
| 对外 API 的 JSON Schema / OpenAPI | `docs/schema/export.py`（由 `src/decis/schema.py` 的模型生成） | 手改 `docs/schema/*.json`（生成物；`tests/test_api_schema.py` 盯着）。改线格式要改 `schema.py` 再 `export.py --write` |
| 用户文档的语言版本 | `docs/<name>.md` + `docs/<name>.zh-CN.md` 成对存在 | 只改一种语言；两侧的相对链接 / `DECIS_*` 变量 / 代码块语言 / 生成标记不一致（`tests/test_docs.py` 盯着） |

`tests/test_conventions.py` 是这些规则的守卫（照抄 kev 的做法：一张"唯一事实来源"表 + 断言）。**新增一个 canonical helper 时，同时加一行守卫。**

---

## 3. 契约不变量（不可破坏）

每一条都必须有测试守着。改坏它们等于破坏项目存在的理由。

**唯一例外是第 18 条**：目前没有攒批器，所以那条**没有守卫**——它是写给将来那个实现的约束，
不是对现有代码的描述。**而且 M5 的实测结论已经让"要不要实现攒批器"本身成了待决项**：如果实现，
这条依然有效，且必须同时把守卫补上，否则它就是空文。

1. **`score` 的 `legend` 与 `probabilities` 的键是字符串** `"0"`、`"1"`、…。不是数组，不是整数。
2. **`noul` answer 是标量** `{"type":"noul","noul":p}`，**没有 `confidence`，没有 `probabilities`**。
3. **`noul` 在内部展开成 2 个选项** `["false","true"]`，`noul = p[1]`。
4. **`choice.probabilities` 的键与请求 `criteria` 的键逐字相同**，顺序一致；`choice == argmax(probabilities)`。
5. **`score == Σ k · p_k`**（0 基）。
6. **`answers` 的键集合 == `questions` 的键集合**；question id **永远不发给模型**。
7. **顶层响应恒含** `model`、`answers`、`usage{input_tokens, output_tokens}`。
8. **`model` 字段回填版本化 id**（如 `decis/laya-multilingual@0.3.5`），不是请求里的别名。
9. **`GET /v1/models` 返回 `{"models":[{"name","description","release_date"}]}`**。额外字段只能加在 `decis` 命名空间下。
10. **每个响应带 `x-typesafe-request-id`**，同一 id 出现在该请求的结构化日志里。
11. **`/v1/systemone` 必须是纯函数**——官方 SDK 会对 POST 自动重试（`{408,429,500..599}` **以及连接错误与超时**）。请求路径上不允许有可变的业务状态写入。
12. **不实现流式**。官方没有流式接口，加了就是偏离契约。
13. **认证先于请求体校验**。无凭证 + 非法 body 必须返回 403（不是 422）——线上实测确认真 jev 就是这个顺序。理由是安全：反过来会向未认证调用方泄露校验细节。
14. **401 与 403 分工不可混用**：缺凭证 / scheme 不是 `Bearer` → **403**；凭证无效 → **401**。两者 body 都是 `{"detail":{"error_type":"authentication_error","message":"…"}}`。线上实测确认（`docs/contract/observations-2026-09-22.md`）。
15. **所有响应都必须带 `x-typesafe-request-id`，错误响应也不例外**，格式 `req_` + 32 位小写十六进制。SDK 的成功响应模型在缺该头时**抛异常**。
16. **返回 429 时必须带 `retry-after-ms`**（或 `Retry-After`）。不带会让官方 SDK 退化成指数退避，把已过载的服务打得更狠。
17. **任何同步等待都不得让单次请求超过 10 s**。官方 SDK 的单次 HTTP 超时是 10 s 且超时会重发，服务端还在算时客户端已重发会把负载放大。队列等待计入这个预算。
    - **实现**：`InProcessScheduler.run` 用 `DECIS_REQUEST_TIMEOUT_MS`（默认 **8000**，刻意小于 SDK 的 10 s）
      给**取锁**设上限。超时返回 **429 + `retry-after-ms`**，不是 504——两者都在 SDK 的重试集里，
      但 504 不带退避指令，会按 §3-16 退化成指数退避，反而打得更狠。守卫在 `tests/test_request_budget.py`。
    - **说清楚做不到的部分**：同步 `torch` 前向一旦开始就无法中断，所以这个预算约束的是**排队等待**，
      不是已经在算的工作。而它成立的前提是**阻塞路由不能写成 `async def`**——否则序列化发生在事件
      循环上，锁和预算都形同虚设（`routes.py`、`design-review.md §2-D8`）。
18. **攒批器不得按 `state` 分组**。`WorkItem` 每项自带 `state_text`，跨 state 组批是引擎的内部实现细节（`design.md §5.1`）。按 state 分组会让真实流量下的 batch 恒为 1，使批处理永不触发。
    **（尚未实现，因此暂无守卫——见本节开头的说明。）**
19. **未配置 `DECIS_API_KEY` 且监听非回环地址时，服务必须拒绝启动**，除非显式设置 `DECIS_ALLOW_NO_AUTH=1`。不安全的默认值会被原样部署到生产。
20. **一个引擎实例在任一时刻只能被一个线程执行 `predict`**。不得跨线程共享引擎内部对象（tokenizer 除外）。

---

## 4. 架构分层与依赖方向

```
        routes/app                        ← HTTP：只做解析、委派、序列化
            ↓
        service.py                        ← 编排：解析模型、归一化、容量校验、组装响应
            ↓
   schema / render / answers              ← 归一化：线格式 ⇄ 领域类型 ⇄ 文本
            ↓
        scheduler.py                      ← 调度：进程内、串行化（一次前向一个 item；跨请求攒批经 M5 实测后没有实现）
            ↓
        engines/                          ← 引擎：只吃 PreparedQuestion，只吐 ProbDist
            ↓
        paths.py                          ← 权重定位
        domain.py                         ← 以上所有层的共享词汇表（不依赖包内任何模块）
```

- 只能向下依赖。`domain.py` 不参与此序：它是各层的共同词汇，被任何层 import 都是对的。
- **引擎层不得 import HTTP 层**（FastAPI、路由、请求对象）。
- **归一化层（`schema`/`render`/`answers`）不得 import 任何引擎**。
- 引擎通过 `DecisionEngine` 暴露，返回 `ProbDist`，**不返回线格式**。
- `routes.py` 里不允许有判断逻辑；业务判断放 `service.py`，这样它可以脱离 HTTP 测试。

`tests/test_conventions.py` 会解析 AST 来验证上述方向。

---

## 5. 新增一个引擎（标准作业）

新增引擎是 Decis 最常见的扩展，必须按这个顺序做，不要跳步：

1. 在 `src/decis/engines/<name>.py` 实现 `DecisionEngine`：`info()` / `load()` / `predict()` / `close()`。
2. 在 `registry.py` 注册：id → `"module:ClassName"`（**字符串路径，惰性 import**）+ 可选依赖 extra 名。
   若引擎自己的代码需要比 `requires-python` 更高的解释器（Jeff：3.12，因为 vendored 语法），
   填 `EngineSpec.python_min`，并在 extra 的依赖上加同样的 `python_version` marker：`status()` 会在
   依赖检查之前报 floor，否则会报"缺依赖"并给一条在低版本上装不出东西的补救命令（`design-review.md §2-D31`）。
3. 在 `pyproject.toml` 加 extra：`<name> = [...]`。**引擎的重依赖只能出现在 extra 里**，不能进 `[project.dependencies]`。
4. 实现 `weights()`（声明权重来源、pin 的 commit、体积）与 `measure()`。`EngineInfo` 必须诚实声明 `max_options`、`max_sequence_tokens`、`max_question_tokens`、`max_state_tokens`、`primitives`、`device`、`dtype`。
   - **`max_sequence_tokens` 是"state + 一个问题"的总预算**，不是 state 单独的预算。state 与 head 共享同一条序列，分开检查会让两边都合规、合起来超长的请求被静默截断（`design.md §4.1`）。
   - **若引擎额外限制 state 本身**（kev：state ≤ 384 而 state+问题 ≤ 1024），必须填 `max_state_tokens`。不填就意味着"序列上限已经覆盖了"，而 kev 那种情况不填会让超长 state 通过校验后被静默截断（`design-review.md §2-D10`）。
   - **`max_question_tokens` 要填最宽松的可靠上界**，不要用"最坏情况"（如 `max_sequence - max_state`）：那会拒掉引擎其实处理得了的请求。真正生效的比较是序列那一条。
   - **`measure()` 必须报真实长度，不能从上游"截断后"的输出反推**：`encode` 会把 state 截到上限，反推出来的数字永远等于上限，上限检查就成了永不触发的摆设（D10 实际发生过）。
   - **`measure()` 必须用真实 tokenizer 和真实的序列布局测量，不能退回 `len(text)//4`。** 如果上游会截断，就把它的不截断条件压成一个可验证的表达式（Laya 的做法：`budgeted_head`），并用上游函数本身断言这个表达式正确。
5. 加**两套**测试：
   - 快速套（无权重，CI 必跑）：`tests/test_engines_laya.py` 的做法——假 tokenizer + stub 掉上游渲染，覆盖 `measure` 的算术与 `_internal` 的形状；
   - `weights` 套：`tests/test_laya_inference.py` 的做法——真实权重，覆盖**批不变性**、"一次 `predict` 只做一次前向"、以及"超长请求被拒而不是被截断"。
6. 更新 `docs/design.md §7.2` 的权重清单（来源、**实测**体积、pin 的 revision、实测过的依赖组合）。
7. 若引擎复用上游包的函数（公开的或 `__all__` 之外的），加 `tests/test_upstream_contract.py` 的断言：符号存在、签名未变、以及**你所依赖的行为**（如 `collate_items` 会展平分组）。一个只在 ImportError 时才失败的守卫是不够的——上游改了行为会静默给出错误答案。

**不要**为了接一个引擎去改 `render.py` / `answers.py`。如果非改不可，说明抽象错了——先改 `docs/design.md §2` 并说明理由。

**已记录的唯一例外（Jeff，2026-09-30）**：接 Jeff 时确实动了 `render.py`——不是改渲染规则，而是给
`PreparedQuestion` 加了 `raw`、给 `PreparedRequest` 加了 `raw_state`，把调用方原始 JSON 原样带到引擎，
因为 Jeff 的提示词是 `json.dumps(state)` 而不是扁平化文本（`docs/design.md §2` 已补）。判断标准没变：
"任意 JSON → 可读文本"仍然只住在 `render.py`，"某个 primitive 的选项长什么样"仍然由引擎决定。第二个
引擎若又要求"另一种 JSON 渲染"，先改 `design.md §2` 再动手，不要各自再解析一遍请求。

---

## 6. 惰性 import 是硬要求

理由：一个只装 `decis[laya]` 的镜像里没有 peft；反之亦然。`/healthz`、`/readyz`、`GET /v1/models` 必须在一个引擎的依赖缺失时仍然可用。

- 引擎类通过字符串路径注册，用时才 import。
- 引擎模块顶层**不得** import `torch`/`transformers`/`laya` 之外的重型依赖——把 import 放进 `load()`。
- `src/decis/` 的核心模块（除 `engines/` 外）**不得**在任何路径上 import `torch`。

---

## 7. 命令

已实现：

```bash
uv sync --extra dev                  # 开发环境（含 pytest / ruff / typesafe-sdk）
uv sync --all-extras                 # 全部引擎依赖 + dev（本地 checkout 一把装齐）
#   引擎依赖只在 extras 里，所以裸 `uv sync` 一个都不装，而且会删掉上一次 sync 装上的引擎依赖；
#   只服务一个引擎就点名它的 extra（`uv sync --extra dev --extra kev`）。
#   Windows 上这条命令从 PyTorch 的 CUDA index 取 torch/vision（PyPI 的 Windows wheel 是 CPU-only
#   构建，装了也永远探测不到 GPU，见 §2-D36）；没有 N 卡、或驱动老到跑不了那个 CUDA 通道时，
#   `uv sync --all-extras --no-sources` 拿回 PyPI 的小 wheel。
cp .env.example .env                 # 至少要改 DECIS_API_KEY
uv run pytest -q                     # 无权重测试（CI 跑这个：不需要权重，也不联网）
uv run ruff check && uv run ruff format --check

uv run decis serve --host 0.0.0.0 --port 8000   # 加载默认引擎 laya-multilingual（需要它的依赖与权重）
uv run decis serve --host 127.0.0.1  # 本地开发：回环地址允许不带 token；同样需要引擎就绪
uv run decis models                  # 列出已注册引擎及其在本机是否可用
uv run decis doctor                  # 环境自检：依赖、配置安全性、绑定地址、线程数，
                                     #   以及 compute 一节（选中的设备、torch 构建、nvidia-smi 看到的 GPU；不报 dtype）
```

服务行为相关的配置（都有默认值，`decis doctor` 会报告实际取值）：

```bash
DECIS_REQUEST_TIMEOUT_MS=8000        # 单请求取锁预算；必须小于官方 SDK 的 10 s
DECIS_SHUTDOWN_GRACE_MS=20000        # 关闭时等在途加载的上限；要小于 terminationGracePeriodSeconds
DECIS_DEVICE=cpu                     # 强制设备；不设则按 cuda → xpu → npu → mps → cpu 自动选
                                     #   （`engines/devices.py`；npu 需要 torch_npu 插件）。
                                     #   设备也会决定 dtype 查表的哪一列
DECIS_DTYPE=bf16                     # 强制精度；不设则查 registry.DTYPE_DEFAULTS。
                                     #   已知性能很差的组合（kev CPU 上 bf16）只告警不拒绝
```
`DECIS_DTYPE` 只对**查 `DTYPE_DEFAULTS` 的引擎**生效（目前是 kev）；Laya 由它自己的
`Agent` 决定精度，不受这个变量影响。

真实模型：

```bash
uv sync --extra laya
uv run decis download --engine laya-multilingual                    # 约 647 MiB，进 Hub 缓存（零配置时 serve 读这里）
#   连不上 huggingface.co 时自动改用 ModelScope（DECIS_HUB=auto，探测失败才回退，探测打的是
#   <endpoint>/api/models?limit=1，任何 HTTP 状态码都算『连得上』）；写死源用
#   `--hub huggingface|modelscope`。这些仓库在 ModelScope 上只有 master，所以回退时 pin 的
#   commit 兑现不了，命令会在传输前警告一次（§2-D32）
uv run decis serve --engine laya-multilingual --host 127.0.0.1     # 冷启动实测见 docs/performance.md 的延迟表
#   想落到挂载目录：`--dest ./models` 写成 ./models/laya-multilingual/，再用 DECIS_MODEL_DIR=./models 服务
#   冷启动期间 /healthz 立即可用，/readyz 报 {"status":"loading"}；加载失败则报 "failed"
#   即"端口先开、引擎后好"：绑定前会打印引擎 / 权重来源 / 绑定地址。要反过来（先加载再开端口、
#   加载期间连 /healthz 都不应答）用 `decis serve --preload`，容器与编排器下不要用它
uv run pytest -m weights             # 真实推理 + 批不变性（需要权重，CPU 上慢）

uv sync --extra kev
uv run decis download --engine kev-0.8b   # adapter 取 3 个文件约 43 MiB（仓库共 62.5 MiB）+ 基座 1.65 GiB
#   两个仓库都走同一套规则：源由 hub.py 选（`--hub` / DECIS_HUB）、pin 由 paths 兑现，
#   适配器和基座都以**目录**交给加载器——把 repo id 交给 vendored 的 Checkpoint 会让它自己去
#   联网（§2-D33），`--hub modelscope` 就白设了
uv run decis serve --engine kev-0.8b --host 127.0.0.1   # 需要权重；容器内未验证

uv sync --extra jeff
uv run decis download --engine jeff-qwen3.5-0.8b   # 全权重微调：一个目录 1.61 GiB，没有基座要另外放
uv run decis serve --engine jeff-qwen3.5-0.8b --host 127.0.0.1
#   `jeff` / `jeff-qwen` / `jeff-qwen3.5` 都是它的别名。同一个 extra 里的 `jeff-gemma4-e2b` 是
#   8.65 GiB 的另一个 checkpoint：CPU 上 fp32 加载实测 139 s、峰值 RSS **23.8 GiB**（权重本身
#   18.5 GiB，加载过程中的 bf16 → fp32 转换是另算的），所以 24 GB 以下的机器会开始换页；
#   `-m weights` 默认只加载 Qwen 那个，连它一起测要显式 `DECIS_TEST_JEFF_GEMMA=1`，
#   而且**分两半跑**（两个引擎同时常驻 ≈ 26 GiB，一台 31 GB 的机器放不下）
#   两个 Jeff 引擎要 **Python 3.12+**（vendored 代码用了 PEP 695 别名，改不掉，见 §2-D31）：
#   低于 floor 时 `decis models` 报 `needs Python 3.12+`，extra 的 marker 让 3.11 上不装依赖
```

需要让**某一个**引擎读你自己的目录时用 `--model-path ENGINE=PATH`（`serve` / `models` / `doctor`
都有，可重复，接受别名）：

```bash
uv run decis serve --engine kev-0.8b --model-path kev-0.8b=/srv/finetunes/acme-triage
uv run decis models --model-path jeff=/srv/jeff-qwen        # 别名在这里也会被规范化
```

**没有** `DECIS_MODEL_PATH_<ENGINE_ID>` 这类环境变量，而且是刻意删掉的：它把引擎 id 编进变量**名**，
任何变量名都装不下 `kev-0.8b`、`jeff-qwen3.5-0.8b`、`jeff-gemma4-e2b` 里的点号——`kev-0.8b` 会规范化成
`kev-0-8b`，一个没注册的 id，于是覆盖被静默丢弃。把 id 放进**值**里就没有这个限制；未知 id 现在是
`ConfigError`，`decis doctor` 会打印解析到的每一条覆盖。

镜像（CI 构建并推送，tag 方案见 `docs/deployment.md`）：

```bash
docker run --rm -p 8000:8000 chaitin/decis:laya-multilingual   # 权重在镜像里，不需要网络也不需要挂卷
docker run --rm -p 8000:8000 chaitin/decis:kev-0.8b
#   想换成自己的权重目录：-v /srv/models:/models，但那个目录里必须已经有 <engine-id>/
#   ——挂在 /models 上会盖掉镜像里已有的权重（§2-D21）。要"权重放卷"就用 `<engine>-runtime`
#   （每次推送到 master 都会重新发布）或 release 的 `<engine>-runtime-<version>` 镜像先
#   `decis download` 填一次卷。
```

本地编排（发布镜像；profile 决定起哪个引擎，选择写在 `.env` 的 `COMPOSE_PROFILES` 里）：

```bash
cp .env.example .env                      # COMPOSE_PROFILES=laya-multilingual
docker compose -f docker-compose.yml up -d --wait   # 只起默认引擎，没有下载也没有预取容器
#   这次 `--wait` 真的等到能作答（compose 层探针打 /readyz）；
#   不想用 --wait：until curl -fsS localhost:8000/readyz >/dev/null; do sleep 2; done
docker compose --profile kev-0.8b up -d   # 换一个引擎（宿主端口 8001）；这个 flag 会取代 .env 里的选择
docker compose config -q                  # 只校验 schema / 插值 / profile，不拉镜像（CI 的 docker job 跑这个）
docker compose up -d --build              # 源码目录里：自动叠 docker-compose.override.yml，构建 decis-local:*
```

`docker-compose.override.yml` 是靠**文件名**被 Compose 自动发现的，所以源码目录里裸
`docker compose up` 走本地构建；部署只拷 `docker-compose.yml`（或加 `-f docker-compose.yml`）。

`Makefile` 是这些命令的**快捷方式，不是第二份定义**：目标全部转调 Compose，里面不写任何
镜像 tag、引擎 id 或构建参数，而是问 Compose（`config --services` / `config --profiles` /
`config --images`），所以 `make` 与 `docker compose` 不会各说各话，`COMPOSE_PROFILES=...`
的作用也一样（shell 覆盖 `.env`）。守卫在 `tests/test_makefile.py`：它用一个假 `docker`
真跑 `make`，因此不需要 daemon，其中一条还断言 docs 里出现的每个 `make <target>` 都真的存在。

```bash
make help                    # 目标清单 + 本目录解析到的引擎
make up / down               # 起（构建源码）/ 停
make up-local                # 同上，但先重建镜像（引擎那次会重新下载全部权重，慢）
make build-playground        # 只重建游戏页面镜像：几秒（那个 Dockerfile 没有 RUN）
make up-playground           # 只起游戏页面，旁边接一个跑在任何地方的引擎
make build-engine / up-engine ENGINE=kev-0.8b
make ps / logs / images / config / pull
make test / lint
```

本地构建（不传 `DECIS_EXTRAS` 得到的是 engine-free 的纯 API 镜像：能起、能列模型，但不会 ready；
不传 `DECIS_PREDOWNLOAD` 得到的是不带权重的变体）：

```bash
docker build -f docker/Dockerfile \
  --build-arg DECIS_EXTRAS=laya \
  --build-arg DECIS_ENGINE=laya-multilingual \
  --build-arg DECIS_PREDOWNLOAD=laya-multilingual \
  -t decis:laya-multilingual .
```

性能数据（`AGENTS.md §8` 的落地）：

```bash
uv run decis bench --engine laya-multilingual --batch 1,3,10,30   # 采集，写 benchmarks/results/
uv run python benchmarks/report.py --write   # 由原始 JSON 生成 docs 里的表
uv run python benchmarks/report.py --check   # CI 跑这个：手改过的数字会让它失败
```

对外 API 的 JSON Schema 与 OpenAPI 文档（生成物，进 CI）：

```bash
uv run python docs/schema/export.py --write   # 由 src/decis/schema.py 重新生成 docs/schema/*.json
uv run python docs/schema/export.py --check   # CI 跑这个：手改过的 schema 会让它失败
```

`openapi.json` 由 `create_app(..., load_engine=False)` 导出，因此它描述的是**真正在跑的那个 app**，
不是另写一份；`tests/test_api_schema.py` 会把生成结果与运行中的应用对比。

跨请求批处理的收益（M5）——`decis bench --cross-request` 转调 `benchmarks/batch_gain.py`
（第二个 harness，不是第二份实现）：

```bash
uv run decis bench --engine laya-multilingual --batch 1,2,4,8,16 --threads 24   # 串行 vs 合成批 + 正对照
uv run decis bench --engine laya-multilingual --cross-request --threads 24 --processes 4   # 多进程
```

`--threads` 是**每个进程**的线程数，`--processes N` 时 CLI 会把它除以 N，**总预算保持不变**；
直接调 `batch_gain.py` 时这个除法要自己算，忘了就是在测线程超配。别加 `--items` 时改小它：
item 数决定每个 batch size 有多少个样本，16 是当前 JSON 用的值。

`--check` 已在 CI 里，覆盖 `docs/performance{,.zh-CN}.md`、`docs/design-review.md`、`docs/feasibility.md` 与 `benchmarks/RESULTS.md`。生成器在渲染前会断言同一组内各配置处理的是**同一个输入**
（`input_sha256` + token 数），不一致就拒绝生成；对攒批数据还会拒绝**没有正对照**的文件
（测不出收益的 harness 无法区分"机制没用"和"测量坏了"）。它第一次运行就抓到了 §2-D12
（README 曾把两个线程数的数字混进同一行）——**同一缺陷当时还留在 `README.zh-CN.md` 里**，
因为那个文件靠手工抄表；现在 README 一行性能数字都不印，这些表只生成到 `docs/` 里。没有 checked-in 原始 JSON 支撑的数字不许进文档。

**两套测试各自都会漏东西，声称"测试通过"之前必须在两个环境里都跑过。**

- 无权重环境（`uv sync --extra dev`）跑得快，但引擎的任何 **`requires`/权重/依赖已装** 的分支
  都不会被走到。Stage 1 就有一个真实 bug 藏在这里：`decis models` 在"装了 `laya` extra
  但还没下载权重"的机器上会 `AttributeError` 崩溃——那恰好是文档让用户做的第一步——
  而无权重的那套因为提前 return 而全绿。
- 有额外依赖的环境（`uv sync --extra dev --extra laya`）会发现上面那类 bug，
  但会漏掉"依赖缺失时的提示是否清楚"，因为那时依赖是齐的。

- **解释器版本也算一维**：CI 覆盖 Python 3.11 / 3.12 / 3.13 三个版本，而本地默认只有一条（当前是 3.14）。
  `design-review.md §2-D31` 就是这么漏的：vendored 代码里的 PEP 695 语法在 3.11 上直接
  `SyntaxError`，两个本地环境全绿，只有 CI 的 3.11 任务失败。接引擎或改 vendored 之后，在项目
  floor（`requires-python`，当前 3.11）上真跑一次：
  `uv python install 3.11 && uv venv --python 3.11 .scratch/venv311 && uv pip install -e ".[dev]"`，
  然后跑全量。没有 3.11 解释器时，`ast.parse(..., feature_version=(3, 11))` 也能把"这个文件需要
  更新的语法"在任何解释器上断言出来。

所以提交前的最低要求是：`.venv`（无 extra）与 `.scratch/venv`（真实 `laya`）各跑一次全量，
外加至少一次项目 floor 上的解释器。
写测试时不要假设自己在哪个环境里——**不要断言 `laya` 没被安装、不要假设会走网络、
不要假设别的测试没 import 过 torch**。需要"某个模块不存在"就挑一个真的不存在的名字，
需要判断环境就 `pytest.skip` 并说清理由。**给配方一个最小 `PATH` 的 fixture，要把配方可能
exec 的每个程序都放进去**：GNU make 对不含元字符的整行会绕过 shell 直接 `exec`，`@echo` 这种
空行正好落在这一档，而**走不走这条捷径取决于 make 版本**（Ubuntu 的 4.3 走 shell、macOS 的
3.81 不走），于是同一个 fixture 在一个任务上通过、在另一个任务上失败——`tests/test_makefile.py`
里补的那个 `echo` 就是这么被 macOS 那个任务抓出来的。

---

## 8. 性能数字的纪律

- **文档、README、PR 描述里的任何性能数字都必须来自 `benchmarks/results/` 里的 checked-in 原始 JSON**，由 `benchmarks/report.py` 生成。**禁止手写数字**，禁止引用单次跑的"感觉"。
- **README 不打印任何性能数字。** 延迟强依赖硬件，落地页上的表说明的是测量那台机器，而不是 Decis；README 只链接 `docs/performance.md`。
  守卫：`tests/test_benchmark_report.py::test_the_readmes_do_not_carry_performance_numbers`。
- 报告生成前断言各对比配置处理的**输入 sha256 与 token 数完全一致**（照抄 laya-mlx 的做法）。
- 报告性能时**必须同时给出**：引擎、设备、dtype、线程数/进程数、批大小、state 长度、问题数。缺任一维度的数字没有意义。
- 不要把 GPU 数字和 CPU 数字放在同一张表里比较而不标注。
- 不许把上游项目 README 里的宣传数字抄进 Decis 文档当作自己的实测。

---

## 9. 禁用清单

- ❌ 手写规则里的性能数字
- ❌ 在请求路径上做首次权重加载（冷启动必须在服务就绪前完成，或 `/readyz` 明确报告未就绪）
- ❌ 把 `host` 硬编码成 `127.0.0.1`（容器里必须能监听 `0.0.0.0`；kev 上游就有这个问题）
- ❌ 把模型权重提交进 git（`.gitignore` 必须挡住 `models/`、`*.safetensors`、`*.pt`）
- ❌ 在 `answers.py` 之外构造 answer dict
- ❌ 让引擎返回 `confidence`
- ❌ 引入需要外部服务（Redis/Postgres/Celery）才能单机运行的依赖
- ❌ 未经许可与署名就复制第三方代码进仓库
- ❌ 让引擎在超预算时静默截断输入（上游这么干，Decis 不能跟着干）
- ❌ 在 `paths.py` 之外决定权重从哪来，或让"本地有权重"输给网络请求
- ❌ 把 pin 的 revision 交给一个**没有 commit 语义**的客户端（`design-review.md §2-D32`：
  ModelScope 的 `revision` 是分支名 / tag 名，收到 Hugging Face 的 commit sha 不报错、
  打一行 `No files to download`、**成功返回一个空目录**，于是“换源”悄悄变成“换权重”）。
  pin 只有 Hugging Face 能兑现：`hub._from_modelscope` 不带 `revision`，`Selection.pinned`
  把这件事变成值，每个显示源的地方都要说清楚；**探测要探 endpoint**
  （`<endpoint>/api/models?limit=1`），不要探某个仓库——那会把“仓库改名 / 转私有”读成“被墙”
- ❌ 在 `hub.py` 之外挑 Hub 或调 `snapshot_download`（§2；守卫
  `tests/test_conventions.py::test_only_hub_names_a_hub_client`），或在 `paths.module_available`
  之外再写一遍 `find_spec`（守卫 `test_module_presence_has_one_home`）
- ❌ 把 repo id 交给**会自己联网**的上游 loader（`design-review.md §2-D33`：`kev.py` 把
  `jaredpalmer/kev-0.8b@<sha>` 交给 vendored 的 `Checkpoint`，而它的 `resolve_run` 自己调
  `huggingface_hub.snapshot_download`——于是 `DECIS_HUB=modelscope` / `--hub modelscope` 对适配器
  完全无效——默认配置下 `paths.resolve` 只给得出 `hub`，于是这条分支永远回 Hugging Face 取，
  `auto` 的回退对 kev 的加载一次都没生效过（本地命中那条路旧代码是好的）；已有的字符串守卫看不见它，
  因为那个客户端名字在 vendored 代码里）。引擎只做两件事：把 `WeightSpec` 交给 `paths.resolve` /
  `paths.fetch_checkpoint`，把拿回来的**目录**转交出去
- ❌ 调用共享的分类器/解析函数时不把**本次解析到的**配置传进去（`design-review.md §2-D34`：
  `decis doctor` 的引擎清单用默认配置调 `registry.status`，于是同一个终端里它一边打印
  `model paths kev-0.8b=/srv/…`，一边说 `kev-0.8b needs weights`。共享函数不够，喂给它的输入
  也得是同一份；守卫 `tests/test_status.py::test_doctor_asks_the_classifier_with_the_settings_it_resolved`）
- ❌ 让“回退到镜像”变成一个**默认看不见**的行为：选源、原因、以及兑现不了的 revision 必须在
  传输任何字节**之前**打印出来（`decis download` 的源行 + 警告，`decis doctor` 的 `weight hub` 行），
  需要 pin 生效的部署用 `DECIS_HUB=huggingface` 把回退变成失败
- ❌ 用 `len(text) // 4` 给一个会截断的引擎做容量校验
- ❌ 把调用阻塞函数的路径写成 `async def` 路由（会堵死事件循环，连探针一起堵）
- ❌ 在请求路径上无限期等引擎（取锁必须有 `DECIS_REQUEST_TIMEOUT_MS` 上限）
- ❌ 对"引擎永久加载失败"报 `retry-after`（等于让 SDK 永远重试一个不会恢复的服务）
- ❌ 让引擎自己写设备回退（`settings.device or "cpu"` 曾让 `kev-0.8b` 在 Apple 芯片上默认跑 CPU，
  `design-review.md §2-D24`）。设备由 `src/decis/engines/devices.py` 决定，引擎只问它
- ❌ 在 `devices.py` 之外再判断一次"有没有 GPU / 驱动看到了什么"（`design-review.md §2-D36`：
  PyPI 的 Windows torch 是 CPU-only 构建，`torch.cuda.is_available()` 把"没有显卡"和"这个 wheel
  用不了显卡"报成同一个 `False`，于是"为什么在 CPU 上"只能由一处回答——`torch_build` /
  `nvidia_gpus` / `accelerator_advice`，引擎调 `warn_if_accelerator_is_idle`，
  `decis doctor` 打 `compute` 一节；守卫
  `tests/test_conventions.py::test_the_idle_gpu_explanation_has_one_home`）。也**不要**用"装没装 torch"
  之类的方式绕开它：诊断要走这一处，否则又会多出第二份判断
- ❌ 在 `render.py` 里决定某个模型的选项文本（kev 的 `no`/`yes` 与裸层级文本必须由引擎决定，搞错会无提示地降低答案质量）
- ❌ 改动 `src/decis/engines/_kev_vendor/` 里的任何字节（要更新就整体 re-vendor 并改 `VENDOR.md`/`NOTICE`）
- ❌ 改动 `src/decis/engines/_jeff_vendor/` 里的任何字节（同一条规则：整体 re-vendor，并一起改
  `VENDOR.md` / `NOTICE` / `jeff.REVISION`。`tests/test_jeff_vendor.py` 同时比对 vendored 与上游两份
  sha256，还会把 vendored 文本反向套回 import 重写再和上游逐字节比——只更新期望哈希是过不去的）
- ❌ 让一个按调用方 JSON 训练的引擎去读 `render.py` 扁平化后的文本（Jeff 的提示词是
  `json.dumps(state)` 与原样 criteria，不是 `key: value` 行；传入错误的文本不会报错，只是答案比 checkpoint 的
  基准差。引擎读 `WorkItem.raw_state` / `PreparedQuestion.raw`，不要自己再解析一次请求）
- ❌ 用把引擎 id 编进**变量名**的方式覆盖单个引擎的权重目录（`DECIS_MODEL_PATH_<ENGINE_ID>` 已在本轮
  删除）。任何变量名都装不下 `kev-0.8b`、`jeff-qwen3.5-0.8b`、`jeff-gemma4-e2b` 里的点号，那个名字会
  规范化成一个没注册的 id 然后被静默丢弃。引擎目录覆盖只有 `--model-path ENGINE=PATH`：id 放在**值**
  里，别名由 `cli._parse_model_paths` 规范化，未知 id 是配置错误而不是空操作
- ❌ 用"上游截断后的输出"反推 `measure()` 的数字（会得到永远等于上限的假测量）
- ❌ 手改 `<!-- MEASUREMENTS -->` / `<!-- LATENCY -->` / `<!-- DTYPE -->` / `<!-- BATCHING -->` 标记块里的数字
  （会被 `report.py --check` 拦下；要改就改原始 JSON 或重测）。**标记块里的散文同样是生成物**——
  措辞要改就改 `benchmarks/report.py` 里的模板再 `--write`。中文那两句的语病（"随后已经测过"、
  "旁边的散文"）就是这么修的，手改会让 `--check` 失败
- ❌ 手改 `benchmarks/RESULTS.md`（它是生成物）
- ❌ 手改 `docs/schema/*.json`（生成物；改线格式要改 `src/decis/schema.py` 再 `export.py --write`，
  `docs/schema/export.py --check` 与 `tests/test_api_schema.py` 会拦下）
- ❌ 只改双语指南的一种语言，或让两侧的相对链接 / `DECIS_*` 变量 / 代码块语言 / 生成标记不一致
  （`tests/test_docs.py` 盯着；用户文档是产品的一部分，不是附带说明）
- ❌ 凭印象写文档里的字段值、CLI 输出或错误码。这一轮从用户文档里一次抓到五处：
  `max_options` 写成 64 而代码是 255；把 `decis models` 的输出写成 `not-installed`
  （那是 `/v1/models` 在**依赖**缺失时给的 `version`，CLI 报的是 `deps missing` /
  `needs weights`）；说容量能在 `decis models` 里看到（它只报"这台机器能不能跑"）；
  把一个点号无法表示的引擎覆盖变量写成可用（`kev-0.8b` 会规范化成 `kev-0-8b`，
  `config.py` 静默丢弃）；给一个从不返回的 504 写了错误行。**文档里出现的每个值都要能在
  代码里指出处。** 其中两类已经机械化：`tests/test_docs.py` 断言配置文档里的每个 `DECIS_*`
  都被某处读到，且没有任何面向读者的页面还写着 `DECIS_MODEL_PATH_*`，每个 `--model-path`
  示例里的引擎都能解析（别名算数）
- ❌ 让一行性能表的不同列取自不同配置（`design-review.md §2-D12`：线程数混用曾真实发生过）
- ❌ 手工把一张表从一个文件抄到另一个文件（`README.zh-CN.md` 抄过，于是它**只在中文版里**带着 D12）：
  抄写就是第二处实现（§2），要么生成，要么不要放
- ❌ 报跨请求批处理的结论时省略它的范围：**上界**（合成批、无队列）、**CPU**、**只有 Laya**
- ❌ 用"一个大小测到底"的顺序做批次扫描（序效应会伪装成批大小的效果，`§4-M2` 与 M5 各出现过一次）
- ❌ 增加一个收益类机制却没有正对照或能证明它触发的指标（`§2-D1`）
- ❌ 比较不同进程数时让总线程预算不一致（那测的是线程超配，不是进程扩展性）
- ❌ 用 `A && B || C` 表达"可能为空的三元"（GitHub 表达式里没有三元运算符，而**空字符串是假值**，
  所以"没有 extra"这种分支永远选不中：当时那个无权重镜像因此装上了 Laya + torch，多出 3.1 GB，
  见 `design-review.md §2-D14`；那个镜像已随测试替身一起移除）。选一个可以合法为空的值时，
  用真正的分支，或把决定搬到脚本里
- ❌ 让测试里的映射表是**手抄的常量**而不是从被执行的那份东西读出来的
  （`design-review.md §2-D14`：表达式错了、抄本对了，于是测试恒绿地放过了一个 3.1 GB 的缺陷）
- ❌ 在**共享仓库**里让多个构建/合并任务写同一个 tag（`design-review.md §2-D16`：
  per-arch 中间 tag 少了 artifact 名，六个构建任务互相覆盖，三个 tag 发出同一个 manifest）。
  检查"我这个任务的 tag 对不对"是不够的，必须有一条测试断言**任意两个任务的 tag 集合不相交**
- ❌ 在文档里写出一个**没被任何测试对照过**的镜像 tag / URL / 文件名
  （`design-review.md §2-D15`：README 曾写一个实际不存在的镜像 tag，照着文档拉的第一条命令就失败）。
  凡是文档里出现的、由 CI 产出的名字，都要有一条测试从**真正产出它的那段脚本**里读出来比对
- ❌ 让"预取/下载"写到一个 loader 不会去读的目录（`design-review.md §2-D17`：`decis download`
  用 `local_dir` 复制的是**仓库布局**，而 `paths.resolve` 只认 `<DECIS_MODEL_DIR>/<engine id>/`，
  于是这条命令对**任何** `--dest` 都失败，`DECIS_PREDOWNLOAD` 离线镜像也从没构建成功过）。
  下载完必须用 `paths.resolve` 本身验证；测试必须让 stub 写出**真实下载器的布局**，
  而不是只断言"传了哪些参数"——那是把写和读之间剪断还宣称它们连着
- ❌ 用一个 marker 文件判断**分片**权重是否完整（`design-review.md §2-D29`：分片是陆续到达的，
  `config.json` / `decision_config.json` 与 `*.safetensors.index.json` 先落地，于是"下到一半"
  被 `decis models` 报成 ready，加载器再抛 `FileNotFoundError` 指向一个没人被告知要等的分片；
  更糟的是它**盖住了网络那条路**，让"本地优先"从离线保证变成"坏副本赢过好副本"）。
  完整性要问的是**加载器接下来会打开哪些文件**：走 `paths.missing_shards`（读 checkpoint 自己的
  `weight_map`），不要新增第二个"代表文件"
- ❌ 在 compose 的 `command:` 里只写镜像 `CMD` 的后半截（compose 的 `command:` **替换** `CMD`
  而不是追加：`Dockerfile` 没有 `ENTRYPOINT` 时容器会去 exec 一个叫 `download` 的程序，
  `design-review.md §2-D18`）。测试要**从 Dockerfile 读**入口点再决定断言什么，
  不要把当前写法当常量抄一遍——那是把错误的期望固化成守卫
- ❌ 让容器的 liveness 探针继承环境里的代理（`urllib` 读 `HTTP_PROXY`，于是探针去问代理要
  `http://127.0.0.1:8000/healthz`、拿到 502，一个 `ready after 86.1s` 的服务被判成 unhealthy，
  Kubernetes 会一直重启它：`design-review.md §2-D19`）。探针命令必须 `env -u` 掉代理变量，
  并且有一条测试盯住这一点
- ❌ 让**引擎名那个 tag** 指向不带权重的镜像（`design-review.md §2-D20`：一个引擎的镜像存在的
  意义就是"拿到就能用"，而 README 曾把它写成"需要网络或挂卷"，同时真正的内置权重变体叫 `-offline`
  并且从没构建成功过）。默认变体必须是内置权重那个，瘦身变体只能带后缀。裸 `<engine>` 与
  `<engine>-v<version>` 是这条规则的**唯一例外**，而且是有意的：前者是随 release 移动的别名
  （`docker-compose.yml`、README、`make pull` 都按这个名字拉），后者钉住一次发布；两者永远指向
  同一个 manifest，由 merge 任务从同一组 per-arch digest 一次创建（工作流里的 `alias_tag`）。
  **不许再有第三种叫法**——`-offline` 那种"同一个东西两个名字、其中一个还不存在"仍然禁止
- ❌ 在测试里**重建**被测系统会拼的东西，尤其是引擎的提示词文本
  （`design-review.md §2-D30`：weights 套里的 `prompt_of()` 用 `decision_messages` +
  `processor.apply_chat_template` 拼了一份提示词，而 Gemma 那个 loader 走的是
  `decoder.py: chat_text`/`tokenizer`、user 轮被改写成最后一个 text part、而且**根本没有
  `.processor`**，于是 4 项断言在 Gemma 上直接 `AttributeError`，而配来"钉住这份镜像"的那条守卫
  自己也是用同一份镜像写的、在 Qwen 上永远绿）。要断言"引擎实际发出去的字节"，就从**引擎自己的
  产物**读回来——解码它 `prepare()` 出来的 `input_ids`、看它真的写出去的请求体——不要重算一遍
- ❌ 在 compose 里把卷/目录挂到镜像写入权重的路径上（`DECIS_MODEL_DIR`，`design-review.md §2-D21`：
  命名卷会用镜像内容初始化一次然后自己留一份，bind mount 直接盖掉整个目录，于是"离线镜像"变成
  "启动就联网下载"，而且不报任何错）。守卫必须**从 Dockerfile 读出**那个路径再断言没人挂它
- ❌ 在编排器里把 liveness 探针指向 `/readyz`（`design-review.md §2-D22`：镜像还在加载引擎就被判
  不健康，等于每次冷启动都重启一遍）。这个分工的例外只有 compose——它不会因为探针失败重启容器，
  所以那里的探针**就是**就绪探针，`--wait` 才有意义
- ❌ 以为 `.env` 里的网络配置会进入构建步骤（`design-review.md §2-D23`：`env_file` 只作用于容器，
  Docker 也不转发 shell 的 `HTTP_PROXY`，于是"依赖装好了、权重下不来"，而 `.env.example` 曾
  建议在那里设代理来跑构建）。构建要用 `--build-arg` 传，文档必须写成两条路
- ❌ 在 `config.py` 之外再解析一遍代理绕过名单（`design-review.md §2-D25`：`NO_PROXY` 里的
  `[::1]` 会让 httpx 在**构造客户端**时就抛 `InvalidURL: Invalid port: ':1]'`，于是每一次权重下载
  都在联网前失败，而报错看不出跟代理有关）。守卫：
  `tests/test_conventions.py::test_the_proxy_bypass_rule_has_one_home` +
  `test_the_shell_probe_exports_the_canonical_loopback_list`
- ❌ 让"端口开了"充当"服务可用"（`design-review.md §2-D26`）。绑定前必须打印引擎、权重来源与
  绑定地址，并在日志里说清 `/readyz` 仍是 503；要"先加载再开端口"用 `decis serve --preload`，
  且必须同时写明它会让 `/healthz` 也等到加载结束——容器与编排器下不要用它。守卫：
  `tests/test_cli_serve.py`
- ❌ 把仓库里 pin 的 revision 交给一个**没有这个参数**的上游加载器（`design-review.md §2-D26`：
  `laya.Agent(repo_id)` 没有 `revision`，于是日志承诺 pin、实际读 `main`，同一个模型 id 在不同时间
  给出不同答案）。走 `paths.fetch_checkpoint`，它用 `download_arguments` 带 pin 取权重、用
  `checkpoint_root` 验证拿到的目录真能被读到，再把**目录**交给上游
- ❌ 在项目声明支持的 Python 上让引擎直接 `SyntaxError`，或把"代码需要更新的语法"报成"缺依赖"
  （`design-review.md §2-D31`：`_jeff_vendor/types.py` 用了 PEP 695 别名，项目写着
  `requires-python = ">=3.11"`、CI 也覆盖 3.11，于是 3.11 上连 `import` 都过不去——而
  `decis models` 当时报的是 `deps missing   uv sync --extra jeff`，那条命令在 3.11 上装不出任何东西）。
  floor 声明在 `registry.EngineSpec.python_min`，extra 的依赖带同样的 `python_version` marker，
  引擎自己的报错也用同一个数字；守卫要**从 vendored/引擎源码本身**推出来
  （`ast.parse(..., feature_version=(3, 11))` 必须失败、floor 的语法必须成功），不能只在最新解释器上
  跑一遍就宣称测过

---

## 10. 第三方代码与许可

- Decis 自身：Apache-2.0。
- **Laya**：通过 PyPI `laya` 依赖使用（Apache-2.0），不复制其源码。若将来需要 vendor，必须在 `NOTICE` 里保留 Convai Innovations / NandhaKishorM 的署名。
- **kev**：已 vendor 最小子集（Apache-2.0），来源 `https://github.com/jaredpalmer/kev`，pin commit
  `90990a5`，文件清单、sha256 与取舍见 `src/decis/engines/_kev_vendor/VENDOR.md`，`NOTICE` 已记录。
  逐字节复制，**不得修改**；`tests/test_kev_vendor.py` 守卫其 sha256。
- **jeff**：已 vendor 最小子集（**MIT**，不是 Apache），来源 `https://github.com/firelex/jeff`，
  pin tag `v1.1` = `f0397f3`，文件清单、两份 sha256（vendored 与上游）与取舍见
  `src/decis/engines/_jeff_vendor/VENDOR.md`，`NOTICE` 已记录，署名保留 Mathias Strasser 与
  Denis Yarats（AutoJev）。这是**唯一一份非逐字节复制**的 vendored 代码：它需要把绝对自引用
  （`from jeff.X import`）改成相对引用（`from .X import`）才能在 `decis` 里当子包用，
  `tests/test_jeff_vendor.py` 把这个改写**双向**钉住。**除此之外不得修改**。
  注意代码许可与权重许可是两件事：两个 checkpoint 在 Hub 上都声明 `apache-2.0`，Gemma 那个还带
  Google 的 Gemma 4 terms 链接。
- **laya-mlx**：仅作为工程做法参考（测试 fixture、基准方法论），**不复制代码**。若复制，其 `NOTICE` 要求保留对 Convai Innovations 的署名。
- 新增任何第三方代码前，先在 `NOTICE` 加条目。

---

## 11. 写作规则

- 对外文档（README、docs）用**简洁的技术英语或中文**，与所在文件保持一致；`README.md` 面向国际受众，用英语；`docs/` 内部设计文档可中文。
- 讲开发者的问题，直接对读者说话，代码尽量靠前。避免口号、排比、"赋能"类词。
- **诚实优先**：不确定的写"未说明/未实测"，不要编造字段名、性能数字或上游行为。`docs/api-compatibility.md` 的证据等级表就是这个原则的体现，沿用它的做法。
- 提到某个结论时**给出处**：文件路径 + 行号，或 URL。
- **用户指南是双语的**：`docs/<name>.md` 与 `docs/<name>.zh-CN.md` 成对，`README.md` 与
  `README.zh-CN.md` 成对。改一种语言必须同时改另一种；代码、命令、字段名、环境变量、链接目标
  与生成标记不翻译，两侧保持一致。

---

## 12. 提交与 PR

- 一个 PR 只做一件事。接一个新引擎 = 一个 PR；改契约 = 单独一个 PR 且必须同时改 `docs/api-compatibility.md`。
- CI 必须绿：`ruff` + `pytest`（无权重那套）。
- 涉及契约的 PR，描述里必须贴出**官方 `typesafe-sdk` 跑通**的证据（测试名或输出）。
- **改契约或错误码之前，必须先跑一次线上差分（L5）**，把结果贴进 PR。理由：L0 只能保证"我们和自己的 OpenAPI 快照一致"，**没有任何离线测试能发现线上服务端偏离它自己的 OpenAPI**。`docs/design-review.md §4-M1` 记录了这个教训——初版的 401/403 结论就是被一次手工差分推翻的。
- 涉及性能的 PR，描述里必须贴出 `benchmarks/results/` 里新增的 JSON 路径。
- **测量类 PR 必须带正对照**：一个测不出收益的 harness，无法区分"机制没用"和"测量坏了"。
  结论为负时，正对照是让这个负结论可信的唯一东西。
- **实现一个"设计文档说是核心卖点"的机制时（批处理、缓存、并发），PR 必须附带一个能证明它真的被触发的测试或指标**，而不只是"实现完了"。`docs/design-review.md §2-D1` 的教训是：一个从不触发的批处理实现，和没有批处理，在测试上是无法区分的。
