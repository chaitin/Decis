# 设计审查：这份设计够科学、够尊重标准吗？

**日期**：2026-09-22
**审查对象**：`docs/` 下的三份设计文档，以及它们所依据的实测与证据。
**审查方式**：不是"再读一遍确认它说得对"，而是主动去找**它在什么情况下会错**——补做线上观测、用一手标准核对断言、重新推演性能论证的每一步。

---

## 0. 结论

**架构方向是科学的，契约对齐现在是扎实的，但初版有三处真实缺陷和一批方法论上的不严谨。**

分项判定：

| 维度 | 判定 | 说明 |
|---|---|---|
| **核心抽象是否站得住** | ✅ **扎实** | 两个架构完全不同的模型（BERT 系 vs 自回归 LM 系）各自独立收敛到同一中间表示。这不是类比，是两条独立实现路径的实证 |
| **契约对齐** | ✅ **现在扎实，初版不扎实** | 初版只看服务端的 schema 声明；补做线上观测后**发现了一处真错误**（§2.2） |
| **性能论证** | ⚠️ **部分未验证** | 最核心的断言（跨请求批处理带来吞吐）**完全没测过**；已测的部分方法上有序效应 |
| **标准符合性** | ⚠️ **多数尊重，少数有意偏离且已记录** | 见 §3。最关键的一点：**契约兼容与标准符合在这一个点上冲突，我们选了兼容，并把它写下来** |
| **安全** | ❌ **初版有硬缺口，已补** | 初版**完全没有鉴权设计**，而镜像监听 `0.0.0.0` |
| **可运维性** | ❌ **初版有硬缺口，已补** | 缺少优雅下线、启动顺序；而冷启动是 75 秒 |

下面把"发现的问题"和"已经改了什么"逐条写清楚。**没改的也列出来，不假装都解决了。**

---

## 1. 先说哪些是真的扎实（以及为什么）

审查不是只挑毛病。以下几点经得起推敲，且不是靠"看起来合理"成立的：

1. **抽象被两个独立实现验证。** kev 的 `api.py` 与 Laya 的 `common.py:QTYPES` 是两个团队、两种架构、各自独立写出的同一套三段式（`noul`/`choice`/`score`）到概率的映射。这比"我们设计了一个优雅的接口"强得多——它是**收敛证据**。新增引擎只需一个模块加一行注册，这个断言有先例支撑。

2. **契约现在有可 diff、可测的权威来源。** `https://api.typesafe.ai/openapi.json` 是公开的（HTTP 200），OpenAPI 3.1.0，`info.version = 0.2.0`。已收进 `docs/contract/`。这意味着契约不再依赖"某个 SDK 版本的生成物可能滞后"，而是有了一份可以写同源测试（L0）的基准文件。

3. **批处理是复用官方原语，不是 hack。** Laya 的 `collate_items` 实现是 `[it for group in batch for it in group]`——**展平多组问题**是它的原生语义。这说明"跨请求组批"是上游设计者预期内的用法。

4. **性能纪律是可执行的，不是口号。** `AGENTS.md §8` 要求任何性能数字必须来自 checked-in 原始 JSON，且报告前断言输入 sha256 一致。这条规则已经**自我执行过一次**：本次调查的原始数据进了 `benchmarks/results/`，README 的表格由它支撑。

---

## 2. 发现的设计缺陷

### D1（严重）`predict` 的签名会让批处理永远不发生 — 已修正

**初版**：

```python
def predict(self, batch: Sequence[Sequence[PreparedQuestion]], state_text: str) -> list[list[ProbDist]]
```

整批共享一个 `state_text`，并规定"攒批器按 state 分组"。

**为什么这是缺陷**：真实流量里**每个请求的 state 都不同**——不同的工单、不同的邮件、不同的对话。按 state 分组意味着每组的批大小约等于 1（同一 state 的重复请求是罕见情况，不是常态）。于是**跨请求批处理——整个吞吐论点的基石——在真实流量下永远不会触发**。设计文档花了一整节论证批处理是高 QPS 的来源，而协议本身把它排除掉了。

更糟的是，这个问题不会以"报错"的形式暴露：它会表现为"用了 Decis 但 QPS 没有提升"，而不是"启动失败"。**沉默的设计缺陷比会崩的缺陷危险。**

**修正**：改成扁平工作项，每项自带 state：

```python
@dataclass(frozen=True)
class WorkItem:
    request_id: str
    state_text: str
    question: PreparedQuestion


def predict(self, items: Sequence[WorkItem]) -> list[ProbDist]: ...
```

这个签名**严格更一般**：能跨 state 批的引擎（Laya、kev `rows`）直接全量喂；不能的（kev `prefix`）在引擎内部自行按 state 分组再拼回。**"能不能跨 state 批"从协议约束降级为实现细节**，职责放到了正确的地方。

同时删掉了"攒批器按 state 分组"的规定——它现在是纯粹的错误约束。

> 顺带发现的一个推论：这个修正让 `prefix` 模式的适用面变窄了。既然攒批器不再按 state 分组，`prefix` 只在该批内恰好有重复 state 时才有用——也就是"固定文档 + 多变问题"这类场景，而不是通用路径。初版把 `prefix` 当成主路径来对比 `rows`，是建立在错误前提上的。

### D2（严重）完全缺少鉴权 — 已补

初版设计了 401/429 的**响应形状**，却没有任何**认证机制**。而 Dockerfile 里是：

```dockerfile
CMD ["decis", "serve", "--host", "0.0.0.0", "--port", "8000"]
```

**监听所有网卡，无鉴权。** 任何能访问该端口的人都可以白用你的 GPU。

这不是理论风险：kev 上游把 `host` 硬编码成 `127.0.0.1`，正说明作者清楚这个接口默认不该暴露；而 Decis 的整个卖点是"部署出去给别人用"。

**修正**（`design.md §3.1`）：

- `DECIS_API_KEY` / `DECIS_API_KEYS`，用 `hmac.compare_digest` 常数时间比较。
- **先认证、再校验请求体**——线上实测确认真 jev 就是这个顺序（无 key + 非法 body → 403 而非 422）。反过来的实现会向未认证调用方泄露校验细节。
- **默认安全**：未配 key 且 `--host` 非回环地址时**拒绝启动**，需 `DECIS_ALLOW_NO_AUTH=1` 显式承担风险。从 kev 学到的教训是：**不安全的默认值会被原样部署到生产**。
- 同时补了请求体上限（`DECIS_MAX_REQUEST_BYTES`，在读取 body 前用 `Content-Length` 拒绝）、限流、以及 **CORS 默认关闭**（它是 API，不是给浏览器直接调的；开 `*` 会让任意网页用用户浏览器里的 key 打你的服务）。

### D3（中）没处理"批处理会改变数值结果" — 已补

批处理会改变浮点归约顺序、GEMM tiling，Laya 的 option 预算还按批内最长项截断。所以**同一个 `(state, question)` 在 `batch=1` 与 `batch=32` 下可能得到不同概率**，极端情况下 argmax 会翻转。

这与契约直接冲突：官方 SDK **会自动重试 POST**，所以"重发同一请求拿到不同答案"是可观测行为，会被当成 bug 报上来。初版对此**完全沉默**。

**修正**（`design.md §6.6`）：不是忽略，而是明确面对——

1. 文档写明：承诺"相同输入 + 相同批组成"下确定，**不承诺跨批组成逐位一致**。这是所有批处理推理服务的共同性质。
2. 加 `tests/test_batch_invariance.py`：`batch=1/8/32` 比较概率向量，断言最大偏差 < 阈值，且 **argmax 必须不变**。argmax 翻转视为测试失败——那是用户能感知的错误。
3. 提供 `DECIS_BATCH_MAX_SIZE=1` 逃生门，换取逐位可复现。
4. 列入 Stage 3 的**前置**验证，不是优化项的附属品。

**Stage 1 的实测（部分完成，比预期乐观）。** 原判断有两处过度悲观，一处仍然成立：

- ✅ **padding 不会污染结果**。`build_sequence` / `collate_items` 把不同长度的 state 补齐到同一宽度，
  实测"每题单独算"与"混在不同 state 的 batch 里算"**最大绝对偏差 8.345e-07，argmax 翻转 0 次**
  （16 条 different state × 三种原语 × batch=2/4/8/16，原始记录 `docs/contract/stage1-batch-invariance.json`；
  `tests/test_laya_inference.py` 的 `test_batching_across_different_states_is_invariant` 与
  `test_a_long_and_a_short_state_padded_together_stay_correct` 以 1e-5 为界把它钉住）。
  偏差量级说明原因是 attention mask 被正确遵守——这值得测，但不需要为它牺牲批处理。
- ✅ **Laya 的 option 预算不是按批内最长项截断的**。原判断说"Laya 的 option 预算还按批内最长项截断"，
  实测不成立：`build_sequence` 是**逐项**算 head 预算的，`collate_items` 只做 padding。
  所以批组成不会改变某一项的 option 预算，也就不会改变它的文本。
