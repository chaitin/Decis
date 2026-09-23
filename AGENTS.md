# AGENTS.md — Decis 工程约束

Decis 是"一个 API 跑所有轻量决策模型"的推理服务框架。它把 jev / TypeSafe System One 的线格式实现一次，把各家开源决策模型（kev、Laya 等）作为可插拔引擎接进来。

本文件是**在这个仓库里工作的契约**。它写给 AI agent，也写给人类。规则不是建议，是约束；违反约束的改动即使"能跑"也不接受。

> **当前状态**：**Stage 0–2 已完成，Stage 3 部分完成，Stage 4（镜像）已接上 GitHub Actions**。两个真实模型家族都跑在同一个契约后面：
> `uv sync --extra laya && uv run decis serve --engine laya-multilingual`，
> `uv sync --extra kev && uv run decis serve --engine kev-0.8b`。
> 契约层（`schema.py` / `render.py` / `answers.py` / `errors.py` / `auth.py`）、引擎抽象、
> `paths.py` 权重解析、`scheduler.py`、`config.py`、`cli.py`（含 `decis download`）、
> `docker/Dockerfile` 与 CI 均已实现。
>
> **测试**：`uv run pytest -q` 跑无权重的那套（**422 通过 / 31 跳过**，约 10 秒，不联网）；
> 装了真实 `laya` 的环境另有 22 个依赖相关用例会真正执行（**444 通过 / 28 跳过**）；
> `uv run pytest -m weights` 跑真实权重的那套（**27 个**：14 个 Laya + 10 个 kev + 3 个真实权重批不变性，CPU 上约 5 分钟）。
> 另有 `tests/test_contract_sdk.py` 里由 `TYPESAFE_LIVE_API_KEY` 门控的线上差分测试。
>
> **已注册引擎**：`mock`（无权重）、`laya`、`laya-multilingual`、`laya-typed-decisions`、
> `kev-0.8b`（kev 的适配器 + Qwen3.5-0.8B 基座，见 `paths.BaseModel`）。
> **已实测的关键负结论**：`docs/design-review.md §4-M5` 已关闭，答案是**否定**——
> 跨请求批处理在本机 CPU 上不提升吞吐（真实长度下最高 1.09x，长度倾斜时慢 3–5 倍），加进程也不提升。
> **因此跨请求攒批器不应按 `design.md §6` 的原设计实现**，`/metrics` 与进程池的必要性也随之下降。
> **未实现**：kev 的 prefix 缓存路径、`/metrics`、`docker-compose.yml`。
> `docs/design.md §11` 的目录树是目标结构，其中未出现的文件即为尚未实现的部分。
>
> **镜像**：`.github/workflows/docker-build.yml` 在 push/打 tag 时按引擎构建并推送到 **Docker Hub**：
> **所有引擎共用一个仓库 `kingfs/decis`，引擎进 tag**（`kingfs/decis:laya-multilingual-latest`、
> `kingfs/decis:kev-0.8b-v1.2.0`、`kingfs/decis:laya-multilingual-offline-latest`），
> 另外 `mock` 在 master 推送时同时打一个裸 `latest`，让 `docker pull kingfs/decis` 开箱可用。
> 多架构（amd64 + arm64 原生 runner，不用 QEMU），带 SBOM 与 provenance；
> PR 只构建 amd64 的 `mock` 以验证 Dockerfile。矩阵与 tag 生成逻辑由 `tests/test_docker_workflow.py`
> 把脚本从 YAML 里抠出来**真跑**验证（用一个假的 `docker` 记录它被要求创建哪些 tag）。
> **GHCR 已不再推送**（只有 Docker Hub 一个 registry）。
> **已跑通一次**（2026-09-22，commit `17a084e`，当时还推 GHCR，
> [run 35742701211](https://github.com/kingfs/Decis/actions/runs/35742701211)：6 个构建腿 + 3 个 merge 全绿）。
> 那次证明了矩阵与多架构合并可用；**换成 Docker Hub 之后的路径见下面的"未验证"**。
> **未验证**：Docker Hub 这条推送路径本身、`-offline` 变体、`v*` tag 触发的 release 路径、
> 镜像体积、容器内冷启动、Docker Hub 仓库可见性。
> 报镜像相关的结论时不要超出这个范围。

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
| 「一个 checkpoint 需要哪些仓库」（适配器 + 它适配的基座） | `src/decis/paths.py: BaseModel` / `WeightSpec.bases` | 引擎自己下载基座；把基座写成引擎里第二个硬编码 repo id |
| `(引擎, 设备) → dtype`、以及"能跑但已知很糟"的组合 | `src/decis/engines/registry.py: DTYPE_DEFAULTS` / `DEGRADED` | 引擎自己判断 dtype；全局统一一个 dtype（kev 在 CPU 上 bf16 比 fp32 慢 83 倍） |
| 「某个 primitive 的选项在提示里长什么样」 | 各引擎自己的 record 构造 | 让 `render.py` 决定——kev 的 noul 是 `no`/`yes`、score 是裸层级文本，与 Decis 的 `Option.name` 不同（`design-review.md §2-D9`） |
| 「这个引擎**现在**能不能跑」的分类（依赖 + 权重） | `src/decis/engines/registry.py: status` | 在 CLI 或路由里各写一份"就绪"判断；把"注册了"当成"能跑"报给用户 |
| 引擎 id → 实现的映射 | `src/decis/engines/registry.py` | `if engine == "..."` 散落在业务代码里 |
| 环境变量 | `src/decis/config.py` | `os.environ` 出现在其他模块 |

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
4. 实现 `weights()`（声明权重来源、pin 的 commit、体积）与 `measure()`。`EngineInfo` 必须诚实声明 `max_options`、`max_sequence_tokens`、`max_question_tokens`、`max_state_tokens`、`primitives`、`device`、`dtype`。
   - **`max_sequence_tokens` 是"state + 一个问题"的总预算**，不是 state 单独的预算。state 与 head 共享同一条序列，分开检查会让两边都合规、合起来超长的请求被静默截断（`design.md §4.1`）。
   - **若引擎额外限制 state 本身**（kev：state ≤ 384 而 state+问题 ≤ 1024），必须填 `max_state_tokens`。不填就意味着"序列上限已经覆盖了"，而 kev 那种情况不填会让超长 state 通过校验后被静默截断（`design-review.md §2-D10`）。
   - **`max_question_tokens` 要填最宽松的可靠上界**，不要用"最坏情况"（如 `max_sequence - max_state`）：那会拒掉引擎其实处理得了的请求。真正生效的比较是序列那一条。
   - **`measure()` 必须报真实长度，不能从上游"截断后"的输出反推**：`encode` 会把 state 截到上限，反推出来的数字永远等于上限，上限检查就成了永不触发的摆设（D10 实际踩到过）。
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
uv run pytest -q                     # 无权重测试（CI 跑这个：422 通过 / 31 跳过，约 10 秒）
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
DECIS_DEVICE=cpu                     # 强制设备；不设则自动选。也会影响 dtype 的选择
DECIS_DTYPE=bf16                     # 强制精度；不设则查 registry.DTYPE_DEFAULTS。
                                     #   已知很糟的组合（kev CPU 上 bf16）只告警不拒绝
```
`DECIS_DTYPE` 只对**查 `DTYPE_DEFAULTS` 的引擎**生效（目前是 kev）；Laya 由它自己的
`Agent` 决定精度，不受这个变量影响。

真实模型：

```bash
uv sync --extra laya
uv run decis download --engine laya-multilingual --dest ./models   # 约 647 MiB
uv run decis serve --engine laya-multilingual --host 127.0.0.1     # CPU 冷启动约 75-90 秒
#   冷启动期间 /healthz 立即可用，/readyz 报 {"status":"loading"}；加载失败则报 "failed"
uv run pytest -m weights             # 真实推理 + 批不变性（CPU 上约 113 秒）

uv sync --extra kev
uv run decis download --engine kev-0.8b   # adapter 43 MiB + 基座 1.65 GiB
uv run decis serve --engine kev-0.8b --host 127.0.0.1   # CPU 冷启动约 12-45 秒
```

镜像（已发布，CI 构建）：

```bash
docker run --rm -p 8000:8000 kingfs/decis:mock          # 无权重，立刻可跑
docker run --rm -p 8000:8000 kingfs/decis:laya-multilingual   # 需要机器上已有权重或联网下载
docker run --rm -p 8000:8000 kingfs/decis:kev-0.8b
```

本地构建：

```bash
docker build -f docker/Dockerfile -t decis:mock .
docker build -f docker/Dockerfile \
  --build-arg DECIS_EXTRAS=laya \
  --build-arg DECIS_ENGINE=laya-multilingual \
  --build-arg DECIS_PREDOWNLOAD=laya-multilingual \
  -t decis:laya-multilingual .
```

性能数据（`AGENTS.md §8` 的落地）：

```bash
uv run decis bench --engine laya-multilingual --batch 1,3,10,30   # 采集，写 benchmarks/results/
uv run python benchmarks/report.py --write   # 由原始 JSON 生成 README 与 docs 里的表
uv run python benchmarks/report.py --check   # CI 跑这个：手改过的数字会让它变红
```

跨请求批处理的收益（M5）——`decis bench --cross-request` 转调 `benchmarks/batch_gain.py`
（第二个 harness，不是第二份实现）：

```bash
uv run decis bench --engine laya-multilingual --batch 1,2,4,8,16 --threads 24   # 串行 vs 合成批 + 正对照
uv run decis bench --engine laya-multilingual --cross-request --threads 24 --processes 4   # 多进程
```

`--threads` 是**每个进程**的线程数，`--processes N` 时 CLI 会把它除以 N，**总预算保持不变**；
直接调 `batch_gain.py` 时这个除法要自己算，忘了就是在测线程超配。别加 `--items` 时改小它：
item 数决定每个 batch size 有多少个样本，16 是当前 JSON 用的值。

`--check` 已在 CI 里，且**覆盖两个 README**。生成器在渲染前会断言同一组内各配置处理的是**同一个输入**
（`input_sha256` + token 数），不一致就拒绝生成；对攒批数据还会拒绝**没有正对照**的文件
（测不出收益的 harness 无法区分"机制没用"和"测量坏了"）。它第一次运行就抓到了 §2-D12
（README 曾把两个线程数的数字混进同一行）——**同一缺陷当时还留在 `README.zh-CN.md` 里**，
因为那个文件靠手工抄表；现在两个 README 由同一个生成器写。没有 checked-in 原始 JSON 支撑的数字不许进文档。

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
- ❌ 在 `render.py` 里决定某个模型的选项文本（kev 的 `no`/`yes` 与裸层级文本必须由引擎决定，搞错会静默降质）
- ❌ 改动 `src/decis/engines/_kev_vendor/` 里的任何字节（要更新就整体 re-vendor 并改 `VENDOR.md`/`NOTICE`）
- ❌ 用"上游截断后的输出"反推 `measure()` 的数字（会得到永远等于上限的假测量）
- ❌ 手改 `<!-- MEASUREMENTS -->` / `<!-- LATENCY -->` / `<!-- DTYPE -->` / `<!-- BATCHING -->` 标记块里的数字
  （会被 `report.py --check` 拦下；要改就改原始 JSON 或重测）
- ❌ 手改 `benchmarks/RESULTS.md`（它是生成物）
- ❌ 让一行性能表的不同列取自不同配置（`design-review.md §2-D12`：线程数混用曾真实发生过）
- ❌ 手工把一张表从一个文件抄到另一个文件（`README.zh-CN.md` 抄过，于是它**只在中文版里**带着 D12）：
  抄写就是第二处实现（§2），要么生成，要么不要放
- ❌ 报跨请求批处理的结论时省略它的范围：**上界**（合成批、无队列）、**CPU**、**只有 Laya**
- ❌ 用"一个大小测到底"的顺序做批次扫描（序效应会伪装成批大小的效果，`§4-M2` 与 M5 各踩过一次）
- ❌ 增加一个收益类机制却没有正对照或能证明它触发的指标（`§2-D1`）
- ❌ 比较不同进程数时让总线程预算不一致（那测的是线程超配，不是进程扩展性）

---

## 10. 第三方代码与许可

- Decis 自身：Apache-2.0。
- **Laya**：通过 PyPI `laya` 依赖使用（Apache-2.0），不复制其源码。若将来需要 vendor，必须在 `NOTICE` 里保留 Convai Innovations / NandhaKishorM 的署名。
- **kev**：已 vendor 最小子集（Apache-2.0），来源 `https://github.com/jaredpalmer/kev`，pin commit
  `90990a5`，文件清单、sha256 与取舍见 `src/decis/engines/_kev_vendor/VENDOR.md`，`NOTICE` 已记录。
  逐字节复制，**不得修改**；`tests/test_kev_vendor.py` 守卫其 sha256。
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
- **测量类 PR 必须带正对照**：一个测不出收益的 harness，无法区分"机制没用"和"测量坏了"。
  结论为负时，正对照是让这个负结论可信的唯一东西。
- **实现一个"设计文档说是核心卖点"的机制时（批处理、缓存、并发），PR 必须附带一个能证明它真的被触发的测试或指标**，而不只是"实现完了"。`docs/design-review.md §2-D1` 的教训是：一个从不触发的批处理实现，和没有批处理，在测试上是无法区分的。
