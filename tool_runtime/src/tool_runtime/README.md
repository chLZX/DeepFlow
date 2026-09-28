# Tool Runtime 最小实现说明

本文档配合 `main.py` 阅读，`main.py` 是 Function Calling / Tool Runtime 六个知识点
（1.1～1.6）的一个最小可运行示例，用 LangChain 的 `@tool` 实现。

---

## 一、`main.py` 整体逻辑是什么

整个文件按"一次工具调用真正跑起来需要哪些东西"，从下往上分成五层：

```
Schema 层         StrictModel / RefundInput / RefundOutput / ToolError
                  ——约束"参数长什么样""结果长什么样""失败长什么样"

工具定义层         @tool 装饰的 refund_order / list_recent_orders
                  ——业务函数本身 + .metadata 治理规则（权限/风险/超时/是否幂等）

Registry 层        ToolRegistry / REGISTRY
                  ——谁被注册了、谁能被模型看见，和 Agent Loop 解耦

Runtime 执行层     ExecutionContext / run_handler / execute / execute_batch
                  ——一条 Tool Call 从"模型提议"到"真正执行"要过的检查关卡

Agent Loop 层      run_agent
                  ——模型决策 → Runtime 执行 → 结果写回 → 模型再决策，循环到给出最终回答
```

一条请求的完整生命周期（对应 `main()` 里的演示顺序）：

1. `REGISTRY.register(...)` 把两个工具注册进去，Registry 现在"认识"它们。
2. `convert_to_openai_tool(refund_order)` 可以看到模型实际收到的工具定义——只有
   `name`/`description`/Input Schema，`metadata` 里的治理字段完全不在里面。
3. `execute(call, ctx)` 模拟 Runtime 收到一条 Tool Call 后依次做的检查：
   参数校验（Input Schema）→ 权限 → 审批 → 执行（带超时/幂等重试）→ 结果校验
   （Output Schema）→ 生成 `ToolMessage`（带 `Error Schema`）。
4. `execute_batch(...)` 演示两条调用并行发出、按完成顺序而非发出顺序返回，靠
   `tool_call_id` 对应结果，不会配错。
5. `REGISTRY.unregister("refund_order")` 演示撤销一个工具之后，`run_agent` 的代码
   完全不用改，模型自然就看不到这个工具了。
6. 如果设置了 `OPENAI_API_KEY`，`run_agent(...)` 会真的接一次大模型，走完整的
   "模型决策 → 执行 → 写回 → 再决策"循环。

---

## 二、具体问题解答

### 1. `refund_order.metadata = {...}` 是干嘛的？

```python
refund_order.metadata = {
    "permission": "order:refund",
    "risk": "high",
    "timeout": 1.0,
    "max_retries": 0,
    "idempotent": False,
    "output_model": RefundOutput,
}
```

这是挂在工具对象上的一个**自由字典**，专门放"只有 Runtime 要用、模型永远看不到"
的治理规则：

| 字段 | 作用 |
|---|---|
| `permission` | 调用这个工具需要什么权限，`execute()` 里用 `ctx.permissions` 比对 |
| `risk` | 风险等级，`"high"` 表示必须先有用户审批（`ctx.approved_call_ids`）才能执行 |
| `timeout` | 单次执行最长等待秒数，配合 `asyncio.wait_for` 使用 |
| `max_retries` | 失败后最多重试几次（仅幂等工具生效） |
| `idempotent` | 是否可以安全重试；`False` 的写操作默认只给一次机会 |
| `output_model` | 用来校验 handler 返回值的 Output Schema |

之所以不写进 `@tool(args_schema=...)` 或函数签名里，是因为 `bind_tools()` /
`convert_to_openai_tool()` 只会读取 `.name`、`.description`、`.args_schema` 这几样
东西生成协议，完全不会读 `.metadata`——这是刻意让模型永远碰不到这些字段的方式，
对应"权限和审批不能作为模型参数"这条原则。

**如何输出 function 的 name 和 description：**

`@tool` 装饰器会自动把函数名和 docstring 变成工具对象的属性，不需要手写：

```python
print(refund_order.name)         # "refund_order"        —— 来自函数名
print(refund_order.description)  # docstring 的内容        —— 来自函数的 """...""" 文档字符串
```