- ✅ **跨请求批处理的收益已测（M5），结论是否定的**。上面两条只说明"批处理不会算错"；
  收益在 `benchmarks/batch_gain.py` 里单独测了：短序列有 1.92x，真实长度下没有，长度倾斜时慢 3–5 倍。
  见 §4-M5。
- ⬜ **覆盖面**：只测了同一进程内、同一引擎实例、batch ≤ 16。多进程/多 worker 之间的一致性
  （Stage 3 的进程池）没有覆盖——不同进程的浮点归约顺序可能不同，但每个回答本身是可复现的。

### D4（中）`confidence` 的决策被讲成了没有代价的改进 — 已改写

初版说"Decis 统一定义 confidence，各引擎不各算各的"，把它当成纯粹的正确选择。

**这不诚实。** 事实是：

- kev 的 `(p_max − 1/K)/(1 − 1/K)` 是 `p_max` 的单调重标定，**没做过标定**；
- Laya 的 `1 − H(p)/log k` 背后**有温度标定**（出厂 ECE 0.466，包内按 (question type, option count) 重新拟合并 clamp）。

统一到 Decis 公式，等于**用可比性换掉了 Laya 已经做过的标定**。这是一个取舍，不是免费的改进。

**修正**（`api-compatibility.md §7`）：保留统一口径作为线格式（契约的可预测性需要它），但——

- **始终**把引擎原生置信度放进响应的 `decis.native_confidence`。对 `noul` 尤其重要：契约规定 `noul` answer **没有** confidence 字段，所以扩展字段是用户唯一能拿到 noul 不确定性的地方；
- 文档明确写出：**用于风险决策时请在自己的标注集上重新标定，不要跨引擎复用阈值**。

实现时进一步发现两个原语的公式口径必须一致（**0 = 无倾向，1 = 确定**）。kev 的 score 公式 `1 − E|level − mode|/(L−1)` 加上众数约束后在 `L=3` 的均匀分布上得 `0.5`，可达区间并非 `[0,1]`；于是 `score` 改用归一化熵 `1 − H(p)/ln(L)`，与 `choice` 口径一致。这是对 kev 的**有意偏离**，记录在 `api-compatibility.md §7`。原计划在 `/v1/models` 里声明 `confidence_formula` 一条已取消：公式不因引擎而异，声明它反而暗示它会变。

### D5（中）缺少启动顺序与优雅下线的设计 — 已补

实测冷启动 **73–76 秒**，而初版只写了"`/healthz` 与 `/readyz` 分开"一句话。缺的是：

- **必须先加载、后监听**。先 bind 端口再加载，编排器会看到端口通了就送流量，请求排在加载后面直到超时。
- **`/readyz` 必须包含一次 warmup 前向**。Laya 首次前向会做惰性初始化，把它算进"就绪"会导致第一个真实用户请求特别慢。
- **加载失败要进程退出非零**，不能"起来了但没引擎"。
- **优雅下线**：`SIGTERM` → 停止接受新连接 → 排空在途 → 释放引擎。**并且 `terminationGracePeriodSeconds` 必须大于冷启动时间**，否则滚动更新永远健康不了（新 Pod 还没 ready，旧 Pod 已被杀）。这是 75 秒冷启动的直接推论。

已写入 `design.md §10.1–10.3`。

> **Stage 1 补充**：上面第一条"必须先加载、后监听"实现之后，代价才显现出来——
> 它意味着冷启动的 80 秒里**服务完全不响应**，`/healthz` 也挂起。这不是本条写错了，
> 而是当时只算了"先 bind 会让流量排在加载后面"这一侧的风险。完整记录与两个选项见 **D7**。

### D6（轻）单请求超时预算与官方 SDK 不匹配 — 已修正

初版写"超时返回 529"。但官方 SDK 的 `DEFAULT_TIMEOUT` 是 **10.0 s / 次 HTTP 操作**，且**连接错误与超时也在默认重试集合里**。所以：

- 服务端算 30 s 才返回 → 客户端 10 s 就放弃并**重发** → 客户端还在算的时候服务端又多了一份活 → 负载被放大 2–3 倍；
- 529 不在 SDK 的"已知状态码"表里（表里只有 400/401/403/404/422/429），会落到 `TypeSafeInternalServerError` 并被重试。

已改为：`DECIS_REQUEST_TIMEOUT_MS` 默认 **8000**（给网络留余量），超时返回 **504**；**队列等待计入这个预算**；过载优先用 **429 + `retry-after-ms`**（在重试集合内且语义准确）。`design.md §6.5`。

### D7（中）冷启动期间服务完全不响应，且文档说的是反的 — 已修正（方案 B）

接 Laya 之后第一次真的用真实权重起服务时实测到的：

```
18:14:04  loading engine laya-multilingual; /readyz reports 503 until this finishes
18:14:06  loading laya-multilingual from convaiinnovations/laya/multilingual@1c5edc17...
18:15:23  loaded laya-multilingual (device=cpu dtype=float32 max_len=1024 head_max_len=256)
18:15:23  {"method":"GET","path":"/healthz","status":200, ...}   ← 第一条 HTTP 响应
```

**冷启动 79.7 秒内，服务不返回任何 HTTP 响应。** 原因是结构性的：`app.py` 的 lifespan 里
`service.load()` 是同步阻塞的，而 uvicorn 要等 lifespan 跑完才进入协议循环——socket 已 bind，
所以探针是**挂起**而不是拿到连接错误。

两个后果：

1. **文档与实现相反。** README 原文写"`/healthz` 立刻可用，而 `/readyz` 在权重进内存之前报未就绪"。
   已改成实测到的事实，并补上"必须给足 start-period"这条操作性警告——本仓库的镜像用 180 秒，
   但按默认探针配置部署到 k8s 的人会遇到重启循环，而原文档不会告诉他。
2. **"未就绪"是一个生产上到不了的状态。** `/readyz` 的 503、`/v1/systemone` 的 503、
   以及 `tests/test_readiness.py` 里那几个断言，都只能靠 `load_engine=False`（测试专用开关）触达。
   也就是说 `AGENTS.md §9` 要求的"用 `/readyz` 明确报告未就绪"这条路当前是死代码。

**为什么还没改**：这是 Stage 0 的一个**有意选择**（见 D5 第一条）——`test_an_engine_that_fails_to_load_stops_startup`
断言"加载失败必须让启动失败"，理由是启动一个永远 503 的服务比直接退出更难察觉。
这个理由成立，所以不能顺手把它反过来。两条路都要付代价：

| 方案 | 好处 | 代价 |
|---|---|---|
| A. 维持同步加载 | 加载失败立刻让进程退出，最不容易被忽略 | 冷启动窗口内无任何响应；探针必须调好，否则重启循环；"未就绪"路径在生产上是死代码 |
| B. 后台线程加载，失败时 `/readyz` 报 `failed` 并返回非 200 | 冷启动期间 `/healthz` 200、`/readyz` 503 且能说明原因；未就绪路径在生产上可达；探针可区分"还在加载"和"崩了" | 加载失败不再让进程退出，需要靠 `/readyz` 的 `failed` + 日志告警；要改掉那个 Stage 0 测试 |

**决定：采用方案 B。** 理由是方案 A 的"快速失败"其实并不更快——容器照样要退出、照样要重启，
而 503 至少让已经在轮询的探针能说出原因。B 保留了 A 真正想要的东西（绝不服务答不了的流量：
`/v1/systemone` 在 loading 和 failed 两种状态下都是 503），同时把"为什么"变成一个可观测的状态。

**已实现**：引擎在 lifespan 起的后台线程里加载；`LoadPhase` 有 `idle/loading/ready/failed` 四态，
`failed` 是终态且**不带 `retry-after`**（官方 SDK 会重试 5xx，告诉它"退避"等于让它永远 hammer 一个
不会恢复的服务）；加载失败记录错误文本（截断到 300 字符）并写完整 traceback 到日志，进程不再退出。

**实测（真实 Laya 权重，CPU，80 秒冷启动）**：

```
1. liveness and readiness DURING the cold start
  ok  /healthz answers during the cold start  -- 0.50s after the request, 0.6s in
  ok  /readyz answers during the cold start   -- 0.01s
  ok  /readyz says loading, not failed        -- {'status': 'loading', ...}
  ok  inference mid-load is 503               -- 503 in 0.0s
2. cold start: 74.9s total
3. the official typesafe-sdk over a real socket  -- 全部通过
```

对照修正前的同一次测量：**第一条 HTTP 响应在 79.7 秒**。现在 `/healthz` 在冷启动开始后
0.5 秒内就答，`/readyz` 明确说 `loading`。

