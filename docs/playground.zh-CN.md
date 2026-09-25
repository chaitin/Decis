# Playground

[English](playground.md) · **简体中文**

[文档索引](../README.zh-CN.md#文档) · [部署](deployment.zh-CN.md) · [API](api.zh-CN.md)

playground 是三个浏览器小游戏，你可以自己玩，也可以交给一个正在运行的引擎。AI 档下每一步都是
一次真实的 `POST /v1/systemone` 请求，概率条就是模型真实的答案，所以你可以看着一个决策模型玩游戏，
而不是读 JSON。

`docker compose up` 会把它和引擎一起起在 <http://localhost:8080>。

| 页面 | 每次决策问什么 |
|---|---|
| `/snake` | 一次 `choice`，在 flood-fill 安全分析给出的四个方向里选 |
| `/dino` | 一次 `choice`，在物理规划器标出的 jump/duck/run 里选 |
| `/tetris` | 一次从五个落点中选一个的 `choice`、一个 `choice`（策略）、一个 `score`（堆叠健康度）、一个 `noul`（契合度） |

在只有 CPU 的机器上，游戏比最初的 GPU 部署慢，每个游戏都按自己测到的延迟来调整节奏。调用失败时
页面不会把它装成模型的决定：贪吃蛇跑自己的模拟并把状态写成 `运行中（本地模拟）`，恐龙显示
`API 错误` 标记并让规划器的安全兜底继续生效，俄罗斯方块退回本地启发式或重试。无论哪种，控制台里
都留着那次请求和错误。

## 它是怎么接起来的

playground 是代理，不是客户端：

```
browser ──POST /v1/systemone──▶ playground ──POST /v1/systemone──▶ engine
        (same origin, no key)              (attaches DECIS_API_KEY)
```

浏览器永远不持有 API key，也不需要：它请求的是 playground 自己这个源，token 由 playground
在服务端附上。由此有两个后果，两个都是有意为之：

- **谁能访问 playground 的端口，谁就能用这个模型。** 发布它就等于发布引擎的端口，只是连
  token 都不用填。默认绑定所有网卡；`DECIS_PLAYGROUND_HOST_PORT=127.0.0.1:8080` 可以把它留在本机。
- **playground 的端口就是模型的端口。** playground 会把请求里的 `model` 改写成引擎在
  `/readyz` 上报出的 id，所以页面可以发 `jev-latest`，在两个引擎 profile 下都能用。

### 找到引擎

playground 找引擎不需要任何配置：它按候选清单依次打 `/readyz`，谁先答就连谁：

1. `http://laya-multilingual:8000`
2. `http://kev-0.8b:8000`
3. `http://host.docker.internal:8000`
4. `http://127.0.0.1:8000`

在 Compose 下这份清单被显式设成前三个（`docker-compose.yml`），所以 `127.0.0.1` 只在 playground
直接跑在宿主机上时才会被试到——这就是同一份服务在 `--profile kev-0.8b` 下、以及在宿主机上跑
`decis serve` 时都能用的原因。引擎还在加载时，playground 报 `searching`，游戏也会如实显示而不是
直接报错。设 `DECIS_PLAYGROUND_UPSTREAM` 可以直接指定引擎、跳过搜索，设
`DECIS_PLAYGROUND_CANDIDATES` 可以替换整个候选清单。

它不 `depends_on` 任何引擎：没启用的 Compose profile 其服务名根本不解析，所以依赖一个没在跑的
引擎会直接起不来。

## 界面

五个页面共用一份样式表（[`playground/web/theme.css`](../playground/web/theme.css)）、一套 i18n
机制（[`playground/web/i18n.js`](../playground/web/i18n.js)）和一个外壳
（[`playground/web/game.js`](../playground/web/game.js)）。页面只带自己的文案、自己的棋盘和自己的
选项；手动/AI 开关、推理面板、"最近一次调用"的控制台、引擎状态灯和快捷键都来自外壳，所以没有任何
一页重抄调色板，也没有任何一页自己再实现一遍模式开关。五个页面里三个是游戏，另外两个是索引页和
`/api`（[`playground/web/api.html`](../playground/web/api.html)）——后者是给人看的 API 参考：端点是哪个、
三个原语分别长什么样、一对从贪吃蛇页面抓下来的真实请求/响应、一个可以自己发一次的输入框、参数、
上限和错误。索引页还给每个游戏配了一段短录屏
（[`playground/web/media/`](../playground/web/media/)），都是在这几个页面上以 AI 档实录的；画面没变化的
帧会被丢掉。

三个游戏的阅读顺序是一样的。游戏栏带标题、实时状态、得分和操作按钮；它下面是棋盘、模型读到的东西
和推理面板；再往下是"最近一次调用"的控制台，**默认展开**，左边请求右边响应。推理面板放哪儿由棋盘的
形状决定：贪吃蛇和俄罗斯方块在棋盘旁边有一列读数，面板就放在那一列的末尾，最下面一行只剩控制台、
占满游戏区的宽度；恐龙的画布是横向的，占满整行、模型那三个动作并排放在它下面，所以它的推理面板和
控制台共用最下面那一行。只想看棋盘的话，点一下就能把控制台折成一行。

一个开关（`M`）决定谁来玩：你，或者模型。`回车` 在每一页都是开始/暂停，`R` 重置。手动档下贪吃蛇和
俄罗斯方块用方向键——俄罗斯方块里 `空格` 是硬降——恐龙用 `空格` 或上方向键跳、下方向键蹲。

推理面板只由 API 自己报的 `usage` 和浏览器的时钟喂出来。契约里没有服务端计时，所以延迟与吞吐都是
端到端的；p50 和 p95 描述最近 200 次调用；还没调用过的页面显示破折号，而不是一个看起来很合理的数。
控制台印的是最近一次 `/v1/systemone` 调用**发出去时的请求体**，旁边就是响应或错误。那个请求体就是
契约定义的那三个字段——`state`、`model`、`questions`：参考项目还会发 `samples`、`steps` 和 `seed`，
现在都去掉了，所以从控制台抄出来的东西就是 [`docs/api.md`](api.md) 里写的那份。

界面是中英双语的。默认按 `navigator.languages` 选，`?lang=zh` 可以指定；顶栏的开关会覆盖它，
选过之后记在 `localStorage` 里。

**只有界面被翻译。** 发给模型的 `state`、`instructions` 和 `criteria` 仍然是英文，因为提示词
就是用英文写的，而且这些页面的 token 预算正是按那些字符串量出来的。页面要显示某个同时也要发
出去的值时——选项名、落点 id——它周围的标签可以本地化，但线格式的那个值本身不动。

## 落点短名单

**只问五个，不是六个。** Laya 会把整个问题都算进 `head_max_len`，所以选项清单必须装得进 Decis
支持的最小问题预算（192 token，checkpoint 没有声明任何上限时的兜底值）。五个是页面上每个数字
被测量时用的那套配置，而下面这次判据改写是把预算省下来而不是花掉。每个旋转在短名单里都有代表，
所以模型既在挑旋转，也在挑列。

**判据是词，不是数字。** 一个选项长这样：`clears two lines, no new holes, keeps the stack very
low.`，它的名字是 `option_a`..`option_e`，而不是它自己的旋转和列。这两半都是对运行中的引擎测出来
的，不是风格问题：用这个页面以前发的数字写法（`cols 1-3: 0 clear, 0 new holes, height 2,
flat`）时，模型只是取列表里**最后**那个落点，两个种子各 60 个真实棋盘上分别只选中页面自己认为
最好的落点 0 次和 1 次；换成上面的写法后，它有 87% 和 93% 的时候选中那个落点，每 60 个方块清
21-23 行，与页面自己的规划器持平。旋转和列仍然由推理面板显示为 `r2 · c4`。完整的对照测量写在
[`playground/web/tetris.html`](../playground/web/tetris.html) 里 `describePlacement` 上方的注释中。

## 致谢

游戏改编自开源项目，API 与推理是 Decis 的：

| | |
|---|---|
| [taeold/djev-run](https://github.com/taeold/djev-run) | Snake、Dino 和 Tetris 三个页面。Decis 把它们的请求地址改成 playground 自己的源，删掉借来的延迟基线，把 Tetris 从 16 个冗长选项压到 5 个精简选项以适配默认引擎的 token 预算，并重新设计样式、翻译了界面。游戏里的物理与规划代码来自那个仓库致谢的项目。 |
| [trungdq88/jev-tetris](https://github.com/trungdq88/jev-tetris) | Tetris 页面也致谢了它。 |
| [chaitin/Decis](https://github.com/chaitin/Decis) | 服务端：System One 线格式、引擎抽象、模型推理与 token 计量。playground 只渲染游戏并转发它们的请求，不拥有任何决策逻辑。 |

署名记录在 [`NOTICE`](../NOTICE) 里。

## 自己构建并运行

playground 的镜像没有任何 Python 依赖，它的 Dockerfile 里也没有 `RUN`，所以几秒就能构建完：

```bash
docker build -f playground/Dockerfile -t decis-playground .
docker run --rm -p 8080:8080 -e DECIS_API_KEY=change-me \
  --add-host=host.docker.internal:host-gateway \
  -e DECIS_PLAYGROUND_CANDIDATES=http://host.docker.internal:8000 decis-playground
```

`--add-host` 是让 `host.docker.internal` 在 Linux 上能解析的原因；Docker Desktop 自己会解析。
`DECIS_PLAYGROUND_UPSTREAM` 指定一个先试的引擎（仍然要探测，不是无条件信任）。不设它时，代理
会依次尝试两个引擎容器名、`host.docker.internal` 和 `127.0.0.1`；第一个 `/readyz` 返回 200 的
胜出。

或者通过 Compose，接着一个已经跑在任何地方的引擎：

```bash
make build-playground
make up-playground
```
