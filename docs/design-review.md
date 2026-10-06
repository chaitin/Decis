# 设计审查：这份设计够科学、够尊重标准吗？

**日期**：2026-09-22
**最后一次逐条复核**：2026-09-30（31 个设计缺陷 + 6 个方法论问题，逐条对着当时的代码与测试核过一遍）
**审查对象**：`docs/` 下的设计文档，以及它们所依据的实测与证据。
**审查方式**：不是"再读一遍确认它说得对"，而是主动去找**它在什么情况下会错**——补做线上观测、用一手标准核对断言、重新推演性能论证的每一步。

> **这份文档怎么读。** 已经修好、并且有测试守卫的问题，在 §2.2 里压缩成一小段（症状 / 怎么修的 /
> 谁在守着它）；**现在仍然成立**的问题放在 §2.1 的最前面；方法论问题在 §4；还没修的清单在 §5。
> 每条都有一个稳定的编号（`D1`…`D36`、`M1`…`M6`），`AGENTS.md`、代码注释与测试都按编号引用它，
> 所以编号不会重排。逐条的事故复盘在 git 历史里（`git log -p docs/design-review.md`），这里不再重述。

---

## 0. 结论

**架构方向是科学的，契约对齐现在是扎实的，但初版有三处真实缺陷和一批方法论上的不严谨。**

分项判定：

| 维度 | 判定 | 说明 |
|---|---|---|
| **核心抽象是否站得住** | ✅ **扎实** | 两个架构完全不同的模型（BERT 系 vs 自回归 LM 系）各自独立收敛到同一中间表示。这不是类比，是两条独立实现路径的实证 |
| **契约对齐** | ✅ **现在扎实，初版不扎实** | 初版只看服务端的 schema 声明；补做线上观测后**发现了一处真错误**（401 与 403 的分工写反了） |
| **性能论证** | ✅ **已测，结论为否定** | 最核心的断言（跨请求批处理带来吞吐）由 M5 实测否定：CPU 上真实流量形状下不提升吞吐。已测的部分方法上有顺序效应（M2） |
| **标准符合性** | ⚠️ **多数尊重，少数有意偏离且已记录** | 见 §3。最关键的一点：**契约兼容与标准符合在一个点上冲突，我们选了兼容，并把它写下来** |
| **安全** | ❌ **初版有硬缺口，已补** | 初版**完全没有鉴权设计**，而镜像监听 `0.0.0.0` |
| **可运维性** | ❌ **初版有硬缺口，已补** | 缺少优雅下线、启动顺序；而冷启动要 75 秒左右 |

下面把"发现的问题"和"已经改了什么"写清楚。**没改的也列出来，不假装都解决了。**

---

## 1. 先说哪些是真的扎实（以及为什么）

审查不是只挑毛病。以下几点经得起推敲，且不是靠"看起来合理"成立的：

1. **抽象被两个独立实现验证。** kev 的 `api.py` 与 Laya 的 `common.py:QTYPES` 是两个团队、两种架构、各自独立写出的同一套三段式（`noul`/`choice`/`score`）到概率的映射。这比"我们设计了一个优雅的接口"强得多——它是**收敛证据**。新增引擎只需一个模块加一行注册，这个判断后来又被两个 Jeff checkpoint 验证了一次（第三种架构，同样没有改契约层）。

2. **契约现在有可 diff、可测的权威来源。** `https://api.typesafe.ai/openapi.json` 是公开的（HTTP 200），OpenAPI 3.1.0，`info.version = 0.2.0`。已收进 `docs/contract/`。这意味着契约不再依赖"某个 SDK 版本的生成物可能滞后"，而是有了一份可以写同源测试（L0）的基准文件。

3. **批处理是复用官方原语，不是 hack。** Laya 的 `collate_items` 实现是 `[it for group in batch for it in group]`——**展平多组问题**是它的原生语义。这说明"跨请求组批"是上游设计者预期内的用法。（收益问题另说，见 M5。）

4. **性能纪律是可执行的，不是口号。** `AGENTS.md §8` 要求任何性能数字必须来自 checked-in 原始 JSON，且报告前断言输入 sha256 一致。这条规则已经**自我执行过一次**：本次调查的原始数据进了 `benchmarks/results/`，文档里的表由它生成。

---

## 2. 发现的设计缺陷

### 2.1 仍然成立的

#### D11（中）kev 挂载"外部基座"无效：基座只能按 repo id 去 Hub 找 — **未修正，已记录**

需求里有一条是"支持挂载外部模型"。适配器这一半成立：`paths.resolve` 找到的本地目录会作为
`Checkpoint(<目录>)` 传进去，`_kev_vendor/checkpoint.py:29` 的 `resolve_run` 见到本地路径就直接用，不发网络请求。

**基座那一半不成立。** 基座是 checkpoint 元数据里的一个**字符串** `meta.base`（形如 `Qwen/Qwen3.5-0.8B-Base`），
vendored 加载器把它原样交给 `load_tokenizer(meta.base, ...)` 与 `DecisionModel(meta.base, ...)`
（`checkpoint.py:127-128`），于是无论适配器从哪来，基座都只会按 repo id 去 Hub 找（缓存命中或联网）。
实测后果：用户把基座单独下到本地目录、用 `--model-path kev-0.8b=<目录>` 指过去，**基座那份配置不会生效**。
离线机器上只要 Hub 缓存里有基座就没问题（`decis download` 正是把它放进缓存的），所以这不是"离线不可用"，
而是"**挂载基座不生效**"。

**为什么现在不修**：修它要动 vendored 的 `checkpoint.py`，而那份文件是逐字节复制、有 sha256 守卫、
刻意不做本地修改的（`VENDOR.md`）。两条可行路径都留到真要支持"挂载自定义基座"时再做：
① 在 `load()` 里把 `meta.base` 映射成本地目录再交给一个不改 vendored 代码的加载路径——但 vendored 的
`DecisionModel.__init__` 只接受 repo id，这条路实际要复制它的加载逻辑，等于在 Decis 里维护第二份；
② 向上游提 PR 让 `meta.base` 支持本地路径，然后整体 re-vendor——**这条更干净**，它把"基座可以是目录"
留在唯一事实来源里。

在此之前，README 与 `AGENTS.md` 不许把"挂载目录"说成对基座也成立——只对适配器成立。
**这条没有任何测试守卫**，只有文档层的约束。

#### D13（中）`noul.criteria` 写错键名会被**静默忽略**，答案照常返回 200 — **待决策**

`noul` 的 `criteria` 只有 `"true"` 和 `"false"` 两个键（`src/decis/schema.py:44-48`），
但写 `noul` 问题时最自然的手感是 `"yes"` / `"no"`。实测：

| 请求里的 criteria 键 | HTTP | `noul` | `input_tokens` |
|---|---|---|---|
| `{"true": …, "false": …}` | 200 | **0.9435** | 65 |
| `{"yes": …, "no": …}` | 200 | **0.3694** | 47 |

两个都是 200。第二行里那段评分标准**根本没被渲染进提示**（token 数从 65 掉到 47），
调用方拿到的却是一个看起来完全正常的概率值。**用户不会知道自己的 rubric 被丢了。**

这违反本项目已经承认的一条原则（§9 禁止"让引擎在超预算时静默截断输入"）。机制不同（未知字段 vs 超预算），
失效模式相同：**调用方以为模型看到了一段它其实没看到的信息，而返回的概率看起来仍然合理。**

**但它不是一个契约违规。** 官方快照 `docs/contract/typesafe-openapi-0.2.0.json` 里 `NoulCriteria`
（以及其余所有对象）**都没有 `additionalProperties: false`**，JSON Schema 中缺失即允许额外属性。
顺带一个更微妙的不一致：官方 SDK 的 `NoulCriteria` 是封闭的（`closed=True`），走 SDK 的用户会拿到
`ValidationError`，走裸 JSON 的用户拿到的是一个悄悄降级的结果——同一个错误，两条路径的表现不一样。

**为什么留给你决策**：没有线上 key，无法观测真 jev 对未知键是忽略还是 422，任何"和 jev 一致"的说法
现在都是猜的。两种合理选择：**保持宽松**（向前兼容最好，代价是上面那个问题）或在 `noul.criteria`
这一层收紧（只对这一处报 422，代价是比契约更严格）。**折中方案**：接受，但在日志里 warning，
并在响应的 `decis` 命名空间回一个 `ignored_fields` 列表——不改变契约行为（仍 200），只把"静默"去掉，
且 `decis.*` 是扩展字段，官方 SDK 会忽略，零兼容风险。

**倾向**：折中方案。**但它需要先确认一件事**：真 jev 是否在 `decis` 之外的路径上也返回额外字段——
如果它严格拒绝，那我们选择宽松就等于让一批在真 jev 上会失败的请求在我们这里"成功"。

**当前状态：未修**，也**没有测试守卫**（没有任何测试固定"宽松行为"或"应该有 warning"）。
交付物 `examples/curl.md` 已用正确的 `"true"`/`"false"`，并在该处标注了这个问题与本节编号。

---

### 2.2 已修正的（按编号，每条：症状 → 怎么修的 → 谁在守着它）

#### D1（严重）`predict` 的签名会让批处理永远不发生 — 已修正

初版签名让整批共享一个 `state_text`，并要求攒批器按 state 分组。真实流量里每个请求的 state 都不同，
按 state 分组等于批大小恒为 1：设计文档花一整节论证的"批处理是高 QPS 的来源"，在真实流量下永远不会触发，
而且**不报错**，只是没有提升。这比会崩的缺陷更难发现。

修法：改成扁平工作项 `WorkItem(request_id, state_text, question, raw_state)`，
`predict(items: list[WorkItem]) -> Prediction`。"能不能跨 state 批"从协议约束降级为实现细节
（kev 的 `prefix` 路径至今没有实现，见 `AGENTS.md` 顶部的未实现清单）。

守卫：`tests/test_laya_inference.py::test_one_predict_call_handles_every_state_in_one_forward_pass`
（weights 套：不同 state 一次前向）、`tests/test_batch_invariance.py::test_batch_composition_does_not_change_the_answer`
（无权重套：混合 state 平铺）。"真的批起来"这一半**只在 weights 套里有守卫**，无权重套只测形状。

#### D2（严重）完全缺少鉴权 — 已补

