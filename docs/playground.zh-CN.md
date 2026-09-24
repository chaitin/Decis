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

所以同一份服务在 `--profile kev-0.8b` 下、以及在宿主机上跑 `decis serve` 时都能用。引擎还在
加载时，playground 报 `searching`，游戏也会如实显示而不是直接报错。设
`DECIS_PLAYGROUND_UPSTREAM` 可以直接指定引擎、跳过搜索，设 `DECIS_PLAYGROUND_CANDIDATES`
可以替换整个候选清单。

它不 `depends_on` 任何引擎：没启用的 Compose profile 其服务名根本不解析，所以依赖一个没在跑的
引擎会直接起不来。

## 界面

五个页面共用一份样式表（[`playground/web/theme.css`](../playground/web/theme.css)）、一套 i18n
机制（[`playground/web/i18n.js`](../playground/web/i18n.js)）和一个外壳
（[`playground/web/game.js`](../playground/web/game.js)）。页面只带自己的文案、自己的棋盘和自己的
选项；手动/AI 开关、推理面板、"最近一次调用"的控制台、引擎状态灯和快捷键都来自外壳，所以没有任何
一页重抄调色板，也没有任何一页自己再实现一遍模式开关。五个页面里四个是游戏，还有一个 `/api`
（[`playground/web/api.html`](../playground/web/api.html)）是给人看的 API 参考：端点是哪个、三个
原语分别长什么样、一对从贪吃蛇页面抓下来的真实请求/响应、一个可以自己发一次的输入框、参数、上限和
错误。索引页还给每个游戏配了一段短录屏（[`playground/web/media/`](../playground/web/media/)），
都是在这几个页面上以 AI 档实录的。录屏工具（`.scratch/record_gif.py`）会丢掉画面没变化的帧，
并在按下 Start 的那一帧给按钮描一圈边框——那圈边框是录制工具画的，不是页面画的。

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

Tetris 到底问哪些落点，是容量问题不是口味问题，而这问题的两半都是量出来的。

**问几个。** Laya 会把整个问题都算进 `head_max_len`，超预算就拒绝而不是截断，所以页面按 checkpoint
可能退回到的最小预算来定这个数量。用引擎自己的 `measure()` 实测：落点问题在五个选项下、空棋盘上是
**178 token**，棋盘堆起来之后最坏 **185**（两个 AI 会话共 45 个真实问题——棋盘越满，选项那行字越长）；
整个请求从 **415 涨到 475**，而声明不了任何上限的 checkpoint 兜底预算正是 512 的
`state + 问题`。六个落点约 207，会被 422 拒掉。引擎仍然拒绝时页面会退回自己的启发式，而不是把一个
猜测当成答案显示出来。

**问哪些。** 把合法落点按页面自己的启发式排序取前五，并不等于给出五个决策：棋盘平的时候，启发式会
连着偏好同一个朝向的五个列位，于是这个页面发出的第一个请求里五个选项**全是 `rot2`**——模型只在挑列，
没在挑落点，而这在旁观者眼里就是"它根本不会旋转"。现在页面先把**每个旋转**里最好的那个落点放进去，
剩下的名额再给其余落点里最好的；只有一种旋转的 `O` 自然就是同一朝向的五个落点。改动后头 26 个真实
问题实测：23 个给出两种以上旋转、11 个四种都给到了，而只给一种的那些全是 `O`。每个选项的键写着它是
哪个旋转（`rot<R>_col<C>`，`R` = 方块顺时针转 90 度的次数），面板里同一个旋转也显示在选项的进度条旁边。

改了页面的问题之后重新量并不需要 tokenizer：超预算的问题会以
``Question 'placement' is about N tokens, over this model's limit of M per question`` 的形式返回，
而成功的响应会在 `usage.input_tokens` 和 `decis` 命名空间里报出同一组数字。把页面自己发的请求体
发一次，从控制台里读出来即可。

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

## 测试

[`tests/test_playground.py`](../tests/test_playground.py) 在回环地址上起一个 playground 和一个
假引擎，并在不 import `decis` 的前提下检查代理，因为 playground 的镜像里没有它。它断言页面是
自包含的、每个页面问到的文案都已声明、三个游戏挂的是同一个外壳而不是各自实现模式开关或轮询引擎、
转发出去的请求带的是 playground 的 token 和改写后的 model id，以及仓库 URL 只有一处。