**代价（明确接受）**：加载失败不再让进程退出，所以"容器没起来"这个信号变成了"`/readyz` 持续
`failed`"。这需要监控 `/readyz` 或在日志上加告警；只监控进程存活的人会漏掉它。
`test_an_engine_that_fails_to_load_stops_startup` 因此被改写为
`test_a_failed_load_is_reported_as_failed_and_does_not_stop_the_server`。

**顺带修掉的两处**：
1. `service.answer` 现在**先检查就绪再 `measure()`**。容量校验需要已加载的 tokenizer，
   而后台加载意味着请求真的可能落在"引擎还没好"的窗口里——原先这个顺序会从测量路径里抛 500，
   而不是干净的 503。
2. `load_status` 与 `ready` 归一。引擎自己才是"能不能服务"的权威，所以当引擎已加载而调度器
   没驱动加载时（`create_app` 明确支持传入已加载的引擎），`phase` 不再是 `idle` 而是 `ready`。

### D8（严重）`/v1/systemone` 是 `async def` 却调用阻塞函数，一次推理会堵死整个事件循环 — 已修正

写请求预算的测试时发现的，不是设计推演出来的：`routes.py` 里 `/v1/systemone` 写成 `async def`，
而 `service.answer` 是同步阻塞函数，于是 FastAPI 直接在**事件循环线程**上跑它。后果是

- 一次推理（几百毫秒，长 state 到秒级）期间，服务器**不响应任何其他请求**；
- `/healthz` 与 `/readyz` 也在其中，所以**探针会在推理期间挂起**，编排器可能杀掉一个完全正常的容器；
- 并发度实际为 0，`InProcessScheduler` 的锁根本没机会起作用——序列化发生在事件循环上，而不是引擎上。

这个 bug 之前测不出来，因为 `.venv` 里无权重的测试替身推理是微秒级，而真实 Laya 的 300 毫秒没有被
任何并发测试覆盖过。发现它的测试是 `test_healthz_and_readyz_answer_while_inference_is_running`：
把一个假引擎卡在 `predict` 里，然后在推理进行中探 `/healthz`。

**修正**：把路由从 `async def` 改成 `def`，Starlette 便会在它的工作线程池里执行它，事件循环空出来，
序列化交回给调度器的锁——那本来就是它存在的理由。`/v1/models` 同样改了（它要 import 引擎类）。
`/healthz`、`/readyz` 保持 `async def`：它们只读内存里的状态，不阻塞。

这个 bug 与 D7 属于同一族——**"服务在忙时不回答探针"**——但成因和修法完全不同，所以分开记录。

### D9（严重，会静默降质）引擎的"选项文本"不能由 Decis 统一决定 — kev 揭示的抽象缺口

接 kev 时发现的，属于 Stage 2 存在的意义。`render.py` 是"任意 JSON → 可读文本"的唯一所在，但**选项在序列里长什么样是引擎自己的格式**。Stage 1 的 Laya 没有暴露这个区别，因为 Laya 恰好也用 `"name: description"`（choice）和 `"level N: "`（score）；kev 用的是另一套：

| primitive | Decis `PreparedQuestion`（`render.py`） | kev 训练与服务的文本（`api.py:102`） |
|---|---|---|
| choice | `Option(name=criteria 键, description=渲染值)` | `f"{name}: {description}"`（无描述则裸 `name`）— **与 Decis 一致** |
| noul | `Option("false", …)`、`Option("true", …)` | `"no"`、`"yes"`（`option_text("no", …)`）— **不同** |
| score | `Option("0".."n-1", description=层级文本)` | **只有层级文本**，不带 `"0: "` 前缀 — **不同** |

若 kev 引擎直接照搬 Decis 的 `Option.name`，模型会收到它训练时从未见过的文本（`"false: …"`、`"0: Can wait"`），**准确率下降而没有任何报错**——正是本项目最不能接受的那类 bug。

**证据（不是推断）**：kev 的 `data.py:395 materialize()` 明确写着 "Labelled request -> internal record **via the serving path (api.to_record)**"——训练数据与线上服务走的是同一个函数。所以 `api.py` 的渲染约定**就是**模型学到的约定。

**结论：抽象是对的，不需要改 `render.py` 或 `answers.py`。** 需要的是承认"选项文本"属于引擎的序列格式（`AGENTS.md §2` 已按 Stage 1 的结论这么规定），因此 kev 引擎自己把 `PreparedQuestion` 映射成 kev 的文本。已实测验证：用 Decis 的 `PreparedRequest` 构造出的 record 与 kev 自己的 `api.to_record` **逐字段相等**，`encode` 出来的 `ids/seg/pos/opt/decide_idx/opt_idx` 全等，概率与上游 `model.probs()` **差 0.00e+00**（kev-0.8b，CPU fp32，`.scratch/kev_probe.py`）。

顺带得到两条独立佐证（与 Laya 的一样，属"未计划的一致性"）：kev 的 `question_keys`（`api.py:94`）与 Decis 的 `answers.py: question_keys` **逐字相同**；kev 的 `choice_confidence`（`api.py:120`）与 Decis 的 choice confidence **公式相同**。kev 的 `score_confidence`（`api.py:125`，"距离众数层级"）与 Decis 的归一化熵**不同**，且它自称是"未公开公式的近似"——这正好是 `decis.native_confidence` 存在的理由；但它在 `noul` 上没有定义，而 `native_confidences` 是按 item 对齐的列表，混合请求里没有连贯的值可报，所以 kev 引擎不填这个字段（`answers.py` 的 `confidence` 三个 primitive 都覆盖）。

另外记录一处**已知偏差，未实测影响**：Decis 的 `render_value` 对 dict **按键排序**（`render.py:46`），kev 的 `render` 保留插入顺序（`api.py:55`）。state 的渲染归 `render.py`（`AGENTS.md §2`），且 Laya 已按现行行为发布并测试，所以不改；但 kev 的 state 文本因此可能与它训练时见过的字段顺序不同。影响未测。

### D10（中）容量接口没有"state 单独上限"的位置，而 kev 有一个 — 已修正

kev 的 `encode(strict=True)` 同时施加两条**不同**的限制（`model.py:44,54`）：

1. `len(state) + 1 <= MAX_STATE`（384）
2. 每个问题 `len(state) + len(branch) <= MAX_BRANCH`（1024）

而 `validate_capacity` 只有两个比较位：`sequence_tokens <= max_sequence_tokens` 和每个问题 `head_tokens[qid] <= max_question_tokens`（`schema.py:222,260`）。`MeasuredTokens.state_tokens` **只被报告、从不校验**。

于是 (2) 可以落在 `sequence_tokens` 上，而 (1) 无处安放：state 500 token、问题 10 token 的请求 `sequence_tokens = 510 <= 1024` 会通过校验，然后被 `encode` 截断（`strict=False` 时它静默 `state_tokens[:max_state-1]`，并在 `state_truncated` 里留一个没人看的标记）。

Laya 没有这个问题：它只有一个约束（`max_len`），state 按剩余空间截断，所以一个比较位就够。**这属于 `EngineCapacity`/`validate_capacity` 的接口缺口，不是 kev 的怪癖**——任何有独立 state 窗口的引擎都会撞上。

**已修正**：`EngineInfo` 增加 `max_state_tokens`（默认 `0` = "没有独立上限"，Laya 行为不变），
`EngineCapacity` 协议同步，`validate_capacity` 在做序列比较之前先查 `measured.state_tokens`，
错误定位到 `body.state` 并说明是"内容本身"超限。kev 的 `info()` 声明 384。

实现这条时又踩到一个自己挖的坑，值得记下来：初版 `measure()` 从 `encode` 的输出反推 state 长度
（`len(ids) - branch`），而 `encode` 会**把它截断到 384**，于是量出来的 state 永远等于 384，
新加的上限检查**永远不会触发**——一个"实现了但从不生效"的检查。现在直接量原始 token 数
（`1 + len(tokenizer(state))`），branch 用一个空 state 的 record 单独量，两边都不经过截断。

**同时修掉一处会误拒的设计**：`max_question_tokens` 最初写成 `MAX_BRANCH - MAX_STATE`（640），
这会把"100 token 的 state + 700 token 的问题"拒掉，而 kev 实际处理得了（800 ≤ 1024）。
现在报告的是最宽松的**可靠**上界 `MAX_BRANCH`，真正生效的是序列比较；`measure` 仍然报每个问题
自己的开销，所以 422 还是能指出是哪个问题。

**顺带确认 Stage 2 的抽象结论**：接 kev 全程**没有改** `render.py`、`answers.py` 或路由层。
需要改的只有 `paths.py`（`BaseModel`：适配器 + 基座是两个仓库）与 `schema.py`/`base.py`
（state 上限）。这两处都不是"抽象错了"，而是原来的接口少了一个维度——正是 Stage 2 要发现的东西。

### D11（中）kev 挂载"外部基座"无效：基座只能按 repo id 去 Hub 找 — 未修正，已记录

