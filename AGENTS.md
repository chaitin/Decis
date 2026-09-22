# AGENTS.md — Decis 工程约束

Decis 是"一个 API 跑所有轻量决策模型"的推理服务框架。它把 jev / TypeSafe System One 的线格式实现一次，把各家开源决策模型（kev、Laya 等）作为可插拔引擎接进来。

本文件是**在这个仓库里工作的契约**。它写给 AI agent，也写给人类。规则不是建议，是约束；违反约束的改动即使"能跑"也不接受。

> **当前状态**：**Stage 0 与 Stage 1 已完成，Stage 2 的地基已铺好**（引擎后台加载、请求预算、
> 非阻塞路由——见 `design-review.md §2-D7/D8`）。API server 可用，且已经接了真实模型：
> `uv sync --extra laya && uv run decis serve --engine laya-multilingual`。
> 契约层（`schema.py` / `render.py` / `answers.py` / `errors.py` / `auth.py`）、引擎抽象、
> `paths.py` 权重解析、`scheduler.py`、`config.py`、`cli.py`（含 `decis download`）、
> `docker/Dockerfile` 与 CI 均已实现。
>
> **测试**：`uv run pytest -q` 跑无权重的那套（**292 个通过**，约 8 秒，不联网）；
> `uv run pytest -m weights` 跑真实 Laya 推理的那套（14 个，需要权重，CPU 上约 110 秒）。
> 另有 `tests/test_contract_sdk.py` 里由 `TYPESAFE_LIVE_API_KEY` 门控的线上差分测试。
>
> **已注册引擎**：`mock`（无权重）、`laya`、`laya-multilingual`、`laya-typed-decisions`。
> **未实现**：kev（Stage 2）、跨请求攒批调度器与 `decis bench`（Stage 3）、多架构镜像矩阵（Stage 4）。
> `docs/design.md §11` 的目录树是目标结构，其中未出现的文件即为尚未实现的部分。

---

## 1. 必读文档

| 文档 | 作用 | 什么时候必须读 |
|---|---|---|
| [`docs/api-compatibility.md`](docs/api-compatibility.md) | 对外线格式的**唯一事实来源**，含证据等级（L > S > A > B > C > D） | 任何涉及请求/响应字段的改动 |
| [`docs/design.md`](docs/design.md) | 架构、抽象、并发、打包方案 | 任何新增模块或引擎的改动 |
| [`docs/design-review.md`](docs/design-review.md) | 对本设计的**自我审查**：已修正的缺陷、方法论局限、尚未验证的假设 | 动手实现前；以及任何"这个设计是不是已经想清楚了"的疑问 |
| [`docs/feasibility.md`](docs/feasibility.md) | 调查证据、实测数字、风险登记 | 讨论性能预期或选型时 |
| [`docs/contract/`](docs/contract/) | 官方 OpenAPI 快照（L0 测试基准）+ 线上观测原始记录（L 级证据） | 任何契约相关改动；**改前必须跑一次线上差分** |

冲突时优先级：`api-compatibility.md` > `design.md` > 其余。

**`design-review.md` 不是历史文档，是活文档。** 它列的"尚未验证"清单（尤其 M5：跨请求批处理的收益从未被测过）在对应验证完成前一直有效。**在 M5 有数据之前，不得在任何对外材料里承诺 QPS。**

---

## 2. 唯一事实来源（One canonical home）

每个概念只能有一个实现处。**发现第二处实现就是 bug**，即使两处当前行为相同。

