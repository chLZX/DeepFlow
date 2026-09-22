# sse_ex.py 代码讲解

对应课件 `1-3.pdf` 第 1-21 页（Streaming 流式输出）。这份 README 先讲 SSE 协议
本身，再按 `create_run → asyncio.create_task → get_events/subscribe → 断线重连
→ cancel_run/task.cancel()` 的顺序讲代码。

---

## 一、SSE 协议是什么

SSE（Server-Sent Events）是一种**只能服务端往客户端单向推送**的 HTTP 协议
（对应 PDF 第 11 页：LLM 文本、Run 状态这种"服务端 → 客户端"的场景优先用 SSE，
而不是 WebSocket）。它本质上就是一个**一直不关闭的普通 HTTP 响应**，靠
`Content-Type: text/event-stream` + chunked 传输，让服务端可以陆续往同一个
连接里写数据，浏览器边收边解析、边触发事件。

### 1. 三层结构（对应 PDF 第 9 页）

```
Layer 1 · HTTP 传输层    把响应 body 分批送到客户端（bytes / packets）
Layer 2 · SSE 帧协议     规定一条事件怎么编码、怎么分隔（SSE frames）
Layer 3 · 业务事件协议   规定 text.delta / run.completed 这些字段的业务含义（本项目里的 RunEvent）
```

网络层的一个 TCP 包、一条 SSE 事件、一个业务事件，这三者**不是一一对应的**：
一个包可能只装了半条 SSE 事件，也可能装了好几条。

### 2. SSE 的线上格式（对应 PDF 第 10 页）

服务端往连接里写的其实就是纯文本，格式长这样：

```
id: 17
event: text.delta
data: {"run_id":"run_123","seq":17,"delta":"正在分析"}

```

规则：

- `id:` —— 这条事件的编号。浏览器会记住**收到的最后一个 `id`**。
- `event:` —— 事件类型，浏览器用 `EventSource.addEventListener(type, ...)` 按类型分发。
- `data:` —— 事件内容，通常是一个 JSON 字符串。
- **一个空行**表示这条事件结束（分隔符）。
- 每一行都必须是 UTF-8 文本，`Content-Type` 必须是 `text/event-stream`，
  否则浏览器的 `EventSource` 会直接拒绝连接。

`sse_ex.py` 里对应编码这段格式的函数就是：

```python
def encode_sse(event: RunEvent) -> str:
    payload = json.dumps(asdict(event), ensure_ascii=False)
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"
```

### 3. 心跳机制

SSE 连接会长时间挂着不关闭，但很多中间层（Nginx、负载均衡、云厂商网关）会把
"太久没有数据"的连接当成死连接直接掐断。心跳的做法是：服务端每隔一段时间
（比如 15 秒）发一行**注释行**：

```
: heartbeat

```

以 `:` 开头的行是 SSE 规范里的注释，浏览器的 `EventSource` 会**直接忽略**它
（不会触发任何 `event` 监听），它唯一的作用就是让连接里持续有字节流动，
防止被中间代理判定为空闲连接而断开。心跳行不应该占用业务的 `seq`
编号，因为它根本不是一个业务事件。

> `sse_ex.py` 里的 `subscribe()` **没有实现心跳**（文件顶部注释里也写明了
> "没有做心跳"），是为了保持代码简单。如果连接经常被代理断开，可以在
> `await asyncio.sleep(0.2)` 的轮询循环里加一个计时器，超过一定时间没有
> 新事件就 `yield ": heartbeat\n\n"`。

### 4. 为什么客户端用 `EventSource`

浏览器原生的 `EventSource` 就是按上面的格式写的解析器，它自带两个很重要的能力
（对应 PDF 第 21/23 页），这也是这份代码把接口拆成三个的原因：

- **断线自动重连**：连接意外断开后，`EventSource` 会自动发起新请求，并且
  自动带上 `Last-Event-ID` 请求头，值就是它最后收到的那个 `id`。