初版设计了 401/429 的响应形状，却没有任何认证机制，而镜像是
`CMD ["decis", "serve", "--host", "0.0.0.0", "--port", "8000"]`——任何能访问端口的人都能白用你的 GPU。
kev 上游把 `host` 硬编码成 `127.0.0.1`，正说明作者清楚这个接口默认不该暴露；而 Decis 的卖点是"部署出去给别人用"。

修法：`DECIS_API_KEY` / `DECIS_API_KEYS` 用 `hmac.compare_digest` 常数时间比较；**先认证、再校验请求体**
（线上实测真 jev 就是这个顺序：无 key + 非法 body → 403 而不是 422）；**默认安全**——未配 key 且监听
非回环地址时**拒绝启动**，要 `DECIS_ALLOW_NO_AUTH=1` 显式承担风险；补了请求体大小上限
`DECIS_MAX_REQUEST_BYTES`；CORS 默认关闭。**限流没有实现**：`DECIS_RATE_LIMIT_*` 没有任何代码读，
过载时唯一能做的是取锁预算返回 429（D6）。

守卫：`tests/test_contract_errors.py`（403/401 分工、认证先于 body 校验与大小限制）、
`tests/test_config.py`（非回环无 key 拒绝启动、回环允许、显式 opt-in）。

#### D3（中）没处理"批处理会改变数值结果" — 已补

批处理会改变浮点归约顺序与 GEMM tiling，所以同一个 `(state, question)` 在不同批组成下可能得到不同概率，
极端情况下 argmax 会翻转；而官方 SDK 会自动重试 POST，"重发同一请求拿到不同答案"是可观测行为。
初版对此完全沉默。

修法：写明承诺"相同输入 + 相同批组成"下确定，**不承诺跨批组成逐位一致**；加
`tests/test_batch_invariance.py`（`batch=1/8/32` 比较概率向量，最大偏差要小、**argmax 必须不变**）；
替代路径是一次前向只放一个 item（`predict([item])` 永远合法，不需要新旋钮）。实测比原判断乐观：
padding **不会**污染结果（最大绝对偏差 8.345e-07、argmax 翻转 0 次），Laya 的 option 预算也是逐项算的、
不按批内最长项截断。**覆盖面**：只测了同一进程内、同一引擎实例；多进程之间的一致性没有覆盖。

`docs/design.md §6.6` 里写的阈值"初值 0.02"与代码不符——`tests/test_batch_invariance.py:55` 的
`TOLERANCE` 是 `1e-3`，以代码为准。

#### D4（中）`confidence` 的决策被讲成了没有代价的改进 — 已改写

初版把"Decis 统一定义 confidence"当成纯粹的正确选择。**这不诚实**：kev 的公式是 `p_max` 的单调重标定，
**没做过标定**；Laya 的公式背后**有温度标定**（出厂 ECE 0.466，包内按 question type / option count 重新拟合）。
统一到 Decis 公式，等于用可比性换掉了 Laya 已经做过的标定，这是取舍，不是免费的改进。

修法：保留统一口径作为线格式，但**引擎提供原生置信度时就放进响应的 `decis.native_confidence`**
（对 `noul` 尤其重要：契约规定 `noul` answer 没有 confidence 字段，扩展字段是用户唯一能拿到不确定性的地方）。
目前**只有 Laya 提供**；kev 与两个 Jeff 的 `native_confidences` 是 `None`，响应里该字段为 `null`。
文档同时写明：用于风险决策时请在自己的标注集上重新标定，不要跨引擎复用阈值。
实现时还统一了两个原语的公式口径（0 = 无倾向，1 = 确定）：kev 的 score 公式在均匀分布上只能到 0.5，
于是 `score` 改用归一化熵 `1 − H(p)/ln(L)`，与 `choice` 一致——这是对 kev 的**有意偏离**，
记录在 `api-compatibility.md §7`。原本打算在 `/v1/models` 里声明 `confidence_formula` 一条已取消。

#### D5（中）缺少启动顺序与优雅下线的设计 — 已补

实测冷启动 73–76 秒，而初版只写了"`/healthz` 与 `/readyz` 分开"一句话。缺的是监听与加载的顺序
（初版要求"先加载、后监听"，代价见 D7）以及**优雅下线**。

现行契约：端口先开、引擎在后台加载（D7），关闭时按 `DECIS_SHUTDOWN_GRACE_MS`（默认 20000）等在途加载。
守卫：`tests/test_readiness.py` 的三条关闭行为测试、`tests/test_cli_serve.py` 的 banner 与 `--preload` 测试。

#### D6（轻）单请求超时预算与官方 SDK 不匹配 — 已修正

初版写"超时返回 529"。官方 SDK 的单次 HTTP 超时是 **10.0 s**，且超时与连接错误都在默认重试集合里，
529 也不在 SDK 的已知状态码表内。于是服务端算 30 s、客户端 10 s 就放弃并重发，负载被放大 2–3 倍。

修法：`DECIS_REQUEST_TIMEOUT_MS` 默认 **8000**（给网络留余量）给**取锁**设上限，取不到返回
**429 + `retry-after-ms`**——不是 504，因为 504 不带退避指令，会退化成指数退避，把已经饱和的服务打得更狠。
**队列等待计入这个预算；已经开始的前向无法中断**，这一点在 `AGENTS.md §3-17` 明确写出。
守卫：`tests/test_request_budget.py`（含"默认值必须小于 SDK 的 10 s"一条）。

#### D7（中）冷启动期间服务完全不响应，且文档说的是反的 — 已修正（采用方案 B）

第一次用真实权重起服务时实测到：**79.7 秒内不返回任何 HTTP 响应**。原因是结构性的——lifespan 里
`service.load()` 同步阻塞，而 uvicorn 要等 lifespan 跑完才进入协议循环；socket 已经 bind，
所以探针是**挂起**而不是拿到连接错误。两个后果：文档与实现相反；`/readyz` 的 503 在生产上到不了。

两条路都要付代价：**方案 A**（维持同步加载）的好处是加载失败立刻让进程退出，代价是冷启动窗口内无响应、
探针必须调好、未就绪路径是死代码；**方案 B**（后台线程加载，失败时 `/readyz` 报 `failed`）的好处是
冷启动期间 `/healthz` 200、`/readyz` 503 且能说明原因，代价是加载失败不再让进程退出、需要监控 `/readyz`。

**决定：方案 B。** 理由是 A 的"快速失败"并不更快——容器照样要退出、照样要重启，而 503 至少让已经在轮询的
探针能说出原因。B 保留了 A 真正想要的东西：`/v1/systemone` 在 loading 与 failed 两种状态下都是 503。

实现：`LoadPhase` 四态（`idle/loading/ready/failed`），`failed` 是终态且**不带 `retry-after`**
（官方 SDK 会重试 5xx，告诉它退避等于让它一直打一个不会恢复的服务）；加载失败记录错误文本并写完整
traceback 到日志。实测：`/healthz` 在冷启动开始后 0.50 s 内应答，`/readyz` 明确报 `loading`。
顺带修掉两处：`service.answer` 现在**先检查就绪再 `measure()`**（否则会从测量路径里抛 500 而不是干净的 503）；
`load_status` 与引擎自己的 `loaded` 归一。守卫：`tests/test_readiness.py` 四条。

#### D8（严重）`/v1/systemone` 是 `async def` 却调用阻塞函数，一次推理会堵死整个事件循环 — 已修正

写请求预算的测试时发现的：FastAPI 会在**事件循环线程**上执行那个同步阻塞函数，后果是一次推理期间
服务器不响应任何其他请求，`/healthz` 与 `/readyz` 也在其中，并发度实际为 0——序列化发生在事件循环上，
调度器的锁根本没机会起作用。这个 bug 之前测不出来，因为无权重测试替身的推理是微秒级。

修法：路由从 `async def` 改成 `def`，Starlette 会在工作线程池里执行它，序列化交回调度器的锁；
`/v1/models` 同样改了。`/healthz`、`/readyz` 保持 `async def`（只读内存状态，不阻塞）。
守卫：`tests/test_request_budget.py::test_healthz_and_readyz_answer_while_inference_is_running`
（把一个测试替身卡在 `predict` 里，期间探针必须 200）。

#### D9（严重，会无提示地降低答案质量）引擎的"选项文本"不能由 Decis 统一决定 — 已修正

`render.py` 是"任意 JSON → 可读文本"的唯一所在，但**选项在序列里长什么样是引擎自己的格式**。
Laya 恰好也用 Decis 的写法，kev 不是：choice 一致，但 noul 是 `no`/`yes`（不是 `false`/`true`），
score 只发裸层级文本（不带 `"0: "` 前缀）。若 kev 引擎照搬 `Option.name`，模型会收到训练时从未见过的文本，
**准确率下降而没有任何报错**。

证据（不是推断）：kev 的 `data.py:395 materialize()` 写明训练数据与线上服务走同一个函数
`api.to_record`，所以 `api.py` 的渲染约定**就是**模型学到的约定。**结论：抽象是对的，不需要改
`render.py` 或 `answers.py`**，需要的是承认"选项文本"属于引擎的序列格式，由 kev 引擎自己映射。

守卫：无权重套 `tests/test_engines_kev.py`（选项文本、noul 的 `no`/`yes`）；weights 套
`tests/test_kev_inference.py::test_the_record_matches_upstreams_serving_path` 与
`test_probabilities_match_upstreams_own_path`（用 Decis 的 `PreparedRequest` 构造出的 record 与
kev 自己的 `api.to_record` 逐字段相等，概率差 0.00e+00）。**已知偏差、未实测影响**：`render_value`
对 dict 按键排序，而 kev 保留插入顺序，所以 state 文本的字段顺序可能与训练时不同。

#### D10（中）容量接口没有"state 单独上限"的位置，而 kev 有一个 — 已修正

kev 的 `encode(strict=True)` 同时施加两条不同的限制：`len(state) + 1 <= 384` 与
每个问题 `len(state) + len(branch) <= 1024`。而 `validate_capacity` 只有两个比较位（序列、问题），
`MeasuredTokens.state_tokens` **只被报告、从不校验**。于是 state 500 + 问题 10 的请求会通过校验，
然后被 `encode` 静默截断。这属于接口缺口，不是 kev 的怪癖。

