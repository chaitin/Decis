# 中秋快乐：三条命令，在你自己的 Mac 上跑起 Jev 的开源平替

> **Decis —— 一个 API，跑所有轻量决策模型。**
> <https://github.com/chaitin/Decis>

明天就是中秋了，不聊排期，聊点好玩的。

最近我们在做一个叫 **Decis** 的东西：它说的是 TypeSafe System One 那套 `/v1/systemone` 契约——也就是 **Jev 的 API**——后面接的却是开源决策模型。现在仓库已经开源，镜像也推到了 Docker Hub。

我不想用 PPT 介绍它。了解它最快的方式，是把服务跑起来，然后让模型替你打三局游戏。

## 第一步：跑起来（真的就三条命令）

Mac 上装好 Docker Desktop 或 OrbStack，然后：

```bash
git clone https://github.com/chaitin/Decis && cd Decis
cp .env.example .env
docker compose -f docker-compose.yml up -d --wait
```

然后打开 <http://localhost:8080>。

就这么多。几个小说明：

- 第一次要拉 **4.4–4.5 GB** 的镜像，因为**权重是烤进镜像里的**——之后启动不再需要联网、不需要挂卷、也不需要多一个下载容器。这就是"开箱即用"的字面意思。
- 镜像是 **amd64 + arm64 双架构**的，M 系列芯片拉到的是原生 arm64。
- `-f docker-compose.yml` 不是装饰：不加它，Compose 会看到仓库里的 override 文件，转而用源码给你**本地构建**（慢，而且要先把权重烤一遍）。只想体验就别加。
- `--wait` 会一直等到模型真的能作答，CPU 上冷启动一分钟上下，第一次别慌。想自己确认一下：

```bash
curl -s localhost:8000/readyz     # {"status":"ready","engine":"laya-multilingual"}
```

- 模型加载后常驻 4–5 GB，Docker Desktop 建议分到 8 GB 以上内存。
- 玩完了：`docker compose -f docker-compose.yml down`。
- 只想要推理服务、不要游戏：`docker compose -f docker-compose.yml up -d --wait laya-multilingual`。

## 你会看到什么

三个网页小游戏——**贪吃蛇、恐龙、俄罗斯方块**——外加一个专门讲 API 的 `/api` 页。

每一局你都可以自己玩，也可以把开关打到 AI，看模型玩。AI 档下**每一步都是一次真实的 `POST /v1/systemone`**：贪吃蛇问"往哪走"，恐龙问"跳、蹲还是跑"，俄罗斯方块一次问四件事（落点、策略、堆叠健康度、契合度）。推理面板上的延迟和吞吐只由 API 自己报的 `usage` 加上浏览器时钟算出，不是画上去的；"最近一次调用"的控制台会把**真正发出去的请求体**摊开给你看，可以直接对照 API 文档抄。