如果想看模型实际会收到的完整协议（包含 name/description/Input Schema 的 JSON
Schema），用 `main.py` 里已经调用的这个函数：

```python
from langchain_core.utils.function_calling import convert_to_openai_tool
print(convert_to_openai_tool(refund_order))
```

---

### 2. `REGISTRY = ToolRegistry()` 是干嘛的？

```python
class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, t: BaseTool) -> None: ...
    def unregister(self, name: str) -> None: ...
    def get(self, name: str) -> BaseTool | None: ...
    def visible_tools(self, ctx: "ExecutionContext") -> list[BaseTool]: ...

REGISTRY = ToolRegistry()
```

它是一个**全局唯一的工具注册表**，负责回答"系统里现在有哪些工具、这个用户能看到
哪些"，和 Agent Loop 的执行逻辑彻底分开（对应知识点 1.6）：

- `register(tool)` / `unregister(name)`：运行时随时可以增删工具，不需要改
  `run_agent`/`execute` 的代码——比如某个工具出故障需要紧急下线，只需要调一次
  `unregister`，不用重新发布整个 Agent。
- `get(name)`：`execute()` 执行一条 Tool Call 时，靠这个方法按名字查到工具对象。
- `visible_tools(ctx)`：按 `ctx.permissions` 过滤，`run_agent()` 每一轮都问它要
  "当前这个用户能用的工具"，而不是在代码里写死一份固定列表。

一句话：Registry 是"数据"（有哪些工具、谁能用），`run_agent`/`execute` 是
"逻辑"（怎么执行），数据变化不需要改逻辑代码。

---

### 3. `config = {"configurable": {...}}` 是干嘛的，放在哪用？

```python
config = {
    "configurable": {
        "user_id": ctx.user_id,                # 可信身份，模型碰不到
        "idempotency_key": f"idem_{call_id}",   # 同一条调用，重试也用同一个 key
    }
}
```

这是 LangChain `Runnable` 接口统一支持的 `RunnableConfig`，用来在**调用工具的
那一刻**，把"模型永远看不到、也改不了"的可信数据（身份、幂等键）单独传进去。

**在哪用**：在 `run_handler()`（第 157～172 行）里现场构造，紧接着传给
`tool_obj.ainvoke(args.model_dump(), config=config)`。工具函数只要在签名里声明了
`config: RunnableConfig` 这个参数（`refund_order`/`list_recent_orders` 都这样写），
LangChain 就会自动把这个字典注入进函数体，函数内部用
`config["configurable"]["user_id"]` 取出来用。

这跟 `ChatOpenAI(model=..., api_key=...)` 里的参数不是一回事——那是配置"怎么连接
大模型 API"的，跟这里"怎么给工具调用传可信上下文"完全无关，只是恰好都叫
"config"。

---

### 4. `ToolError` 有哪几类？

```python
class ToolError(StrictModel):
    code: Literal[
        "INVALID_ARGUMENT",   # 参数不对：模型可以改参数重试
        "PERMISSION_DENIED",  # 没权限：重试没用
        "APPROVAL_REQUIRED",  # 高风险操作，需要用户先确认
        "UPSTREAM_ERROR",     # handler 执行报错
        "TIMEOUT",            # 执行超时
        "INVALID_OUTPUT",     # handler 返回的数据不合格（代码 bug）
    ]
    message: str
    retryable: bool = False
```

六类，分别对应 `execute()` 里六个不同的失败关卡：

| code | 触发时机 | 能不能重试 |
|---|---|---|
| `INVALID_ARGUMENT` | Input Schema 校验没过，或工具名根本不存在 | 模型改好参数可以再调 |
| `PERMISSION_DENIED` | `ctx.permissions` 里没有这个工具要求的权限 | 不能，换身份/权限才行 |
| `APPROVAL_REQUIRED` | 高风险操作，`call["id"]` 不在 `ctx.approved_call_ids` 里 | 需要先拿到用户确认 |
| `UPSTREAM_ERROR` | handler 内部抛了异常（比如金额超限） | 视情况，一般不自动重试 |
| `TIMEOUT` | 执行超过 `metadata["timeout"]` 还没返回 | 只有幂等工具才允许重试 |
| `INVALID_OUTPUT` | handler 返回的数据不符合 Output Schema | 是代码 bug，不该让模型猜 |

