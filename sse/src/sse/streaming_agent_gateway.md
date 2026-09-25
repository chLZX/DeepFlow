# Streaming Agent Gateway —— 核心概念笔记

---

## 目录

1. [为什么需要 Streaming](#1-为什么需要-streaming)
2. [三种延迟指标](#2-三种延迟指标)
3. [三层协议：HTTP / SSE / Harness Event](#3-三层协议http--sse--harness-event)
4. [SSE 与 EventSource](#4-sse-与-eventsource)
5. [RunEvent：统一事件协议](#5-runevent统一事件协议)
6. [Model Adapter：屏蔽供应商差异](#6-model-adapter屏蔽供应商差异)
7. [六层 Harness 架构](#7-六层-harness-架构)
8. [FastAPI 三接口分离](#8-fastapi-三接口分离)
9. [客户端消费](#9-客户端消费)
10. [取消机制](#10-取消机制)
11. [流式失败与恢复](#11-流式失败与恢复)
12. [Event Store 与实时通知](#12-event-store-与实时通知)
13. [Checkpoint：长任务恢复](#13-checkpoint长任务恢复)
14. [恢复矩阵：断线 vs 崩溃](#14-恢复矩阵断线-vs-崩溃)
15. [错误事件协议](#15-错误事件协议)
16. [测试重点：11项边界](#16-测试重点11项边界)
17. [本仓库代码实现对照表](#17-本仓库代码实现对照表)

---

## 1. 为什么需要 Streaming

Agent 运行可能持续几十秒，如果全程只用一个转圈图标、最后一次性返回一整段 JSON，用户完全无法判断系统当前处于什么状态：

- 正在生成，还是正在排队？
- 正在调用工具，还是已经卡死？
- 仍在执行，还是早已失败？
- 方向正确，还是应该立即停止？

**核心结论：Streaming 是 Agent Runtime 到交互层的实时事件通道**，第一价值是让"运行过程"对用户和系统都可见，而不是单纯的"打字机视觉效果"。

⚠️ **常见误区**：`stream=True` 不会让模型推理得更快。非流式和流式两种方式，任务的**总完成时间是一样的**，唯一的区别是流式让用户能**更早看到内容、并且有机会提前取消**。

---

## 2. 三种延迟指标

一个 `latency_ms` 无法解释完整的流式体验，必须拆成三个独立指标：

| 指标 | 定义 | 主要影响因素 |
|---|---|---|
| **TTFT**（Time to First Token） | 请求开始 → 首个**非空**文本 delta 到达客户端 | 排队、输入长度、Prefill、调度 |
| **GEN**（Generation Time） | 首 Token → 模型生成结束 | 输出长度、Decode 速度、模型强度 |
| **E2E**（End-to-End Latency） | 用户操作 → Agent 最终完成 | 网络、模型、Loop、工具、存储、渲染 |

⚠️ **工程陷阱**：首个 SDK chunk 不一定包含文本（可能只是 `role` 元数据）。TTFT 必须记录**首个非空**的 `text.delta`，而不是"收到的第一个 chunk"，否则指标会失真。

---

## 3. 三层协议：HTTP / SSE / Harness Event

网络 Chunk、SSE Event、模型 Token 不是一回事，必须清楚区分三层：

```
Layer 1 · HTTP 传输层     —— 把响应 Body 分批送到客户端（chunked transfer）
Layer 2 · SSE 帧协议      —— 规定一条事件如何编码和分隔（text/event-stream）
Layer 3 · Harness 事件协议 —— 规定 text.delta / tool.started 等业务含义
```

**常见错误**：假设"1 Token = 1 SSE = 1 Chunk"一一对应。实际上一次 `read()` 可能只拿到半条 SSE，也可能一次拿到三条 SSE 粘在一起。

**正确姿势**：把客户端写成"无限字节流 + 状态机"：`buffer 累积 → lines 切分 → events 解析 → RunEvent`。

---

## 4. SSE 与 EventSource

**SSE（Server-Sent Events）** 是基于 HTTP 的单向事件流协议，格式如下：

```
id: 17
event: text.delta
data: {"run_id":"run_123","seq":17,"delta":"正在分析"}

: heartbeat        ← 注释行，用于心跳，不占用业务 seq
```

| 字段 | 含义 |
|---|---|
| `event` | 事件类型，客户端用 `addEventListener` 路由 |
| `data` | 事件负载，通常是 JSON 字符串 |
| `id` | 事件位置，用于断线重连（`Last-Event-ID`） |
| 空行 | 一条 SSE 事件的分隔符 |

**EventSource** 是浏览器原生 JS API，用来"消费"SSE 流。它的能力边界很清楚：

- ✓ 自带断线自动重连（浏览器原生行为，无需手写代码）
- ✓ 客户端负责：展示文本、事件去重、状态展示
- ✗ 客户端不负责：模型调用、工具执行、重试逻辑（这些全在服务端）

**协议选型**：普通 JSON（无增量反馈）/ **SSE**（服务端→客户端单向流，模型输出的首选）/ WebSocket（双向全双工，仅在确有双向高频场景时才需要，连接治理复杂）。**不要为了一个停止按钮就引入 WebSocket。**

---

## 5. RunEvent：统一事件协议

不管背后是哪个模型供应商，最终都要转换成统一的事件格式再往外传：

```python
class RunEvent(BaseModel):
    schema_version: Literal["1"] = "1"   # 协议演化护栏，未知type不应让旧客户端崩溃
    run_id: str
    seq: int = Field(ge=0)               # Run内的事件位置键值：排序/去重/重放/Trace
    type: EventType                      # Literal类型，编译期即可发现拼错
    created_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)  # schema可演化字段
```

**三类事件，缺一不可**：

| 类别 | 作用 | 示例 |
|---|---|---|
| **DATA · 数据面** | 展示模型输出 | `text.delta`、`message.completed` |
| **PROCESS · 过程面** | 暴露 Loop 进度 | `tool.started`、`tool.completed`、`run.retrying` |
| **CONTROL · 控制面** | 表达唯一终态 | `run.completed`、`run.failed`、`run.cancelled` |

> 只实现 `text.delta` + `tool.completed` → 只是一个聊天 Demo；
> 补齐 `run.cancelled` / `failed` / `completed` → 才真正成为 Harness。

---

## 6. Model Adapter：屏蔽供应商差异

**绝对不能把供应商原始 Chunk 直接透传给前端**：

```python
# ❌ 反例：把 OpenAI SDK 的内部对象原样丢出去
async for chunk in deepseek_stream:
    yield chunk.model_dump_json() + "\n"
```

这样做的4个系统性后果：前端依赖具体模型SDK字段结构、换模型时下游全部跟着改、工具/取消/重试没有统一事件类型、内部字段可能意外泄露。

**正确边界**：

```
DeepSeekChunk → ModelStreamEvent → RunEvent → SSE bytes
```

Adapter 处于"协议边界"，Harness Event 处于"业务边界"。切换模型供应商时，**只替换 Adapter，Client 无需任何修改**。

> 本仓库的简化代码按需求跳过了这一层，直接用 LangChain 的 `ChatOpenAI` 调用模型，
> 这意味着换模型供应商需要改动 `agent_loop` 内部逻辑——这是有意识的简化取舍，
> 生产环境建议补上这一层抽象。

---

## 7. 六层 Harness 架构

```
① Vendor Stream    (DeepSeek/Claude/GPT chunk)
        ↓
② Model Adapter    (归一化为 ModelStreamEvent)
        ↓
③ Agent Loop        (驱动模型与工具，产生 RunEvent)
        ↓
④ Event Store       (持久化/排序/重放，facts only)
        ↓
⑤ SSE Gateway        (编码/心跳/订阅管理)
        ↓
⑥ Client            (Web/CLI/IDE，按事件类型渲染)
```

**每层职责表**：

| 组件 | 负责 | 不负责 |
|---|---|---|
| Model Adapter | 模型流转换 | UI 输出 |
| Agent Loop | 执行任务 | HTTP |
| Tool Runtime | 工具执行 | 页面展示 |
| Event Store | 保存事件 | 重新执行 |
| SSE Gateway | 推送事件 | 业务决策 |
| Client | 展示和取消 | 模型解析 |

> **核心原则**：每个组件只关心自己的"应做"与"不应做"。跨层耦合的 bug 比想象中更常出现——
> 真正难查的 bug 往往不是"某层逻辑写错了"，而是"某层偷偷做了不该它做的事"。

---

## 8. FastAPI 三接口分离

创建、订阅、取消必须是**三个独立接口**，职责互不重叠：

| 接口 | 方法 | 职责 |
|---|---|---|
| **创建** | `POST /runs` | 启动后台任务，立即返回 `run_id`（202风格，不等模型跑完） |
| **订阅** | `GET /runs/{id}/events` | SSE 持续推送，支持断线重连，**不重新发起模型请求** |
| **取消** | `POST /runs/{id}/cancel` | 修改控制状态，Loop 确认后写入 `run.cancelled` |

**分离带来的3项收益**：
- 浏览器可以原生使用 `EventSource`
- 页面刷新不会重复创建模型调用
- 客户端断线不会自动取消任务

> **核心原则**：断开连接 ≠ 取消任务。

**常见的5个转发陷阱**：伪Streaming（等完整结果才返回）、同步SDK阻塞事件循环、代理层缓冲（Nginx/CDN需关闭buffering）、每个delta都写数据库（应该批量保存文本delta，立即保存状态/工具/审批事件）、断连直接取消（应区分"用户主动停止"和"临时断线"）。

---

## 9. 客户端消费

### EventSource（最简单场景）

```
POST 创建 Run → 获取 run_id → GET 订阅事件 → 按 seq 顺序消费
```

### 渲染节流

每个 delta 都直接更新页面会导致高频 DOM 更新、性能下降。正确做法是**网络层高频接收，UI 层合并刷新**：

```javascript
let scheduled = false;
function scheduleRender() {
  if (scheduled) return;
  scheduled = true;
  requestAnimationFrame(() => {
    output.textContent = accumulatedText;
    scheduled = false;
  });
}
```

### fetch + ReadableStream（EventSource 不够用时）

当需要 POST Streaming、自定义 Authorization Header、或更灵活的请求控制时，改用 `fetch` + 手动解析：

```javascript
const reader = response.body.getReader();
const decoder = new TextDecoder("utf-8");
let buffer = "";
while (true) {
  const { value, done } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });
  // 从 buffer 里切分完整的 SSE 事件，剩余部分留在 buffer 里等下一次拼接
}
```

**4个字节流陷阱**：字节流解码要注意UTF-8边界、SSE帧切分不代表一次read()、一个chunk可能包含多条事件、buffer必须自己维护拼接。

### 多端共享同一协议

Web（EventSource + 富文本渲染）、CLI（httpx流式请求 + ANSI打印）、IDE（Webview面板）—— **三套客户端，一套 Harness Event 协议**，差异只停留在渲染层/IO层。

---

## 10. 取消机制

### 10.1 客户端取消的局限

`AbortController` 只能停止**当前 HTTP 请求的本地读取**，不能保证：
- 后台 Agent 是否真的停止
- 模型是否停止计费

**正确的取消是三步**：
```javascript
async function cancelRun(runId) {
  controller.abort();                                     // ① 停止本地读取
  await fetch(`/runs/${runId}/cancel`, { method: "POST" }); // ② 调用 Cancel API
  // ③ 服务端传播取消信号 → Loop / Model / Tool
}
```

> **核心原则**：AbortController 只能停"看"。真正"停做"必须走 Cancel API。

### 10.2 关闭连接 ≠ 停止生成

关闭 SSE 订阅后，以下事情仍会继续发生：Agent Loop 继续执行、模型仍在生成、工具仍在运行、外部副作用继续产生。

```
Subscription（订阅） → GET /runs/{id}/events  → close() 即可停（只影响客户端）
Run state（业务状态）  → POST /runs/{id}/cancel → 需要显式调用才能停
```

### 10.3 协作式取消 vs 强制取消

| | Cooperative（协作式） | Forced（强制） |
|---|---|---|
| 谁决定退出时机 | 任务自己（检查点） | 调用方（立即） |
| 是否需要业务代码配合 | 需要（`if cancel_flag.is_set()`） | 不需要 |
| 能否保存中间状态 | 能，退出前可自主清理 | 不一定，可能卡在写一半 |
| 适用场景 | 正常取消流程（默认优先） | 协作式不响应时的兜底 |

生产环境采用"先礼后兵"的5步流程：**设置取消状态 → 等待清理（宽限期）→ 超时未退出 → 终止外部（task.cancel()/SIGTERM）→ 记录本次是优雅退出还是强制打断**。

⚠️ **绝对不能用 `except Exception` 吞掉 `CancelledError`**——否则任务"假装取消成功"但实际还在跑。捕获后必须重新 `raise`。

### 10.4 工具取消比模型取消更复杂

不同副作用等级的工具，需要分别建模取消语义：

| 类型 | 示例 | 取消策略 |
|---|---|---|
| 可重试·只读 | 搜索、读取文件 | 停止等待，无副作用可重复 |
| 中等代价·长任务 | Shell、测试、构建 | 保存进程句柄，发送 SIGTERM |
| 不可逆·副作用 | 邮件、支付、发布 | 查询外部状态，禁止盲目重放 |

**支撑不可逆副作用取消的4个机制**：
1. **幂等 Key** —— 重复调用时外部系统能识别"这是同一个请求"，不会真的重复执行
2. **Tool Call ID** —— Agent 内部追踪账本，即使进程崩溃重启也能查到"调用到哪一步了"
3. **状态查询** —— 提供接口主动查询"操作最终成功了吗"，而不是靠猜
4. **人工确认** —— 查不清楚状态、又特别敏感的操作，交给人工核实

> **核心原则**：超时说明"未收到结果"，不代表"动作未发生"。`unknown` 是必须被建模的状态，不能简单二分成"成功/失败"。

### 10.5 取消状态机（CAS）

取消不能直接 `running → cancelled`，必须经过 `cancelling` 过渡态：

```
running → cancelling → cancelled
             ↓
        (给清理留出时间，避免"完成"与"取消"竞态)
```

用数据库 CAS（Compare-And-Swap）防止竞态：

```sql
UPDATE agent_runs
SET status = 'cancelling'
WHERE run_id = :run_id
  AND status IN ('created', 'running');   -- 只有当前确实"还没结束"才允许更新
```

**这个前提检查是双向的**：不仅取消要检查"当前是不是还在running"，Loop 收尾写 `completed` 时也要检查"当前是不是还在running"（不包含`cancelling`）——两边都做条件更新，才能真正杜绝"完成覆盖取消"或"取消覆盖完成"的竞态问题。

### 10.6 取消后的部分输出处理

取消时必须保存6项内容：已输出文本、最后事件序号(`last_seq`)、当前Loop步骤、工具状态(`call_id`/是否已发出)、Token使用量、取消原因(`cancelled_at`)。

**半成品不能直接提交**（不完整的JSON、半执行的SQL、拼接中断的Patch），但**已确定发生的事实可以安全重放**（已输出的文本、终态`run.cancelled`、已用Token、已跑的Tool）。

> **核心原则**：取消即存档点，不等于可执行点。

---

## 11. 流式失败与恢复

### 11.1 Streaming 失败的5个阶段

越晚失败，已产生事实越多，越不能直接重试：

```
连接(无事实) → 响应(无事实) → 首事件(role chunk) → 持续事件(大量delta) → 终态
   └────── A·可有限重试 ──────┘   └─B·有限重试─┘   └──C·重放不重跑──┘
```

**判定标准**（不是靠"数时间"，而是靠具体检查点）：
- 能否在 Event Store 里查到 `seq ≥ 0` 的已落库业务事件？
- **查不到** → 还在"连接/响应"阶段，可以安全地整个重来
- **查得到** → 已经跨过"首事件"门槛，之后都归为"持续事件"，必须谨慎处理

### 11.2 为什么不能简单重试模型

LLM 不是确定性的，第一次输出到一半网络断了、重新请求，模型很可能给出完全不同的另一条逻辑路径。如果把两段拼接在一起，会产生自相矛盾的输出。

**必须区分3种完全不同的恢复概念**：

| 类型 | 含义 |
|---|---|
| **R-01 Transport Resume** | 恢复事件传输，走 Event Store 重放（模型没被重新调用） |
| **R-02 Model Retry** | 重新调用模型，**仅当尚未产生业务事件** |
| **R-03 Task Resume** | 从 Checkpoint 继续下一步（用于多步骤长任务） |

### 11.3 有边界的连接重试（Model Retry 的具体规则）

**白名单**（可以自动重试）：DNS错误、网络失败、429（限流）、503（服务不可用）
**黑名单**（绝不能机械重试）：认证失败、参数错误、Schema错误、内容拒答

6项必须限制：`max_attempts` 上限、`max_total_seconds` 总耗时上限、白名单过滤、指数退避、jitter抖动、Run级别预算封顶。

> **CRITICAL CHECK**：是否已发出业务事件？
> **未发出** → 允许有限重试
> **已发出** → 只能走 Replay，重新调用 model 会产生"第二份独立输出"，绝不能拼接

### 11.4 断线续传核心：事件重放

> **断线续传恢复的是 Harness Event，不是模型 Token。**

| 角色 | 职责 |
|---|---|
| 客户端 | 保存 `Last-Event-ID`，重连时通过 header 自动回传（浏览器原生行为） |
| 服务端 | 按 `seq` 查询：`SELECT * WHERE run_id=? AND seq > last ORDER BY seq ASC`，重放顺序=写入顺序 |

**效果**：不重新调用模型、不产生重复任务、客户端按 `seq` 去重。整个断线重连流程本质上是一次纯粹的数据库查询+补发，跟"重新触发模型生成"没有任何关系——客户端永不因网络抖动被重复计费。

---

## 12. Event Store 与实时通知

职责分离：**PostgreSQL 保存事实，Redis 做实时通知**。

| | PostgreSQL（FACTS） | Redis（NOTIFY） |
|---|---|---|
| 特性 | 强一致、可重放、永远不能丢事实 | 不重、可丢、高速 |
| 唯一约束 | `(run_id, seq)` | — |
| 定位 | 系统的"事实最终来源" | 只是"提醒"，本身不可作为事实来源 |

**完整流程**：`Loop写事件 → 保存Postgres → Redis发布通知 → Gateway拉取 → 客户端按seq去重`。

**为什么要分两套**：如果只轮询 Postgres，延迟高、浪费资源；Redis 负责"尽快通知"，即使某条通知因网络抖动丢失也无所谓——因为真正的数据早已安全存在 Postgres 里，客户端随时可以重新查询补上。

> **核心原则**：通知可以丢，事实事件不能丢。

---

## 13. Checkpoint：长任务恢复

Streaming 解决的是"看得快"，解决不了：Context Window 限制、一次性读不完所有token、Worker中途崩溃导致任务丢失。

长任务需要3项配套能力：**分步骤执行、保存中间状态、支持恢复**。

**Checkpoint 的6个字段**：

```json
{
  "loop_step": 15,                 // ① 当前步骤
  "completed": ["file1", "..."],   // ② 已完成内容
  "next_cursor": "file15.py",      // ③ 下一步位置
  "context_digest": "...",         // ④ 上下文摘要（不是完整历史）
  "tool_state": {...},             // ⑤ 工具状态
  "version": "1"                   // ⑥ checkpoint版本
}
```

> **核心原则**：Checkpoint 保存可恢复状态，不是把全部历史塞回 Prompt。

---

## 14. 恢复矩阵：断线 vs 崩溃

| 场景 | 触发条件 | 恢复方式 |
|---|---|---|
| ① 客户端断线+短暂 | 网络抖动几秒 | 重新订阅，服务端无需任何动作 |
| ② 客户端断线+长期 | 长时间无人订阅 | 服务端后台清理Run，通知前端已回收 |
| ③ Worker崩溃+未落地 | 进程死，无外部副作用未确认 | 读Checkpoint恢复，重跑未完成步骤 |
| ④ Worker崩溃+涉及外部副作用 | 进程死时正卡在有副作用的调用中 | 标记`unknown`状态，必要时人工确认 |

**核心区分**：客户端断线时"模型继续工作"（服务器没死，只是连接断了），走**重放**；Worker崩溃时"Work in memory lost"（服务器本身死了），走**Checkpoint**。

---

## 15. 错误事件协议

客户端需要的（稳定）：错误码`STABLE_CODE`、是否可重试、恢复方式、失败阶段定位。
服务端自己保留（不透传）：原始异常、Trace ID、Request ID、Token使用、工具副作用状态。

**绝不能暴露给客户端**：API Key、Prompt原文、内部堆栈。

```json
{
  "type": "run.failed",
  "data": {
    "code": "TOOL_TIMEOUT",
    "stage": "tool.completed",
    "retryable": false,
    "hint": "需要确认是否属于 unknown 状态"
  }
}
```

---

## 16. 测试重点：11项边界

不要只测"页面会不会打字"，必须专门覆盖这些容易被忽略、但线上一定会撞见的边界情况：

1. SSE 编码（特殊字符是否破坏格式）
2. 拆包恢复（一次 read() 只收到半条事件）
3. 粘包处理（一次 read() 收到多条事件粘在一起）
4. seq 顺序（乱序到达能否正确重排）
5. 事件去重（重复推送能否正确过滤）
6. 断线续传（Last-Event-ID 重连能否精确补发）
7. 取消传播（取消信号是否真正传到Loop/Model/Tool）
8. 完成/取消竞态（CAS能否保证只有一个终态）
9. 流失败恢复（R-01/R-02/R-03 是否分别正确触发）
10. checkpoint恢复（崩溃重启能否从正确步骤继续）

> **核心原则**：Harness 的 bug 都是"小颗粒度边界条件"，不是大架构错误。

---

## 17. 本仓库代码实现对照表

`simple_streaming_agent.py` 是本笔记对应的简化实现（用 LangChain 直连模型，跳过了 Model Adapter 抽象层），对照上述知识点的覆盖情况：

| 知识点 | 覆盖情况 | 代码位置 |
|---|---|---|
| RunEvent 统一协议 | ✓ 已实现 | `RunEvent` dataclass |
| 三接口分离（创建/订阅/取消） | ✓ 已实现 | `create_run` / `subscribe` / `cancel_run` |
| 协作式取消 | ✓ 已实现 | `agent_loop` 里的 `cancel_flag.is_set()` 检查 |
| 强制取消兜底 | ✓ 已实现 | `cancel_run` 里的 `asyncio.wait_for` + `task.cancel()` |
| 断线重放（Last-Event-ID） | ✓ 已实现 | `subscribe` 里的 `Last-Event-ID` header 解析 |
| Model Retry（有边界的连接重试） | ✓ 已实现 | `agent_loop` 里的 `while True` 重试循环 + 白名单/黑名单分类 |
| 结构化失败协议 | ✓ 已实现 | `run.failed` 事件带 `code/stage/retryable` |
| TTFT 统计 | ✓ 已实现 | `Run.first_delta_at` 字段 |
| Trace ID | ✓ 已实现 | `Run.trace_id` 字段，贯穿所有事件 |
| Checkpoint（简化版） | ✓ 已实现（简化） | `save_checkpoint` / `load_checkpoint`，落盘到本地JSON文件 |
| Model Adapter 抽象层 | ✗ 未实现 | 按需求直接用 LangChain `ChatOpenAI`，未做供应商隔离 |
| Postgres/Redis 分离 | ✗ 未实现 | Run状态仍是内存 dict + 本地文件，未做"事实/通知"分离 |
| 多步骤 Task Resume | ✗ 未实现 | 当前只有单次模型调用，没有"步骤"概念，暂无实际内容可续跑 |
| 客户端 SSE 解析器（拆包/粘包处理） | ✗ 未实现 | 只写了服务端编码，没写健壮的客户端解析代码 |
| 11项边界测试 | 部分覆盖 | 已写7个测试覆盖主要路径，未覆盖全部11项 |