- **单向推送**：`EventSource` 只能收，不能发。所以"创建任务"和"取消任务"
  必须走单独的 `POST` 接口，不能塞进 SSE 连接里——这就是为什么代码里是
  三个独立接口，而不是一个大接口。

---

## 二、`create_run` 是怎么实现的

```python
@app.post("/v1/runs")
async def create_run(body: CreateRunRequest) -> dict:
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    state = RunState(run_id=run_id)
    RUNS[run_id] = state

    task = asyncio.create_task(run_model(state, body.prompt))
    TASKS[run_id] = task

    return {"run_id": run_id, "status": state.status}
```

它做的事情很少，故意设计成"轻"：

1. 生成一个 `run_id`（`uuid4` 的十六进制前 8 位，够用且短）。
2. 创建一个 `RunState`：内存里的一个对象，保存这次 Run 的状态
   (`status`)、已经产生的事件列表 (`events`)、以及一个取消标志位
   (`cancelled`)，存进全局字典 `RUNS`。
3. **用 `asyncio.create_task` 把真正调用模型的协程 `run_model(...)` 丢到后台去跑**，
   不 `await` 它。
4. 把这次任务的 `run_id` 立刻返回给客户端。

关键点（对应 PDF 第 11/21 页"创建"接口的职责）：**这个接口不会等模型把话说完**。
`run_model` 才是真正调用 DeepSeek、一点点吐字的地方；`create_run` 只负责
"把这件事安排上"，然后立刻返回 `run_id`，前端拿到 `run_id` 后再单独去订阅事件。

---

## 三、`asyncio.create_task` 的作用 & 它返回的是什么类型

```python
task = asyncio.create_task(run_model(state, body.prompt))
```

- `run_model(state, body.prompt)` 这一步只是**创建了一个协程对象**
  （coroutine object），此时它的代码**还没有开始执行**。
- `asyncio.create_task(coro)` 把这个协程对象包装成一个 **`asyncio.Task`**，
  并且立刻把它**注册到当前正在运行的事件循环里**，让它跟当前这个 HTTP 请求
  的协程"并发"执行——不是"另起一个线程"，而是同一个事件循环下的另一条
  可以被调度的执行路径。
- 因为它是 `Task`（而不是普通协程对象），事件循环会在下一次有空档时主动去
  推进它，**不需要谁去 `await` 它**。这正是"接口立刻返回、模型在后台继续跑"
  的关键：`create_run` 函数直接 `return`，但 `run_model` 并不会因此被打断。

**返回类型**：`asyncio.create_task()` 返回的是 `asyncio.Task[T]`，这里
`T` 是被包装的协程的返回类型。`run_model` 的签名是
`async def run_model(...) -> None`，所以这里的类型是 **`asyncio.Task[None]`**。

`Task` 是 `asyncio.Future` 的子类，除了"能被调度执行"之外，还带着一整套
状态管理能力，这份代码里实际用到的是：

- `task.cancel()` —— 请求取消（下一节详细讲）。
- 把它存进全局字典 `TASKS[run_id] = task`——这一步**不是可选的**：如果不保留
  引用，Python 的垃圾回收有可能在任务跑到一半时把这个 `Task` 对象回收掉，
  导致任务被无声地中断（这是 asyncio 官方文档里明确提醒过的坑）。存住它，
  之后 `cancel_run` 才能通过 `run_id` 找到对应的 `Task` 并取消它。

---

## 四、`get_events` 是怎么实现的，为什么要用 `subscribe`