修法：`EngineInfo` 增加 `max_state_tokens`（默认 `0` = 没有独立上限，Laya 行为不变），
`validate_capacity` 在序列比较之前先查 state，错误定位到 `body.state`；kev 声明 384。
实现时又遇到一个自己造成的问题：初版 `measure()` 从 `encode` 的输出反推 state 长度，而 `encode` 会把它
截断到 384，于是量出来的 state 永远等于上限，新加的检查**永远不会触发**——现在直接量原始 token 数
（`self._tok(..., add_special_tokens=False)`），branch 用一个空 state 的 record 单独量。
同时修掉一处会误拒的设计：`max_question_tokens` 原写成 `MAX_BRANCH - MAX_STATE`（640），
会把"100 token state + 700 token 问题"拒掉，而 kev 实际处理得了；现在报最宽松的**可靠**上界 `MAX_BRANCH`。

守卫：`tests/test_engines_kev.py` 五条（两条窗口、没有 state 上限的引擎行为不变、超限被拒而不是被截断）
与 `tests/test_kev_inference.py` 三条（weights）。

#### D12（低，但正是 §8 要防的）README 性能表把两个线程数混进了同一行 — 已修正，且改成了自动生成

原表四格里三格取自不同配置：`201 ms` 是线程数 16 的 1 问，同一行的 `987 ms` 是线程数 24 的 10 问。
每个数字单独看都对，整行没有意义——而表格里根本没写线程数（同时违反 §8 里"必须给出线程数"那一句）。

修法不是把数字改对，而是让这类错误不可能再被写出来：`benchmarks/report.py` 生成表格时，
一行的所有列从**同一个配置**取，并把线程数写进表格本身；`--check` 在 CI 里跑，手改一个数字就失败。
同源的一个问题：`kev-0.8b-cpu-dtype.json` 的对比轴是 **dtype** 不是线程数，生成器第一版把它当线程扫描渲染，
于是两行因为 `(threads, questions)` 相同被静默合并——改成先探测变化轴再选表格形状。

守卫：`tests/test_benchmark_report.py`（一行一个线程数、手改数字必须被发现、checked-in 文档与原始 JSON 同步、
README 不许出现性能数字）。注意 dtype 那条轴没有直接的单测，钉住它的是 checked-in 生成块加同步检查。

#### D14（中）`mock` 镜像装了 Laya 和 torch：3.2 GB 里 3.1 GB 是它不需要的 — 已修正

> **历史说明（D14–D16，2026-09-23）**：这三条记的是当时那个**无权重的测试替身镜像**（tag 就叫 `mock`），
> 它发布在本仓库迁到 `chaitin` 命名空间之前。测试替身与那个 tag 已从代码、工作流和文档中移除，
> 所以下面 `docker pull <namespace>/decis:mock` 这类命令今天不再可用；**缺陷的形状与教训与具体引擎无关**。
> 今天发布的镜像见 `docs/deployment.md`。

发现方式：镜像推到 Docker Hub 之后用 registry API 逐层列体积，`mock-latest` 是 **3203 MB**，
和 `laya-multilingual-latest` 一模一样——`DECIS_EXTRAS=laya` 而 `DECIS_ENGINE=mock`。
根因是一行 YAML 里的 `A && B || C` 惯用法：GitHub 表达式里没有三元运算符，而这个惯用法**在 `B` 为假值时是错的**
（空字符串是假值，于是 `'' || …` 继续求值右边），"没有 extra"这个分支永远选不中。
测试没抓到是因为它拿一份**手抄的**常量与 registry 比——第一处实现（表达式）错了，第二处（抄本）是对的，于是恒绿。

修法：把"引擎 → pip extra"的映射从 YAML 表达式搬进 `plan` 步骤（`extra_for()`），矩阵里直接带 `extra` 字段，
构建步骤只做 `DECIS_EXTRAS=${{ matrix.extra }}`；出现没有映射的引擎时 `plan` **直接失败**，而不是静默落进默认值。
守卫：测试从 YAML 里**抽出并执行** plan 脚本，再与 `registry.SPECS[...].extra` 比对。

#### D15（低）README 写的 tag 根本不存在 — 已修正

工作流给 master 推送打的 tag 是 `${engine}-latest`（例如 `laya-multilingual-latest`），而 README 与
`AGENTS.md` 写的是 `decis:laya-multilingual`：照着文档拉，第一步就失败。它能溜过去是因为
**没有任何测试拿 tag 和文档对照过**（和 D12 同一个形状：同一个事实存在两份，只有一份被执行）。

修法：tag 方案整个搬进 `plan` 步骤，矩阵里带 `image_tag`，构建与合并步骤只做插值，测试从**执行 plan 后
产出的矩阵**里读 tag；文档里出现的 tag 由 `test_the_documented_image_tags_are_tags_the_workflow_creates`
从同一份产出里比对。**范围说明**：这次集中化覆盖**发布 tag**；per-arch 的 `-sha-<short>-<arch>` 后缀
仍在构建步骤与合并步骤各拼一遍，两处被测试钉在同一个字面量上，不算完全单一来源。

#### D16（严重）三个引擎把 per-arch 中间 tag 互相覆盖，于是三个 tag 发出的是同一个 manifest — 已修正

修完 D15 后用 registry API 核对，发现三个 tag 指向同一个 amd64 manifest。根因：per-arch 中间 tag 当时叫
`sha-<commit>-<arch>`，而**所有引擎共用同一个仓库**，六个构建任务同时往同一个名字上推，后推的覆盖先推的；
merge 必然在所有 build 之后，于是读到的是最后完成的那个引擎。

修法：per-arch tag 里加上 artifact 名——`<image_tag>-sha-<short>-<arch>`。**并加了一条"不许撞车"的测试**：
把所有 (引擎, variant, arch) 组合的构建任务都跑一遍，断言任意两个写出的 tag 集合两两不相交
（旧方案下会失败，列出的正是那 20 组冲突）。教训：共享仓库里由多个任务写同一个 tag，必须把任务身份放进名字；
只检查"我这个 tag 对不对"是不够的，要检查"两个 tag 会不会相同"。

#### D17（高）`decis download` 把权重写到 `resolve` 永远不看的目录，于是每次都失败 — 已修正

发现方式：用一个会写出**真实布局**的 `snapshot_download` stub 跑 `decis download`，三种 `--dest` 全部退出码 1。
根因：`snapshot_download(local_dir=X)` 复制的是**仓库自己的布局**（`laya-multilingual` 落在 `X/multilingual/`），
而 `cli._download` 把 `X` 当作 `DECIS_MODEL_DIR` 交给 `paths.resolve`，后者只找 `X/<engine id>/`。
影响面远不止一条命令：README 与 `AGENTS.md §7` 都把它写成用户的第一步；`DECIS_PREDOWNLOAD` 在
`RUN decis download` 处**直接构建失败**，所以"带权重的离线镜像"不是"未验证"，而是从来没构建成功过。

修法：有模型目录时落盘到 `<dir>/<engine id>/`；没有时写 **Hub 缓存**（那才是零配置时 `serve` 会读的地方）；
两条路都在下载之后**用 `paths.resolve` / `checkpoint_root` 自己验证**才报成功。
守卫：`tests/test_status.py::test_the_downloader_writes_where_the_loader_looks`（stub 写出真实下载布局，
再断言 `resolve` 找得到）与 `test_downloading_with_no_model_directory_warms_the_hub_cache`。
教训：凡是"写到某处、之后从某处读"的两个模块，必须有一条测试把**真实的写**接上**真实的读**。

#### D18（中）compose 的 `command:` 覆盖了镜像的 `CMD`，容器去找一个叫 `download` 的程序 — 已修正

`docker/Dockerfile` 只有 `CMD ["decis", "serve"]`，没有 `ENTRYPOINT`；compose 的 `command:` 是**替换** CMD，
所以 `["download", ...]` 让内核去 exec 一个叫 `download` 的程序。当时的 `tests/test_compose.py`
断言的正是这个错误写法——**它把错误的期望抄了一遍**。

修法：所有 `command` 都以 `decis` 开头；测试改成**从 Dockerfile 读出有没有 `ENTRYPOINT`** 再决定该断言什么，
而不是把当前写法当常量。守卫：`tests/test_compose.py::test_every_command_names_the_console_script`。

#### D19（中）为权重下载配的代理把镜像自己的 liveness 探针打成了 502 — 已修正

引擎**已经加载成功**（日志里 `ready after 86.1s`），但容器一直 `unhealthy`：`urllib.error.HTTPError: 502`。
根因：`urllib` 会读 `HTTP_PROXY`，而 `.env` 里的代理经 `env_file` 进了容器，于是探针去问代理要
`http://127.0.0.1:8000/healthz`，代理当然给不出。同一组合在 Kubernetes 里会让一个完全健康的 Pod 反复重启，
每轮都要重付 86 秒冷启动。修法：`HEALTHCHECK` 命令前 `env -u` 掉六个代理变量（**只作用于探针**，
引擎自己的下载照旧走代理），并在 `.env.example` 的代理段落给出 `NO_PROXY` 的出路。
守卫：`tests/test_compose.py::test_the_probe_cannot_be_hijacked_by_a_proxy_in_the_environment`（逐个变量、逐个探针）。

#### D20（高）默认发布的镜像不带权重，于是"拿到就能用"是句空话 — 已修正

默认 tag 只装依赖，权重打在另一个变体里，而那个变体（当时叫 `-offline`）只在 release 时构建且从未成功过（D17）。
文档甚至同时说了两件矛盾的事：README 有一句 `Images ship with the model`，而镜像表格下面写着默认镜像不含权重、
容器首次启动时下载。**唯一发现它的方式是把镜像拉下来跑一遍**：六条构建任务全绿、SBOM 和 provenance 都在，
没有一条测试说过"这个 tag 里有权重"。

修法：翻转变体——内置权重成为**默认**，引擎名那个 tag 就是它；它只在 release tag 或手动 dispatch
要求时构建，分支推送只发布不带权重的 `-runtime` 变体（引擎名那个 tag 没有权重就不能被移动）；
`-offline` 这个名字删掉（默认已经内置权重时它就是同一个东西的第二个名字）。
守卫：`tests/test_docker_workflow.py`（release 的 baked tag `bake` 等于引擎名、分支推送只发 `-runtime`、
不存在"要内置权重却没装对应 extra"的构建任务）、
`tests/test_compose.py`（compose 用的 image tag 正是那个内置权重的变体）。
**代价（写下来）**：权重的 `RUN` 层在 `COPY src` 之后，所以每次源码改动这条构建线都要重新下载——laya 是 647 MiB，
kev 是约 43 MiB 的 adapter 加 1.65 GiB 的基座，乘两个架构。想省这笔钱就得把权重挪到单独发布的"模型层"镜像里
让 `COPY --from` 命中缓存（`docs/design.md §12.2` 记了这条路，没有实现）。