需求里有一条是"支持挂载外部模型"。适配器这一半成立：`paths.resolve` 找到的本地目录会作为
`Checkpoint(<目录>)` 传进去，`_resolve_run`（`_kev_vendor/checkpoint.py:30-36`）见到本地路径就
直接用它，不发网络请求。

**基座那一半不成立。** 基座是 checkpoint 元数据里的一个**字符串** `meta.base`（形如
`Qwen/Qwen3.5-0.8B-Base`），vendored 加载器把它原样交给 `load_tokenizer(meta.base, ...)` 与
`DecisionModel(meta.base, ...)`（`checkpoint.py:127-128`），于是无论适配器从哪来，基座都只会
按 repo id 去 Hub 找（缓存命中或联网）。

实测后果：用户若把基座单独下到本地目录、用 `DECIS_MODEL_PATH_KEV_0_8B` 指过去，**基座那份
配置不会生效**——引擎仍然去 Hub 要 `Qwen/Qwen3.5-0.8B-Base`。离线机器上只要缓存里有基座就没
问题（`decis download` 正是把它放进缓存的，`paths.BaseModel` 那条路径刻意不带 `local_dir`），
所以这不是"离线不可用"，而是"**挂载基座不生效**"。

**为什么现在不修**：修它要动 vendored 的 `checkpoint.py`，而那份文件是逐字节复制、有 sha256
守卫、且刻意不做本地修改的（`VENDOR.md`）。可行的修法有两条，都留到真要支持"挂载自定义基座"
时再做：
1. 在 `load()` 里先把 `meta.base` 映射成 `paths.resolve` 找到的本地目录（若存在），再传给一个
   **不改 vendored 代码** 的加载路径——但 vendored 的 `DecisionModel.__init__` 只接受 repo id，
   所以这条路实际要复制它的加载逻辑，等于在 Decis 里维护第二份；
2. 向上游提一个 PR 让 `meta.base` 支持本地路径，然后整体 re-vendor。**这是更干净的选项**，
   因为它把"基座可以是目录"这件事留在唯一事实来源里。

在此之前，README 与 `AGENTS.md` 不许把"挂载目录"说成对基座也成立——只能说的是适配器。

### D12（低，但正是 §8 要防的）README 性能表把两个线程数混进了同一行 — 已修正，且改成了自动生成

`AGENTS.md §8` 说"文档里的任何性能数字都必须由 `benchmarks/report.py` 从 checked-in 原始 JSON
生成"。这条规则在被执行之前，先抓到了它要防的东西。

原来的 README 表格：

| 引擎 | 冷启动 | 峰值 RSS | 1 问 | 10 问 |
|---|---|---|---|---|
| `laya-multilingual` | 73 s | 4.84 GB | 201 ms | 987 ms |
| `laya`（英文） | 76 s | 2.80 GB | 432 ms | 3222 ms |

对着 `benchmarks/results/laya-*-sweep.json` 逐格核对：

- `201 ms` 是**线程数 16** 的 `laya-multilingual` 1 问；同一行的 `987 ms` 是**线程数 24** 的 10 问。
- `432 ms` 是**线程数 16** 的英文 1 问；同一行的 `3222 ms` 是**线程数 24** 的 10 问。

**四格里三格取自不同的配置。** 每个数字单独看都对，整行没有意义——而"没有意义"这件事在
表格里看不出来，因为原表根本没写线程数（这同时违反 §8 里"必须同时给出引擎、设备、dtype、
线程数/进程数、批大小、state 长度、问题数"那一句）。

**修法不是把数字改对，而是让这类错误不可能再被写出来**：`report.py` 生成表格时，一行的所有列
都从**同一个配置**里取，并把线程数写进表格本身；`--check` 在 CI 里跑，手改一个数字就会失败。
`tests/test_benchmark_report.py` 里有这一条的负向对照（把一个数字改掉，断言检查器能发现），
按 §12 的要求"证明它真的被触发"。

顺带一个同源的问题：`kev-0.8b-cpu-dtype.json` 的对比轴是 **dtype**，不是线程数。生成器第一版
把它当线程扫描渲染，于是 fp32 与 bf16 两行因为 `(threads, questions)` 相同而被**静默合并成一行**
——这是生成器自己犯的同类错误，改成先探测变化轴再选表格形状。这条也被一个测试钉住了。

**教训**：把"数字必须来自原始数据"写成规则，价值不在数字本身，而在于**它逼你把表格的每个维度
也想清楚**。手写表格时，"这一行的线程数是多少"是一个没人问的问题。

### D13（中，待决策）`noul.criteria` 写错键名会被**静默忽略**，答案照常返回 200 — 写 examples 时发现的

`noul` 的 `criteria` 只有两个键：`"true"` 和 `"false"`（`src/decis/schema.py:44-48`）。
但写 `noul` 问题时最自然的手感是 `"yes"` / `"no"`。实测：

| 请求里的 criteria 键 | HTTP | `noul` | `input_tokens` |
|---|---|---|---|
| `{"true": …, "false": …}` | 200 | **0.9435** | 65 |
| `{"yes": …, "no": …}` | 200 | **0.3694** | 47 |

两个都是 200。第二行里那段评分标准**根本没被渲染进提示**（token 数从 65 掉到 47），
但调用方拿到的是一个看起来完全正常的概率值。**用户不会知道自己的 rubric 被丢了。**

这违反本项目已经承认的一条原则：§9 写"❌ 让引擎在超预算时静默截断输入（上游这么干，
Decis 不能跟着干）"。机制不同（未知字段 vs 超预算），失效模式相同：**调用方以为模型看到了
一段它其实没看到的信息，而返回的概率看起来仍然合理。**

**但它不是一个契约违规。** 查了官方快照：`docs/contract/typesafe-openapi-0.2.0.json` 里
`NoulCriteria`（以及其余所有对象）**都没有 `additionalProperties: false`**
（`components.schemas.NoulCriteria.properties = ["true","false"]`，`additionalProperties` 缺失）。
JSON Schema 中缺失即允许额外属性，所以**接受额外键是符合契约的**，Decis 没有偏离。

顺带一个更微妙的不一致：**官方 SDK 对这一条的严格程度和裸 JSON 不同**。
`typesafe_sdk` 的 `NoulCriteria` 是 `TypedDict(total=False, closed=True)`，
所以走 SDK 的用户会拿到一个响亮的 `ValidationError`；走裸 JSON 的用户拿到静默降级。
同一个错误，两条路径的响亮度不一样。

**为什么留给你决策**（`AGENTS.md §12` 要求改契约行为前先跑线上差分，而这里连差分都做不了）：

1. **参考行为未知**。没有线上 key，无法观测真 jev 对未知键是忽略还是 422。任何"和 jev 一致"
   的说法现在都是猜的。
2. **两种合理选择，代价不同**：
   - **保持宽松**：向前兼容性最好（jev 将来加字段不会把我们打死），代价是上面那个静默降级的坑。
   - **在 `noul.criteria` 这一层收紧**（只对这一处、只有两个已知名字，报 422）：
     坑填掉了，且因为顶层仍 `ignore`，不影响"jev 将来加对象字段"的兼容性。
     代价是比契约更严格——一个合法（虽然大概不是本意）的请求会被我们拒掉。
3. **折中第三条**：接受但在日志里 `warning`，并在 `/v1/systemone` 响应的 `decis` 命名空间下
   回一个 `ignored_fields` 列表。不破坏兼容，但把静默变成可见。

**倾向**：第 3 条。它不改变契约行为（仍 200、仍是 Ignore），却把"静默"这个真正的缺点去掉；
而且 `decis.ignored_fields` 是 `decis` 命名空间内的增量字段，官方 SDK 会忽略，零兼容风险。
**但它需要先确认一件事**：真 jev 是否也在 `decis` 之外的路径上返回额外字段——如果它严格拒绝，
那我们选择宽松就等于让一批在真 jev 上会失败的请求在我们这里"成功"，那是更坏的兼容性问题。

**当前状态：未修。** 交付物 `examples/curl.md` 已用正确的 `"true"`/`"false"`，
并在该处标注了这个坑与本节编号，避免示例把人带进坑里。

---

> **历史记录（D14–D16，2026-09-23）**：这三条记的是当时那个**无权重的假引擎镜像**（tag 就叫
> `mock`）。假引擎和它的镜像 tag 后来已从代码、工作流和文档中移除，所以下面
> `docker pull kingfs/decis:mock` 这类命令今天不再可用；**缺陷的形状与教训与具体引擎无关，
> 仍然有效**，`AGENTS.md §9` 也仍然引用了 D14/D15。

### D14（中，已修正）`mock` 镜像装了 Laya 和 torch：3.2 GB 里 3.1 GB 是它不需要的

**发现方式**：镜像第一次真正推到 Docker Hub 之后，用 registry API 逐层列体积。
`mock-latest` 是 **3203 MB**，和 `laya-multilingual-latest` **一模一样**——

