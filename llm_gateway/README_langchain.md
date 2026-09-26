# LLM Gateway —— LangChain 简化版（gateway_langchain.py）

这份文档只针对 `gateway_langchain.py`（用 LangChain 重写的简化版网关），跟同目录下 `README.md`（对应原版 `gateway.py`）是两份独立的说明。

## 安装与启动

```bash
pip install fastapi uvicorn "langchain-openai>=0.2" jsonschema

export DEEPSEEK_API_KEY=sk-你的主模型key
export DEEPSEEK_BACKUP_API_KEY=sk-你的备用模型key   # 没有备用账号，写和主模型一样的 key 也能跑通演示

uvicorn gateway_langchain:app --reload --port 8000
```

主/备模型的实际模型名、Base URL 可通过环境变量 `PRIMARY_PROVIDER_MODEL`、`PRIMARY_BASE_URL`、`BACKUP_PROVIDER_MODEL`、`BACKUP_BASE_URL` 配置。密钥只由 Gateway 进程读取，调用方不需要接触供应商密钥。

---

## 一、这个网关实现了什么功能

把"直接调用某一家模型 API"封装成一个统一的 HTTP 服务，调用方不用关心背后具体是哪家供应商、Key 存在哪里、失败了要不要换个模型再试。具体包含：

| 能力 | 说明 |
|---|---|
| 统一请求/响应协议 | `LLMRequest` / `LLMResponse`，调用方只跟这几个字段打交道 |
| 模型别名路由 | `general-primary` / `general-backup`，背后对应哪个供应商、哪个具体模型由网关配置决定 |
| 主备降级 + 重试 | 主模型失败（且重试耗尽）自动切换到备用模型，交给 LangChain 内置能力实现 |
| 受控 Prompt 模板 | 调用方只能按名字选模板、传变量，不能自己传模板正文 |
| 结构化输出 | 传 `response_schema` 时，要求模型只返回符合该 JSON Schema 的内容，并做二次校验 |
| 流式返回 | SSE 格式的增量输出 |
| 调用审计 | `/v1/traces`，记录每次调用的模型、用量、成本、耗时、成功/失败状态 |

三个 HTTP 接口：
- `POST /v1/llm`：非流式调用
- `POST /v1/llm/stream`：流式调用（SSE），不支持和 `response_schema` 同时使用
- `GET /v1/traces`：查看调用审计记录

---

## 二、HTTP 状态码含义（400 / 401 / 502）

HTTP 状态码按首位数字分类：`2xx` 成功、`4xx` 客户端错误（问题出在调用方这边）、`5xx` 服务端错误（问题出在网关/上游这边）。这个网关里出现的三种：

| 状态码 | 含义 | 在这个网关里对应的场景 |
|---|---|---|
| **400** Bad Request | 请求本身有问题，服务器还没进入业务逻辑就发现不对劲 | 模型别名不在白名单、Prompt 模板不存在/缺变量、流式请求带了 `response_schema`、上游供应商因为参数不合法拒绝了这次请求（`upstream_bad_request`） |
| **401** Unauthorized | 鉴权失败 | 主备模型的 API Key 全都无效或未配置（`invalid_api_key`）——这是网关自己的配置问题，不是调用方的请求有错，也不是上游偶发故障 |
| **502** Bad Gateway | 网关联系上游服务时失败了，问题出在网关和上游之间 | 网络超时/连接失败/限流等，重试和降级全部用尽依然失败（`model_unavailable`）；模型没按要求返回合法 JSON（`invalid_json`）；返回的 JSON 结构不符合 `response_schema`（`schema_validation_failed`） |

---

## 三、LangChain 的 Runnable 家族：`with_retry` / `with_fallbacks` / `bind` / `with_structured_output`

LangChain 里几乎所有"可以被调用的东西"（聊天模型、Prompt 模板、输出解析器……）都统一实现了同一套接口，叫 `Runnable`。这几个方法的归属层级不一样，决定了它们能不能被链式组合：