#### D21（高）挂在 `DECIS_MODEL_DIR` 上的卷会把镜像里内置的权重盖掉 — 已修正

Docker 对命名卷的处理是"首次创建时用镜像里同路径的内容初始化"，之后卷就是权威；bind mount 更直接——
整个目录被替换。两种情况下容器里的 `/models` 都不再是镜像里那份，而 `paths.resolve` 找不到权重时不会报错，
它会**去联网下载**。于是一个"离线镜像"在有网时工作得完全正常，在断网时失败——典型的"部署到客户现场才发现"。

修法：`docker-compose.yml` 不挂任何卷，并把 `DECIS_MODEL_DIR` 钉在 `/models`；守卫**从 Dockerfile 的 `ENV`
读出那个路径**再断言没有服务挂它、也没有服务把它指到别处。需要"权重放卷"的部署改用 `-runtime` 镜像，
先 `decis download` 填一次卷——例子在 `docs/deployment.md` 与 `docker-compose.yml` 的注释里，不在 README。
守卫：`tests/test_compose.py::test_nothing_is_mounted_over_the_weights_baked_into_the_image`。

#### D22（中）compose 的 `--wait` 等的是 liveness，于是"ready"来得太早 — 已修正

第一次真实 `docker compose up -d --wait` 在 **74 秒**就返回了，而引擎还要再等 27 秒才 `ready`。
根因：`--wait` 等的是健康检查，而镜像的 `HEALTHCHECK` 按 D7 刻意打 `/healthz`——那是给编排器用的 liveness 探针。
修法：compose 层覆盖探针，指向 `/readyz`（`start_period: 300s`），并保留 D19 要求的代理变量清理。
这个覆盖是**安全的**，因为 Docker Compose 不是编排器：探针失败不会重启容器，只影响 `--wait` 与 `docker ps` 的状态。
守卫：`tests/test_compose.py::test_compose_probes_readiness_while_the_image_probes_liveness`
（同时断言镜像探针打 `/healthz` 且不打 `/readyz`、每个引擎的 compose 探针相反）。

#### D23（中）`.env` 里的代理只到达容器，到达不了构建步骤 — 已修正（文档修复）

`.env` 是通过 `env_file:` 交给**容器**的，而构建步骤不属于任何一个容器；Docker 也不会把 shell 里的
`HTTP_PROXY` 带进 `RUN`。于是"依赖安装成功、权重下载失败"这个看起来自相矛盾的现象同时成立——
失败的那一步恰好是唯一真的需要出口的那一步，而 `.env.example` 当时写着"跑构建时也设这些"。
修法：`.env.example` 与 `docs/deployment.md` 写明构建要用 `--build-arg` 显式传代理与 `NO_PROXY`
（两个 README 没有这条命令，只有指向部署文档的链接）。
**这条没有任何测试守卫**，是纯文档修复。

#### D24（高）kev 在 Apple 芯片上默认跑 CPU：设备回退写在了引擎里 — 已修正

`kev.py` 的 `load()` 里写着 `device = settings.device or "cpu"`——**它从来不问机器有什么**。
同一台机器上 `mps` 可用，`DTYPE_DEFAULTS` 甚至已经有 `("kev-0.8b", "mps"): "fp32"` 这一行，只是永远走不到。
代价不是"慢一点"而是数量级：一次性本地探针（`kev-0.8b`、fp32、3 个问题）显示 `cpu` p50 3299 ms
对 `mps` p50 215 ms（这两个数字来自一次性探针，没有进 `benchmarks/results/`，所以不进用户文档）。

修法：设备策略集中到 `src/decis/engines/devices.py`（`DEVICES`、`ACCELERATOR_ORDER`、
`available_devices`、`best_device`、`requested_device`）；kev 与两个 Jeff 只做"pin 优先，否则问机器"，
Laya 继续让上游决定；预加载时 `device` 报 `unloaded` 而不是猜一个 `cpu`。
守卫：`tests/test_devices.py`、`tests/test_engines_kev.py`（四条）、
`tests/test_conventions.py::test_device_choice_has_one_home`（helper 只有一处、引擎里不许再有 `or "cpu"`）。
**仍未验证**：`xpu` 与 `npu` 只实现了探测（本机没有这两种硬件）。

#### D25（高）`NO_PROXY` 里的 `[::1]` 让每一次权重下载都失败，而报错看不出跟代理有关 — 已修正

在公司的 ARM 机器上按文档走第一步，模型起不来，`/readyz` 报 `InvalidURL: Invalid port: ':1]'`，
而且是在**发出任何请求之前**。根因：httpx 的 `get_environment_proxies()` 对每一项调
`ipaddress.IPv6Address(entry)`，带方括号的形式会抛 `ValueError`，于是它退到"这是个域名"的分支，
拼出 `all://*[::1]`，`Client()` 构造时 `urlparse` 把 `:1]` 当端口。`huggingface_hub` 就是 httpx 客户端，
所以**下载器在联网之前就死了**；报错里没有代理、没有 `NO_PROXY`，看起来像权重或磁盘的问题。
这条解析规则在仓库里有 **5 份手抄**，而用户真正会走的那条路径一份都没有。

修法：规则收进 `src/decis/config.py`（`normalize_proxy_environment` 把 `[::1]` 改写成 `::1`、
丢弃 httpx 无法表达的 `[::1/64]` 并记录；`ensure_loopback_bypass` 保证回环在名单里），
4 处 Python 手抄（`tests/conftest.py`、`examples/python_sdk.py`、`benchmarks/run.py`、
`benchmarks/batch_gain.py`）改为调用它们；shell 探针那一处没法调用 Python helper，
留一行常量，由 `test_the_shell_probe_exports_the_canonical_loopback_list` 从 `config.LOOPBACK_BYPASS`
读回来比对。改动记进 `Settings.proxy_rewrites`，由 `decis doctor` 逐条报出。
守卫：`tests/test_config.py`（改写 / 丢弃 / 不改动 / 幂等 / **用 httpx 自己当 oracle**）、
`tests/test_conventions.py::test_the_proxy_bypass_rule_has_one_home` 与
`test_the_shell_probe_exports_the_canonical_loopback_list`。

#### D26（中）端口开了不等于模型能答；顺手牵出"日志说 pin 了 commit，实际读的是 main" — 已修正

用户看到 `Uvicorn running on http://127.0.0.1:8000` 以为可以用了，实际上还在下载模型。两条根因：
① 后台加载是**有意**的设计（D7），文档也写了，但**控制台没说**——绑定那一刻操作者唯一看到的状态来自 uvicorn，
它描述的只是 socket；② `laya.Agent` 的签名里**没有** `revision`，所以 hub 分支把 `repo_id` 交给它时，
日志里承诺的是 `paths` 里 pin 的 commit，实际下载和加载的是 `main`——**同一个模型 id 在不同时间会给出不同答案**。

修法：`cli.startup_banner()` 在绑定之前打印引擎、权重来源与体积、绑定地址，并说明 `/readyz` 返回 503、
要等日志里出现 `engine <id> ready`；新增 `decis serve --preload`（先加载再开端口，代价是加载期间连
`/healthz` 都不应答，容器与编排器下不要用）；hub 分支改走 `paths.fetch_checkpoint`——用
`download_arguments` 带 pin 取权重、用 `checkpoint_root` 验证目录，再把**目录**交给上游。
守卫：`tests/test_cli_serve.py`（banner、`--preload`、加载失败不绑定端口）、
`tests/test_paths.py::test_fetching_a_checkpoint_returns_a_directory_the_resolver_accepts`、
`tests/test_engines_laya.py::test_the_hub_branch_fetches_through_paths_and_hands_over_a_directory`。

#### D27（中）单引擎权重覆盖写成环境变量，对任何带点号的引擎 id 都静默失效 — 已修正

`DECIS_MODEL_PATH_<ENGINE_ID>` 要求把引擎 id 规范化成环境变量名，而 `kev-0.8b` 规范化后是 `KEV_0_8B`——
**一个从未被读回的键**：`config._model_paths()` 把后缀小写化后拿去和 `SPECS` 比对，找不到就**丢弃**，
没有警告、没有日志。用户看到的是"我明明指了目录还是去联网下载"。

修法：**删除这套变量，改成命令行参数 `--model-path ENGINE=PATH`**（可重复，`serve`/`models`/`doctor` 都有）。
id 放进**值**里，点号只是 id 的一部分；别名在 `cli._parse_model_paths` 里规范化，未知 id 直接是 `ConfigError`
而不是空操作。代价与边界：命令行参数进不了 `.env`/compose 的 `environment:`，这正是要点——那套变量形式
"看起来能配"实际不能；`--model-path` 也**不**作用于基座（见 D11）。
守卫：`tests/test_paths.py::test_the_environment_cannot_carry_a_per_engine_override`（真的把三个变量塞进环境
再断言它们不被采纳）、`tests/test_docs.py`（任何面向读者的页面都不许再出现 `DECIS_MODEL_PATH_*`）。

#### D28（中）"纯文本所以不需要 torchvision"：一个从未被加载过的 extra 写下的断言 — 已修正

Jeff 的权重套第一次真的加载 Qwen checkpoint 时，`AutoProcessor.from_pretrained` 在读到任何张量之前抛
`ImportError: Qwen3VLVideoProcessor requires the Torchvision library`。根因：那个 checkpoint 的 processor
配置里列着 `video_processor_type`，而 `AutoProcessor.from_pretrained` 会**急切地构造配置里列出的每一个子
processor**，那个类**被 import 时**就要求 torchvision。于是"这是一条纯文本路径"在模块导入层面就不成立。

修法：`jeff` extra 加 `torchvision>=0.20`，`_REQUIRES` 跟上，文档里"torchvision 故意不装"的说法改成实测结论。
守卫：**无权重**的 `test_the_extra_declares_every_required_module`（从 `pyproject.toml` 读出 extra 的依赖名，
逐条对照 `WeightSpec.requires`）+ `tests/test_jeff_inference.py` 真的加载一次。
教训：这个错误在 CI 上表现为全绿，在用户那里表现为"按文档装好 extra，模型加载不起来"。

#### D29（中）分片权重"下到一半"被判成 ready：marker 在，分片不在 — 已修正