| 概念 | 唯一所在 | 禁止 |
|---|---|---|
| 各层共享的领域类型（`Option`/`PreparedQuestion`/`PreparedRequest`/`ProbDist`） | `src/decis/domain.py` | 在 `schema.py`/`render.py` 里另定义一份；`domain.py` **不得 import 包内任何模块** |
| `state`/`instructions`/`criteria` → 可读文本（任意 JSON 的扁平化） | `src/decis/render.py` | 引擎各自实现 JSON 扁平化；引擎各自做分隔符转义 |
| 把渲染片段排成**某个引擎自己的序列** | 该引擎（并优先用它上游库的函数，如 Laya 的 `build_sequence`） | 在 `render.py` 里重写某个模型的序列格式——那是对上游内部的复制，保证会漂移（`design.md §4.1` 的 Stage 1 修正） |
| `noul` 的选项名 `"false"/"true"` | `src/decis/render.py: noul_options` | 任何地方写字面量 `Option("false", …)` |
| 「这个请求会被吃掉多少 token」（容量校验的**测量**） | 各引擎的 `DecisionEngine.measure` | 用 `len(text)//4` 估算一个会截断的引擎；在 `render.py` 里猜某个模型的 head 开销 |
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
| 「这个引擎**现在**能不能跑」的分类（依赖 + 权重） | `src/decis/engines/registry.py: status` | 在 CLI 或路由里各写一份"就绪"判断；把"注册了"当成"能跑"报给用户 |
| 引擎 id → 实现的映射 | `src/decis/engines/registry.py` | `if engine == "..."` 散落在业务代码里 |
| 环境变量 | `src/decis/config.py` | `os.environ` 出现在其他模块 |

`tests/test_conventions.py` 是这些规则的守卫（照抄 kev 的做法：一张"唯一事实来源"表 + 断言）。**新增一个 canonical helper 时，同时加一行守卫。**

---

## 3. 契约不变量（不可破坏）

每一条都必须有测试守着。改坏它们等于破坏项目存在的理由。

**唯一例外是第 18 条**：攒批器要到 Stage 3 才存在，所以那条现在**没有守卫**——它是写给
将来那个实现的约束，不是对现有代码的描述。实现攒批器时必须同时把守卫补上，否则这条就是空文。

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
        scheduler.py                      ← 调度：进程内、串行化（Stage 3 起负责攒批）
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
3. 在 `pyproject.toml` 加 extra：`<name> = [...]`。**引擎的重依赖只能出现在 extra 里**，不能进 `[project.dependencies]`。
4. 实现 `weights()`（声明权重来源、pin 的 commit、体积）与 `measure()`。`EngineInfo` 必须诚实声明 `max_options`、`max_sequence_tokens`、`max_question_tokens`、`primitives`、`device`、`dtype`。
   - **`max_sequence_tokens` 是"state + 一个问题"的总预算**，不是 state 单独的预算。state 与 head 共享同一条序列，分开检查会让两边都合规、合起来超长的请求被静默截断（`design.md §4.1`）。
   - **`measure()` 必须用真实 tokenizer 和真实的序列布局测量，不能退回 `len(text)//4`。** 如果上游会截断，就把它的不截断条件压成一个可验证的表达式（Laya 的做法：`budgeted_head`），并用上游函数本身断言这个表达式正确。
5. 加**两套**测试：
   - 快速套（无权重，CI 必跑）：`tests/test_engines_laya.py` 的做法——假 tokenizer + stub 掉上游渲染，覆盖 `measure` 的算术与 `_internal` 的形状；
   - `weights` 套：`tests/test_laya_inference.py` 的做法——真实权重，覆盖**批不变性**、"一次 `predict` 只做一次前向"、以及"超长请求被拒而不是被截断"。
6. 更新 `docs/design.md §7.2` 的权重清单（来源、**实测**体积、pin 的 revision、实测过的依赖组合）。
7. 若引擎复用上游包的函数（公开的或 `__all__` 之外的），加 `tests/test_upstream_contract.py` 的断言：符号存在、签名未变、以及**你所依赖的行为**（如 `collate_items` 会展平分组）。一个只在 ImportError 时才失败的守卫是不够的——上游改了行为会静默给出错误答案。

