# Week1 · 1-1 —— Mini Coding Agent（LangChain / LangGraph 单文件版）

这一版是对上一版的简化：不再拆成一堆各自独立、互不调用的文件（adapter、
contract、context_budget、结构化输出 demo、采样参数 demo……），而是把
"真正会被用到"的部分合并成了**一个能直接跑起来的项目** —— `agent_loop_demo.py`。

## 这一版做了什么改动

1. **去掉了 Adapter 这层抽象类。** 之前用一个 `LangChainModelAdapter` 类
   包装模型调用，现在直接拿 LangChain 的聊天模型来用。"切供应商"就是
   `get_chat_model()` 里换一个模型类的构造代码，不需要专门写一个类去做
   协议翻译（这层翻译本来就是 LangChain 已经帮你做掉的部分）。

2. **重试逻辑收窄成两件事**，对照的是课件第 19 页那张表：

   | 阶段 | 典型问题 | 是否重试 | 本项目里的处置 |
   |---|---|---|---|
   | 请求前 | 参数 / Schema 不合法 | 否 | `BadRequestError` -> 直接抛出，不重试 |
   | 认证 | Key 无效 / 权限不足 | 否 | `AuthenticationError` -> 直接抛出，不重试 |
   | 传输 | 网络中断 / 连接超时 | 有边界 | `APIConnectionError` / `APITimeoutError` -> 指数退避 + jitter 重试 |
   | 服务端 | 429 限流 / 5xx 过载 | 有边界 | `RateLimitError` / `InternalServerError` -> 指数退避 + jitter 重试 |
   | 模型输出 | 被截断 / 格式错（JSON/Schema 解析失败） | 有边界 | 把校验错误喂回模型（retry-with-feedback），让它自己修正后重新生成 |

   对应代码：`is_retryable()` 负责判断是否值得重试，`call_with_retry()`
   负责"网络 / 限流 / 过载"这类 API 报错的退避重试，`ask_for_action()`
   里的 for 循环负责"模型输出格式不对"时的 retry-with-feedback。

3. 删掉了和"能跑起来的 Agent"无关的独立 demo（`context_budget.py` 的
   上下文预算演示、`temp_top_p.py` 的采样参数对照实验）。这两个概念仍然
   值得了解，但它们本来就不是 Loop 运行时必须的一部分，硬塞进项目里反而
   让代码变复杂 —— 如果之后需要，可以单独再要。

## 文件

```
agent_loop_demo.py   # 唯一的代码文件，从上到下分四段：
                      #   1) 模型工厂 get_chat_model()
                      #   2) 错误分类 is_retryable() + call_with_retry()
                      #   3) 结构化输出契约 AgentAction + ask_for_action()
                      #   4) LangGraph 决策 Loop build_agent_loop()
requirements.txt
.env.example
README.md
```

## 安装

```bash
cd /Users/lzxx/Desktop/pj/deepflow/week1/1-1
pip install -r requirements.txt
```

需要 Python 3.10+。

## 配置环境变量

```bash
export MODEL_PROVIDER=deepseek        # 或者 openai
export DEEPSEEK_API_KEY=sk-xxxxxxxx
# 如果用 openai：
# export MODEL_PROVIDER=openai
# export OPENAI_API_KEY=sk-xxxxxxxx
```

## 运行

```bash
python agent_loop_demo.py
```

把 `MODEL_PROVIDER` 从 `deepseek` 换成 `openai`（连带把对应的
`*_API_KEY` 设好）再跑一次，`agent_loop_demo.py` 不需要改一行代码 ——
这就是"Loop 依赖能力，不依赖供应商"在这份代码里的体现，即使去掉了
Adapter 类，这个特性也还在，因为它本来就是 LangChain 提供的，不是
Adapter 类提供的。

## 已经验证过的地方

- `is_retryable()` 对认证错误、参数错误、限流、5xx、网络中断、超时
  六种情况的判断，已经用真实的 `openai` 异常类型逐一测试过，结果和上面
  的表格完全一致。