```
Runnable（最顶层抽象基类）
  │  定义了：with_retry() / with_fallbacks() / bind() / invoke() / ainvoke() ...
  │  —— 这几个是"全体 Runnable 通用方法"，任何 Runnable 子类都自动继承到
  │
  └── BaseChatModel（ChatOpenAI 的父类，本身也是 Runnable 的子类）
        │  额外新增：bind_tools() / with_structured_output()
        │  —— 这两个只有"还没脱离 BaseChatModel 身份"的对象才有
```

### 各方法返回的对象类型

| 调用 | 返回的类型 | 这个类型还有没有 `bind_tools`/`with_structured_output` |
|---|---|---|
| `ChatOpenAI(...)` 本身 | `ChatOpenAI`（`BaseChatModel` 的子类） | ✅ 有（它就是 `BaseChatModel`） |
| `.with_retry(...)` | `RunnableRetry` | ❌ 没有（只继承自通用 `Runnable`，不是 `BaseChatModel`） |
| `.with_fallbacks([...])` | `RunnableWithFallbacks` | ❌ 没有 |
| `.bind(...)` / `.bind_tools(...)` | `RunnableBinding` | ❌ 没有（返回的也是通用 `RunnableBinding`，不是 `BaseChatModel`） |
| `.with_structured_output(schema)` | 一个专门的输出解析链（返回值本身也不再是普通 `AIMessage`，而是解析后的结构化对象或 `{"raw","parsed","parsing_error"}`） | —— |

**结论**：`with_retry`/`with_fallbacks`/`bind` 是"全体 Runnable 通用能力"，包多少层都还在；`bind_tools`/`with_structured_output` 是"`BaseChatModel` 专属能力"，一旦调用过 `with_retry`/`bind` 之类的方法"退化"成通用 `RunnableXXX` 对象后就再也用不了——所以调用顺序必须是：**先在裸的 `ChatOpenAI` 上用完专属方法，再往外套 `with_retry`，最后套 `with_fallbacks`**，反过来不行。

### 在这份代码里具体怎么组装的（`build_runnable`）

```python
primary = MODEL_REGISTRY[requested_alias].with_retry(...)   # ChatOpenAI → RunnableRetry
fallback = MODEL_REGISTRY[fallback_alias].with_retry(...)    # ChatOpenAI → RunnableRetry
runnable = primary.with_fallbacks([fallback])                # RunnableRetry → RunnableWithFallbacks
```

调用 `await runnable.ainvoke(messages)` 时的执行逻辑大致是：

```
先调用 primary（重试壳包着主模型，失败按规则最多重试 1 次）
    ↓ 如果最终还是失败
再调用 fallback（重试壳包着备用模型，同样最多重试 1 次）
    ↓ 如果还是失败
异常继续往外抛，被 call_with_fallback 的 try/except 捕获
```

这里为什么这份代码没有用 `with_structured_output()`：因为 `response_schema` 是**每次请求动态传入的**，而一旦用了 `with_structured_output`，返回值类型会从普通 `AIMessage` 变成解析后的结构化对象，跟"没传 schema 时"的返回值结构完全不同，会让 `call_with_fallback` 里统一的结果提取逻辑（`result.content`/`result.response_metadata`）多出一条分支，复杂度上升。这份代码选择了更朴素的做法：在 Prompt 里直接要求模型"只返回符合这个 JSON Schema 的内容"，再手动 `json.loads` + `jsonschema.validate` 校验，兼容性更好、返回值结构也统一。

---

## 四、`with_retry` 三个参数分别是什么意思

```python
primary = MODEL_REGISTRY[requested_alias].with_retry(
    retry_if_exception_type=RETRYABLE_EXCEPTIONS,
    wait_exponential_jitter=True,
    stop_after_attempt=2,
)
```