---

### 5. 两处 `model_validate` 分别是干嘛的？

```python
args = tool_obj.args_schema.model_validate(call["args"])        # 第 191 行
output = rules["output_model"].model_validate(raw_output)        # 第 208 行
```

两处都是"拿一份不可信的原始字典，按 Schema 校验，通过就转成可信的 pydantic
对象"，但守的是协议链路上不同的两段：

- 第一处：`call["args"]` 是**模型生成的参数**，还没被验证过。用 `RefundInput`
  这类 Input Schema 校验，防止模型编错字段、传错类型、或者夹带 Schema 之外的字段
  （`extra="forbid"` 起作用的地方）。
- 第二处：`raw_output` 是**业务 handler 自己返回的原始字典**，可能因为代码 bug
  漏填字段、类型不对。用 `RefundOutput`/`ListOrdersOutput` 这类 Output Schema
  校验，防止一个有问题的返回值原样传回给模型/Agent。

一个守"进来"的那一段（模型 → Runtime），一个守"出去"的那一段
（handler → Agent），都是 `model_validate()`，但校验的对象和防的风险不一样。

---

### 6. `ToolMessage` 里要放哪些东西？

`main.py` 里两个地方构造 `ToolMessage`：成功时在 `execute()` 第 212 行，失败时在
`_error()` 第 175～180 行。必须包含这三样：

```python
ToolMessage(
    content=...,                 # 结果内容：成功时是 output.model_dump_json()，
                                  # 失败时是 {"error": {...}} 的 JSON 字符串
    tool_call_id=call["id"],     # 必须和模型发起这条调用时的 id 完全一致，
                                  # 否则模型没法把结果对应回它发出的哪条 Tool Call
    name=call["name"],           # 工具名，方便追踪/调试
)
# 失败时额外加一个 status="error"，标记这是一条失败结果
```

其中 `tool_call_id` 是三者里最关键的一个——这是整个协议里"结果怎么和调用对应上"
的唯一依据，尤其是 `execute_batch()` 并行执行、完成顺序和发出顺序不一致时，全靠
它，不能靠数组下标之类的顺序假设。

---

### 7. `tool_choice="auto"` 是什么意思？`ai_msg` 的结构是怎样的？

```python
ai_msg: AIMessage = await llm.bind_tools(tools, tool_choice="auto").ainvoke(messages)
```

**`tool_choice="auto"`**：把"这一轮要不要调用工具、调用哪个"的决定权完全交给
模型自己，它可以选择直接用文字回答，也可以选择调用一个或多个工具。对比其他取值：
`"none"` 强制不许调用工具，`"required"` 强制必须调用，指定工具名则强制只能调那一个。
Agent Loop 用 `"auto"` 是因为循环本身就是要让模型自己判断"现在该不该继续调用"。

**`ai_msg` 的结构**：是一个 `AIMessage` 对象，循环里实际用到的是这三个字段：

```python
AIMessage(
    content='',              # 模型直接回答的文本；决定调用工具时这里通常是空字符串
    tool_calls=[              # 模型这一轮提出的调用，可能是 0 条、1 条或多条（并行）
        {
            'name': 'list_recent_orders',   # 选中的工具名
            'args': {},                     # 生成的参数，已经是 dict（但还没被 Schema 校验）
            'id': 'call_abc123',            # 就是 tool_call_id，回写结果时必须原样带上
            'type': 'tool_call',
        }
    ],
    invalid_tool_calls=[],    # 参数不是合法 JSON 的调用会落在这里，main.py 当前没处理这个字段
    response_metadata={...},  # 模型名、finish_reason 等附加信息
)
```

`run_agent()` 里的判断逻辑就是围绕这两个字段展开：`if not ai_msg.tool_calls:` 为真
说明模型选择直接回答，直接 `return ai_msg.content`；否则遍历 `tool_calls`，交给
`execute_batch()` 并行执行，结果写回 `messages`，进入下一轮循环。