```python
@app.get("/v1/runs/{run_id}/events")
async def get_events(
    run_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    state = RUNS.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="run not found")

    after_seq = int(last_event_id) if last_event_id is not None else -1

    return StreamingResponse(
        subscribe(request, state, after_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

`get_events` 本身很薄，就三件事：按 `run_id` 找到 `RunState`；从请求头里
读 `Last-Event-ID`（断线重连用，见下一节）；把 `subscribe(...)` 这个**异步生成器**
包进 `StreamingResponse` 返回。

### 为什么一定要用 `subscribe`（一个 async generator）

`StreamingResponse` 需要的是一个**可以陆续产出多段内容**的东西，而不是
一次性拼好的字符串——如果直接 `return "全部内容"`，FastAPI 会等函数整个跑完
才一次性把所有内容发出去，那就退化成了普通 HTTP 响应，"流式"就没有意义了
（对应 PDF 第 4 页：`stream=True` 不会让模型算得更快，流式的价值是"提前展示
已经生成的部分"）。

`subscribe` 用 `yield` 每产出一条 SSE 文本，Starlette 就会立刻把这一段
`flush` 到 TCP 连接上发给客户端，不用等函数返回：

```python
async def subscribe(request: Request, state: RunState, after_seq: int):
    cursor = after_seq + 1
    while True:
        while cursor < len(state.events):
            event = state.events[cursor]
            cursor += 1
            yield encode_sse(event)
            if event.type in TERMINAL_TYPES:
                return          # 终态事件之后没有更多事件了，结束生成器

        if await request.is_disconnected():
            return              # 客户端断开，只停止推送，不动后台任务

        await asyncio.sleep(0.2)  # 还没有新事件，等一会儿再看
```

它做的事情：

1. `cursor` 指向"下一条要发给这个订阅者的事件"在 `state.events` 里的下标。
2. 内层 `while` 把 `state.events` 里 `cursor` 之后**已经产生**的事件依次
   `yield` 出去；如果 `yield` 出去的是终态事件（`run.completed` /
   `run.failed` / `run.cancelled`），说明这个 Run 已经结束，不会再有新事件了，
   直接 `return`，生成器结束，SSE 连接也就自然关闭。
3. 如果已有事件都发完了、Run 还没结束，就检查 `request.is_disconnected()`
   （客户端是不是主动断开了这次 HTTP 连接）；如果断开了就 `return`，
   ——但注意这里**只是让这个生成器停止**，`RUNS[run_id]` 和后台的 `run_model`
   Task **完全不受影响**，模型还在继续跑、`state.events` 还在继续增长。
4. 如果客户端还连着、也没有新事件，就 `sleep(0.2)` 之后回到循环开头再看看
   ——这是一个简单的轮询，用"多等 200ms"换取代码简单，没有用
   `asyncio.Condition`/`asyncio.Queue` 这类需要"通知唤醒"的写法。

一句话："产生事件"（`run_model` 往 `state.events` 里追加）和"消费事件"
（`subscribe` 从 `state.events` 里读）是完全解耦的两件事，`subscribe`
只是一个**只读的游标**，这也是为什么同一个 Run 可以被多次订阅——包括下面
要讲的断线重连。

### 断线重连是怎么做的

`EventSource` 断线后会自动重新发起 `GET /v1/runs/{run_id}/events`，并且
**自动**带上一个请求头：

```
Last-Event-ID: 17
```

这个 `17` 就是它上一次成功收到的最后一条事件的 `id`（对应 `encode_sse` 里
写的 `id: {event.seq}`），这是浏览器 `EventSource` 内置的标准行为，不是这份
代码自己写的。服务端要做的只是读出这个头、算出应该从哪条事件继续发：

```python
last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
...
after_seq = int(last_event_id) if last_event_id is not None else -1
```

- 第一次订阅（没有 `Last-Event-ID`）：`after_seq = -1`，`subscribe` 里
  `cursor = after_seq + 1 = 0`，从第一条事件开始发。
- 断线重连（带着 `Last-Event-ID: 17`）：`after_seq = 17`，`cursor = 18`，
  直接跳过已经发过的 0~17，从第 18 条继续发。

因为 `state.events` 是**保留在内存里的完整历史**，断线期间产生的事件不会丢，
重新订阅时会被"回放"出来，然后无缝接上继续实时推送——整个过程**没有重新
调用一次 `POST /v1/runs`**，也就是 PDF 第 21 页强调的：**断开连接 ≠ 取消任务**，
浏览器刷新/断网不会导致重新问一次模型。

---

## 五、`cancel_run` 是怎么实现的，`task.cancel()` 是干嘛的

```python
@app.post("/v1/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict:
    state = RUNS.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="run not found")

    if state.status in {"completed", "failed", "cancelled"}:
        return {"run_id": run_id, "status": state.status}

    state.cancelled.set()
    task = TASKS.get(run_id)
    if task is not None:
        task.cancel()

    return {"run_id": run_id, "status": "cancelling"}