**不要**为了接一个引擎去改 `render.py` / `answers.py`。如果非改不可，说明抽象错了——先改 `docs/design.md §2` 并说明理由。

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
cp .env.example .env                 # 至少要改 DECIS_API_KEY
uv run pytest -q                     # 无权重测试（CI 跑这个，292 个，约 8 秒）
uv run ruff check && uv run ruff format --check

uv run decis serve --host 0.0.0.0 --port 8000
uv run decis serve --host 127.0.0.1  # 本地开发：回环地址允许不带 token
uv run decis models                  # 列出已注册引擎及其在本机是否可用
uv run decis doctor                  # 环境自检：依赖、配置安全性、设备
```

服务行为相关的配置（都有默认值，`decis doctor` 会报告实际取值）：

```bash
DECIS_REQUEST_TIMEOUT_MS=8000        # 单请求取锁预算；必须小于官方 SDK 的 10 s
DECIS_SHUTDOWN_GRACE_MS=20000        # 关闭时等在途加载的上限；要小于 terminationGracePeriodSeconds
```

真实模型：

```bash
uv sync --extra laya
uv run decis download --engine laya-multilingual --dest ./models   # 约 647 MiB
uv run decis serve --engine laya-multilingual --host 127.0.0.1     # CPU 冷启动约 75-90 秒
#   冷启动期间 /healthz 立即可用，/readyz 报 {"status":"loading"}；加载失败则报 "failed"
uv run pytest -m weights             # 真实推理 + 批不变性（CPU 上约 110 秒）
```

镜像：

```bash
docker build -f docker/Dockerfile -t decis:mock .
docker build -f docker/Dockerfile \
  --build-arg DECIS_EXTRAS=laya \
  --build-arg DECIS_ENGINE=laya-multilingual \
  --build-arg DECIS_PREDOWNLOAD=laya-multilingual \
  -t decis:laya-multilingual .
```

尚未实现（见 `docs/design.md §12`）：

```bash
uv run decis bench --engine ... --batch 1,8,32
uv run python benchmarks/report.py   # 由原始 JSON 生成文档里的表
```

**两套测试各自都会漏东西，声称"测试通过"之前必须在两个环境里都跑过。**

- 无权重环境（`uv sync --extra dev`）跑得快，但引擎的任何 **`requires`/权重/依赖已装** 的分支
  都不会被走到。Stage 1 就有一个真实 bug 藏在这里：`decis models` 在"装了 `laya` extra
  但还没下载权重"的机器上会 `AttributeError` 崩溃——那恰好是文档让用户做的第一步——
  而无权重的那套因为提前 return 而全绿。
- 有额外依赖的环境（`uv sync --extra dev --extra laya`）会发现上面那类 bug，
  但会漏掉"依赖缺失时的提示是否清楚"，因为那时依赖是齐的。

所以提交前的最低要求是：`.venv`（无 extra）与 `.scratch/venv`（真实 `laya`）各跑一次全量。
写测试时不要假设自己在哪个环境里——**不要断言 `laya` 没被安装、不要假设会走网络、
不要假设别的测试没 import 过 torch**。需要"某个模块不存在"就挑一个真的不存在的名字，
需要判断环境就 `pytest.skip` 并说清理由。

---

## 8. 性能数字的纪律

- **文档、README、PR 描述里的任何性能数字都必须来自 `benchmarks/results/` 里的 checked-in 原始 JSON**，由 `benchmarks/report.py` 生成。**禁止手写数字**，禁止引用单次跑的"感觉"。
- 报告生成前断言各对比配置处理的**输入 sha256 与 token 数完全一致**（照抄 laya-mlx 的做法）。
- 报告性能时**必须同时给出**：引擎、设备、dtype、线程数/进程数、批大小、state 长度、问题数。缺任一维度的数字没有意义。
- 不要把 GPU 数字和 CPU 数字放在同一张表里比较而不标注。
- 不许把上游项目 README 里的宣传数字抄进 Decis 文档当作自己的实测。

---

## 9. 禁用清单

- ❌ 手写规则里的性能数字
- ❌ 在请求路径上做首次权重加载（冷启动必须在服务就绪前完成，或 `/readyz` 明确报告未就绪）
- ❌ 把 `host` 硬编码成 `127.0.0.1`（容器里必须能监听 `0.0.0.0`；kev 上游就踩过这个坑）
- ❌ 把模型权重提交进 git（`.gitignore` 必须挡住 `models/`、`*.safetensors`、`*.pt`）
- ❌ 在 `answers.py` 之外构造 answer dict
- ❌ 让引擎返回 `confidence`
- ❌ 引入需要外部服务（Redis/Postgres/Celery）才能单机运行的依赖
- ❌ 未经许可与署名就复制第三方代码进仓库
- ❌ 让引擎在超预算时静默截断输入（上游这么干，Decis 不能跟着干）
- ❌ 在 `paths.py` 之外决定权重从哪来，或让"本地有权重"输给网络请求
- ❌ 用 `len(text) // 4` 给一个会截断的引擎做容量校验
- ❌ 把调用阻塞函数的路径写成 `async def` 路由（会堵死事件循环，连探针一起堵）
- ❌ 在请求路径上无限期等引擎（取锁必须有 `DECIS_REQUEST_TIMEOUT_MS` 上限）
- ❌ 对"引擎永久加载失败"报 `retry-after`（等于让 SDK 永远重试一个不会恢复的服务）