下载 8.65 GiB 的 `jeff-gemma4-e2b` 期间，用刚下到的那份目录做加载探针，得到
`FileNotFoundError: .../model-00001-of-00002.safetensors`；此时 `decision_config.json`、
`readout.safetensors`、`model.safetensors.index.json` 都在，**只有分片在陆续到达**。
根因：`paths.checkpoint_root` 只问 `WeightSpec.marker` 在不在，而分片 checkpoint 是**先到清单、后到分片**——
`*.safetensors.index.json` 里 `weight_map` 已写着两个分片，第二个还不存在。于是 `resolve` 报 `local`、
`decis models` 报 **ready**，真加载时异常从 `transformers` 深处抛出来。更糟的是它会**盖住网络那条路**：
"本地优先"是离线镜像的安全性质，而一份半拷贝的目录让这个性质变成"一份坏的本地副本赢过一份好的远端副本"。

修法：`paths.missing_shards(root)` 读 checkpoint 自己的 `*.safetensors.index.json`，`weight_map` 点到的文件
挨个查在不在（清单本身读不出来也算不合格）；`checkpoint_root` 现在要**两条**都过。不检查 `expected_bytes`
是因为那是手写的数字、会漂移，而 index 是**加载器真正会去打开的那份清单**。下载失败的文案分成两种
（`cli._why_unusable`）：缺 marker 说"期待找到 X"，缺分片说"清单里有 N 个文件不在，重新下载"。
守卫：`tests/test_paths.py` 五条（缺分片不算、分片齐全算、清单读不出来不算、单文件 checkpoint 不受影响、
半拷贝的挂载目录必须回退到 `hub`）。

#### D30（中）测试自己重建提示词，于是第二个 checkpoint 上守卫的是**另一条**渲染路径 — 已修正

给 Gemma 那个 checkpoint 跑 weights 套时 14 项通过、4 项失败，全部是
`AttributeError: 'GenericDecoderDecisionModel' object has no attribute 'processor'`。
根因：两个上游 loader 渲染提示词的方式不一样——Qwen 走 `self.processor.apply_chat_template(...)`，
Gemma 走 `self.chat_text()` → `self.tokenizer.apply_chat_template(...)`，user 轮被改写成只剩最后一个
text part，分词还带 `add_special_tokens=False`，而且**根本没有 `.processor`**。测试里的 `prompt_of()`
只写了 Qwen 那一种。更值得记的是：为它配的守卫用的也是同一份镜像，所以在 Qwen 上永远绿、在 Gemma 上直接崩——
**守卫与被守卫的东西共用同一个错误假设时，它守不住任何东西**。

修法：不再在测试里**重建**提示词，而是从引擎**自己 token 化出来的 `input_ids`** 读回来
（`tokenizer.decode(prepare([row]).inputs["input_ids"][0])`）。镜像被删掉，两个 loader 的模板差异就再也
无法让测试断言的文本与实际发送的文本分叉。守卫 `test_the_engine_tokenises_the_prompt_this_suite_asserts_on`
保留，但断言改成对两个引擎都成立的那件事。

#### D31（高）在项目自己声明的 Python 3.11 上，两个 Jeff 引擎连 import 都做不到 — 已修正

推送后 CI 报错，而且**只有 3.11 那个测试任务**失败：`_jeff_vendor/types.py` 第 8 行的
`type JSONValue = ...` 是 PEP 695 语法，3.12 才有。`pyproject.toml` 声明 `requires-python = ">=3.11"`，
于是 3.11 上这个文件**编译不过**，整个 vendored 包 import 不了；`decis models` 当时报的是
`deps missing   uv sync --extra jeff`——原因错，补救命令在 3.11 上还装不出任何东西。

**为什么不能像 import 改写那样机械修掉**：那些别名是**递归**的（`JSONValue` 的定义里含 `list[JSONValue]`），
换成 `JSONValue: TypeAlias = ...` 会在模块执行时 `NameError`；PEP 695 的惰性求值才能自引用。
**floor 是上游代码的事实，只能声明，不能靠改写消掉。**

修法：同一个数字放在三处——`registry.EngineSpec.python_min = (3, 12)`（`status()` 在依赖检查之前报
`needs Python 3.12+`）、`jeff.MIN_PYTHON`（`load()` 在 import vendored 之前抛 `EngineUnavailableError`，
而不是三帧之下冒出 `invalid syntax (types.py, line 8)`）、`pyproject.toml` 的 `python_version >= '3.12'`
marker（3.11 上根本不装那几个 GB 的依赖）。
守卫：`tests/test_engines_jeff.py` 把三处钉在一起，并用 `ast.parse(..., feature_version=(3, 11))`
**从 vendored 源码本身**把 floor 推出来——低一个小版本的语法必须解析失败、floor 的语法必须成功，
所以它在 3.14 上也能红，而不是等某个老解释器自己撞上。
**教训**："两个环境都跑过"必须带上**解释器版本**这一维：本地两个环境都是 3.14，CI 有 3.11/3.12/3.13，
于是这个缺陷是推送后才发现。如果 CI 只跑最新解释器，它会直接进 release。

#### D32（高）把 pin 住的 commit 交给 ModelScope，不报错，而是“成功”地什么都不下 — 已修正

ModelScope 的 `snapshot_download` 有 `revision` 参数，但它的 revision 是**分支名 / tag 名**。
用 `modelscope 1.40.1` 对 `convaiinnovations/laya` 传 Hugging Face 的 commit sha 实测：不抛异常，
日志打一行 `No files to download for convaiinnovations/laya@<sha>`，**返回成功**，目录是空的。
这三件事正好凑成这个仓库最忌讳的组合——在错误的分支上“成功”、错误在很远的下游才浮现、
而用户以为自己拿到了 pin 住的那份权重。加回退路径时如果只是“把同样的参数转发给另一个客户端”，
这就是必然结果。

同一轮实测的另外两条事实，决定了方案的边界：

- 这些仓库在 ModelScope 上**只有 `master`**（revisions 接口只返回 Branches，没有 Tags）；
- `mstrasser/Jeff-Qwen3.5-0.8B` 与 `Jeff-Gemma4-E2B` 在 ModelScope 上**不存在**
  （404 `{"Code":10010205001,...,"record not found"}`）。所以“回退”只能救有镜像的仓库，
  没有镜像时必须让客户端自己的错误原样冒出来，而不是被翻译成“下载成功”。

修法：`hub._from_modelscope` **不带** `revision`（`Selection.pinned=False` 把“这份不是 pin 的那份”
变成结构化的事实，而不是注释），`decis download` 在传输前打印源与原因、并单独警告一次兑现不了的
revision，`decis doctor` 报告这台机器会走哪个源。要 pin 真正生效就 `DECIS_HUB=huggingface`：
那时连不上是失败，而不是换一份字节下载。

守卫：`tests/test_hub.py::test_the_modelscope_path_never_receives_the_pinned_commit` 断言
`"revision" not in kwargs`，并且**让假客户端真的写出布局、再交给 `paths.checkpoint_root` 去读**——
只断言参数会把“写”和“读”剪断还宣称它们连着（D17 的教训）；`tests/test_cli_download.py` 三条覆盖
选源、措辞与缺客户端时的退出码。

**教训**：当一个“可选参数”在上游属于**另一个命名空间**时，传错值可能连异常都没有。
验收要问的是“加载器能不能读到”（`checkpoint_root`），不是“调用返回了吗”。

**补测（2026-10-06，就在本机，`--hub modelscope` 真跑一次）**：回退路径此前只有单元测试走过，
这一轮把它端到端跑了一遍（`decis download --engine kev-0.8b --hub modelscope --dest /tmp/ms-check`），
顺手得到一个 D32 一直只是**推断**的结论的硬证据——镜像上的字节和 pin 的那份**不一样**：

| 文件 | Hugging Face `54f4f87`（pin） | ModelScope `master`（当前 head `39278b1`） |
|---|---|---|
| `adapter_model.safetensors` | 43,338,624 B，sha256 `c81d5716…` | 43,338,624 B，sha256 `9b908623…`（**同样大小、不同字节**） |
| `head.pt` | 2,103,103 B | 2,103,999 B |
| `adapter_config.json` | 1,273 B | 1,273 B |

也就是说 `hub.Selection.pinned=False` 不是保守的措辞，而是事实：**回退拿到的是另一个 commit 的
权重**。这正是“回退必须打印出来、需要 pin 就用 `DECIS_HUB=huggingface`”的理由；反过来，客户端的
“下载成功”什么都不能证明。

#### D33（高）kev 的适配器绕过了 Hub 选择：`DECIS_HUB=modelscope` 也在从 Hugging Face 下 — 已修正

D32 修完“选哪个源”，漏掉了**谁去取**。`kev.py` 把适配器交给上游加载器时是这样写的：

```python
target = str(source.path) if source.is_local else f"{source.repo_id}@{source.revision}"
checkpoint = Checkpoint(target)
```

`_kev_vendor/checkpoint.py:29 resolve_run` 收到这个字符串后自己调
`huggingface_hub.snapshot_download(repo, revision=…)`。于是 `load()` 手里的适配器**只能**来自
Hugging Face：`DECIS_HUB=modelscope`、`--hub modelscope` 对这条分支完全无效。

影响范围要说准（这条第一次写成“从 ModelScope 取来的适配器永远读不回来”，说过头了，这里按 `paths.candidate_directories` 的实际行为改准；`--dest` 那条路旧代码是好的）：`--dest` / `DECIS_MODEL_DIR`
下适配器是**本地命中**（`source.is_local`），旧代码把目录原样转交，那条路是好的；坏掉的是**默认配置**
（什么都不设）——`paths.resolve` 对 kev 只会给出 `hub`，因为 `candidate_directories` 里没有
`~/.cache/modelscope/hub/...` 这种 ModelScope 缓存的布局。所以在**Hugging Face 不可达**的机器上
（也就是这个功能存在的理由），旧代码里 kev **加载不起来**：`decis download --engine kev-0.8b` 明明
已经通过 `hub.py` 从 ModelScope 落好了缓存，`load()` 却又自己回到 Hugging Face，`auto` 的回退对它
一次都没生效。实测确认（`DECIS_HUB=modelscope`，把 `huggingface_hub.snapshot_download` 换成记录器）：

```
vendored resolve_run returned /tmp/fake
it downloaded through: [('jaredpalmer/kev-0.8b', '54f4f877…')]
DECIS_HUB was modelscope; modelscope was never asked
```