- `ask_for_action()` 的 retry-with-feedback 逻辑用一个假的结构化输出
  模型测试过：第一次解析失败会把错误信息喂回去，第二次成功后正常返回，
  行为符合预期。

## 拓展知识：max_tokens、temperature 与 thinking 模式、重试参数设计

这一节记录几个当前代码里没直接用到、但理解 LLM API 调用绕不开的概念，方便以后调参数时不踩坑。

### 1. `max_tokens` 到底限制的是什么

普通场景下，`max_tokens`（Chat Completions）/ `max_completion_tokens`（新版接口）**只限制模型这次"生成"出来的 token 数**，也就是回复内容的长度，跟你发过去的 prompt（输入）有多长没有关系——输入再长，也不占这个额度，它们是分开计费、分开限制的两个维度。

但一旦模型开启了 **thinking / 推理模式**（比如 DeepSeek 的 `deepseek-reasoner`、OpenAI 的 o 系列 / GPT-5 系列），情况会变化:这个 token 上限会**同时覆盖"看不见的思考过程（reasoning/思维链）"和"最终展示给用户的正式回答"**,OpenAI 官方文档原话是"reasoning tokens、visible output tokens、formatting tokens 都算在这个上限里"。

实际影响是:如果开了 thinking 模式,又把 `max_tokens` 设得太小,模型可能把整个额度都花在"思考"上,还没来得及写正式回答就被截断,你拿到的结果可能是空的或者不完整的——而且这不是报错(不会走到 `is_retryable` 那套逻辑),只是正常返回了一个内容不完整的响应,容易被误以为是模型能力问题,其实是参数设置的问题。本项目默认用的是 `deepseek-chat`(不是推理模型),暂时不受这个影响,但如果之后把 `get_chat_model()` 里的模型换成 `deepseek-reasoner` 或者 OpenAI 的推理模型,就要把 `max_tokens` 留出足够的"思考预算"。

### 2. `temperature` 和 thinking 模式的关系

`temperature` 本身是控制采样随机性的参数:值越高,输出越随机、越有"创造性";值越低,输出越确定、越倾向于选概率最高的那个词。这套逻辑在普通(非推理)模型上是成立的。

但**thinking 模式下,`temperature` 基本是被禁用或者直接不生效的**,两家的做法略有差别:

- DeepSeek 的 thinking 模式:官方文档明确写了"`temperature`、`presence_penalty`、`frequency_penalty` 这几个参数在思考模式下不生效",你传了也不会报错,只是模型会忽略它,内部走自己固定的采样策略;`top_p` 倒是部分生效,但有个下限——低于 0.95 的值会被自动提到 0.95。
- OpenAI 的推理模型(o 系列、GPT-5 系列,除了 GPT-6 Astra 这个例外):做得更严格,直接**不支持**这几个参数,你传了 `temperature` 会直接报 `BadRequestError`(400),而不是像 DeepSeek 那样默默忽略。

背后的原因:thinking 模式本质上是模型在内部做多步骤的自我探索、验证、修正,这套"结构化推理"的训练方式本身已经决定了它该怎么一步步展开思路,再在最外层叠加一个"随机撒点噪声"的采样参数,只会干扰这套内部机制,对最终答案质量没有好处,所以厂商干脆把这个开关在推理阶段关掉或直接禁用,把"随机性/创造性"这个旋钮留给不需要深度推理的常规对话场景。

这一点跟前面聊过的错误分类也接上了:如果以后往这个项目里换 OpenAI 的推理模型,又不小心保留了 `temperature=xxx` 这种参数,触发的正好是 `BadRequestError`——对照 `is_retryable()` 的判断,这类错误是"本地就能确定,重试也没用"，需要你自己去掉这个参数,而不是指望重试机制帮你兜底。

### 3. 各类异常对应的 HTTP 状态码

`is_retryable()` 里用到的几个 `openai` 异常类,背后各自对应一个 HTTP 状态码(DeepSeek 走的是 OpenAI 兼容协议,状态码含义完全一致):