```
layer                size  history
...（python:3.13-slim + uv，约 66 MB）
41887076501f        3137MB  RUN |3 DECIS_EXTRAS=laya DECIS_ENGINE=mock DECIS_PREDOWNLOAD= ...
TOTAL               3203MB
```

`DECIS_EXTRAS=laya`，而 `DECIS_ENGINE=mock`。**`mock` 镜像里装了 torch、transformers、peft。**
一个"什么都不需要、起来就有"的引擎，镜像比 python 基础镜像大 60 倍。

**根因是一行 YAML 里的三元表达式惯用法**：

```yaml
DECIS_EXTRAS=${{ matrix.engine == 'mock' && '' || (matrix.engine == 'kev-0.8b' && 'kev' || 'laya') }}
```

GitHub Actions 的表达式里没有真正的三元运算符，社区惯用 `A && B || C`。这个惯用法
**在 `B` 为假值时是错的**：`'mock' == 'mock'` 为真 → `''`，然后 `'' || …` 因为**空字符串是假值**
而继续求值右边 → `'laya'`。于是"没有 extra"这个分支永远不可能被选中。
`kev-0.8b` 和 `laya-multilingual` 恰好都落在默认值上，所以**只有 `mock` 是错的**，
而它偏偏是每个用户第一次 `docker pull` 的那个（也是 CI 里"镜像能起来并服务"那条腿用的那个）。

**为什么原来的测试没抓到**：`test_the_extra_mapping_matches_the_registry` 里写着一份
**手抄的** `expected = {"mock": "", …}`，然后拿它和 registry 比——它比的是两份硬编码常量，
从来没有读过 YAML 里的那个表达式。这正是 §2"抄写就是第二处实现"的又一个实例：
第一处实现（表达式）错了，第二处实现（测试里的常量）是对的，于是测试恒绿。
现在的测试改成读**执行 plan 脚本后产出的矩阵**：表达式已经不存在了，
映射搬进了 plan 脚本（`extra_for()`），而那个脚本的测试是真跑的。

**修法**：把 `引擎 → pip extra` 的映射从 YAML 表达式搬进 `plan` 步骤，
矩阵里直接带 `extra` 字段，构建步骤只做 `DECIS_EXTRAS=${{ matrix.extra }}`。
顺带得到两个好处：(1) 该映射只剩一处，且与 `registry.SPECS[...].extra` 有测试比对；
(2) 出现一个没有映射的引擎时 `plan` **直接失败**，而不是静默落进 `laya` 默认值。

**教训（可以推广的）**：`A && B || C` 不能用来表达"可能为空的三元"。空字符串、`0`、
空数组在 GitHub 表达式里都是假值，所以这个惯用法只在所有分支都为真值时才等价于三元。
凡是"选一个可以合法为空的值"，都不要用它。

### D15（低，但同一类）README 写的 tag 根本不存在：文档说 `:laya-multilingual`，镜像叫 `:laya-multilingual-latest`

推完之后按文档去拉，第一步就失败：

```
$ docker pull kingfs/decis:mock
Error response from daemon: manifest for kingfs/decis:mock not found
```

工作流给 master 推送打的 tag 是 `${engine}-${MOVING_TAG}`，也就是 `mock-latest`。
用户（以及本仓库 README 和 AGENTS.md）预期的名字是 `kingfs/decis:mock` ——
**一个引擎一个仓库时才是那个名字，改成"引擎进 tag"之后 `-latest` 就纯属多余**：
`:laya-multilingual` 本身就是"当前的 Laya 镜像"，正如 `:latest` 是单用途仓库的当前镜像。

更值得记的是它为什么能溜过去：**没有任何测试拿 tag 和文档对照过**，
而 D14 刚刚证明"文档/测试里的名字与工作流真正产出的名字不一致"这类缺陷是真实存在的。
和 D12（手抄的性能表）是同一个形状：**同一个事实存在两份，只有一份被执行**。

**修法**：tag 方案整个搬进 `plan` 步骤，矩阵里带 `image_tag`，构建与合并步骤只做插值，
测试从**执行 plan 后产出的矩阵**里读 tag（master 推送 → 引擎名；release tag → 引擎名 + 版本；
offline → 引擎名 + `-offline`）。顺带把"两个步骤各自拼一遍 tag"这个第二处实现也消掉了。
已按新方案删掉 Docker Hub 上 21 个旧 tag，避免留下再也不会更新、但看起来仍然有效的 `-latest` 别名。

### D16（严重，已修正）三个引擎把 per-arch 中间 tag 互相覆盖，于是三个 tag 发出的是同一个 manifest

修完 D15（tag 改名）之后照例用 registry API 核对，发现**三个 tag 指向同一个 amd64 manifest**：

```
mock               amd64 child=('sha256:35786f81c1cebb6ed663a800096425d2dbe26152ebb5eec0b3e29169cf569f61', 3206 MB)
laya-multilingual  amd64 child=('sha256:35786f81c1cebb6ed663a800096425d2dbe26152ebb5eec0b3e29169cf569f61', 3206 MB)
kev-0.8b           amd64 child=('sha256:35786f81c1cebb6ed663a800096425d2dbe26152ebb5eec0b3e29169cf569f61', 3206 MB)
```

**`docker pull kingfs/decis:mock` 会拿到一个 3.2 GB 的、`DECIS_DEFAULT_ENGINE` 是别的引擎的镜像。**

根因：合并步骤要从 per-arch 镜像拼多架构 manifest，而 per-arch 的中间 tag 当时叫
`sha-<commit>-<arch>`。**所有引擎共用同一个仓库**，于是六个构建腿同时往
`sha-a0f2ee5c16b1-amd64` 这一个名字上推。后推的覆盖先推的；等三个 merge job 跑起来时
（merge 必然在全部 build 之后），它们读到的是**最后完成的那个引擎**的镜像，
于是三个 tag 都被合成了同一份 manifest。

这正是 D14/D15 的同一个形状第三次出现：**一个名字被两份东西写，只有一份是"对的"**。
D14 是"表达式 vs 抄本"，D15 是"文档 vs 工作流"，这里是"腿 A vs 腿 B"。
一次真正的端到端核对（照着文档 `docker pull` + 比对 digest）才能发现它——
所有 30 个 workflow 测试当时都是绿的，因为它们都在检查**一个**腿产出的字符串对不对，
没有检查**两个腿会不会撞车**。

**修法**：per-arch tag 里加上 artifact 名 —— `${IMAGE_TAG}-sha-${short}-${ARCH}`，
即 `laya-multilingual-sha-a0f2ee5c16b1-amd64`。
**并加了一条"不许撞车"的测试**：把所有 (引擎, variant, arch) 组合的构建腿都跑一遍，
断言任意两个腿写出的 tag 集合两两不相交。这条测试在旧方案下会失败（已实测），
所以它不是空文——它列出的正是上面那张表里的 20 组冲突。

**教训**：在一个**共享仓库**里，任何由多个矩阵腿写入的 tag 都必须把腿的身份放进名字里。
只检查"我这条腿的 tag 对不对"是不够的，必须检查"两条腿的 tag 会不会相同"。

**同一批发现里更值得记住的一点**：这个缺陷是**镜像体积**这种维度暴露的，
而它此前躲过了全部 422 个测试。体积不是被断言出来的，是推上去之后用 registry API 量出来的——
`AGENTS.md §8` 那条"数字必须来自实测"在这里救了一次场。

### D17（高，已修正）`decis download` 把权重写到 `resolve` 永远不看的目录，于是每次都失败

**发现方式**：为 compose 设计"一次性 init 容器把权重预取进卷"时，用一个会写出**真实布局**的
`snapshot_download` stub 跑 `decis download`，三种 `--dest` 全部退出码 1：

```
dest='models'                     exit=1  written=multilingual/rl_agent_config.json
dest='models/laya-multilingual'   exit=1  written=multilingual/rl_agent_config.json
error: download finished but ... still has no usable checkpoint
```

**根因**：`snapshot_download(local_dir=X)` 复制的是**仓库自己的布局**，所以 `laya-multilingual`
落在 `X/multilingual/…`；而 `cli._download` 把 `X` 当作 `DECIS_MODEL_DIR` 交给 `paths.resolve`，
后者只找 `X/<engine id>/`。两边各自的"唯一事实来源"（`download_arguments` 的 allow_patterns 与
`candidate_directories`）都没错，错在下载器选了个两边都不认的落盘位置。

**影响面远不止一条命令**：
- `decis download` 从来没能成功过一次，而 README 与 `AGENTS.md §7` 都把它写成用户的第一步；
- `docker/Dockerfile` 的 `DECIS_PREDOWNLOAD` 在 `RUN decis download` 处**直接构建失败**——
  所以"带权重的离线镜像"不是"未验证"，是从来没构建成功过（AGENTS 里那条"未验证"也要跟着改）；
- 任何"先把权重预取进卷、再起服务"的编排（compose、K8s initContainer）都不成立。