| 参数 | 含义 |
|---|---|
| `retry_if_exception_type` | 一个异常类型元组，**只有**抛出的异常属于这里面列的类型，才会触发重试；其他类型的异常（比如 401/400）第一次失败就直接判定整体失败，不浪费时间重试 |
| `wait_exponential_jitter` | 是否在两次重试之间做"指数退避 + 随机抖动"。为 `True` 时，底层用 tenacity 的 `wait_exponential_jitter()`（默认参数：初始 1 秒，之后每次翻倍，再叠加 0~1 秒的随机抖动），避免上游刚恢复就被大量重试请求瞬间打满 |
| `stop_after_attempt` | 这个 Runnable 总共最多尝试几次（**含第一次**，不是"第一次之后再重试 N 次"）。这里设的是 `2`，意味着最多跑 2 次：第 1 次失败 → 等待退避（约 1~2 秒）→ 第 2 次尝试 → 还失败就整体判定失败 |

---

## 五、哪些错误值得重试，哪些不值得

```python
RETRYABLE_EXCEPTIONS = (
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    InternalServerError,
    TimeoutError,
    ConnectionError,
)
```

| 异常 | 触发原因 | 耗时特点 | 是否放进 `RETRYABLE_EXCEPTIONS` | 原因 |
|---|---|---|---|---|
| `APITimeoutError` | 等了 `timeout` 秒（这里配的是60秒）还没响应 | 耗时接近你配置的 timeout 值；同时包含"连接阶段卡住"和"读取响应卡住"两种情况，两者最终都统一成这一个异常类型 | ✅ | 很可能只是这次网络/上游偶然慢了，换个时间点大概率成功 |
| `APIConnectionError` | 连接失败但不是因为等太久——DNS解析失败、连接被拒绝、TLS握手失败等 | 通常很快 | ✅ | 多半是瞬时网络抖动 |
| `RateLimitError`（429） | 触发限流 | 快，服务端在真正推理前就直接拒绝 | ✅ | 配合退避等待，等一会儿限流窗口就重置了 |
| `InternalServerError`（500） | 上游服务自己内部临时出错 | 不固定，可能发生在处理流程的任意阶段 | ✅ | 是上游偶发故障，不是这次请求本身有问题 |
| `AuthenticationError`（401） | API Key 无效/缺失 | 很快，服务端检查鉴权头就直接拒绝 | ❌ | Key 是错的，重试多少次结果都一样 |
| `BadRequestError`（400） | 请求本身格式不对（参数非法、模型名不存在等） | 很快，服务端校验请求就直接拒绝 | ❌ | 同样的错误请求重试还是错 |

不值得重试的这两种（`AuthenticationError`/`BadRequestError`）虽然不会在同一个模型上重试，但依然会被 `with_fallbacks` 捕获并立刻切换到备用模型尝试——只是不会在明知没用的情况下，还在原地多等一次退避、多打一次注定失败的请求。

在 `call_with_fallback` 里，这两种异常最终会被单独捕获，转换成更精确的 HTTP 错误（401 / 400），而不是和其他故障混在一起统一报成 502：

```python
except AuthenticationError as exc:
    raise HTTPException(401, {"code": "invalid_api_key", ...}) from exc
except BadRequestError as exc:
    raise HTTPException(400, {"code": "upstream_bad_request", ...}) from exc
except Exception as exc:
    raise HTTPException(502, {"code": "model_unavailable", ...}) from exc
```

---

## 六、`record_trace` 记录了哪些内容

每次调用（不管成功还是失败）都会往内存里的 `TRACES` 列表追加一条记录，字段如下：