| 异常类 | HTTP 状态码 | 服务器是否返回了响应 |
|---|---|---|
| `AuthenticationError` | 401 | 是 |
| `BadRequestError` | 400 | 是 |
| `RateLimitError` | 429 | 是 |
| `InternalServerError` | 500 / 503 | 是 |
| `APIConnectionError` | 无 | 否——请求根本没到达服务器,或连接失败 |
| `APITimeoutError` | 无(不确定) | 不确定——请求发出去了,但客户端等超时就放弃了,不代表服务器没处理完 |

也就是说,表格里前四个是服务器真正处理了请求、并明确告诉你"哪里错了";后两个是客户端自己判断"连不上"或"等太久",压根没拿到一个可以看状态码的正常响应。

### 4. 退避重试为什么一定要加 jitter

如果只做"指数退避"、不加随机抖动,会有一个隐藏问题:假设是限流(429)或者服务过载(5xx)导致失败,同一时刻很可能不止一次调用受影响(比如 Agent 内部并发跑了好几个任务,或者很多用户在用同一个 API Key)。如果大家都用同一套固定的退避时间表(比如失败后统一等 1 秒、下次统一等 2 秒),那么这些请求会**在完全相同的时间点同时醒来、同时重试**,再次一起打到已经过载的服务器上,相当于人为制造了一波新的流量高峰,很可能又触发一次限流或过载,进入"重试 -> 再次撞墙 -> 再重试"的恶性循环,业内把这个现象叫"重试风暴"(retry storm)/ "惊群效应"(thundering herd)。

加 jitter(本项目里是 `delay += random.uniform(0, delay * 0.25)`,给退避时间额外叠加最多 25% 的随机量)之后,每个请求实际等待的时间都不完全一样,重试请求会被"打散"到一个时间窗口里,而不是全部挤在同一个时间点,大大降低了大家再次同时冲击服务器的概率,是分布式系统里做退避重试的标准做法。

### 5. 为什么参数是 `max_attempts=3`、`base_delay=0.5`、`max_delay=8.0` 这么设置

这几个数字背后是"给暂时性故障一点恢复时间,但不能让用户等太久"的权衡:

- `base_delay=0.5` 秒:第一次失败后只等半秒,是因为大部分真正的"暂时性故障"(比如网络抖了一下)几乎是瞬间自愈的,不需要等太久就能拿到结果,等太久反而拖慢了正常场景下的响应速度。
- 每次翻倍(`2 ** (attempt - 1)`):是标准的指数退避思路——如果第一次重试还是失败,说明问题可能不是"抖一下"这么简单,那就给服务器更多喘息时间再试,避免越败越试、越试越加重服务器负担。
- `max_delay=8.0` 秒:给指数增长设一个天花板,防止重试次数多的时候等待时间无限膨胀(比如翻倍到第 5、6 次可能就是几十秒、上百秒),8 秒是"还能接受的等待上限"和"给服务器足够恢复时间"之间的折中。
- `max_attempts=3`:这是一个 Agent Loop,用户/上层调用方大概率是在同步等结果,不是丢到后台慢慢跑的批处理任务,所以重试次数不能设太多——3 次(加上两次退避,最坏情况也就多等 1.5 秒左右的 sleep 时间,不算真正的网络等待)是"给暂时性故障几次机会"和"避免用户等待过久"之间的一个常见折中选择,如果这是一个可以容忍长时间等待的后台任务,可以适当调大。

## 已知的取舍（教学版，不是生产版）

- "拒答"（模型主动拒绝回答）没有单独识别和处理，目前会和"格式错"一样
  走 retry-with-feedback 兜底重试几次，如果真的是拒答，重试大概率还是
  会失败，最终会抛出 `MODEL_SCHEMA_INVALID`。生产环境如果需要区分拒答，
  可以检查 `finish_reason == "content_filter"`，单独判定为不重试。
- 结构化输出场景下 `temperature` / `top_p` 之类采样参数没有暴露出来，
  因为这一版的重点是"错误分类 + 重试"，不是"参数调节"；如果需要，
  在 `model.with_structured_output(...)` 之前对 `model` 调用
  `.bind(temperature=..., top_p=...)` 即可加回来（注意上面第 2 节提到的
  限制:如果换成推理模型,这个参数可能会被忽略甚至报错）。