**为什么测试没抓到**：`test_download_accepts_an_alias` 把 `snapshot_download` stub 成"什么都不写"，
只断言传进去的 `allow_patterns` 与 `revision`——它验证的是**我们请求了什么**，不是**下完之后
loader 能不能找到**。D14 是"表达式 vs 抄本"，这里是"请求参数 vs 真实落盘"：同一形状第四次。

**修法**：
1. 有模型目录时落盘到 `<dir>/<engine id>/`——`Dockerfile` 的注释、`.env.example` 与
   `paths.resolve` 三处一直就是这么写的，缺的只是让下载器照做；
2. 没有模型目录时（`DECIS_MODEL_DIR` 未设、也没有 `--dest`）写 **Hub 缓存**，那才是 `serve`
   在零配置时会读的地方，于是 README 的"下载一次、然后 serve"真的只下载一次；
3. 两条路都在下载之后**用 `paths.resolve` 自己验证**才报成功（缓存那条用 `checkpoint_root` 验）；
4. 回归测试让 stub 写出**真实下载器的布局**，并断言 `resolve` 找得到——只断言参数的测试
   在这类缺陷面前是恒绿的。

**教训**：凡是"写到某处、之后从某处读"的两个模块，必须有一条测试把**真实的写**接上
**真实的读**；把写 stub 掉再断言参数，等于把这条链子中间剪断还宣称它连着。

### D18（中，已修正）compose 的 `command:` 覆盖了镜像的 `CMD`，容器去找一个叫 `download` 的程序

**发现方式**：第一次真的用发布镜像跑 `docker compose up -d --wait`（D17 修好之后的端到端验证），
`weights-laya-multilingual` 立刻失败：

```
OCI runtime create failed: exec: "download": executable file not found in $PATH
```

**根因**：`docker/Dockerfile` 只有 `CMD ["decis", "serve"]`，**没有 `ENTRYPOINT`**。
compose 的 `command:` 是**替换** CMD，不是追加，所以 `["download", ...]` 让内核去 exec 一个
叫 `download` 的程序。YAML 层的测试测不出来——更糟的是，`tests/test_compose.py` 当时断言的
正是 `command == ["download", "--engine", X]`：**它把错误的期望抄了一遍**（D14 的同一形状，
只是这次的"抄本"是编排文件本身的写法）。

**修法**：所有 `command` 都以 `decis` 开头；测试改成**从 Dockerfile 读出有没有 `ENTRYPOINT`**
再决定该断言什么，而不是把当前写法当常量。

**教训**：镜像的入口点（`ENTRYPOINT`/`CMD`）与编排层的 `command:` 是两个东西，它们的组合语义
只有真起一次容器才会暴露。凡是"编排文件该怎么写"的判断，至少要有一条测试把它和镜像定义对起来，
并且**真的跑一次**——这里从 `up` 到报错只用了 1 秒。

### D19（中，已修正）为权重下载配的代理把镜像自己的 liveness 探针打成了 502

**发现方式**：真实 `docker compose up -d --wait` 的下一次尝试。引擎**已经加载成功**——
日志里明明白白 `engine laya-multilingual ready after 86.1s`——但容器一直是 `unhealthy`：

```
urllib.error.HTTPError: HTTP Error 502: Bad Gateway
```

**根因**：这台机器没有直连出口（`sysctl` 之外还有网络策略），容器要么走代理、要么什么都下不来，
于是 `.env` 里有 `HTTP_PROXY=…`，`env_file` 把它传进容器。而 `docker/Dockerfile` 的 HEALTHCHECK 是

```
python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', …) …)"
```

**`urllib` 会读 `HTTP_PROXY`**：探针于是去问代理要 `http://127.0.0.1:8000/healthz`，
代理当然给不出这个地址，返回 502。服务是健康的，**探针不是**。

**为什么危险**：这不是"启动慢"，是"探针永久失败"。同一组合在 Kubernetes 里会让一个完全健康的
Pod 反复重启——每轮都要重付 86 秒冷启动，而日志里全是 `ready`。§2-D7 论证了 liveness 与
readiness 的分工，却没料到 liveness **自身**会被环境变量劫持。

**修法**：
1. `HEALTHCHECK` 的命令前加 `env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy
   -u ALL_PROXY -u all_proxy`——**只作用于探针**，引擎自己的下载照旧走代理；
2. `.env.example` 的代理段落一并给出 `NO_PROXY=127.0.0.1,localhost,::1`，
   给"镜像早于这个修复"的场景一条出路（本次验证就是这么过的）；
3. `tests/test_compose.py` 加守卫：从 Dockerfile 的 HEALTHCHECK 块里断言它确实 `-u` 掉了代理变量。

**教训**：探针是"从容器内部发出的一个请求"，因此它继承该容器的一切环境（代理、`SSL_CERT_FILE`、
DNS、`NO_PROXY`）。把它写成"用通用 HTTP 客户端访问 loopback"就等于假设这些环境是干净的——
而在"需要代理才能下载权重"这个**恰恰是本项目镜像最常见部署前提**的场景里，假设不成立。

---

## 3. 标准符合性对照

用户问"是否尊重标准"。逐条对照，**包括我们有意不遵守的地方**：