| 字段 | 含义 |
|---|---|
| `request_id` | 这次请求的唯一 ID，方便串联日志 |
| `requested_model` | 调用方原本请求的模型别名 |
| `actual_model` | 实际生效的模型别名（如果和 `requested_model` 不一样，说明发生了降级）；调用彻底失败时为 `None` |
| `input_tokens` / `output_tokens` | 输入/输出 token 数量（从 `response_metadata["token_usage"]` 里取，失败时为 0） |
| `cost_usd` | 按 `PRICE_PER_MILLION` 里配置的单价，用 token 数量算出来的成本 |
| `latency_ms` | 从请求开始到这一刻（成功返回或最终判定失败）为止，一共经过的毫秒数，包含了所有重试和降级切换消耗的时间 |
| `status` | `"success"` 或 `"failed"` |
| `error_code` | 失败时的具体错误码（`invalid_api_key`/`upstream_bad_request`/`model_unavailable`/`invalid_json`/`schema_validation_failed` 等），成功时为 `None` |

> **已知的简化/限制**：这份记录目前没有 `timestamp`（发生时间）字段，也没有记录具体重试了几次（`attempts`）——这两项在原版 `gateway.py` 的 `CallTrace` 里是有的，这里为了简化被省略了。如果需要按时间段筛选审计记录，或者精确统计重试次数，需要额外补上。

---

## 七、结构化输出的两层校验：`validate(...)` 和两种解析错误

```python
parsed = json.loads(content)                                # 第①步：文本 → Python 对象
validate(instance=parsed, schema=request.response_schema)   # 第②步：校验对象结构对不对
```

### `validate(instance=parsed, schema=request.response_schema)` 是什么

来自 `jsonschema` 库，检查 `parsed`（已经解析好的 Python 对象）是不是符合 `response_schema` 这份 JSON Schema 定义的结构要求（哪些字段必填、类型对不对、数值范围对不对）。符合就什么都不做（正常返回），不符合就抛出 `jsonschema.exceptions.ValidationError`（这份代码里改名叫 `JsonSchemaError` 导入进来）。

### 两种异常对应的是完全不同阶段的问题

| 异常 | 出问题的阶段 | 具体原因 | 例子 |
|---|---|---|---|
| `json.JSONDecodeError` | 第①步：文本解析 | 模型返回的内容**连合法 JSON 语法都不满足**（可能混了 Markdown 代码块标记、多了逗号、少了引号） | `content = "好的，这是结果：\`\`\`json\n{...}\n\`\`\`"` → 解析直接报错 |
| `JsonSchemaError`（即 `ValidationError`） | 第②步：结构校验 | 文本本身是合法 JSON，但**内容结构不符合要求**（缺字段、类型不对、数值超范围） | `content = '{"answer": "你好"}'`，但 schema 要求还必须有 `confidence` 字段 → 校验报错 |

分开捕获、分开记录不同的 `error_code`（`invalid_json` / `schema_validation_failed`），是为了让排查方向更明确：前者说明模型没听懂"只返回 JSON"的指令（提示词需要改进），后者说明模型确实返回了 JSON，但对字段要求理解错了（需要检查 schema 定义或者调整提示词里对字段的说明）。

---

## 八、调用示例

**非流式调用**
```bash
curl http://127.0.0.1:8000/v1/llm \
  -H 'content-type: application/json' \
  -d '{"model":"general-primary","messages":[{"role":"user","content":"解释什么是 LLM Gateway"}]}'
```

**结构化输出**
```bash
curl http://127.0.0.1:8000/v1/llm \
  -H 'content-type: application/json' \
  -d '{"model":"general-primary","messages":[{"role":"user","content":"返回一个答案"}],"response_schema":{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"]}}'
```

**流式调用**
```bash
curl -N http://127.0.0.1:8000/v1/llm/stream \
  -H 'content-type: application/json' \
  -d '{"model":"general-primary","messages":[{"role":"user","content":"用一句话解释流式输出"}]}'
```

**查看调用审计记录**
```bash
curl http://127.0.0.1:8000/v1/traces
```

**验证降级效果**：把 `DEEPSEEK_API_KEY` 改成一个无效值再发请求，响应里的 `model` 字段应该会变成 `"general-backup"`，并且会收到 `401 invalid_api_key`（如果备用模型的 Key 也失效）或正常结果（如果备用模型的 Key 有效）。
