# Tool 安全治理：一次工具调用的完整护栏

> **依据**
> - `tool_runtime_real/README.md`：注册 → 发现 → 执行的骨架（来自 2-2）
> - `week02/2-4.pdf`《工具治理和安全边界》：在骨架每一步上加的细治理
>
> **怎么读**
> - 第 1 章：一页看懂全貌。
> - 第 2–9 章：按一次调用的生命周期，从前往后逐步展开。
> - 第 10–11 章：怎么验收，以及常见问题。
> - 附录：和原 README 相比升级了什么。
>
> 标 **【补充】** 的内容，是两份材料都没覆盖、我补上的衔接设计。

---

## 目录

1. [一页看懂](#1-一页看懂)
2. [准备：可信上下文 ExecutionContext](#2-准备可信上下文-executioncontext)
3. [注册：工具上架前要想清楚的事](#3-注册工具上架前要想清楚的事)
4. [发现：这一轮给模型看哪些工具](#4-发现这一轮给模型看哪些工具)
5. [prepare：不该执行的调用，绝不进入 execute](#5-prepare不该执行的调用绝不进入-execute)
6. [execute：允许执行之后，怎样不失控](#6-execute允许执行之后怎样不失控)
7. [finalize：结果出门前的收口](#7-finalize结果出门前的收口)
8. [写回 Agent Loop：错误码决定下一步](#8-写回-agent-loop错误码决定下一步)
9. [编排：多个调用一起来的时候](#9-编排多个调用一起来的时候)
10. [验收：怎么证明治理真的生效](#10-验收怎么证明治理真的生效)
11. [常见问题](#11-常见问题)
- [附录：相对原 README 的升级点](#附录相对原-readme-的升级点)

---

## 1. 一页看懂

### 1.1 核心思想

1. **模型只提出"候选动作"，候选动作不是执行许可证。** 要不要真的执行，由确定性的工程规则决定。
2. **治理不是让所有调用都成功。** 该成的成；不该成的，在副作用发生**之前**停下，并返回一个稳定、可解释、可追踪的结果。
3. **所有调用都走同一个入口 `Runtime.invoke()`，检查顺序固定。** 治理代码只有放在谁都绕不过的入口里，才算安全边界。

### 1.2 先看效果：六次调用的治理结果

下面是 2-4 演示里的六次调用，场景是退款。

| # | 调用 | 结果 | 原因 |
|---|---|---|---|
| 1 | `get_order` 查订单 | 放行 `OK` | 正常查询；返回里的邮箱和 token 已脱敏 |
| 2 | `create_refund`，用户还没确认 | 暂停 `APPROVAL_REQUIRED` | 资金操作风险高，等用户确认 |
| 3 | `create_refund`，用户已确认 | 放行 `OK` | 审批和参数完全匹配，带幂等键执行 |
| 4 | `create_refund`，参数里偷塞了 `user_id`、`approved` | 拒绝 `INVALID_ARGUMENT` | 严格 Schema 不接受多余字段 |
| 5 | `run_shell`，当前是 bypass 模式 | 拒绝 `DENY_RULE` | deny 规则优先级最高，bypass 也盖不住 |
| 6 | `create_refund`，当前是 plan 模式 | 拒绝 `PLAN_MODE_DENIED` | plan 模式只读 |

最终副作用：**退款真实执行 1 次，Shell 执行 0 次，审计记录 8 条。**

### 1.3 全流程

```
用户请求
  → [0] 建立可信上下文      谁在调用、什么权限，只来自服务端
  → [1] 注册               工具上架时一次性完成
  → [2] 发现               决定本轮给模型看哪些工具
  → 模型生成 Tool Call     只是候选动作
  → [3] prepare            判断这一次能不能执行
  → [4] execute            安全地执行
  → [5] finalize           结果投影、脱敏、审计
  → [6] 写回 Agent Loop    按错误码决定下一步

  多个调用同时到达时 → [7] 编排（invoke_many）
```

### 1.4 每个环节回答什么问题、靠什么手段

| 环节 | 回答的问题 | 主要治理手段 |
|---|---|---|
| 可信上下文 | 是谁在调用？ | 身份、租户、权限只来自服务端 |
| 注册 | 平台认不认识这个工具？它有多危险？ | 风险建模、严格参数、能力收窄 |
| 发现 | 本轮给模型看哪些工具？ | 版本路由、启停过滤、白名单（第 1 次）、冻结快照 |
| prepare | 这一次调用允许发生吗？ | deny、**权限模式**、白名单（第 2 次）、RBAC、**业务预检查**、审批 |
| execute | 执行时怎样不失控？ | 超时、**恢复**（按幂等决定是否重试）、幂等键、资源锁 |
| finalize | 结果给谁看、看多少？事后能不能证明？ | **脱敏**、**审计** |
| 写回 Loop | 失败之后下一步做什么？ | 稳定错误码 |
| 批量 | 多个调用怎么跑？ | **编排**：并行、串行、按资源排队 |

> 关于术语：本文的"白名单"指本轮允许使用的工具集合 `allowed_tools`；"审批"和"人工确认"是同一件事；"副作用"指对外部世界产生的真实改变，比如扣款、写库。

---

## 2. 准备：可信上下文 ExecutionContext

**一句话：** 谁在调用、属于哪个租户、有什么权限，这些"授权事实"只能来自服务端，不能来自模型。

### 2.1 结构和来源

```python
@dataclass(frozen=True, slots=True)
class ExecutionContext:
    trace_id: str
    user_id: str
    tenant_id: str
    mode: PermissionMode                 # default / plan / bypassPermissions / dontAsk
    permissions: frozenset[Permission]   # 细粒度权限，如 "order:read"
    allowed_tools: frozenset[str]        # 本轮工具白名单
    approval_id: str | None = None
```

| 字段 | 从哪里来 |
|---|---|
| `user_id` | 网关解析 JWT |
| `tenant_id` | 会话上下文 |
| `permissions` | 查询 IAM |
| `allowed_tools` | Agent 配置、租户策略、任务策略 |
| `approval_id` | 用户在可信界面确认后，由审批服务生成 |
| `trace_id` | 服务端生成，用来串起整次运行 |

这个对象由 Web 鉴权中间件创建，并且不可变（`frozen=True`）。它贯穿整条调用链，所有权限判断都基于它。

### 2.2 划清界线：哪些字段模型可以给，哪些不行

| 模型可以给（业务候选参数） | 模型绝不能给（授权事实） |
|---|---|
| `order_id`、`amount`、`reason` | `user_id`、`tenant_id` |
| `shop_id`、`product_id`、`size` | 角色、权限 |
| | `approved`、`approval_id` |

**例子：** 当前 token 属于 user_456，模型却在参数里写"帮我查 user_123 的订单"。系统不会采信这个说法：查询订单时只用 `context.user_id` 去核对归属，订单不属于当前用户就直接拒绝。

---

## 3. 注册：工具上架前要想清楚的事

**一句话：** 让平台认识这个工具，并且在上线前把它的风险说清楚。注册成功只代表"平台认识它"，不代表本轮会开放，也不代表已经执行过。

### 3.1 一个工具由什么组成

```python
@dataclass(frozen=True, slots=True)
class ToolDefinition:
    # ① 调用契约：唯一会投影给模型看的部分
    name: str
    version: str
    description: str
    parameters_model: type[StrictArgs]   # 严格的参数模型（见 3.3）

    # ② 运行治理：模型永远看不到
    policy: ToolPolicy                   # 风险策略（见 3.2）

    # ③ 真实实现
    handler: Handler

    # ④ 2-4 新增
    canonical_target: CanonicalTarget    # 规范化目标函数
    precheck: Precheck | None = None     # 业务预检查函数（见 5.6）
```

关于 `canonical_target`：课件只给了"规范化目标函数"这个名字。从用途推断，它负责把参数归一成稳定形式，审批的参数摘要和资源键都依赖这种稳定表示。

### 3.2 风险建模：先回答 7 个问题，再填 ToolPolicy

给工具分级的依据是**副作用、数据敏感度和恢复成本**，而不是工具的名字。

| 要回答的问题 | 落到哪个字段 |
|---|---|
| 1. 会不会改变外部状态？ | `effect`（READ / WRITE） |
| 2. 读到的数据敏感吗？ | 数据分级，供 finalize 阶段脱敏 |
| 3. 需要哪项业务权限？ | `permission` |
| 4. 影响面是单条资源、一个租户，还是整个环境？ | `risk` |
| 5. 失败后能安全重放吗？ | `idempotent`、`max_retries` |
| 6. 需要用户确认具体参数吗？ | `requires_approval` |
| 7. 超时、并发、审计用什么策略？ | `timeout_seconds`、`resource_key` 等 |

```python
@dataclass(frozen=True, slots=True)
class ToolPolicy:
    effect: Effect              # READ / WRITE
    risk: Risk                  # 低 / 中 / 高 / 极高
    permission: Permission      # 如 "refund:create"
    requires_approval: bool
    timeout_seconds: float
    max_retries: int
    idempotent: bool            # 重复执行是否安全
    execution_mode: str         # "parallel" / "sequential"（来自原 README）
    dependencies: tuple[str, ...]
    resource_key: str | None    # 【补充】例如 "order_id"：同一资源的写操作要排队
```

每个字段都必须有明确的消费者：
- PermissionEngine 读 `permission`、`risk`、`requires_approval`；
- 执行器读 `timeout_seconds`、`max_retries`、`idempotent`、`resource_key`；
- 批量调度读 `execution_mode`。

**示例策略表：**

| 工具 | 副作用 | 风险 | 权限 | 人工确认 | 自动重试 |
|---|---|---|---|---|---|
| `get_order` | 只读，含客户信息 | 中 | `order:read` | 不需要 | 瞬时失败可有限重试 |
| `create_refund` | 写，产生资金变化 | 高 | `refund:create` | 必须，绑定订单和金额 | 非幂等，禁止盲目重试 |
| `run_shell` | 取决于命令，影响面可能很大 | 中到极高 | `shell:run` | 危险动作要确认，或直接拒绝 | 默认不重试 |

### 3.3 参数模型要"严"

```python
class StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)   # 多余字段直接拒绝，类型不做自动转换

class CreateRefundArgs(StrictArgs):
    order_id: str = Field(pattern=r"^ord_[0-9]{4}$")
    amount: float = Field(gt=0, le=10_000)
    reason: str = Field(min_length=4, max_length=200)
```

下面这些输入都是合法的 JSON，但都会被拦下：

| 输入 | 被哪条规则拦下 |
|---|---|
| `"amount": -1` | `gt=0` |
| `"amount": 99999999` | `le=10_000` |
| `"order_id": "../../etc/passwd"` | `pattern` |
| 参数里多了 `"user_id": "admin", "approved": true` | `extra="forbid"` |

规则：`user_id`、`tenant_id`、角色、权限、`approved`、`approval_id` **不允许出现在任何参数模型里**。

### 3.4 不要注册"万能工具"

```python
# ✗ 不要这样做
run_sql({"sql": "模型生成的任意 SQL"})
run_shell({"command": "模型生成的任意命令"})
```

```python
# ✓ 把能力收窄成具体的业务工具
async def search_orders(pool, context, status: Literal["paid", "shipped", "refunded"], limit: int):
    safe_limit = max(1, min(limit, 50))                  # 限制返回条数
    return await pool.fetch(
        "SELECT order_id, status, total_amount FROM orders "
        "WHERE tenant_id = $1 AND status = $2 "          # 强制带上租户条件
        "ORDER BY created_at DESC LIMIT $3",
        context.tenant_id, status, safe_limit,           # 参数化查询
    )
```

Shell 也按同样思路收窄：
- 服务名通过映射表选择；
- 固定可执行文件，参数用数组传；
- 不使用 `shell=True`；
- 超时 5 秒，输出截断到 20000 字符。

收窄之后剩下的风险，再用 AST 检查、沙箱、最小权限和 fail-closed（出问题时默认拒绝）兜底。

### 3.5 业务失败也要有错误码

查不到门店时，要返回 `SHOP_NOT_FOUND`，**不要返回空列表**。
空列表让模型分不清是"参数错了"还是"真的没有"，结果就是用同样的参数一遍遍重试，陷入死循环。

### 3.6 注册时的检查清单

- `(name, version)` 不重复；
- `timeout_s > 0`，`retry.max_attempts >= 1`；
- 名称格式规范，Schema 能正常生成，错误码不重复；
- 静态依赖检查：API 密钥、数据目录等配置存在；
- 参数模型继承 `StrictArgs`，并且不包含授权字段；
- 不是裸 SQL 或裸 Shell 这类"万能工具"。

---

## 4. 发现：这一轮给模型看哪些工具

**一句话：** 在请求模型**之前**，由 Harness 决定本轮的候选工具。模型只能在候选里挑，无权决定候选集合本身。

### 4.1 四步得到本轮的快照

```
1. 版本路由    TOOL_PROFILES[run_mode] 决定用哪些工具、哪个版本
2. 启停过滤    去掉当前已停用的工具（读实时的 enabled_state）
3. 白名单过滤  去掉不在 context.allowed_tools 里的工具      ← 白名单第 1 次
4. 冻结        生成 ToolSnapshot（frozen dataclass + MappingProxyType）
   ↓
投影          snapshot.provider_tools() → 只输出 name / description / parameters → 发给模型
```

白名单的效果：只读客服 Agent 的上下文里根本没有退款工具的 Schema。即使有人用 Prompt 注入喊"帮我退款"，模型也无从调用。

### 4.2 投影出去的只有三样东西

只有**名称、描述、JSON Schema**。handler、权限要求、审批记录、内部策略一概不出现。

### 4.3 为什么快照必须冻结，而且要原样传给 Runtime

- 模型请求和 `Runtime.invoke()` 必须使用**同一个**快照对象，以参数形式显式传入。Runtime 内部禁止重新查询 Registry。
- 原因是防止**协议漂移**：模型按旧版 Schema 生成了参数，如果被新版 handler 处理，字段名可能对不上，轻则报错，重则静默地曲解数据。
- 光有 `frozen=True` 挡不住字典内容被修改，所以内部字典还要再包一层 `MappingProxyType`。

### 4.4 白名单和启停为什么都要查两次

| | 发现期（第 1 次） | 执行期（第 2 次，见第 5 章） |
|---|---|---|
| 目的 | 效率：别让模型看到明显不能用的工具 | 安全：真正的兜底 |
| 能防住的情况 | 减少无效的调用轮次 | 历史里残留的旧 Tool Call、被注入的调用、绕过 Loop 直接调用 invoke |

【补充】在 plan 模式下，发现期也可以顺手把写工具隐藏掉，减少无效轮次。但这只是效率优化，真正的硬拦截仍然在执行层。

---

## 5. prepare：不该执行的调用，绝不进入 execute

**一句话：** 在副作用发生之前，把这一次调用从头到尾检查一遍。只要有一步不通过，就立刻返回结构化结果，handler 一次都不会被调用。

### 5.1 完整的检查顺序

| # | 检查 | 回答的问题 | 不通过时 |
|---|---|---|---|
| 1 | 查找工具 | 快照里有这个工具吗？（查快照，不查 Registry） | `TOOL_NOT_FOUND` |
| 2 | 启停复查 | 工具此刻还启用吗？（读实时状态） | `TOOL_DISABLED` |
| 3 | 参数校验 | 参数符合 Schema 吗？有没有多余字段？ | `INVALID_ARGUMENT` |
| 4 | deny 规则 | 是否命中硬拒绝规则？ | `DENY_RULE` |
| 5 | 权限模式 | 当前模式允许这类动作吗？ | `PLAN_MODE_DENIED` |
| 6 | 执行期白名单 | 本轮允许用这个工具吗？ | `TOOL_NOT_ALLOWED` |
| 7 | RBAC | 当前用户有这项业务权限吗？ | `PERMISSION_DENIED` |
| 8 | 依赖检查 | 依赖的下游系统现在健康吗？ | `DEPENDENCY_UNAVAILABLE` |
| 9 | 业务预检查 | 资源归属、当前状态、额度允许吗？ | `BUSINESS_RULE_DENIED` |
| 10 | before hook | 业务方自定义的规则通过吗？ | 自定义错误码 |
| 11 | 审批校验 | 高风险动作：用户确认过的正是这组参数吗？ | `APPROVAL_REQUIRED`（confirm） |
| 12 | 放行规则 | allow 规则 → 危险命令检测 → 默认放行 | 危险命令被拒 |
| 13 | 消费审批 | 原子地把审批标记为"已使用" | `APPROVAL_REQUIRED` |

第 4 到 11 步就是 2-4 规定的"不可交换"的决策顺序：deny → plan → 白名单 → RBAC → 业务预检查 → 强制审批 → bypass → 普通 allow。
第 2、8、10 步来自原 README。

**为什么这样排：**
- **便宜的、跟参数无关的检查放前面**（第 1、2 步）。
- **参数先校验成合法对象**（第 3 步），后面的检查才能放心使用它。
- **deny 放在所有决策之前**：无论什么模式、什么 allow 规则，都盖不住它。
- **业务预检查放在审批之前**：不让用户去确认一个根本做不成的动作，比如一杯已经取走的咖啡。
- **审批放在 bypass 之前**：自动化模式也跳不过高风险确认。
- **【补充】审批最后才消费**：前面任何一步拒绝，都不会白白作废用户的确认。消费必须是原子操作（compare-and-set），防止两个并发的重放同时通过。

### 5.2 三态决策：allow / deny / confirm

```python
PermissionDecision(
    action,   # allow | deny | confirm
    code,     # 稳定错误码，如 "PERMISSION_DENIED"
    reason,   # 可以给模型看的安全说明
    source,   # 哪条规则做出的决定：whitelist / rbac / precheck / approval ...
)
```

- **deny** 和 **confirm** 都要立刻返回结构化的 ToolResult，handler 零调用。
- **confirm 不等于 allow**：它表示"暂停，等人确认"。

### 5.3 "合法"其实分五层

```
JSON 能解析  ≠  Schema 合法  ≠  业务合法  ≠  有权限  ≠  用户已确认
```

| 层 | 检查什么 | 数据来源 | 失败例子 |
|---|---|---|---|
| Schema | 字段、类型、格式、范围 | ToolDefinition | 金额为负、多了未知字段 |
| 业务预检查 | 归属、状态、额度 | Repository / 业务 API | 订单不属于本租户、已退款、超出可退金额 |
| 权限 | 主体资格 | ExecutionContext / IAM | 普通客服没有 `refund:create` |
| 审批 | 用户是否确认了这个具体动作 | 审批服务 | 金额被改、审批过期、审批已用过 |

参数校验失败时，返回结构化错误 `[{"path": "...", "message": "..."}]`，方便模型修正参数。

### 5.4 权限模式

| 模式 | 用在哪 | 效果 | 依然拦得住的 |
|---|---|---|---|
| `default` | 普通交互 | 必要时返回 confirm | deny、白名单、RBAC、业务检查、审批 |
| `plan` | 规划、分析阶段 | **所有写操作和 Shell 在执行层被拒** | deny、白名单、RBAC（读操作照常检查） |
| `bypassPermissions` | 受控环境里的自动化 | 跳过普通的低风险确认 | deny、plan、白名单、RBAC、业务检查、**强制审批** |
| `dontAsk` | 无人值守，没法和用户交互 | 需要确认的动作**直接 deny** | 同 default，并且绝不偷偷执行 |

### 5.5 三道权限闸门，各管一件事

| 闸门 | 管什么 | 信谁 | 拦下之后 | 例子 |
|---|---|---|---|---|
| 白名单 | 这个 Agent 本轮有哪些能力 | Agent 配置、租户策略、任务策略 | 本轮不再尝试 | 只读客服看不到退款工具 |
| RBAC | 这个人有没有资格 | 鉴权系统给出的权限 | 换有权限的人，或者结束 | 普通客服没有 `refund:create` |
| 人工确认 | 这一次具体动作对不对 | 服务端审批记录 | 暂停，进入确认流程 | 用户确认"ord_1001 退款 399 元" |

三道闸门层层兜底：
- 白名单让模型压根看不到退款工具；
- 就算有人伪造出退款的 Tool Call，RBAC 也会拦下；
- 就算权限够，参数和审批对不上照样拒绝。

注意：RBAC 用**细粒度权限**（`refund:create`）判断，而不是角色名（"客服"）。

### 5.6 业务预检查

预检查回答的是"这件事此刻在业务上能不能做"。它的数据来源是 Repository 或业务 API，不依赖模型的任何判断。

```python
async def refund_precheck(args: CreateRefundArgs, ctx: ExecutionContext) -> None:
    order = ORDERS.get((ctx.tenant_id, args.order_id))     # 用服务端的租户去查
    if not order or order["status"] != "paid":
        raise PolicyDenied("BUSINESS_RULE_DENIED", "订单不存在或状态不可退款")
    if args.amount > float(order["refundable"]):
        raise PolicyDenied("BUSINESS_RULE_DENIED", "退款金额超过可退金额")
```

**例子：** 用户说"取消最近一单"，但这杯咖啡 5 分钟前已经被取走（状态是 `picked_up`）。预检查直接返回 `BUSINESS_RULE_DENIED`，handler 根本没有机会执行。

### 5.7 审批：绑定参数，并且只能用一次

**流程：**

```
模型发起 create_refund(ord_1001, 399)
  → 返回 confirm（APPROVAL_REQUIRED），handler 不执行
  → Agent Loop 暂停
  → 可信界面向用户展示："退款 ord_1001，399 元"
  → 用户确认，审批服务生成 approval_id
  → 原参数 + approval_id 重新进入 Runtime
  → 绑定信息全部匹配才放行；任何一项对不上，就再次 confirm
```

**审批记录绑定了这些内容：**
- `user_id`、`tenant_id`、`tool_name`；
- 参数的 SHA-256（先递归排序键、再做稳定的 JSON 序列化，然后计算哈希）；
- 过期时间；
- 一次性的 `used` 标记。

**带来的效果：**
- 批准的是 100 元，参数被改成 399 元 → 旧审批失效。
- 同一个审批成功用过一次后再重放 → 回到 confirm，副作用总数仍然是 1。
- 模型自己写 `approved=true` → 没有任何用（而且会先被 `extra="forbid"` 挡掉）。

**为什么不能只记录"哪个 call.id 被批准过"：** call.id 只能证明"这次调用被点过确认"，证明不了"用户确认的就是这个金额"。

【补充】建议把工具的 **version** 也绑进审批记录（见 11.2）。

### 5.8 before hook

业务方可以挂载自定义检查，比如"非营业时间不能下单"。hook 可以返回错误、阻止执行。好处是这类规则不需要改 Runtime 的核心代码。

---

## 6. execute：允许执行之后，怎样不失控

**一句话：** handler 一旦被调用，真实的副作用就发生了，而且不可逆。这一阶段的任务是控制超时、重试和并发。

### 6.1 先分清四种"重试"

| 类型 | 发生在哪 | 会产生新的模型决策吗 | 会重放同一个业务动作吗 | 例子 |
|---|---|---|---|---|
| Provider 重试 | 调用 LLM API 时 | 否 | 否，还没进 handler | 429、模型服务 5xx |
| Agent 重新规划 | 结果回填后的下一轮 | 是，会生成新的 Tool Call | 不一定 | 参数错了，模型修正金额 |
| Runtime 重试 | execute 阶段 | 否，沿用同一个 Tool Call | **是，所以必须看幂等性** | 只读查询遇到数据库断连 |
| 业务状态恢复 | 结果未知或长流程中断后 | 不一定 | 先查状态，不直接重放 | 支付超时后去查退款单 |

### 6.2 重试次数由工具属性算出来

```python
retries = (
    tool.policy.max_retries
    if tool.policy.effect is Effect.READ or tool.policy.idempotent
    else 0                                   # 非幂等的写操作：一次都不重试
)

for attempt in range(retries + 1):
    try:
        async with asyncio.timeout(tool.policy.timeout_seconds):
            return await tool.handler(tool_call_id, arguments, context)
    except TransientToolError:               # 只有瞬时错误才重试
        if attempt == retries:
            raise
        await asyncio.sleep(min(0.05 * (2 ** attempt), 0.2))   # 指数退避
```

### 6.3 恢复策略速查

| 失败场景 | 应该这样做 | 不能这样做 |
|---|---|---|
| 参数错误 | 返回 `INVALID_ARGUMENT`，让模型修正参数 | 用原参数重试 |
| 权限不足或被 deny | 结束、换有权限的主体，或转人工 | 让模型改写提示词去绕过 |
| 只读操作瞬时失败 | 在总 deadline 内指数退避，有限次重试 | 无限重试占满资源 |
| 幂等写操作失败 | 用**同一个**幂等键有限重试 | 每次生成新的幂等键 |
| 非幂等写操作超时 | 返回 `TIMEOUT_UNKNOWN`，去查真实状态 | 直接重放 |
| 依赖持续不可用 | 用缓存、只读降级，或人工兜底 | 伪造一个成功结果 |

### 6.4 幂等键

- 演示里用 `idempotency_key = tool_call_id`，并传给下游接口。
- 重试时必须复用同一个键。每次都换新键，就等于没有幂等保护。
- 接真实的支付接口时，四件套缺一不可：**幂等键 + 超时 + 状态查询接口 + 对账**。

### 6.5 写操作超时：TIMEOUT_UNKNOWN

超时只说明"调用方没收到结果"，不说明"下游没执行成功"。所以：

```
非幂等写操作超时
  → 不抛异常、不重试，返回 TIMEOUT_UNKNOWN
  → Agent Loop 强制调用查单接口
      → 查到了：按实际结果继续
      → 查不到：报错，转人工处理
```

反面例子：`createOrder` 超时后自动重试 3 次，结果瑞幸收到 3 笔订单，用户被扣了 3 倍的钱。

### 6.6 同一资源的写操作要排队

```
写操作，并且声明了 resource_key
  → 获取 "tenant_id:order_id" 锁（asyncio.Lock）
  → 在超时时间内执行 handler
  → 释放锁

读操作，或者没有资源键
  → 直接执行，瞬时错误按策略重试
```

这样同一个订单的两笔退款不会同时执行，不同订单之间的操作照样可以并行。

### 6.7 沙箱和权限判断是两回事

- **权限判断**回答"这个动作该不该做"，关乎业务正当性。
- **沙箱 / 隔离**回答"代码就算失控，最多能伤到哪里"，关乎技术上的爆炸半径。

两者不能互相替代。

### 6.8 异常不外泄

handler 内部的异常要捕获，并归一化成 `TOOL_ERROR`（原 README 叫 `INTERNAL_ERROR`）。原始 traceback 只进受保护的诊断通道，绝不进入模型消息。

---

## 7. finalize：结果出门前的收口

**一句话：** 保证吐出去的结果干净、可信、可关联。

### 7.1 一份原始结果，四种视图

| 视图 | 给谁、做什么 | 可以包含 | 默认不应包含 |
|---|---|---|---|
| handler 原始结果 | 内部业务处理 | 下游返回的完整对象 | 不直接往外传 |
| 模型视图 | 支撑模型的下一步推理 | 状态、可退金额、业务 ID | token、密码、内部堆栈、无关的个人信息 |
| 用户视图 | 告诉用户结果和下一步 | 用户有权查看的字段 | 内部权限规则、系统实现细节 |
| 审计视图 | 证明决策和执行过程 | trace、Tool Call、主体、错误码、摘要 | 完整 Prompt、原始客户资料、密钥 |

模型只消费"经过治理的结果"，开发者看到"完整的决策过程"。这两条数据管道必须隔离。

### 7.2 脱敏三步：先投影，再脱敏，最后限长

1. **字段投影**：只挑出下一步真正需要的字段。
2. **递归脱敏**：处理嵌套结构里的敏感值。
3. **限长**：截断过长的内容。

```python
def _redact(value):
    if isinstance(value, Mapping):
        return {
            k: "***" if re.search(r"token|secret|password|authorization", k, re.I) else _redact(v)
            for k, v in value.items()
        }
    if isinstance(value, str):
        return re.sub(EMAIL_PATTERN, "***@***", value)
    return value
```

- 门店的详细地址只保留路名和距离。
- 模型结果和审计记录**共用同一套**敏感字段识别规则。
- 生产环境用统一的 Masking 中间件，配合字段投影和数据分级。

### 7.3 ToolResult 长什么样

```json
{"tool_call_id": "call_01", "tool_name": "get_order", "ok": true, "action": "allow", "code": "OK",
 "content": {"status": "paid", "refundable": 399.0, "customer_email": "***@***", "access_token": "***"}}

{"tool_call_id": "call_02", "tool_name": "create_refund", "ok": false, "action": "confirm",
 "code": "APPROVAL_REQUIRED", "content": "需要确认本次具体动作"}

{"tool_call_id": "call_04", "tool_name": "create_refund", "ok": false, "action": "deny",
 "code": "INVALID_ARGUMENT", "content": [{"path": "user_id", "message": "Extra inputs are not permitted"}]}
```

- 每条结果都**原样保留** `tool_call_id`。
- 失败时只带稳定的错误码和安全说明。内部地址、密钥名、堆栈只留在受控日志里。
- 结果先写回 Agent Loop 的对话历史，下一轮请求模型时才会被模型读到。

### 7.4 审计：每次调用分两条记录

```python
@dataclass(frozen=True, slots=True)
class AuditRecord:
    trace_id: str                     # 串起整次运行
    tool_call_id: str                 # 关联模型的 Tool Call 和 Tool Result
    tool_name: str
    user_id: str
    tenant_id: str
    phase: Literal["decision", "execution"]
    decision: str                     # allow / deny / confirm / executed / failed
    code: str
    argument_keys: tuple[str, ...]    # 只记参数的键名，不记值
    latency_ms: int | None = None
```

除了这些字段，还应该记下：规则来源（白名单 / RBAC / 预检查 ……）、`approval_id` 引用、业务 ID（订单号、退款单号），以及参数摘要的 SHA-256（用于对账，不存明文）。

| 记录 | 什么时候写 | 记什么 |
|---|---|---|
| decision | **每次调用都写**，包括被拒的 | allow / deny / confirm、规则来源、错误码 |
| execution | 只有真正执行了才写 | executed / failed、延迟、业务结果摘要 |

被拒绝的调用只有 decision、没有 execution，这本身就是"副作用为零"的证据。

### 7.5 一次调用的时间线

```
放行的调用：
10:00:00.120  model_tool_call   call_03 create_refund
10:00:00.123  schema_valid      call_03
10:00:00.127  policy_allow      call_03 code=APPROVED
10:00:00.130  tool_start        call_03
10:00:00.286  tool_end          call_03 refund_id=ref_9001
10:00:00.288  result_redacted   call_03
10:00:00.291  tool_message      call_03

被拒的调用：时间线停在 policy_deny，绝不能出现 tool_start
```

### 7.6 审计本身不能变成泄漏源

- 参数先按字段分类，默认只记键名和摘要。
- traceback 进入受保护的诊断通道。
- 审计存储要做访问控制、设定保留期限，并且不可篡改（append-only）。
- **查看审计这件事本身也要被审计。**
- 保留期限：
  - 高风险写操作长期保留决策码、主体、审批引用和业务 ID；
  - 普通只读调用只留聚合指标或短期的诊断记录；
  - 敏感字段默认不采集。

### 7.7 用审计指标反过来改进策略

| 指标 | 说明什么问题 |
|---|---|
| `INVALID_ARGUMENT` 比例高 | Schema 不好用，或者描述不清楚 |
| `TOOL_NOT_ALLOWED` 比例高 | 任务装配或工具描述有问题 |
| `APPROVAL_REQUIRED` 到批准的转化率 | 确认流程是否设置过度 |
| `PERMISSION_DENIED` 的分布 | 角色配置是否匹配实际需要 |
| `TIMEOUT_UNKNOWN` 的数量 | 写接口缺少状态查询或幂等设计 |
| 敏感字段命中次数 | 下游返回的数据太多 |

---

## 8. 写回 Agent Loop：错误码决定下一步

**原则：** 让 Loop 根据稳定的错误码做确定性的决策，不要让它去解析"抱歉，门店已关闭"这类自然语言。

### 8.1 错误码速查表

| 错误码 | 在哪一步产生 | Loop 下一步 | 能否自动重试 |
|---|---|---|---|
| `TOOL_NOT_FOUND` | 查找 | 结束这条路径 | 否 |
| `TOOL_DISABLED` | 启停复查 | 下一轮重建快照后再说（见 11.1） | 否 |
| `INVALID_ARGUMENT` | 参数校验 | 把字段错误回填给模型，让它生成新的 Tool Call | 不能用原参数重试 |
| `DENY_RULE` | deny 规则 | 结束 | 否 |
| `PLAN_MODE_DENIED` | 权限模式 | 只做分析；切换模式需要人来操作 | 否 |
| `TOOL_NOT_ALLOWED` | 执行期白名单 | 本轮不开放，结束这条路径 | 否 |
| `PERMISSION_DENIED` | RBAC | 换有权限的主体，或者结束 | 否 |
| `DEPENDENCY_UNAVAILABLE` | 依赖检查 | 缓存、只读降级，或人工兜底 | 有条件 |
| `BUSINESS_RULE_DENIED` | 业务预检查 | 重新规划，或转人工 | 通常否 |
| `APPROVAL_REQUIRED` | 审批 | **暂停 Loop**，进入可信确认界面 | 否 |
| `TIMEOUT` | execute（只读或幂等） | 按策略恢复 | 有条件 |
| `TIMEOUT_UNKNOWN` | execute（非幂等写） | **先查真实状态** | **禁止** |
| `TOOL_ERROR` | handler 异常 | 记录内部诊断，只给模型有限的说明 | 按映射决定 |
| `INVALID_OUTPUT` | 输出校验 | 【补充】写操作出现这个码时，副作用可能已经发生，按 `TIMEOUT_UNKNOWN` 处理 | 否 |
| `SHOP_NOT_FOUND` 等业务码 | handler 的业务结果 | 修正参数再试一次；仍然失败就问用户 | 有上限 |

### 8.2 Loop 层的其他防护

- **Prompt 注入的数据层防护**：网页、邮件、Tool Result 进入上下文前，做来源标记、限长和内容隔离。
- **轮数上限**：比如最多 8 轮，防止死循环。
- **回填格式**：用 `role="tool"` 加上原始的 `tool_call_id` 回填。Loop 里不要写 if/else 直接分发 handler，所有调用都走 `invoke()`。

---

## 9. 编排：多个调用一起来的时候

**一句话：** 先判断这些调用之间是什么关系，再决定并行、串行还是排队。

### 9.1 三种关系

| 关系 | 怎么判断 | 怎么跑 | 例子 |
|---|---|---|---|
| 相互独立 | 输入完整，结果互不依赖 | 有上限地并行 | 同时查订单、物流、优惠券 |
| 数据依赖 | 后一步的参数来自前一步的结果 | 显式串行 | 先查可退金额，再发起退款 |
| 资源冲突 | 多个动作要改同一个资源 | 按资源键串行 | 同一订单的两笔退款 |

原 README 的规则是"批次里只要有一个工具要求 sequential，整批都串行"。按资源键排队比它更细：只有改同一资源的调用需要排队，其余调用照样可以并行。并发上限可以用 Semaphore 控制（【补充】）。

### 9.2 完成顺序和消息顺序要分开

```python
async def invoke_many(calls, context):
    tasks = {asyncio.create_task(runtime.invoke(c, context)): c.tool_call_id for c in calls}
    by_id = {}
    for task in asyncio.as_completed(tasks):
        result = await task
        by_id[result.tool_call_id] = result
    return [by_id[c.tool_call_id] for c in calls]   # 按原调用顺序返回
```

每个结果都带着自己的 `tool_call_id`。聚合时按 ID 把结果放回原位，不要用"第几个完成"去猜"对应第几个调用"。

### 9.3 顺序本身也要校验

下单链路必须是：查店 → 搜品 → 预览 → 等待确认 → 下单。如果模型先调下单再调查店，Runtime 应该直接拒绝。实现上可以由业务预检查去确认前置状态是否已经存在。

### 9.4 什么时候才需要 DAG 或工作流引擎

只有当业务出现稳定的多步依赖、条件分支、长时间等待和补偿逻辑时才值得升级。判断标准是：**是否需要持久化进度、在故障后恢复，以及补偿语义。**

【补充】同一批 Tool Call 的参数在模型输出时就已经填好了，所以真正的"数据依赖"通常发生在不同轮次之间，由 Loop 来保证。同一批内部主要需要处理的是资源冲突。

---

## 10. 验收：怎么证明治理真的生效

**原则：** 测试要断言**副作用计数**（handler 调用次数、数据库写入、支付记录），不能只断言中文错误文案。

### 10.1 八项必测

| # | 测试 | 期望结果 |
|---|---|---|
| 1 | bypass 模式下调用危险 Shell | deny 规则仍然拒绝，Shell 执行次数为 0 |
| 2 | plan 模式下调用写工具 | 在审批和 allow 之前就被拒 |
| 3 | 参数里伪造 `user_id`、`approved` | 返回 `INVALID_ARGUMENT` |
| 4 | 权限不足 | handler 调用次数为 0 |
| 5 | 批准 100 元，参数改成 399 元 | 旧审批失效 |
| 6 | 返回结果含敏感字段 | 模型结果和审计记录都已脱敏，审计保留了 `tool_call_id` |
| 7 | 模型看不到的工具，以及历史里的旧 Tool Call | 执行期仍然被拒 |
| 8 | 同一审批重放第二次 | 返回 confirm，退款副作用总数为 1 |

### 10.2 防止测试被"绕过"

- 把业务硬性要求写在任务最前面，未经批准不能改。
- 每一轮只允许修改实现文件；修改测试需要单独说明理由。
- 在 CI 里保护关键的安全测试。
- 用变异测试：故意往代码里注入错误，证明测试确实会变红。
- 每次只接入一条规则，走"失败测试 → 最小修复 → 全量回归"的循环。

---

## 11. 常见问题

### 11.1 工具从 v1 升级到 v2，快照什么时候切换？

**当前这一轮不变，等到下一轮请求模型前、Harness 重新走"发现"阶段时才切换。**

```
第 N 轮：snapshot_N 里是 v1 → 模型按 v1 的 Schema 生成调用
          ← 这时你注册了 v2，把路由改到 v2，并停用 v1 →
          invoke(call, snapshot_N)
            · 在 snapshot_N 里找到 v1，按 v1 的 Schema 校验
            · 启停复查读到实时状态：v1 已停用 → TOOL_DISABLED，handler 不执行
第 N+1 轮：重新发现 → snapshot_N+1 里是 v2 → 模型按 v2 重新生成调用
```

需要注意的几点：
- **要改路由，不能只注册 v2。** 快照取哪个版本由 `TOOL_PROFILES[run_mode]` 决定，而不是"Registry 里最新的那个"。只停用 v1 却不改路由，下一轮这个工具会直接消失。
- **Runtime 不能在本轮中途改用 v2。** 模型的参数是按 v1 的 Schema 生成的，换成 v2 的 handler 就会发生协议漂移。
- **两种下架方式：**
  - 硬下架（停用 v1）：在途的调用会被拒。适合 v1 有 bug 或安全问题的情况。
  - 软切换（只改路由）：在途的调用按 v1 正常执行完。适合普通升级和灰度发布。
- **Loop 收到 `TOOL_DISABLED` 后，不要用原参数重试。** 让下一轮按 v2 重新生成。
- **不要从 Registry 里删除 v1，只把它设为停用。** 在途的快照和审计记录里的 version 还要用到它。
- **快照的粒度是一个设计选择。** 本文按"每轮一份"。如果按"每次会话一份"，v2 要等新会话才生效，但硬下架照样会被执行期的启停复查拦住。

### 11.2 用户确认审批期间，工具升级了怎么办？

第 N 轮返回了 confirm；用户点确认时，路由已经切到 v2。原参数是按 v1 生成的，重新进入 Runtime 时可能不符合 v2 的 Schema。

【补充】建议审批记录同时绑定 `tool_name + version`。恢复执行时如果版本不一致，旧审批直接失效，按 v2 重新走一次 confirm。

### 11.3 为什么审批要放到最后才消费？

如果先消费了审批，后面又因为依赖不可用或 before hook 被拒绝，用户的这次确认就白白作废了，只能让他再点一次。放在最后消费，并且用原子操作，就能同时保证"不浪费"和"不能被并发重放"。

---

## 附录：相对原 README 的升级点

| 环节 | 原 README | 现在 |
|---|---|---|
| 可信上下文 | 只说"只信 ExecutionContext" | 明确了 7 个字段和各自来源，并划清业务参数和授权事实的界线 |
| 注册 | 调用契约 / 运行治理 / handler | 加上 7 问风险建模、`StrictArgs`、能力收窄、`precheck`、`canonical_target`、资源键 |
| 发现 | 版本路由 + 启停过滤 | 加上白名单过滤（第 1 次） |
| 权限 | 单独一步权限检查 | PermissionEngine 三态决策 + 固定顺序 + 四种权限模式 |
| 白名单 | 无 | 发现期和执行期各查一次 |
| 审批 | `call.id in ctx.approved_call_ids` | ApprovalStore：绑定参数哈希、一次性、带过期时间，最后才原子消费 |
| 业务检查 | before hook | 独立的业务预检查层 |
| 重试 | 幂等可重试，非幂等不重试 | 区分四种重试；`retries` 由属性计算；幂等键；新增 `TIMEOUT_UNKNOWN` |
| 并发 | 无 | 按 `tenant_id:order_id` 加资源锁 |
| 脱敏 | 按 data_classification 脱敏 | 四种视图；先投影、再脱敏、最后限长；模型和审计共用同一套规则 |
| 审计 | 一条记录，只记字段名 | decision 和 execution 两阶段；加上规则来源、审批引用、业务 ID、参数哈希；时间线；保留策略；指标 |
| 错误码 | 9 个 | 新增 `DENY_RULE`、`PLAN_MODE_DENIED`、`TOOL_NOT_ALLOWED`、`BUSINESS_RULE_DENIED`、`TIMEOUT_UNKNOWN` 和业务码；ToolResult 加 `action` 字段 |
| 批量 | 有一个 sequential 就整批串行 | 区分三种关系；按资源键排队；结果按 `tool_call_id` 归位 |
| 验收 | 无 | 八项测试，断言副作用计数 |