| 标准 / 规范 | 我们是否遵守 | 说明与依据 |
|---|---|---|
| **OpenAPI 3.1.0** | ✅ 遵守 | 契约基准就是官方 OpenAPI（`docs/contract/typesafe-openapi-0.2.0.json`）。**并且 Decis 自己也会暴露 `/openapi.json`**，让用户能生成客户端 |
| **RFC 9110 §15.5.2**（401 必须带 `WWW-Authenticate`） | ⚠️ **上游违反，Decis 修正** | 一手核对：*"The server generating a 401 response **MUST** send a `WWW-Authenticate` header field"*（[RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html)）。线上实测确认真 jev 的 401 **不带**该头。**Decis 补上**——这是纯增量、对 SDK 无影响，但让 Decis 通过标准 HTTP 客户端与网关的检查 |
| **RFC 6750（Bearer 用法）** | ⚠️ 部分修正 | Bearer 方案的注册规范：[RFC 6750 §3](https://datatracker.ietf.org/doc/html/rfc6750) 要求 401 **MUST** 带 `WWW-Authenticate`，并定义 `error="invalid_token"`。所以 Decis 的 401 用 `WWW-Authenticate: Bearer error="invalid_token"`；缺凭证时用不带 error 参数的 `Bearer`。**注意**：RFC 6750 倾向用 401 表示"未提供凭证"，而 jev 用 403——Decis **跟随 jev**，因为客户端兼容是首要目标 |
| **RFC 9457（Problem Details，取代 RFC 7807）** | ❌ **有意不遵守** | 业界标准的 API 错误体是 `application/problem+json`。jev 用的是 `{"detail": …}` 且 `detail` 还是多态（对象/数组/字符串，见 `observations-2026-09-22.md §2.6`）。**这是"契约兼容 vs 标准符合"的真实冲突点**。我们选兼容——因为整个项目的存在理由是"换 base_url 就能跑"。已在 `api-compatibility.md §5` 记录为有意偏离 |
| **RFC 9110 §9.2.2（POST 非幂等）** | ✅ 无冲突 | 虽然 POST 定义上非幂等，但 `/v1/systemone` 是纯函数；SDK 会重试，我们保证重复执行结果一致（除 D3 的批组成例外） |
| **OCI Image Spec 注解** | ✅ 遵守（已补） | 补齐 `org.opencontainers.image.{title,description,source,url,licenses,version,revision,created,base.name}`。自有扩展用 `ai.decis.engine`，不占用 OCI 保留键 |
| **容器安全基线**（非 root） | ✅ 遵守（已补） | `USER 10001:10001`。这也让镜像能通过 Kubernetes `restricted` 档 Pod Security |
| **SemVer 2.0** | ✅ 遵守 | Python 包与镜像 tag 都用 SemVer（`<engine>-<semver>`） |
| **PEP 621 / 440 / 517-518** | ✅ 遵守 | `pyproject.toml` 用 PEP 621 元数据，hatchling 作 PEP 517 后端，uv 生成 `uv.lock` |
| **PEP 8 / 484 / 561** | ✅ 遵守 | ruff 强制；公开 API 全部类型标注 |
| **12-Factor（配置来自环境）** | ✅ 遵守 | 所有配置集中在 `config.py`，`os.environ` 不得出现在其他模块（`AGENTS.md §2` 已列为唯一事实来源） |
| **SPDX / Apache-2.0** | ✅ 遵守 | `LICENSE` + `NOTICE` 记录 kev vendoring 与 Laya 署名；不复制 laya-mlx 代码 |
| **供应链：SBOM + provenance** | ⚠️ 计划中 | Stage 4 用 buildx `--provenance`/`--sbom`。**尚未实现，不假装已有** |
| **Prometheus 暴露格式** | ✅ 遵守（计划） | `/metrics` 用标准 exposition format，不从零发明指标名 |
| **模型卡 / 可复现性规范** | ⚠️ 无正式标准 | 没有 IETF/ISO 级的规范。我们采用社区惯例：报告必须同时给出引擎、设备、dtype、线程/进程数、批大小、state 长度、问题数（`AGENTS.md §8`） |

**这一节里最值得注意的一点**：RFC 9457 那一行。一个"尊重标准"的项目在这里**故意不遵守标准**——因为遵守它就会破坏与 jev 的线格式兼容，而那正是项目的全部价值。**正确的做法不是假装没有冲突，而是把冲突显式写下来并说明选择理由。** 这也是我把 §5 从"推断"改写成"实测 + 有意偏离记录"的原因。

---

## 4. 方法论问题（关于证据本身）

设计文档的可信度取决于证据的质量。这部分是自我审查里最不舒服、但最有价值的部分。

### M1（严重，已修正）初版契约只有客户端视角

初版全部结论建立在两件事上：读官方 OpenAPI **生成物**、读 SDK 源码。这有个结构性盲点：**它无法验证服务端是否真的按声明行事。**

补做线上观测后，立刻发现：

- **认证错误码写错了**（初版说 401，实际无凭证是 **403**、凭证无效才是 401）；
- **认证先于校验**这一顺序，任何离线阅读都推不出来；
- **错误响应也带 `x-typesafe-request-id`**（初版只说了成功响应）；
- **request id 的格式**是 `req_` + 32 位十六进制（可测的格式）；
- **401 缺 `WWW-Authenticate`**，是一处 RFC MUST 违规。

**方法论修正**：

1. 证据等级表新增 **L 级（线上观测）** 并置于最高优先级，`L > S > A > B > C > D`。
2. 原始材料保留在 `docs/contract/observations-2026-09-22.md`，**保留命令与原始输出**，不只留结论。
3. 新增验收层 **L3b（错误契约）** 进 CI。
4. **L5 差分测试从"可选"提升为"改契约前必须跑"**。理由：L0 只能保证"我们和自己的 OpenAPI 快照一致"，**没有任何离线测试能发现线上服务端偏离它自己的 OpenAPI**。这次发现的 401/403 问题就是手工跑了一次 L5 的直接产物。

### M2（中，未修正）性能测量有顺序效应

Laya 的线程扫描是 `for threads in [1,2,4,8,16,24]` **顺序执行**的，每个配置跑 6 次取中位数。这意味着**热漂移、内存碎片、其他负载的时间趋势与线程数是混淆的**——看起来像"线程数影响"，可能部分是"跑得越晚越慢/越快"。

**未修正的原因**：这台机器当时还在并发跑 kev 的探测，测量环境本身不干净。**这一批数据的定位本就是"可行性证据"而非正式基准**（已在 `benchmarks/README.md` 写明）。正式基准属于 Stage 3，届时必须：

- 随机化配置顺序，并**重复整轮**；
- 报告**分布**而不只是中位数（至少 p50/p95 与样本数）；
- 记录机器是否被其他任务占用。

**我不打算事后粉饰这批数据**——它的价值是"量级正确、结论方向正确"，不足以支撑精细比较（比如"16 线程比 24 线程好 13%"这类结论）。

### M3（中，已修正措辞）kev 的 "83×" 精度过高

bf16 vs fp32 的对比，**每个 dtype 只报了最后一次请求的 `latency_ms`**，本质上是**单次观测**。文档里写成"**83 倍**"给人一种精密测量的错觉。

**事实**：137174 ms vs 1656 ms = 82.8×，但这背后各只有一次测量。正确的表述是"**约 80 倍，单次观测**"。

**修正**：`benchmarks/results/kev-0.8b-cpu-dtype.json` 里已注明"single observation per dtype, recorded verbatim"；README 里保留"83×"作为**该次观测的比值**，但它不是统计量。**这个结论的方向（bf16 在 CPU 上病态地慢）是可靠的，因为它差了两个数量级——量级差异不需要精密的统计就能确认。** 这正是"显著性"与"效应大小"的区别。

### M4（轻，已修正）benchmark JSON 有个误导字段名

Laya 扫描的输出里有个字段叫 `questions_per_second`，但它实际算的是 `1000 / (p50_ms / n_questions)`——**这是"单个串行调用方的速率"，不是吞吐量**。它没有考虑并发，也没有考虑批处理。

**修正**：字段改名为 `single_caller_questions_per_second`，并在 `benchmarks/README.md` 说明它的含义。**一个误导性的字段名比没有字段更糟**，因为它会被下游引用。

### M5（严重，**已测，结论为否定**）最核心的性能断言被测掉了

`design.md §6` 的整个论证是：跨请求批处理把多个请求的问题合并成一个大 batch，从而把每问成本降下来，这是高 QPS 的来源。

**已测**（2026-09-22，`benchmarks/batch_gain.py`，原始 JSON 见下表）：合成跨请求批处理——不同 state、不同问题长度、直接调用引擎、批大小 1/2/4/8/16。

结论是**否定**的，而且比"没测过"更糟：

1. **短序列下批处理确实有收益。** 正对照（所有项完全相同、padding 恒为 1.00）在 115 token 时达到 1.92x。
   这条正对照同时验证了测量本身：它复现了已发布的请求内结论（`laya-multilingual-sweep.json`：1 问 231 ms → 10 问 98.7 ms/问），
   两组数据独立地给出同一个数字。**所以"没有收益"不是因为测不出来。**
2. **收益随序列变长而消失。** 同样的正对照在 393 token 时只剩 1.10x。收益来自摊薄每次调用的固定开销，
   而固定开销相对几百 token 的前向计算很小，摊薄不出东西。
3. **真实流量形状下是负收益。** 长度倾斜（140–821 token）时每个批大小都慢 3–5 倍，因为每行都补齐到批内最长，
   padding 达到 2.47x，浪费的算力直接变成更慢的每项成本。
4. **加进程也不增加吞吐。** 固定总线程预算（24）下，1/2/4 进程的吞吐差 1.18x，即**没有扩展性**：
   单个 Laya 前向已经用满 24 线程，把预算切开只是重新分配同一份算力。

**因此**：`design.md §6` 把跨请求批处理当作主要吞吐手段是**错的**（在这台 CPU 机器上）。
一个按原设计实现的攒批器在异构 state 下会是 3–5 倍的性能回归——比"没实现"差得多。
`stage 3` 的攒批器**不应按原设计实现**；见 `design.md §6` 的修订与 §12 的待决策项。

**残留的未知量**（写清楚，不用它来救结论）：

- **只有 CPU 数据（M6）**。GPU 上批处理通常是提升利用率的标准手段，本结论**不适用于 GPU**。
  没有 GPU 数据之前不得反推 GPU 行为。
- **只测了 Laya**。kev 是 prefill-only 架构，批处理特性可能不同，未测。
- **是上界**：合成批没有队列、没有等待、没有取消、没有部分失败。真实攒批器只会更差。
- **不是并发负载测试**：没有 p99，也没有真实多客户端。

**方法论上的两处硬要求**（都是本设计以前踩过的坑）：

- **交错轮次**：每个轮次按打乱的顺序访问所有批大小，而不是"一个大小测到底再测下一个"。
  升序跑到底正是 M2 记录的序效应，会制造出"batch 4 比 batch 2 更慢"的假结论。
- **正对照**：一个测不出收益的 harness 无法区分"机制没用"和"测量坏了"（D1 的教训）。
  短序列正对照必须显示收益，否则整个文件拒绝生成——`report.py` 会检查这一点。

<!-- BATCHING:START -->
跨请求批处理的实测结果。原始 JSON 在 `benchmarks/results/`，本表由
`python benchmarks/report.py --write` 生成，**不要手改**。

正对照（所有项完全相同、padding 为 1.00）达到 **1.92x**（`shared_short`，batch 8），证明瓶颈确实是每次调用的固定开销，也证明这套测量能测出收益。 真实流量形状（不同 state）最高 1.09x（`uniform`，batch 8），而最差 0.34x（`skewed`，batch 8）。 长度倾斜时 padding 最高 2.47x（`skewed`，batch 16）：每一行都要补齐到批内最长，浪费的算力直接变成更慢的每项成本。

`shared_short` 与 `shared` 是正对照：所有项完全相同（padding 恒为 1.00），即请求内批处理的形状。短序列那组必须显示出收益，否则这套测量根本测不出收益，其余结论无效。`shared_short`（115 token）复现了已发布的请求内结论：本表 217.8 ms/项 → 113.7 ms/项，`laya-multilingual-sweep.json` 里 1 问 231 ms → 10 问 98.7 ms/问。两组数据互相印证。

### 进程数 vs 吞吐

总线程预算是固定的（进程数 × 线程数 = 24），所以这张表比较的是「把机器分给多个进程」与「全部给一个进程」，而不是线程超配。

| 进程数 | 线程数 | 项/秒 | ms/项 | 相对最好 | 来源 |
|---:|---:|---:|---:|---:|---|
| 1 | 24 | 1.23 | 814.1 | 0.93x | `laya-multilingual-batch-gain.json` |
| 2 | 12 | 1.12 | 890.4 | 0.85x | `laya-multilingual-multiprocess-p2.json` |
| 4 | 6 | 1.32 | 759.0 | 1.00x | `laya-multilingual-multiprocess-p4.json` |

最慢与最快只差 1.18x：在这台机器上**加进程不增加吞吐**。单个 Laya 前向已经能用满 24 线程，把预算切开只是重新分配同一份算力。

1 进程那一行取自跨请求批处理文件里的串行配置（同一序列集、同一线程预算），不是另一组实验。

### `laya-multilingual` — 1 进程 × 24 线程

- `benchmarks/results/laya-multilingual-batch-gain.json` · cpu · float32
- 加载 82.0 s · peak RSS 4.83 GB

| 序列集 | 批大小 | n | ms/项 (p50) | 相对串行 | 盈亏平衡等待 ms | padding |
|---|---:|---:|---:|---:|---:|---:|
| shared_short | 1 | 64 | 217.8 | 1.00x | 0.0 | 1.00 |
| shared_short | 2 | 32 | 152.4 | 1.43x | 65.5 | 1.00 |
| shared_short | 4 | 16 | 127.8 | 1.71x | 90.0 | 1.00 |
| shared_short | 8 | 8 | 113.7 | 1.92x | 104.1 | 1.00 |
| shared_short | 16 | 4 | 117.1 | 1.86x | 100.8 | 1.00 |
| shared | 1 | 64 | 464.9 | 1.00x | 0.0 | 1.00 |
| shared | 2 | 32 | 422.7 | 1.10x | 42.2 | 1.00 |
| shared | 4 | 16 | 696.6 | 0.67x | -231.7 | 1.00 |
| shared | 8 | 8 | 500.4 | 0.93x | -35.5 | 1.00 |
| shared | 16 | 4 | 374.0 | 1.24x | 90.9 | 1.00 |
| uniform | 1 | 64 | 814.1 | 1.00x | 0.0 | 1.00 |
| uniform | 2 | 32 | 769.6 | 1.06x | 44.5 | 1.06 |
| uniform | 4 | 16 | 1535.9 | 0.53x | -721.9 | 1.10 |
| uniform | 8 | 8 | 748.8 | 1.09x | 65.2 | 1.10 |
| uniform | 16 | 4 | 885.2 | 0.92x | -71.2 | 1.13 |
| skewed | 1 | 64 | 322.1 | 1.00x | 0.0 | 1.00 |
| skewed | 2 | 32 | 1514.6 | 0.21x | -1192.6 | 1.37 |
| skewed | 4 | 16 | 1176.8 | 0.27x | -854.7 | 2.38 |
| skewed | 8 | 8 | 932.5 | 0.34x | -610.4 | 2.41 |
| skewed | 16 | 4 | 933.6 | 0.34x | -611.5 | 2.47 |

序列 token 数: shared_short 115-115, shared 393-393, uniform 530-674, skewed 140-821

- 未测: queue wait, cancellation and partial-failure behaviour -- they need the batcher
- 未测: cross-state prefix sharing, which a real engine could exploit and this cannot
- 未测: p99 under sustained load

### `laya-multilingual` — 2 进程 × 12 线程

- `benchmarks/results/laya-multilingual-multiprocess-p2.json` · cpu · float32
- 加载 104.1 s · peak RSS 4.82 GB

| 进程数 | 线程数 | 请求数 | 墙钟秒 | ms/项 | 项/秒 |
|---:|---:|---:|---:|---:|---:|
| 2 | 12 | 128 | 113.97 | 890.4 | 1.12 |

- 未测: whether the OS scheduler actually keeps them on separate cores
- 未测: p99 under this load

### `laya-multilingual` — 4 进程 × 6 线程

- `benchmarks/results/laya-multilingual-multiprocess-p4.json` · cpu · float32
- 加载 86.1 s · peak RSS 4.81 GB

| 进程数 | 线程数 | 请求数 | 墙钟秒 | ms/项 | 项/秒 |
|---:|---:|---:|---:|---:|---:|
| 4 | 6 | 256 | 194.29 | 759.0 | 1.32 |

- 未测: whether the OS scheduler actually keeps them on separate cores
- 未测: p99 under this load

**范围**：批是直接调用引擎合成出来的，没有队列、没有等待、没有客户端放弃、没有部分失败。这些数字是真实攒批器的**上界**——上面每一条缺失的机制都只会让它更差。

<!-- BATCHING:END -->

### M6（中，未修正）没有 GPU 数据，却已出现依赖 GPU 的设计决策

所有实测都在无 GPU 的 aarch64 机器上。但文档里已经出现了 GPU 相关的判断（kev 定位为 GPU 引擎、CUDA 镜像体积估算、`flash-linear-attention` 的可用性）。**这些是推断，不是实测。**

**未修正的原因**：没有 GPU 机器。已在 `feasibility.md §5` R1 与 §7 明确标注为"需要 GPU 机器验证"，并且**没有把任何 GPU 性能数字写进 README**。这条纪律必须守住。

---

## 5. 还没修 / 修不了的（诚实清单）

| # | 问题 | 状态 |
|---|---|---|
| 1 | 跨请求批处理的收益 | **未测**，Stage 3 第一件事。可能推翻 §6 的收益预期 |
| 2 | GPU 上的真实延迟、CUDA 镜像能否装成 | **未测**，需 GPU 机器 |
| 3 | 429 / `Retry-After` 的真实行为 | **未观测**（无 API key，无法触发限流） |
| 4 | 真实 200 响应体的取值 | **未观测**（无 key）。形状由 OpenAPI + SDK + kev 三方交叉确认，但取值没有对照样本 |
| 5 | 性能测量的顺序效应与样本量 | **未重做**，属 Stage 3 正式基准 |
| 6 | 多进程 × 线程数的最优点 | **未测** |
| 7 | Qwen3 世代 kev 在 CPU 上是否可用 | **未测**。若可用，可能是 CPU 镜像更好的默认选择 |
| 8 | SBOM / provenance | **计划中**，Stage 4 |
| 9 | `remote` 引擎转发真 jev 的合规性（用户自有 key） | **未评估**，需要时再确认 ToS |

---

## 6. 一句话总结

**初版的方向是对的，但有三个"不会崩、但会让产品失效"的缺陷**：协议让批处理永不触发（D1）、没有鉴权却监听全网卡（D2）、没处理批处理带来的数值不确定性（D3）。三个都已修。

**方法论上最大的改进是承认"读 schema 不等于验证实现"**——补做线上观测后立刻发现了一处真错误和四条只能靠观测得知的契约，并因此把差分测试从可选提升为改契约前的必跑项。

**M5 已经测掉，结论是否定的**：跨请求批处理在这台 CPU 机器上不增加吞吐，长度倾斜时因为 padding 反而慢 3–5 倍；
加进程也不增加吞吐。**因此 `design.md §6` 的吞吐论证需要改写**，而不是继续等一个会兑现它的实现。
"对外材料不许出现 QPS"这条纪律的**理由**消失了（数字已经有了），但**新的理由**接上了：
现在能报的是一条明确的负结论和一个测得的单进程吞吐上界，不是"批处理带来的高 QPS"。

---

## 附：本次审查产生的改动

| 文件 | 改动 |
|---|---|
| `docs/api-compatibility.md` | 新增 L/S 证据等级与 §5 错误契约（重写）、§3.2 空 criteria、§4.3 usage 非空、§4.5 容错边界、§7 confidence 改写、§8 新增 L0 与 L3b |
| `docs/contract/typesafe-openapi-0.2.0.json` | **新增**：官方 OpenAPI 快照（L0 测试基准） |
| `docs/contract/observations-2026-09-22.md` | **新增**：线上观测原始记录（命令 + 原始输出） |
| `docs/design.md` | §3.1 认证与安全（新增）、§5.1 `WorkItem` 协议（重写）、§6.2/6.3 攒批与模式修正、§6.4 并发不变量、§6.5 背压与超时、§6.6 批不变性（新增）、§8.1 Dockerfile 硬化、§9 测试表、§10.1–10.3 健康检查/启动/下线、§11 仓库结构、§12 里程碑、§13 待定问题 |
| `docs/design-review.md` | **新增**：本文 |
| `README.md` / `README.zh-CN.md` | 语言切换；性能节保留实测数字并标注来源 |
| `benchmarks/README.md` | `single_caller_questions_per_second` 命名说明与局限 |
| `benchmarks/probe/` | **新增**：探测脚本 checked in，使数字可复现 |
| `AGENTS.md` | 新增契约不变量（认证顺序、状态码分工、request-id 格式、`Retry-After`、不得按 state 分组） |