这是 §2 那类缺陷的典型形态：**决定“权重从哪来”的代码出现在 `paths.py`/`hub.py` 之外**，而且
它躲过了已有的守卫——`test_weight_location_is_decided_only_in_paths` 找的是 `snapshot_download` /
`huggingface_hub` 这两个*字符串*，`kev.py` 里一个都没有；调 `snapshot_download` 的是 vendored 代码，
而 vendored 代码按 §2 的豁免不参与这类检查。“把 repo id 交给别人”也是一种 fetch，字符串扫描看不见它。

修法：适配器与基座一样走 `paths`。新增 `KevEngine._adapter_directory(settings, source)`，本地命中直接
返回目录，否则 `paths.fetch_checkpoint`（和 `decis download` 同一个调用，于是下载与加载不可能对 pin 或
源产生分歧），再把**目录**交给 vendored 加载器。`_base_directory` 早就是这条路（D32 修的），这轮把
另一半补齐——同一个缺陷的两次出现，值得记一笔。

守卫（两层，因为单看字符串已经证明不够）：

- 行为守卫 `tests/test_engines_kev.py::test_the_vendored_loader_is_handed_a_directory_not_a_hub_id`：
  假 `torch` + 假 `_kev_vendor`，让 `load()` 真的走到调用点，断言拿到的是 `paths` 给的目录、里面没有
  `@`、`settings` 与 pin 都跟着走。**无权重套也跑**（这正是当年漏掉这条分支的原因），并且已确认在
  回退成旧写法时失败，报出的正是 `jaredpalmer/kev-0.8b@54f4f877…`。
- 形状守卫 `tests/test_conventions.py::test_engines_do_not_build_a_hub_id_string`：引擎层任何
  f-string 都不许插值 `.repo_id`。它不是通用规则（“不许把 Hub id 交给加载器”没有一般的 AST 表达），
  所以 docstring 里写明了它只钉住这个历史形状，真正的守卫是上面那条行为测试。同时把
  `test_weight_location_is_decided_only_in_paths` 从字符串扫描改成 AST（import / 属性调用 / 裸调用）：
  改之前它连 `kev.py` **解释**自己在躲哪个函数的那句 docstring 都会判成违规。

**教训**：“唯一的 fetch 入口”不能只靠“这个文件里有没有出现客户端名字”来守。凡是把仓库 id 当**值**
传出去的地方，都要问一句“接住它的那层会不会自己去联网”；vendored 代码是这个仓库里唯一能让这句话
失效的角落（D10 的 `measure`、D30 的提示词都栽在“以为守卫盖住了、其实盖的是另一条路径”）。

#### D34（中）`decis doctor` 的引擎清单问的不是它自己解析出来的配置 — 已修正

`_doctor` 打印了 `model paths` 行（来自本次解析的 `Settings`），紧接着的引擎清单却是
`status(engine_id)`——不传 settings，`registry.status` 就自己 `load_settings()` 一份默认配置。于是
`decis doctor --model-path kev-0.8b=/srv/models/kev-0.8b` 会同时打印“你把 kev 指到了 /srv/…”，和
“kev-0.8b needs weights  decis download --engine kev-0.8b”；`DECIS_MODEL_DIR` 同理。`decis models`
是对的（它传了 settings），所以两个命令在**同一个终端里互相矛盾**——而 §2 表里那条“就绪判断只有一个家”
想防的正是这个。

修法：`status(engine_id, settings)`。守卫
`tests/test_status.py::test_doctor_asks_the_classifier_with_the_settings_it_resolved` 不去比字符串，而是
把 `registry.status` 换成一个记录参数的探针：`doctor` 必须调用共享的分类器，且必须把**它解析到的**
settings 传进去。这样在任何环境（有没有 engine extra、有没有权重）都成立——比“印出来的词一样”更接近
那条不许出现第二份判断的规则。

**教训**：共享一个函数不够，还得共享**喂给它的输入**。“两个调用者问同一个分类器不同的问题”比
“两个调用者各写一份判断”更难发现，因为它看起来已经合规了。

#### D35（中）kev 的体积是一个手打猜测，小了 3.5 倍，还抄进了五份文档 — 已修正

这一轮为了确认 D33 的修复真的在从 ModelScope 取字节，量了一次真实文件，于是发现体积数字是错的：

```
docs/design.md §7.2        adapter 三文件 = 13,000,000 B (12.4 MiB)      ← 错
docs/design.md §7.3        “合计约 13 MB”                              ← 错
docs/engines{,.zh-CN}.md    “约 13 MB …… 整个仓库共 43 MiB”             ← 两个数都错
docs/feasibility.md         “仓库 ≈43 MiB，只取 3 个文件 ≈13 MB”        ← 错
AGENTS.md §7               “约 13 MB（仓库共 43 MiB）”                  ← 错
src/decis/engines/kev.py   expected_bytes=13_000_000                    ← 错，而且有功能影响
实测（pin `54f4f87` 的 Hub 快照；`adapter_model.safetensors` sha256 `c81d5716…`、`head.pt` `39f4343c…`）
    adapter_config.json 1,273 + adapter_model.safetensors 43,338,624 + head.pt 2,103,103
                         = 45,443,000 B (43.3 MiB)；整个仓库 65,512,436 B (62.5 MiB)
```

根因不是算错，是**一个手打的数量级猜测被自己的格式化函数洗成了“实测值”**：
`13_000_000` 是写死在 `kev.py` 里的整数，`human_bytes` 把它渲染成 `12.4 MiB`——
于是 CLI 印的是 `downloading to … (12.4 MiB)`（本轮实测日志里还能看到这一行），文档里写的也是
`12.4 MiB`，一个“看起来精确到小数”的猜测就这样在代码与五份文档之间互相引用了一整轮。
`43.3 MiB` 那处更直接：那是 `adapter_model.safetensors` **单个文件**的大小，被当成了整个仓库的清单。

`expected_bytes` 有功能影响，不只是文案：`decis download`
用它打印体积并调用 `filesystem_has_room`，所以之前它按 13 MB 而不是 43 MiB 检查磁盘空间；
更糟的是基座那半边（1.65 GiB，占一次 kev 下载的 98%）`BaseModel.expected_bytes` 是 **`None`**，
于是 1.65 GiB 的落盘**完全没有检查**，而 `docs/design.md` 却为它写着一个没人能核对的
`1,769,896,333`（比 pin 版本的真实总和少 776 B）。

修法：
1. `engines/kev.py` 里体积变成**导出量**：`_ADAPTER_MANIFEST`（文件名 → 实测字节）求和得
   `expected_bytes`，不再是手打的整数；
2. `_BASE_08B.expected_bytes = 1_769_897_109`（pin 版本 9 个文件之和，实测），基座的体积与
   磁盘检查这才第一次存在；
3. 五份文档改成实测值，并写明“整个仓库”是哪个数；
4. 守卫 `tests/test_engines_kev.py`：
   `test_the_declared_size_is_the_sum_of_the_files_a_fetch_writes`（清单键集合 == 下载清单 ==
   `checkpoint_files`，且 `expected_bytes == sum(清单)`，另外断言它大于单个权重文件——这条能抓住
   “比一个文件还小”的抄错）与 `test_the_base_model_declares_the_bytes_it_publishes`。

**教训**：进度条是**传输量**，不是**体积**，两者在“已经缓存过”时差一个数量级。凡是要写进代码或
文档的数字，都要问“这个数是哪台机器、哪次操作量出来的，包含什么”——这次的正确答案是“Hub 上那个
revision 的文件清单”，而它恰好是**可以逐字节核对**的东西（文件名 + 大小 + sha256）。
`docs/design.md §7.2` 要求“实测体积”已经很久了；这条缺陷说明**要求写下来不等于有人核对**，
而唯一能自动核对的形态是把它变成代码里的求和 + 一条守卫。

#### D36（高）Windows 上 `uv sync` 装的是 CPU-only 的 torch，于是"自动探测设备"永远探测不到 GPU — 已修正

来自一台 Windows 机器的实际报告：`uv sync --all-extras` + `decis download` + `decis serve` 之后，
GPU 占用为零，而同一份代码在 Mac 上自动用了 Metal。

根因不在 `devices.py`，也不在引擎：**PyPI 为 Windows 发布的是 CPU-only 的 torch wheel**。
证据可以直接在**修之前的那份 `uv.lock` 里读出来**——`torch 2.14.0` 的依赖列表里，
CUDA 那一串（`cuda-toolkit[cublas,…]`、`nvidia-cudnn-cu13`、`nvidia-nccl-cu13`、`triton`、
`cuda-bindings`）**每一行的 marker 都带 `sys_platform == 'linux'`**，Windows 与 macOS 一个都没有；
对比 PyTorch 自己的 index：`cu130` 的 win_amd64 wheel 是 1,990,604,486 B（自带 CUDA 运行库），
PyPI 的那个 124,096,356 B。也就是说 Windows 上 `torch.cuda.is_available()` 恒为 `False`——
**和一台根本没有 NVIDIA 显卡的机器完全无法区分**。这件事发生在任何 Decis 代码运行之前，
所以"自动探测最可用的设备"这条逻辑再正确也无从发挥：`laya.Agent` 自己的顺序是 CUDA → Metal → CPU，
它在 Windows 上只能落到 CPU。

修法分两半，缺任何一半都还是坏的：

1. **安装期**（`pyproject.toml`）：把 Windows 的 `torch`/`torchvision` 指向 PyTorch 的 CUDA index
   （`[[tool.uv.index]]` + `[tool.uv.sources]` 带 `marker = "sys_platform == 'win32'"`，
   `explicit = true` 免得它变成别的包的源）。于是 `uv.lock` 里出现两个 fork：
   Windows 拿 `2.14.0+cu130`，Linux/macOS 继续从 PyPI 拿原来的 2.14.0（Linux 那份本来就带 CUDA，
   macOS 那份是 Metal），镜像体积与 Docker 构建路径一个字节都没变。
   `uv sync --no-sources` 是给"这台 Windows 没有 N 卡、不想要那 1.9 GiB 下载"的人的退路。