```

步骤：

1. 找不到这个 `run_id` → 404。
2. 如果这个 Run 已经是终态（成功/失败/已取消），直接把当前状态原样返回，
   不重复取消——这一步让接口**幂等**：重复点两次"取消"不会报错。
3. `state.cancelled.set()`：把一个 `asyncio.Event` 标志位置为"已设置"。
4. `task.cancel()`：请求取消后台真正在跑的那个 `Task`。
5. 立刻返回 `"cancelling"`，**不等** `run_model` 真正处理完取消——真正的
   `run.cancelled` 事件会稍后出现在 SSE 流里（对应 PDF 第 7 页控制面：
   完成/取消要"排队"，由事件流做最终裁决）。

### 为什么这里**同时**用了 `cancelled.set()` 和 `task.cancel()`（两道保险）

`run_model` 里对应两条不同的退出路径：

```python
async for chunk in model.astream(messages):
    if state.cancelled.is_set():          # 路径 A：主动检查标志位
        ...
        return
    ...

except asyncio.CancelledError:            # 路径 B：被 task.cancel() 打断
    ...
    raise
```

- `state.cancelled` 是一个**协作式**的标志位：只有 `run_model` 自己在
  循环体里主动检查 `is_set()` 才会生效。但如果它此刻正卡在
  `await model.astream(...)` 里等 DeepSeek 返回下一个 chunk（还没轮到
  检查标志位的那一行代码），光设置这个标志位是**叫不醒**它的。
- `task.cancel()` 才是真正能"打断"一个正在 `await` 的协程的手段：
  asyncio 会在这个 `Task` **下一次挂起等待的地方**（这里就是
  `async for chunk in model.astream(...)` 内部的那个 `await`）抛出一个
  `asyncio.CancelledError`，哪怕它此刻正在等网络 I/O 也会被打断。

所以：`state.cancelled.set()` 覆盖"循环体正在跑到检查点"的情况，
`task.cancel()` 覆盖"正卡在网络等待"的情况，两者一起才能保证不管取消请求
在什么时机到达，`run_model` 都能尽快停下来。

`except asyncio.CancelledError` 里做的事情是：把 `run.cancelled` 事件写进
`state.events`（否则订阅端永远收不到"已取消"这个终态事件，SSE 连接会一直
挂着等下去），然后**必须 `raise` 把这个异常重新抛出去**——如果在这里把异常
吞掉不 `raise`，这个 `Task` 在 asyncio 看来就是"正常结束"而不是"被取消"，
会掩盖真实状态，也是 Python 官方文档特别提醒过的一个反模式。

---

## 小结：三个接口为什么要分开

| 接口 | 只做一件事 |
|---|---|
| `POST /v1/runs` | 创建 `RunState` + `asyncio.create_task` 后台跑模型，立刻返回 `run_id` |
| `GET /v1/runs/{id}/events` | 只读 `state.events`，用 SSE 把它们推出去；能重复订阅、能断线重连 |
| `POST /v1/runs/{id}/cancel` | 只负责喊停：设置标志位 + `task.cancel()` |

三者共享的唯一状态就是 `RUNS[run_id]` 这个 `RunState` 对象——`create_run`
写它的 `events`（间接，通过后台 Task），`subscribe` 只读它的 `events`，
`cancel_run` 写它的 `cancelled` 标志位。谁也不直接调用谁，都是通过这个
共享状态"通信"，这就是为什么断开 SSE 连接、重新订阅、显式取消，互相之间
不会串扰。