| 贪吃蛇 | 恐龙 | 俄罗斯方块 |
|---|---|---|
| ![贪吃蛇](https://raw.githubusercontent.com/chaitin/Decis/master/playground/web/media/snake.gif) | ![恐龙](https://raw.githubusercontent.com/chaitin/Decis/master/playground/web/media/dino.gif) | ![俄罗斯方块](https://raw.githubusercontent.com/chaitin/Decis/master/playground/web/media/tetris.gif) |

## 它其实是个正经服务

软文也得说人话。Decis 的四个卖点：

**1. 开箱即用。** 一个引擎一个镜像，**引擎就是 tag**，所有引擎共用一个 Docker Hub 仓库。以引擎命名的那个 tag 一定带着权重，`docker pull` 下来就能作答，不需要网络、卷或第二个容器。

**2. Jev-like API。** 官方的 `typesafe-sdk` **一个字节都不用改**，只换 `base_url`：

```python
client = TypeSafeClient(api_key="local", base_url="http://127.0.0.1:8000")
```

`choice` / `score` / `noul` 三种原语、错误形状、`x-typesafe-request-id` 请求头都对得上。这份契约不是我们猜的：仓库里有官方 OpenAPI 快照，也有对着线上服务抓下来的原始返回记录，每条结论都标了证据等级。

**3. 默认支持 Laya 与 kev 的 Docker 推理服务。**

```bash
docker run -p 8000:8000 -e DECIS_API_KEY=change-me chaitin/decis:laya-multilingual
docker run -p 8000:8000 -e DECIS_API_KEY=change-me chaitin/decis:kev-0.8b
```

`laya-multilingual` 是默认引擎（mmBERT-base，100+ 种语言），CPU 上就能跑；`kev-0.8b`（Qwen3.5-0.8B + LoRA + pointer head）建议有 NVIDIA GPU 再上，CPU 上要慢一个数量级。想试 kev：

```bash
docker compose -f docker-compose.yml --profile kev-0.8b up -d
```

**4. 三种游戏的 playground。** 就是上面那三个，外加 `/api` 参考页——端点是哪个、三个原语各长什么样、一对真抓下来的请求/响应、一个真的会发出去的输入框。

## 中秋彩蛋：来比一比谁的电脑快

有同事在自己的 **MacBook M1 Pro** 上跑起来之后惊呼"**快得不真实**"——他那边客户端端到端延迟大概 **70ms**。

我不信邪，在我这台**鲲鹏 920** 上跑了一把：**800ms** 上下。

……嗯，也很快，快到我以为网络断了。

所以中秋放个榜：**欢迎把自己推理面板上的数字截图发出来**（p50 延迟、吞吐 tok/s 都行），看看谁的本子最能打。作为参考，我们在 24 vCPU 的 aarch64 CPU 机器上实测默认引擎单题 **231 ms**、一次问十题平均 **98.7 ms/题**、冷启动约 **73 秒**——这些数字来自仓库里签入的 benchmark 原始 JSON，不是手写的。

两点先说好，免得互相反杀：

- 面板上的延迟是**客户端端到端**的，包含网络和排队；要比就比同一个游戏、同一档问题。
- 比的是"本地跑得爽不爽"，不是吞吐上限。真正的吞吐我们测出来是**跨请求批处理在 CPU 上不提升**，这条负结论也在仓库里。

## 最后

中秋快乐，月饼多吃两块。

如果你只是想安安静静看模型玩一局俄罗斯方块，上面那三条命令就够了。如果你手里正好有一段 Jev 调用，想拿开源模型顶一下，那就是改一行 `base_url` 的事。

- 仓库：<https://github.com/chaitin/Decis>
- 上手：<https://github.com/chaitin/Decis/blob/master/docs/getting-started.zh-CN.md>
- API：<https://github.com/chaitin/Decis/blob/master/docs/api.zh-CN.md>
- 部署：<https://github.com/chaitin/Decis/blob/master/docs/deployment.zh-CN.md>
- 引擎：<https://github.com/chaitin/Decis/blob/master/docs/engines.zh-CN.md>
- 性能实测：<https://github.com/chaitin/Decis/blob/master/docs/performance.zh-CN.md>

> 致谢：Jev（TypeSafe AI）是我们对标的接口；kev（Jared Palmer）与 Laya（Convai Innovations）提供了模型与推理内核；playground 的三个游戏改编自 djev-run（Daniel Lee）。

---

## 附：可以直接发群里的短版

> 中秋彩蛋🎑 我们开源的 Decis 现在可以三条命令在 Mac 上跑起来：`git clone` → `cp .env.example .env` → `docker compose -f docker-compose.yml up -d --wait`，然后打开 http://localhost:8080，就能看开源版 Jev 风格模型（Laya / kev）替你打贪吃蛇、恐龙和俄罗斯方块。API 和 Jev 的 `/v1/systemone` 一样，官方 SDK 只改一行 `base_url`。有同事的 M1 Pro 跑出 70ms 延迟，惊呼"快得不真实"；我的鲲鹏 920 是 800ms 😇 欢迎截图推理面板来挑战。仓库：https://github.com/chaitin/Decis