2. **运行期**（`engines/devices.py`）：`torch.cuda.is_available()` 分不开"没有 GPU"和"这个 wheel 用不了 GPU"，
   所以落到 `cpu` 且**没有** `DECIS_DEVICE` 时才去问驱动：`nvidia-smi --query-gpu=name,driver_version`
   （Windows/Linux 随驱动安装，5 秒超时，任何失败都当"问不到"）。
   `torch.version.cuda is None` + 驱动报出显卡 = 这一条的组合，日志里给出显卡型号、驱动版本、
   原因和修复命令；`torch.version.cuda` 有值却看不到设备 = 驱动太老，给的是另一句话；
   没有显卡 = 一个字都不说（那是正常的 CPU 机器，每次启动都警告等于没警告）。
   这条逻辑三个引擎共用（`warn_if_accelerator_is_idle`），`decis doctor` 也用它输出一节 `compute`
   （device / torch 构建 / nvidia-smi 看到的 GPU / advice）——用户真正会去看的地方。

守卫：`tests/test_devices.py`（CPU-only wheel、CUDA wheel、坏掉的 backend、`nvidia-smi` 的解析/缺失/超时、
四种建议组合）、`tests/test_cli_serve.py::test_doctor_names_the_gpu_the_installed_torch_cannot_use`、
`tests/test_conventions.py::test_windows_installs_a_cuda_build_of_torch` 与
`test_the_lock_gives_windows_a_cuda_wheel_and_nobody_else`（**从 `uv.lock` 里读**，所以"把 sources 删掉"
或"三个平台一起挪到 CUDA index"都会红）、`test_the_idle_gpu_explanation_has_one_home`。

**仍未验证**：本仓库没有一台带 NVIDIA 显卡的 Windows 机器，所以"Windows 上真的跑在 `cuda` 上"
**没有实测过**——已验证的是"Windows 解析到 `+cu130` 的 wheel"（`uv.lock` 与守卫）、
"CPU-only wheel + 有显卡时给出的诊断"（桩），以及 macOS/Linux 侧一字未改。
`xpu`/`npu` 的"检测但未实测"状态不变（D24）。
**代价（写下来）**：Windows 上的 `uv sync` 从约 124 MiB 的 torch 变成约 1.9 GiB（CUDA 运行库随 wheel 走），
对没有 N 卡的 Windows 用户是纯亏，`--no-sources` 与这条说明就是为此写的。

**教训**：一个"自动探测硬件"的设计里，**探测不到**的原因可能在依赖解析里，而不在探测代码里。
`devices.py` 从一开始就写对了顺序，但它问的是 `torch` 自己的能力；当 wheel 本身没有那个能力时，
所有探测都返回一个**诚实但无用**的 `False`。这类"配置决定了能力"的缺陷不会在 Mac/Linux 的 CI 上出现，
只有"在真实用户的平台上跑一次"能发现——和 D20/D21/D22 那批容器缺陷是同一类。

---

## 3. 标准符合性对照

用户问"是否尊重标准"。逐条对照，**包括我们有意不遵守的地方**：