---

## 10. 第三方代码与许可

- Decis 自身：Apache-2.0。
- **Laya**：通过 PyPI `laya` 依赖使用（Apache-2.0），不复制其源码。若将来需要 vendor，必须在 `NOTICE` 里保留 Convai Innovations / NandhaKishorM 的署名。
- **kev**：计划 vendor 最小子集（Apache-2.0）。**必须在 `NOTICE` 记录**：来源 `https://github.com/jaredpalmer/kev`、pin 的 commit、被 vendor 的文件清单、许可证全文位置。
- **laya-mlx**：仅作为工程做法参考（测试 fixture、基准方法论），**不复制代码**。若复制，其 `NOTICE` 要求保留对 Convai Innovations 的署名。
- 新增任何第三方代码前，先在 `NOTICE` 加条目。

---

## 11. 写作规则

- 对外文档（README、docs）用**简洁的技术英语或中文**，与所在文件保持一致；`README.md` 面向国际受众，用英语；`docs/` 内部设计文档可中文。
- 讲开发者的问题，直接对读者说话，代码尽量靠前。避免口号、排比、"赋能"类词。
- **诚实优先**：不确定的写"未说明/未实测"，不要编造字段名、性能数字或上游行为。`docs/api-compatibility.md` 的证据等级表就是这个原则的体现，沿用它的做法。
- 提到某个结论时**给出处**：文件路径 + 行号，或 URL。

---

## 12. 提交与 PR

- 一个 PR 只做一件事。接一个新引擎 = 一个 PR；改契约 = 单独一个 PR 且必须同时改 `docs/api-compatibility.md`。
- CI 必须绿：`ruff` + `pytest`（无权重那套）。
- 涉及契约的 PR，描述里必须贴出**官方 `typesafe-sdk` 跑通**的证据（测试名或输出）。
- **改契约或错误码之前，必须先跑一次线上差分（L5）**，把结果贴进 PR。理由：L0 只能保证"我们和自己的 OpenAPI 快照一致"，**没有任何离线测试能发现线上服务端偏离它自己的 OpenAPI**。`docs/design-review.md §4-M1` 记录了这个教训——初版的 401/403 结论就是被一次手工差分推翻的。
- 涉及性能的 PR，描述里必须贴出 `benchmarks/results/` 里新增的 JSON 路径。
- **实现一个"设计文档说是核心卖点"的机制时（批处理、缓存、并发），PR 必须附带一个能证明它真的被触发的测试或指标**，而不只是"实现完了"。`docs/design-review.md §2-D1` 的教训是：一个从不触发的批处理实现，和没有批处理，在测试上是无法区分的。
