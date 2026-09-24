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
> **测试**：`uv run pytest -q` 跑无权重的那套（**671 通过 / 33 跳过**，约 29 秒，不联网）；
> 装了真实 `laya` 的环境（`.scratch/venv`）跑同一套是 **695 通过 / 28 跳过**（多出来的 24 个用例
> 是引擎可用后才参数化出来的依赖分支）；
> `uv run pytest -m weights` 跑真实权重的那套（**27 个**：14 个 Laya + 10 个 kev + 3 个真实权重批不变性，CPU 上约 5 分钟）。
> 另有 `tests/test_contract_sdk.py` 里由 `TYPESAFE_LIVE_API_KEY` 门控的线上差分测试。
>
> **已注册引擎**：`laya`、`laya-multilingual`、`laya-typed-decisions`、
> `kev-0.8b`（kev 的适配器 + Qwen3.5-0.8B 基座，见 `paths.BaseModel`）。
> **注册表里没有假引擎**：无权重的确定性测试替身住在 `tests/fixture_engine.py`，
> 由 `tests/conftest.py` 以 id `stub` 临时注册，出厂镜像永远不会用它作答。
> **已实测的关键负结论**：`docs/design-review.md §4-M5` 已关闭，答案是**否定**——
> 跨请求批处理在本机 CPU 上不提升吞吐（真实长度下最高 1.09x，长度倾斜时慢 3–5 倍），加进程也不提升。
> **因此跨请求攒批器不应按 `design.md §6` 的原设计实现**，`/metrics` 与进程池的必要性也随之下降。
> **未实现**：kev 的 prefix 缓存路径、`/metrics`。
> `docs/design.md §11` 的目录树是目标结构，其中未出现的文件即为尚未实现的部分。
>
> **镜像**：`.github/workflows/docker-build.yml` 在 push/打 tag 时按引擎构建并推送到 **Docker Hub**：
> **所有引擎共用一个仓库 `chaitin/decis`，引擎就是 tag**（`chaitin/decis:laya-multilingual`、
> `chaitin/decis:kev-0.8b`）。命名空间由工作流的 `IMAGE_NAMESPACE` 决定（默认 `chaitin`，
> 可用仓库变量 `DOCKERHUB_NAMESPACE` 覆盖），**不从 `DOCKERHUB_USERNAME` 推导**：那个是登录身份，
> 而组织不是用户账号（组织访问令牌以组织名当用户名），从凭据推导会把镜像推到一个没有任何文档
> 写过的命名空间去。master 推送时 `laya-multilingual` 另外拿一个裸 `latest`（它是默认引擎），
> 让 `docker pull chaitin/decis` 开箱可用。**只有 release tag 才在引擎名后追加版本**（`laya-multilingual-v1.2.0`）——
> "当前版本"不加 `-latest`，加了就等于同一个东西有两个名字，而其中一个是文档里没有的。
> **同一仓库里还有一个非引擎镜像 `playground`**：`chaitin/decis:playground`（release 为
> `playground-v1.2.0`），是三个网页小游戏 + 一个只转发 `/v1/systemone` 的纯标准库代理
> （`playground/`）。它不由引擎矩阵构建，而是单独一个 job；`playground/Dockerfile` 里
> **没有 `RUN`**，所以一个 job 直接推 amd64+arm64 的 manifest，不需要 QEMU 也不需要合并腿。
> 它之所以和引擎共用一个仓库，是为了不新建一个要手工配置的 Docker Hub repository。
> **引擎名那个 tag 一定带权重**（`DECIS_PREDOWNLOAD=<engine>`）：一个引擎的镜像存在的意义就是
> "拿到就能用"，所以 `docker pull` + `docker run` 不许依赖网络、卷或第二容器。不带权重的变体是例外，
> 它带后缀 `-runtime`，且只在 release tag（或手动 dispatch 勾上）时发布：
> `laya-multilingual-runtime-v1.2.0`。**没有 `-offline` 这个名字**——默认已经烤权重的时候，
> `-offline` 就是同一个东西的第二个名字（§2、§9）。
> 多架构（amd64 + arm64 原生 runner，不用 QEMU），带 SBOM 与 provenance；
> PR 只构建 amd64 的 **engine-free 基础镜像**（不装任何 extra）以验证 Dockerfile。
> **矩阵、tag 方案、引擎 → pip extra 的映射全部在 `plan` 步骤里算，并由 `tests/test_docker_workflow.py`
> 把那段脚本从 YAML 里抠出来真跑验证**（构建/合并步骤只做插值，用一个假的 `docker` 记录它被要求创建哪些 tag）。
> **GHCR 已不再推送**（只有 Docker Hub 一个 registry）。
> **已实测**（[run 35807645301](https://github.com/chaitin/Decis/actions/runs/35807645301)，commit `574c0ac`）
> 6 个构建腿 + 3 个 merge 全绿，当时发布出来的仓库是公开的。当时还发布了一个无权重的假引擎镜像
> （**现已从代码、工作流和文档中移除**），并真的拉下来跑过：起容器 → 打 `/v1/systemone`，
> 契约响应完整（`noul` 标量、`score` 的字符串键 + `legend` + `confidence`、`choice` 的键与请求一致、
> `usage`、`x-typesafe-request-id`、`decis` 命名空间），无凭证 **403** / 错 key **401** 也都实测。
> 容器里没配 `DECIS_API_KEY` 而监听 `0.0.0.0` 时**拒绝启动**（§3-19 在容器里同样生效）。
> 体积用 registry API **逐层求和**量出（压缩后的下载量，不是 Docker Hub 页面那个数，
> 也不是本地解压后的大小）。数字量自迁到 `chaitin` 之前那个命名空间里的同名镜像，而体积由引擎与
> checkpoint 决定、与源码无关（源码只改最上层那一层），所以对当前工作流发布的镜像同样成立：
> 烤权重的 `laya-multilingual`（= `latest`）**4401 MB** amd64 / **4543 MB** arm64
> （同一份内容两次构建差 1 MB，所以别把它当指纹读）；
> `kev-0.8b` **6045 / 6188 MB**；
> 不带权重的 `-runtime` 变体分别是 **3203 / 3346 MB** 与 **3206 / 3349 MB**。
> **engine-free 基础镜像尚未量过体积与冷启动。**
> 那个假引擎镜像曾误装 Laya + torch 达到 3203 MB（`design-review.md §2-D14`），
> 修好后构建时间从 9m16s 降到 59s——那个镜像已删除，但这条教训（§9 的三元表达式禁用）仍然有效。
> **本地编排**：根目录 `docker-compose.yml` 是**部署文件**，用 profile 选引擎，
> **profile 名 == 引擎 id == image tag == `--engine` == `DECIS_DEFAULT_ENGINE`**。
> `.env.example` 里 `COMPOSE_PROFILES=laya-multilingual`，所以裸 `docker compose up` 只起一个引擎
> （两个同时驻留要好几 GB 内存）。**一个引擎一个容器**：权重在镜像里，所以没有预取服务、没有卷、
> 也没有 `depends_on`。`DECIS_MODEL_DIR` 被 compose 钉在 `/models`（镜像烤权重的位置），
> 并且**不许往那里挂卷**——挂上去会把烤进镜像的权重盖掉，容器就悄悄开始联网下载（§2-D21）。
> compose 层的探针打的是 `/readyz`（Docker 不会因为探针失败重启容器，所以这里可以当就绪用，
> `--wait` 才真的等到"能作答"，§2-D22）；镜像自带的 `HEALTHCHECK` 仍然是 `/healthz`，给编排器用。
> 名字 `docker-compose.override.yml` 就是机制：Compose 会自动把它叠在基文件上，于是**源码目录里
> 裸 `docker compose up` 用本仓库源码构建 `decis-local:*`**，而部署只拷基文件、拉发布镜像。
> `tests/test_compose.py` 不需要 docker daemon：image tag、profile、端口、鉴权、探针、构建覆盖层
> 全部从工作流 `plan`、Dockerfile 与 compose 文件本身读出来比对，不抄常量。
> **playground 是同一个 compose 里的第二个服务**，`profiles` 同时挂着两个引擎 id，所以它能起来
> 不是因为"默认引擎也在跑"：它**不 `depends_on` 任何引擎**——没启用的 profile 其服务名在
> Compose 网络里根本不解析，`depends_on` 会直接起不来。它靠**自己**按顺序打候选的 `/readyz`
> 找到引擎（容器名两个 + `host.docker.internal:8000`），并把转发请求里的 `model` 改写成引擎
> 报出的 id；token 只留在服务端（页面永远拿不到），也因此**它的端口等于模型的端口**，默认
> 和引擎一样发布在所有网卡上（`.env.example` 给了只绑回环的写法）。`DECIS_PLAYGROUND_UPSTREAM`
> 可以跳过搜索。它有自己的 `DECIS_PLAYGROUND_*` 变量，**不读引擎的 `env_file`**（那个文件里有
> `DECIS_DEFAULT_ENGINE` 之类只对引擎有意义的键）。
> **烤权重的镜像与 override 已实测**（aarch64 本机，源码构建的 `decis-local:laya-multilingual`，7.25 GB 解压后）：`docker compose up -d --wait` 起**一个容器**，
> 没有卷、没有预取服务、**日志里没有任何下载**，加载路径就是 `/models/laya-multilingual/multilingual`；
> `engine laya-multilingual ready after 120.7s`，而 `--wait` 在 **129 s** 返回且返回时 `/readyz` 已是
> **200**（§2-D22 的修法就是这么验的），常驻 **2.804 GiB**。容器内 `/v1/systemone` 真的作答
> （`noul` 标量、`score` 的字符串键 + `legend` + `confidence`、`choice` 的键与请求一致、`usage`、
> `x-typesafe-request-id`、`decis` 命名空间），`jev-latest` 被替换成 `decis/laya-multilingual@0.3.6`，
> 无凭证 **403** / 错 key **401**、加载期间 `/readyz` 报 503 都复现；`docker compose config` 在
> 两个方向（带/不带 override）都解析通过。
> 两个环境事实写进了 `design-review.md §2-D23`：Docker **不把** shell 或 `.env` 里的代理带进 `RUN`
> （本地构建必须 `--build-arg HTTP_PROXY=...`），而代理 build arg **不进** BuildKit 缓存键。
> **发布出去的镜像也实测过**（一个 release 镜像，arm64，走本机那个代理，21 分钟）：容器起来后**日志里
> 0 条下载**，从 `/models/laya-multilingual/multilingual` 加载，`ready after 122.3s`，
> `/readyz` 131 s 变 200，常驻 2.87 GiB，`/v1/systemone` 的 `noul` 与本地自建镜像
> **逐位相同**（0.0107），403/401 与 `/v1/models` 都对。那次拉的是迁移前那个命名空间里的镜像；
> 当时还顺手删掉了 registry 里 7 个 `mock*` tag（那个假引擎的残留）。
> **未验证**：`-runtime` 变体只验证了"能构建、能合并、体积对"，**没有拉下来跑过**（烤权重那个跑了）；
> kev 镜像的容器内冷启动（没在容器里起过 kev）、kev 的 compose 路径（只跑了 laya）。
> 报镜像相关的结论时不要超出这个范围。
> **playground 已实测**（同一台 aarch64 本机）：`docker build -f playground/Dockerfile` 成功
> （没有 `RUN`，所以不装任何依赖），镜像直接跑在一个自建 bridge 网络上、旁边是一个发布镜像；
> 它**自己**从候选里找到了 `http://decis-test-engine:8000`，
> `/readyz` 报 `{"status":"ready","engine":"laya-multilingual"}`，`/`、`/snake`、`/dino`、`/tetris`
> 都是 200，`/../server.py` 是 404，引擎还在加载时它如实报 `searching`。**三个页面自己的 JS**
> （node 里给 DOM 打桩、把 `fetch` 换成记录器）发出的真实请求都拿到了 200：
> snake 一次 `choice`、dino 一次 `choice`、tetris 四问（`choice`/`choice`/`score`/`noul`），
> `model` 被改写成 `decis/laya-multilingual@0.3.6`，token 是 playground 附的而不是页面带的。
> 用引擎自己的 `measure()` 量了这些真实请求（`head_max_len=256`、`max_len=1024`，由 checkpoint
> 的 config 提供而非代码默认）：snake 的 head 79 / 序列 287，dino 78 / 128，tetris 的四个问题
> 分别 178、100、97、69，state 233、序列 415——**全部落在代码兜底的 512/192 之内**。
> 正是这个测量把 tetris 的落点候选从 6 压到 5：6 个约 207 > 192。
> **playground 的界面（统一外壳之前那一版）已实测**（headless Chromium 走 CDP，工具 `webcheck.py`，
> 页面连本机 8299 上的替身引擎，所以棋真的在下）：四页 × 中英各跑一遍（`final_sweep.py`，结果留在同名
> JSON）**`bad` 全空**——无未捕获异常、无 console 输出、无横向溢出（`documentScrollWidth == viewport`）、
> 无看不见的字、每个 canvas 都真有像素；窄视口四页在 380 与 700 都验过。对比度按 WCAG AA 逐节点算
> （渐变取平均色、半透明逐层合成，**是近似不是读数**），没有一个样本低于阈值。语言行为：`--lang=zh-CN` 起的
> 浏览器不带参数进来就是中文，`?lang=en` 覆盖它，点顶栏开关切换后刷新仍是切换后的语言。页面发出的请求
> 被替身引擎记下：snake 9 次 `move`、dino 28 次 `action`、tetris 8 次四问，`state`/`instructions`/
> `criteria` 仍是英文原文，tetris 的落点候选恰好 5 个。**这一轮抓到并修掉两个真实缺陷**（都不是改版引入的）：
> tetris 的"最近一次调用"面板把候选数写成字面量 6（短名单从 6 压到 5 时漏改，页面显示比实际发出去的多一个）；
> tetris 在 380–430px 横向溢出（stage 是 282px 棋盘 + 96px 侧栏，媒体查询只压了外层），已加 440px 断点
> 让 stage 竖排、操作栏换行。统一外壳之后前者那块面板没有了（控制台直接印发出去的请求体），守卫改成盯住
> "请求仍按 `PLACEMENT_SHORTLIST` 定量"且不存在 `Math.min(5|6|16, …)` 这种二次写法。
> **三个页面的外壳已统一并重新实测**（headless Chromium 走 CDP）：
> 手动/AI 是**一个开关**（`data-ai-switch`，快捷键 `M`），推理面板的延迟与吞吐只由真实 `usage`
> 和浏览器时钟算出（延迟是客户端端到端，吞吐 `(input + output) / latency`，p50/p95 取最近
> 200 次，第一次调用之前显示破折号），"最近一次调用"在一个可折叠的控制台里、印的是**发出去的
> 请求体本身**；dino 那个 `/v1/systemone` 端点输入与三张基准图
> （`scoreChart`/`latencyChart`/`compareChart`，于是 dino 的 canvas 从 4 个变成 1 个）随
> "地址可以改"这件事一起删掉了。
> 交互实测（`.scratch/uisweep.py`，三游戏 × 中英，`bad` 全空）：AI 档 telemetry 有真数
> （snake `125 ms` / `421 → 0` / `3368 tok/s`，`0 failed`，控制台里有 `state` 与 `questions`），
> 切到手动档并等 2 s 后计数器**不再增长**（snake/tetris 停在破折号、dino 停在 115），按键能改变
> 棋盘或分数，切语言后再读状态文案也跟着变。`final_sweep.py --narrow` 的宽窄视口（1440 与
> 380/700）结果：四个页面 × 中英共 24 次运行 **`bad` 全空**——未捕获异常 0、console 输出 0、
> 横向溢出 0、i18n 缺键 0、对比度不合格 0、canvas 都有像素（index 0、snake 1、dino 1、tetris 2）。
> **合入 master 之后又跑了一遍同一对 sweep**（master 在此期间给顶栏加了"返回/仓库"两个链接，
> 那是 `.scratch` 那一轮没见过的）：24 次运行仍全部干净，交互 6 次运行的 `bad` 也全空，
> 对比度样本 index 41 / snake 41 / dino 47–48 / tetris 46，没有一个低于阈值。
> **这一遍抓到一个合并带出来的真实缺陷**：380px 下三个游戏页横向溢出，
> `a.link-repo` 跑到 [345..413]（视口 380）——顶栏在 ≤720px 时把导航整行放下，而它现在有五个
> 链接（返回 + 三个游戏 + 仓库），一行放不下。已让 `.app-nav` 在那一档 `flex-wrap: wrap`
> （`playground/web/theme.css`），复测 24 次运行 0 溢出。索引页没有这个链接，所以它当时是过的。
> **这一轮抓到并修掉三个真实缺陷**：tetris 手动档每落一个方块仍向模型要一次"该放哪"的建议
> （改成只用页面自己的启发式画幽灵，调用归 AI 档）；tetris 的 `renderMetrics()` 往一个从不存在的
> `#agree-badge` 写值，AI 每做一次决策就抛一个未捕获的 `TypeError`（补齐了那块面板头部）；
> 上一轮改版丢了 tetris 的 8 条文案（`desc.clear*`、`desc.holes*`、`surface.*`），而 `i18n.js`
> 缺键只警告不抛，所以只有真在浏览器里跑才会看到页面印出键名——现在 `tests/test_playground.py`
> 逐页检查页面问过的每个键都已声明。
> **这一轮按用户反馈重排了三个游戏的界面，并修掉俄罗斯方块"不会旋转"的真正原因**（同一台机器，
> 替身引擎 + headless Chromium 走 CDP）。布局：三页统一成"游戏栏 → 棋盘与模型读数 → **两块面板
> 并排放在游戏区下面**"，那行是 `theme.css` 的 `.game-under`（推理面板 300px + 控制台自适应），
> 控制台**默认展开**、左边请求右边响应（初始状态只在 `game.js` 里定一次，页面不许自己写 `open`）；
> 页面用 `data-layout` 只声明自己的形状：贪吃蛇 `board`（棋盘 360→480，居中）、恐龙 `stack`
> （横向画布占满整行，模型动作放下面，`#row-*` 三行并排成 `.prob-rows.cols-3`）、俄罗斯方块 `rail`
> （棋盘 282×562→382×762，位图 560×1120 只会被缩小，所以不糊）。几何是**量出来的**而不是看出来的
> （`.scratch/layout_probe.py`）：1440 下三页的 `.game-under` 与游戏区**同宽**（1040px）且在其下方，
> 推理面板与控制台左右并排、控制台内请求与响应各 341px 并排；≤700 时那两块面板竖排，无溢出。
> **俄罗斯方块的缺陷**：短名单按页面自己的启发式排序取前五，棋盘平的时候启发式会连续偏好同一个
> 朝向——存下来的真实请求体（`.scratch/payloads/tetris.html.json`）里五个选项**全是 `rot2`**，
> 于是模型只在挑列、没在挑落点，旁观者看到的就是"它不会旋转"。改法是先把**每个旋转**里最好的
> 落点放进去，再用其余落点填空（`shortlistPlacements`，请求/本地模拟/本地启发式/面板兜底四处
> 共用这一处实现，`heuristicDecision` 不再自己排全部落点）。改后实测：26 个真实问题里 23 个给出
> 两种以上旋转、11 个四种都给到，只给一种的全是 `O`（它本来就只有一种）。**顺带**：面板原来固定
> 画 6 行，而请求只发 5 个选项（短名单从 6 压到 5 时留下的字面量），现在行数也来自
> `PLACEMENT_SHORTLIST`。token 用引擎自己的 `measure()` 在 45 个真实问题上重测：head 最坏 **185**
> （空棋盘 178），序列最坏 **475**，都还在兜底的 192/512 之内；给选项键补一句 `rot<R>` 的说明要
> 约 23 token、**装不下**，所以那句话没写，旋转由键本身（`rot2_col3`）和面板（`r2 · c4`）表达。
> 验证：`.scratch/final_sweep.py --narrow` 24 次运行（四页 × 中英 × 1440/380/700）**`bad` 全空**、
> 对比度 index 41 / snake 41 / dino 47–48 / tetris 46 全部合格、canvas 都有像素；
> `.scratch/uisweep.py` 6 次交互运行 `bad` 全空（AI 档真在调用，手动档不再调用，切语言正常）；
> 两套测试都跑过（`.venv` 659 通过 / 33 跳过，`.scratch/venv` 683 / 28）。**另外用用户那套 compose
> 里的真引擎（容器 `decis-laya-multilingual-1`，页面在 `http://127.0.0.1:8080`）在浏览器里真打了
> 150 秒**：36 次调用 0 失败、每次约 3.0 s、**四种旋转都真的被选中**（rot0 11 / rot1 16 / rot2 4 /
> rot3 4），面板每一帧都是混合朝向；但 35 个方块**一行都没消除**，最后 game over。所以"它不会旋转"
> 这个现象修掉了；"Laya 玩得好不好"是另一个问题，这一轮只有这一次运行的数据，不下结论。
> **这一轮按用户对布局/文案/文档/录屏的四点反馈又改了一遍**（同一台机器，真引擎
> `laya-multilingual` 在 `:8000`，页面由源码树里的 `playground/server.py` 起在 `:8200` 上对着它跑；
> 之所以用 8200 而不是用户 compose 里那个 8080，是因为 8080 是旧镜像，新页面必须先重建才对得上）。
> **布局**：贪吃蛇与俄罗斯方块的推理面板从 `.game-under` 移进棋盘旁的读数列（`.game-side` 末尾，
> 在"模型读数"下面），`.game-under` 在这两页只剩控制台一行、占满游戏区宽度；恐龙没动——它没有
> 读数列（三行动作就是它那一行），面板仍与控制台并排。守卫不再手抄一张表：`test_every_game_mounts_the_shared_shell`
> 从页面自己声明的 `data-layout` 和 theme.css 里那条把 `.game-under` 压成单列的规则**双向**读出来，
> 页面与样式表不一致就红。几何用
> `.scratch/layout_probe.py` 量（10 次运行 ALL OK）：1440 下贪吃蛇 `.game-under` 1040×123、控制台
> x=200 宽 1040、推理面板 (892,334) 在读数列里，俄罗斯方块同形（棋盘 380×760），恐龙两块面板并排
> (200,572)/(516,572)，≤700 竖排。
> **修掉两个真实缺陷**：一是三个页面的请求体里还带着参考项目留下的 `samples`/`steps`/`seed`——
> 契约里没有这三个字段，而 pydantic 默认 `extra="ignore"`，所以它们一直静默地跟着请求上线；现在
> 请求体只有 `state`/`model`/`questions`（抓手是抓下来的真实请求体：键集合就是这三个，控制台长度
> snake 1439/1437、dino 712/694、tetris 3217/3207 字符），`docs/playground.md` 双向写明。二是新增的
> `/api` 页在 380px 横向溢出（三列表格里 `authentication_error` / `DECIS_REQUEST_TIMEOUT_MS` 这种
> 不可断的词把 `table.fields` 撑到 475px > 380，webcheck 的 audit 报
> `overflows the viewport: table.fields [35..475] vw=380`）：加 `table.fields td { overflow-wrap: anywhere }`
> 后 380 与 320 都 `bad` 全空、对比度 81 个样本 0 不合格。**顺带发现我的量尺有 bug**：
> `window.innerWidth` 在窄档不是布局宽度（Chromium 窗口有约 475px 的平台下限，`--window-size`
> 下不去，emulation 只改 `documentElement.clientWidth`/`visualViewport`），所以 `layout_probe.py`
> 之前拿 `scrollWidth` 比的是 475——一个真溢出的页面会看起来干净。`webcheck.py` 的 audit 一直用的是
> `clientWidth`（因此 final_sweep / uisweep 的历史结论不受影响），`layout_probe.py` 已改成 clientWidth。
> **新页面 `/api`**（`playground/web/api.html`，`server.py` 的 `PAGES` 里同时有 `/api` 与 `/api.html`，
> 因为 `_page_for` 是精确匹配、`/api/config` 在它之前分支，两者不互相遮蔽）：七段——端点是哪个；
> 三个原语各一段 shape；**一对真抓下来的**请求/响应（`.scratch/api_example_{request,response}.json`，页面里逐字嵌入）；一个真的会发的 "try it"（浏览器里实测
> HTTP 200 / 809 ms、带 `x-typesafe-request-id req_6c3ca52c…`、真回答 `decis/laya-multilingual@0.3.6`
> 的 `answers.move.choice`）；参数与上限（四个上限**不印数值**，指向 `GET /v1/models`，并说明这个 origin
> 只代理 `/v1/systemone`）；错误（两张三列表：引擎那 9 行与 `docs/api.md` 的表**逐行对照**，
> 新守卫 `test_the_api_page_lists_the_errors_the_contract_lists` 会红；playground 自己的 400/404/413/502/503
> 另列，且断言的 `error_type` 必须在 `server.py` 里真的出现）；文档链接（`data-docs-link` 挂载，仓库 URL
> 只有 `i18n.js` 一份）。i18n 71 键/语言，浏览器里中英各跑一遍 `bad` 全空。
> **文案**：索引页的状态提示不再印 `上游 http://… · 每一步都是向它发一次 POST`（引擎 id 已经在旁边的
> chip 里、lede 里也说过一次），改成一行"怎么用"：挑游戏、空格开始、游戏栏的开关切手动。
> **共享**：`.shell`/`.hero`/`.hero h1(.grad)`/`.lede` 原来在 index 与 api 两份页面 `<style>` 里各一份，
> 已搬进 `theme.css`，并加守卫 `test_the_reading_column_has_one_home`；i18n 新增 `data-i18n-alt`
> （图片 alt 用）与 `data-docs-link`/`nav.api`/`nav.docs`。
> **录屏**：工具是 `.scratch/record_gif.py`（CDP `Page.startScreencast`，取不到帧就回退到
> `Page.captureScreenshot` 轮询；共享 255 色 MEDIANCUT 调色板；每帧时长 = 真实间隔），
> 三段真页面录屏放在 `playground/web/media/`（snake 1104×1131 / 34 帧 / 204.5 KiB、dino 1104×1059 /
> 52 帧 / 478.8 KiB、tetris 1104×1391 / 12 帧 / 160.5 KiB，都是 AI 档、真引擎、以 Start 那一按开头），
> 两个 README 与索引页卡片都引用它们；`server.py` 补了 `.gif` 的类型，守卫按 GIF 文件头读出真实尺寸
> 跟标签上的 `width`/`height` 比对（`test_the_recordings_the_index_shows_are_in_the_repository_and_served`），
> `docs/playground.md` 双向注明录屏会丢静止帧、并且 Start 上那圈边框是录制工具画的。
> **这一轮的 sweep**：`final_sweep.py --narrow` 30 次运行（五页 × 中英 × 1440/380/700）`bad` 全空
> （顺带修掉那个脚本的一个坑：`START[page]` 遇到新页面直接 `KeyError`，于是上一轮的 JSON 被留在原地、
> 看起来像"这一轮也跑过了"——现在是 `START.get`）；`uisweep.py` 6 次交互运行 `bad` 全空（AI 档真在调用、
> 手动档不再调用、切语言正常，telemetry 有真数：snake 778 ms / 285→34 tok、dino 1542 ms、tetris
> 3100 ms / 1386→198 tok）；**`make build-playground && make up-playground` 之后在用户那个 8080 容器上
> 又跑一遍，同样 `bad` 全空**（snake 532 ms / 283→33 tok、dino 639 ms、tetris 1699 ms / 1414→197 项），
> 也就是说这一轮改的东西真到了那个容器里。
> **playground 未验证**：没有用 `docker compose up` 起过（是 `docker run` 起在等价网络上），
> 所以 compose 的 profile 交互与健康检查只经 `docker compose config` 校验；kev profile 下没跑过；
> 那条 CI job 也还没在 GitHub 上跑过。界面这一层**没有任何人眼看过**（截图在 `.scratch/shots/`，
> 上面那些结论全部来自程序化审计），窄视口只测了溢出与对比度、**没有真机**，
> 引擎"加载中"那一档 chip 也没有对着真的在加载的引擎跑过。

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
uv run pytest -q                     # 无权重测试（CI 跑这个：671 通过 / 33 跳过，约 29 秒）
uv run ruff check && uv run ruff format --check

uv run decis serve --host 0.0.0.0 --port 8000   # 加载默认引擎 laya-multilingual（需要它的依赖与权重）
uv run decis serve --host 127.0.0.1  # 本地开发：回环地址允许不带 token；同样需要引擎就绪
uv run decis models                  # 列出已注册引擎及其在本机是否可用
uv run decis doctor                  # 环境自检：依赖、配置安全性、绑定地址、线程数（不报设备/dtype）
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
uv run decis download --engine laya-multilingual                    # 约 647 MiB，进 Hub 缓存（零配置时 serve 读这里）
uv run decis serve --engine laya-multilingual --host 127.0.0.1     # CPU 冷启动约 75-90 秒
#   想落到挂载目录：`--dest ./models` 写成 ./models/laya-multilingual/，再用 DECIS_MODEL_DIR=./models 服务
#   冷启动期间 /healthz 立即可用，/readyz 报 {"status":"loading"}；加载失败则报 "failed"
uv run pytest -m weights             # 真实推理 + 批不变性（CPU 上约 113 秒）

uv sync --extra kev
uv run decis download --engine kev-0.8b   # adapter 43 MiB + 基座 1.65 GiB
uv run decis serve --engine kev-0.8b --host 127.0.0.1   # CPU 冷启动约 12-45 秒
```

镜像（CI 构建并推送，tag 方案见 `docs/deployment.md`）：

```bash
docker run --rm -p 8000:8000 chaitin/decis:laya-multilingual   # 权重在镜像里，不需要网络也不需要挂卷
docker run --rm -p 8000:8000 chaitin/decis:kev-0.8b
#   想换成自己的权重目录：-v /srv/models:/models，但那个目录里必须已经有 <engine-id>/
#   ——挂在 /models 上会盖掉烤进镜像的权重（§2-D21）。要"权重放卷"就用 release 的
#   <engine>-runtime-<version> 镜像先 `decis download` 填一次卷。
```

本地编排（发布镜像；profile 决定起哪个引擎，选择写在 `.env` 的 `COMPOSE_PROFILES` 里）：

```bash
cp .env.example .env                      # COMPOSE_PROFILES=laya-multilingual
docker compose -f docker-compose.yml up -d --wait   # 只起默认引擎，没有下载也没有预取容器
#   这次 `--wait` 真的等到能作答（compose 层探针打 /readyz，本机 CPU 冷启动约 2 分钟）；
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
make up-local                # 同上，但先重建镜像（引擎那次是全量烤权重，慢）
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
uv run python benchmarks/report.py --write   # 由原始 JSON 生成 README 与 docs 里的表
uv run python benchmarks/report.py --check   # CI 跑这个：手改过的数字会让它变红
```

对外 API 的 JSON Schema 与 OpenAPI 文档（生成物，进 CI）：

```bash
uv run python docs/schema/export.py --write   # 由 src/decis/schema.py 重新生成 docs/schema/*.json
uv run python docs/schema/export.py --check   # CI 跑这个：手改过的 schema 会让它变红
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
需要判断环境就 `pytest.skip` 并说清理由。**给配方一个最小 `PATH` 的 fixture，要把配方可能
exec 的每个程序都放进去**：GNU make 对不含元字符的整行会绕过 shell 直接 `exec`，`@echo` 这种
空行正好落在这一档，而**走不走这条捷径取决于 make 版本**（Ubuntu 的 4.3 走 shell、macOS 的
3.81 不走），于是同一个 fixture 在一条腿上是绿的、在另一条腿上是红的——`tests/test_makefile.py`
里补的那个 `echo` 就是这么被 macOS 那条腿抓出来的。

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
  （会被 `report.py --check` 拦下；要改就改原始 JSON 或重测）。**标记块里的散文同样是生成物**——
  措辞要改就改 `benchmarks/report.py` 里的模板再 `--write`。中文那两句的语病（"随后已经测过"、
  "旁边的散文"）就是这么修的，手改会让 `--check` 变红
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
  都被某处读到，且每个具体的 `DECIS_MODEL_PATH_*` 都规范化到一个已注册的引擎 id
- ❌ 让一行性能表的不同列取自不同配置（`design-review.md §2-D12`：线程数混用曾真实发生过）
- ❌ 手工把一张表从一个文件抄到另一个文件（`README.zh-CN.md` 抄过，于是它**只在中文版里**带着 D12）：
  抄写就是第二处实现（§2），要么生成，要么不要放
- ❌ 报跨请求批处理的结论时省略它的范围：**上界**（合成批、无队列）、**CPU**、**只有 Laya**
- ❌ 用"一个大小测到底"的顺序做批次扫描（序效应会伪装成批大小的效果，`§4-M2` 与 M5 各踩过一次）
- ❌ 增加一个收益类机制却没有正对照或能证明它触发的指标（`§2-D1`）
- ❌ 比较不同进程数时让总线程预算不一致（那测的是线程超配，不是进程扩展性）
- ❌ 用 `A && B || C` 表达"可能为空的三元"（GitHub 表达式里没有三元运算符，而**空字符串是假值**，
  所以"没有 extra"这种分支永远选不中：当时那个无权重镜像因此装上了 Laya + torch，多出 3.1 GB，
  见 `design-review.md §2-D14`；那个镜像已随假引擎一起移除）。选一个可以合法为空的值时，
  用真正的分支，或把决定搬到脚本里
- ❌ 让测试里的映射表是**手抄的常量**而不是从被执行的那份东西读出来的
  （`design-review.md §2-D14`：表达式错了、抄本对了，于是测试恒绿地放过了一个 3.1 GB 的缺陷）
- ❌ 在**共享仓库**里让多个构建/合并腿写同一个 tag（`design-review.md §2-D16`：
  per-arch 中间 tag 少了 artifact 名，六个腿互相覆盖，三个 tag 发出同一个 manifest）。
  检查"我这条腿的 tag 对不对"是不够的，必须有一条测试断言**任意两条腿的 tag 集合不相交**
- ❌ 在文档里写出一个**没被任何测试对照过**的镜像 tag / URL / 文件名
  （`design-review.md §2-D15`：README 曾写一个实际不存在的镜像 tag，照着文档拉的第一条命令就失败）。
  凡是文档里出现的、由 CI 产出的名字，都要有一条测试从**真正产出它的那段脚本**里读出来比对
- ❌ 让"预取/下载"写到一个 loader 不会去读的目录（`design-review.md §2-D17`：`decis download`
  用 `local_dir` 复制的是**仓库布局**，而 `paths.resolve` 只认 `<DECIS_MODEL_DIR>/<engine id>/`，
  于是这条命令对**任何** `--dest` 都失败，`DECIS_PREDOWNLOAD` 离线镜像也从没构建成功过）。
  下载完必须用 `paths.resolve` 本身验证；测试必须让 stub 写出**真实下载器的布局**，
  而不是只断言"传了哪些参数"——那是把写和读之间剪断还宣称它们连着
- ❌ 在 compose 的 `command:` 里只写镜像 `CMD` 的后半截（compose 的 `command:` **替换** `CMD`
  而不是追加：`Dockerfile` 没有 `ENTRYPOINT` 时容器会去 exec 一个叫 `download` 的程序，
  `design-review.md §2-D18`）。测试要**从 Dockerfile 读**入口点再决定断言什么，
  不要把当前写法当常量抄一遍——那是把错误的期望固化成守卫
- ❌ 让容器的 liveness 探针继承环境里的代理（`urllib` 读 `HTTP_PROXY`，于是探针去问代理要
  `http://127.0.0.1:8000/healthz`、拿到 502，一个 `ready after 86.1s` 的服务被判成 unhealthy，
  Kubernetes 会一直重启它：`design-review.md §2-D19`）。探针命令必须 `env -u` 掉代理变量，
  并且有一条测试盯住这一点
- ❌ 让**引擎名那个 tag** 指向不带权重的镜像（`design-review.md §2-D20`：一个引擎的镜像存在的
  意义就是"拿到就能用"，而 README 曾把它写成"需要网络或挂卷"，同时真正的烤权重变体叫 `-offline`
  并且从没构建成功过）。默认变体必须是烤权重那个，瘦身变体只能带后缀；同一个东西不给两个名字
- ❌ 在 compose 里把卷/目录挂到镜像烤权重的路径上（`DECIS_MODEL_DIR`，`design-review.md §2-D21`：
  命名卷会用镜像内容初始化一次然后自己留一份，bind mount 直接盖掉整个目录，于是"离线镜像"变成
  "启动就联网下载"，而且不报任何错）。守卫必须**从 Dockerfile 读出**那个路径再断言没人挂它
- ❌ 在编排器里把 liveness 探针指向 `/readyz`（`design-review.md §2-D22`：镜像还在加载引擎就被判
  不健康，等于每次冷启动都重启一遍）。这个分工的例外只有 compose——它不会因为探针失败重启容器，
  所以那里的探针**就是**就绪探针，`--wait` 才有意义
- ❌ 以为 `.env` 里的网络配置会进入构建步骤（`design-review.md §2-D23`：`env_file` 只作用于容器，
  Docker 也不转发 shell 的 `HTTP_PROXY`，于是"依赖装好了、权重下不来"，而 `.env.example` 曾
  建议在那里设代理来跑构建）。构建要用 `--build-arg` 传，文档必须写成两条路

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