| 标准 / 规范 | 我们是否遵守 | 说明与依据 |
|---|---|---|
| **OpenAPI 3.1.0** | ✅ 遵守 | 契约基准就是官方 OpenAPI（`docs/contract/typesafe-openapi-0.2.0.json`）。**并且 Decis 自己也会暴露 `/openapi.json`**，让用户能生成客户端 |
| **RFC 9110 §15.5.2**（401 必须带 `WWW-Authenticate`） | ⚠️ **上游违反，Decis 修正** | 一手核对：*"The server generating a 401 response **MUST** send a `WWW-Authenticate` header field"*（[RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html)）。线上实测确认真 jev 的 401 **不带**该头。**Decis 补上**——纯增量、对 SDK 无影响，但让 Decis 通过标准 HTTP 客户端与网关的检查 |
| **RFC 6750（Bearer 用法）** | ⚠️ 部分修正 | [RFC 6750 §3](https://datatracker.ietf.org/doc/html/rfc6750) 要求 401 **MUST** 带 `WWW-Authenticate`，并定义 `error="invalid_token"`。所以 Decis 的 401 用 `WWW-Authenticate: Bearer error="invalid_token"`，缺凭证时用不带 error 参数的 `Bearer`。**注意**：RFC 6750 倾向用 401 表示"未提供凭证"，而 jev 用 403——Decis **跟随 jev**，因为客户端兼容是首要目标 |
| **RFC 9457（Problem Details，取代 RFC 7807）** | ❌ **有意不遵守** | 业界标准的 API 错误体是 `application/problem+json`。jev 用的是 `{"detail": …}` 且 `detail` 还是多态（对象/数组/字符串，见 `observations-2026-09-22.md §2.6`）。**这是"契约兼容 vs 标准符合"的真实冲突点**。我们选兼容——因为整个项目的存在理由是"换 base_url 就能跑"。已在 `api-compatibility.md §5` 记录为有意偏离 |
| **RFC 9110 §9.2.2（POST 非幂等）** | ✅ 无冲突 | 虽然 POST 定义上非幂等，但 `/v1/systemone` 是纯函数；SDK 会重试，我们保证重复执行结果一致（唯一的例外是批组成不同带来的数值差异，见 D3） |
| **OCI Image Spec 注解** | ✅ 遵守（已补） | 补齐 `org.opencontainers.image.{title,description,source,url,licenses,version,revision,created,base.name}`。自有扩展用 `ai.decis.engine`，不占用 OCI 保留键 |
| **容器安全基线**（非 root） | ✅ 遵守（已补） | `USER 10001:10001`。这也让镜像能通过 Kubernetes `restricted` 档 Pod Security |
| **SemVer 2.0** | ✅ 遵守 | Python 包与镜像 tag 都用 SemVer（`<engine>-<semver>`） |
| **PEP 621 / 440 / 517-518** | ✅ 遵守 | `pyproject.toml` 用 PEP 621 元数据，hatchling 作 PEP 517 后端，uv 生成 `uv.lock` |
| **PEP 8 / 484 / 561** | ✅ 遵守 | ruff 强制；公开 API 全部类型标注 |
| **12-Factor（配置来自环境）** | ✅ 遵守 | 所有配置集中在 `config.py`，`os.environ` 不得出现在其他模块（`AGENTS.md §2` 已列为唯一事实来源） |
| **SPDX / Apache-2.0** | ✅ 遵守 | `LICENSE` + `NOTICE` 记录 kev 与 jeff 的 vendoring、Laya 的署名；不复制 laya-mlx 代码 |
| **供应链：SBOM + provenance** | ✅ 已实现 | `docker-build.yml` 在**推送**的构建任务上打开 `sbom=true` 与 `provenance=mode=max`（只在 PR 的 build-only 任务里关掉，因为对不推送的构建请求 attestation 会失败） |
| **Prometheus 暴露格式** | ⬜ 未实现 | `/metrics` 不存在。计划是用标准 exposition format、不从零发明指标名 |
| **模型卡 / 可复现性规范** | ⚠️ 无正式标准 | 没有 IETF/ISO 级的规范。我们采用社区惯例：报告必须同时给出引擎、设备、dtype、线程/进程数、批大小、state 长度、问题数（`AGENTS.md §8`） |

**这一节里最值得注意的一点**：RFC 9457 那一行。一个"尊重标准"的项目在这里**故意不遵守标准**——
因为遵守它就会破坏与 jev 的线格式兼容，而那正是项目的全部价值。**正确的做法不是假装没有冲突，
而是把冲突显式写下来并说明选择理由。**

---

## 4. 方法论问题（关于证据本身）

设计文档的可信度取决于证据的质量。这部分是自我审查里最不舒服、但最有价值的部分。

### M1（严重，已修正）初版契约只有客户端视角

初版全部结论建立在两件事上：读官方 OpenAPI **生成物**、读 SDK 源码。这有个结构性盲点：
**它无法验证服务端是否真的按声明行事。** 补做线上观测后立刻发现：认证错误码写错了
（初版说 401，实际无凭证是 **403**、凭证无效才是 401）；"认证先于校验"这个顺序任何离线阅读都推不出来；
错误响应也带 `x-typesafe-request-id`；request id 的格式是 `req_` + 32 位十六进制；
401 缺 `WWW-Authenticate`，是一处 RFC MUST 违规。

方法论修正：① 证据等级表新增 **L 级（线上观测）** 并置于最高优先级（`L > S > A > B > C > D`）；
② 原始材料保留在 `docs/contract/observations-2026-09-22.md`，**保留命令与原始输出**，不只留结论；
③ 新增验收层 **L3b（错误契约）** 进 CI；④ **L5 差分测试从"可选"提升为"改契约前必须跑"**——
L0 只能保证"我们和自己的 OpenAPI 快照一致"，**没有任何离线测试能发现线上服务端偏离它自己的 OpenAPI**。

### M2（中，**未修正**）性能测量有顺序效应

Laya 的线程扫描是 `for threads in [1,2,4,8,16,24]` **顺序执行**的，每个配置跑 6 次取中位数。
这意味着**热漂移、内存碎片、其他负载的时间趋势与线程数是混淆的**——看起来像"线程数影响"，
可能部分是"跑得越晚越慢/越快"。

**未修正的原因**：这台机器当时还在并发跑 kev 的探测，测量环境本身不干净；**这一批数据的定位本就是
"可行性证据"而非正式基准**（已在 `benchmarks/README.md` 写明）。正式基准属于 Stage 3，届时必须：
随机化配置顺序并**重复整轮**；报告**分布**而不只是中位数（至少 p50/p95 与样本数）；记录机器是否被其他任务占用。
**我不打算事后粉饰这批数据**——它的价值是"量级正确、结论方向正确"，不足以支撑精细比较
（比如"16 线程比 24 线程好 13%"这类结论）。

### M3（中，已修正措辞）kev 的 "83×" 精度过高

bf16 与 fp32 的对比**每个 dtype 只报了最后一次请求的 `latency_ms`**，本质上是**单次观测**，
而文档里写成"**83 倍**"给人一种精密测量的错觉。事实是 137174 ms 比 1656 ms = 82.8×，各只有一次测量；
正确的表述是"**约 80 倍，单次观测**"。`benchmarks/results/kev-0.8b-cpu-dtype.json` 里已注明
"single observation per dtype, recorded verbatim"。**结论的方向（bf16 在 CPU 上病态地慢）是可靠的，
因为它差了两个数量级**——这正是"统计显著性"与"效应大小"的区别。

### M4（轻，已修正）benchmark JSON 有个误导字段名

Laya 扫描的输出里有个字段叫 `questions_per_second`，但它实际算的是 `1000 / (p50_ms / n_questions)`——
**这是"单个串行调用方的速率"，不是吞吐量**，它没有考虑并发，也没有考虑批处理。
已改名为 `single_caller_questions_per_second`，并在 `benchmarks/README.md` 说明它的含义。
**一个误导性的字段名比没有字段更糟**，因为它会被下游引用。

### M5（严重，**已测，结论为否定**）最核心的性能断言被测掉了

`design.md §6` 的整个论证是：跨请求批处理把多个请求的问题合并成一个大 batch，从而把每问成本降下来，
这是高 QPS 的来源。**已测**（2026-09-22，`benchmarks/batch_gain.py`，原始 JSON 见下表）：
合成跨请求批处理——不同 state、不同问题长度、直接调用引擎、批大小 1/2/4/8/16。

结论是**否定**的，而且比"没测过"更糟：

1. **短序列下批处理确实有收益。** 正对照（所有项完全相同、padding 恒为 1.00）在 115 token 时达到 1.92x。
   这条正对照同时验证了测量本身：它复现了已发布的请求内结论（`laya-multilingual-sweep.json`：
   1 问 231 ms → 10 问 98.7 ms/问）。**所以"没有收益"不是因为测不出来。**
2. **收益随序列变长而消失。** 同样的正对照在 393 token 时只剩 1.10x。收益来自摊薄每次调用的固定开销，
   而固定开销相对几百 token 的前向计算很小，摊薄不出东西。
3. **真实流量形状下是负收益。** 长度倾斜（140–821 token）时每个批大小都慢 3–5 倍，
   因为每行都补齐到批内最长，padding 达到 2.47x，浪费的算力直接变成更慢的每项成本。
4. **加进程也不增加吞吐。** 固定总线程预算（24）下，1/2/4 进程的吞吐差 1.18x，即**没有扩展性**：
   单个 Laya 前向已经用满 24 线程，把预算切开只是重新分配同一份算力。

**因此**：`design.md §6` 把跨请求批处理当作主要吞吐手段是**错的**（在这台 CPU 机器上）。
一个按原设计实现的攒批器在异构 state 下会是 3–5 倍的性能回归——比"没实现"差得多。
Stage 3 的攒批器**不应按原设计实现**。

**残留的未知量**（写清楚，不用它来救结论）：**只有 CPU 数据**（M6），GPU 上批处理通常是提升利用率的标准手段，
本结论**不适用于 GPU**；**只测了 Laya**，kev 是 prefill-only 架构，特性可能不同；
这是**上界**（合成批没有队列、没有等待、没有取消、没有部分失败，真实攒批器只会更差）；
**不是并发负载测试**（没有 p99，也没有真实多客户端）。

**方法论上的两处硬要求**：**交错轮次**——每个轮次按打乱的顺序访问所有批大小，而不是"一个大小测到底"，
升序跑到底正是 M2 记录的顺序效应；**正对照**——一个测不出收益的 harness 无法区分"机制没用"和"测量坏了"
（D1 的教训），短序列正对照必须显示收益，否则 `report.py` 拒绝生成整个文件。

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

### M6（中，**未修正**）没有 GPU 数据，却已出现依赖 GPU 的设计决策

所有实测都在无 GPU 的 aarch64 机器上。但文档里已经出现了 GPU 相关的判断（kev 定位为 GPU 引擎、
CUDA 镜像体积估算、`flash-linear-attention` 的可用性）。**这些是推断，不是实测。**
**未修正的原因**：没有 GPU 机器。已在 `docs/feasibility.md §5` 的 R1 明确标注为"需要 GPU 机器验证"，
并且**没有把任何 GPU 性能数字写进 README**。这条纪律必须守住。

---

## 5. 还没修 / 修不了的（诚实清单）

| # | 问题 | 状态 |
|---|---|---|
| 1 | 跨请求批处理的收益 | **已测（M5），结论为否定**：CPU 上不提升吞吐，长度倾斜时慢 3–5 倍。`design.md §6` 的收益预期已被推翻 |
| 2 | GPU 上的真实延迟、CUDA 镜像能否装成 | **未测**，需 GPU 机器。D36 之后补一句更窄的：Windows 上"解析到 `+cu130` 的 wheel"已由 `uv.lock` 与守卫钉住，但**在一台真实的 N 卡 Windows 机器上起服务、确认它跑在 `cuda` 上**仍然没做过 |
| 3 | 429 / `Retry-After` 的真实行为 | **未观测**（无 API key，无法触发限流） |
| 4 | 真实 200 响应体的取值 | **未观测**（无 key）。形状由 OpenAPI + SDK + kev 三方交叉确认，但取值没有对照样本 |
| 5 | 性能测量的顺序效应与样本量 | **Laya 线程扫描未重做**（它的定位本就是可行性证据，M2）；M5 的攒批 harness 已按**交错轮次**跑，并报分布而不只是中位数 |
| 6 | 多进程 × 线程数的最优点 | **已测（M5）**：固定总线程预算（24）下 1/2/4 进程吞吐差 1.18x，加进程不增加吞吐 |
| 7 | Qwen3 世代 kev 在 CPU 上是否可用 | **未测**。若可用，可能是 CPU 镜像更好的默认选择 |
| 8 | SBOM / provenance | **已实现**：推送的构建任务带 `sbom=true` + `provenance=mode=max`（§3） |
| 9 | `remote` 引擎转发真 jev 的合规性（用户自有 key） | **未评估**，需要时再确认 ToS |
| 10 | 两个 Jeff checkpoint 的真实加载与推理 | **都测了**（`tests/test_jeff_inference.py`，`-k <engine>` 各 18 项全过，2026-09-30，CPU 无 GPU）。Gemma 的加载实测 139 s、峰值 RSS **23.8 GiB**（`/usr/bin/time -v` 的 max RSS；`fp32` 权重本身 18.5 GiB，差值来自 bf16 → fp32 的转换），Qwen 的峰值 **5.3 GiB**。两个引擎的 weights 套**分两半跑**，因为同时常驻约 26 GiB，这台 31 GiB 的机器放不下 |
| 11 | 两个 Jeff 引擎镜像的构建、体积与容器内冷启动 | **构建与体积已测**（2026-09-30 工作流首次构建并推送；体积取自已发布 tag 的 registry manifest：Qwen amd64 5.9054 GB / arm64 6.0471 GB，Gemma amd64 17.7453 GB / arm64 17.8870 GB，已记入 `docs/deployment.md`）。**容器内冷启动仍未测**：这两个 tag 从来没被拉下来起过容器 |
| 12 | Jeff 的吞吐/延迟数字 | **未测**（§8：没有 checked-in 的原始 JSON 就不许写数字；`docs/engines.md` 只报**内存占用**，那是"这个模型能不能在这台机器上跑起来"的事实，不是性能数字）。两个引擎各只有一次探针观测（三问题一批，而且当时 CPU 被并发的测试占着），要报就得先进 `benchmarks/results/` |
| 13 | 挂载自定义**基座**（D11）与 `noul.criteria` 未知键的策略（D13） | **两条都未修，而且都没有测试守卫**——它们只有文档层的约定，改动前请先看 §2.1 |
| 14 | ModelScope 回退在**真正访问不到 Hugging Face** 的机器上的端到端行为 | **本机已验（含 kev），目标环境未验**。用 `modelscope 1.40.1` 在本机（能连 huggingface.co）验证过：镜像存在，`allow_file_pattern` 的子目录通配符（`multilingual/tokenizer/*`）落下的布局能通过 `paths.checkpoint_root`；`pyproject.toml` 的 extra 也已解析出这个依赖。2026-10-06 补跑 `decis download --engine kev-0.8b --hub modelscope --dest /tmp/ms-check`：适配器完整落盘（3 个文件、45,443,000 B），`paths.resolve` 判为 `local`——这是 D33 的直接验收；基座也开始从 ModelScope 下载（`model.safetensors` 1.75 GB，实测 100–500 kB/s，到 64 MB 时中断），**只验到一半**：中断后磁盘上是 `*.incomplete`，`paths.resolve` 因此判它 `hub` 而不是 `local`，即半个基座不会被当成可用（D29 的保证对 ModelScope 客户端同样成立，这是这次顺带确认的）。**没有**在一台 huggingface.co 不可达的机器上真跑过一次 `decis download`，也没测过 3 s 探测超时在被墙网络里的真实表现（DNS 污染与连接挂死的耗时不一样），而**这正是 D33 藏了整整一轮的原因**：`auto` 在本机永远选 Hugging Face，那条分支只有人工 `--hub modelscope` 才会走到 |

---

## 6. 总结

**初版的方向是对的，但有三个"不会崩、却会让产品失效"的缺陷**：协议让批处理永不触发（D1）、
没有鉴权却监听全网卡（D2）、没处理批处理带来的数值不确定性（D3）。三个都已修，而且都有测试守着。

**方法论上最大的改进是承认"读 schema 不等于验证实现"**——补做线上观测后立刻发现了一处真错误
（401 与 403 的分工）和四条只能靠观测得知的契约，并因此把差分测试从可选提升为改契约前的必跑项（M1）。

**M5 的结论是否定的**：跨请求批处理在这台 CPU 机器上不增加吞吐，长度倾斜时因为 padding 反而慢 3–5 倍；
加进程也不增加吞吐。**因此 `design.md §6` 的吞吐论证需要改写**，而不是继续等一个会兑现它的实现。
"对外材料不许出现 QPS"这条纪律的**理由**消失了（数字已经有了），但**新的理由**接上了：
现在能报的是一条明确的负结论和一个测得的单进程吞吐上界，**不是**"批处理带来的高 QPS"。

**2026-10-06 复核的结论**：35 个设计缺陷里 33 个已修（各有守卫，少数只有文档修复），
2 个仍未修（D11、D13）。D36 是这一轮由一台 Windows 机器的报告发现的：同一份代码在 Mac 上自动用了
Metal，在 Windows 上一路 CPU——根因在 `uv.lock` 解析到的 wheel 里，不在设备探测代码里。D33 与 D35
是上一轮“换源”功能上线后**为了验证它**才发现的：前者是新分支没被真跑过一次（守卫只盖住了旁边那条路），
后者是量字节时顺手发现体积数字一直抄错——三条都不是新代码引入的，而是“原来就没人核对过”；
6 个方法论问题里 4 个已修正，2 个仍未修正（M2、M6，都需要一台干净的机器或 GPU）。
§5 是这些未验证项的完整清单——**它们是这份文档仍然有效的那一半**。
