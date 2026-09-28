"""
2-1.pdf 六个知识点(1.1-1.6)的最小可运行实现，串在一个文件里。

运行：
  pip install --break-system-packages langchain-core langchain-openai pydantic
  python3 this_file.py
  想跑 1.4 真实模型调用，先 export OPENAI_API_KEY=...
"""

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai import ChatOpenAI


# ============================================================
# 1.3　Input / Output / Error 三份 Schema
#   Input Schema  守 模型 → Runtime  （模型给的参数可信吗）
#   Output Schema 守 handler → Agent （handler 返回的数据可信吗）
#   Error Schema  守 失败 → 下一步   （失败了下一步怎么办）
# ============================================================
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 模型不能夹带 Schema 之外的字段


class RefundInput(StrictModel):
    order_id: str = Field(min_length=1, description="要退款的订单号")
    amount: float = Field(gt=0, description="退款金额，必须大于 0")


class RefundOutput(StrictModel):
    order_id: str
    refunded: bool
    amount: float


class ListOrdersInput(StrictModel):
    pass


class OrderBrief(StrictModel):
    order_id: str
    amount: float


class ListOrdersOutput(StrictModel):
    orders: list[OrderBrief]


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


# ============================================================
# 1.2　工具定义：name/description 来自函数本身，
#      Input Schema 来自 args_schema，
#      permission/risk 等治理字段放进 metadata（模型看不到）
# ============================================================
_PROCESSED: dict[str, dict] = {}  # 模拟下游服务按幂等键记录处理结果


@tool(args_schema=RefundInput)
async def refund_order(order_id: str, amount: float, config: RunnableConfig) -> dict:
    """
    给指定订单办理退款。
    用户明确要求退款、且已经知道订单号和金额时才调用。会真实生效，需要用户提前确认。
    """
    user_id = config["configurable"]["user_id"]              # 身份来自可信 config，不是模型参数（1.5）
    idempotency_key = config["configurable"]["idempotency_key"]
    if idempotency_key in _PROCESSED:                          # 幂等键：重复执行直接返回上次结果
        return _PROCESSED[idempotency_key]
    if amount > 500:
        raise ValueError("单笔退款超过 500，需要人工审批")
    result = {"order_id": order_id, "refunded": True, "amount": amount}
    _PROCESSED[idempotency_key] = result
    print(f"    [handler] 操作者={user_id} 退款 {order_id} {amount} 元")
    return result


@tool(args_schema=ListOrdersInput)
async def list_recent_orders(config: RunnableConfig) -> dict:
    """查询当前用户最近的订单列表（只读，不会修改任何数据）。用户不知道订单号时先调用这个。"""
    return {"orders": [{"order_id": "o1", "amount": 100.0}, {"order_id": "o2", "amount": 600.0}]}


refund_order.metadata = {
    "permission": "order:refund",
    "risk": "high",        # 高风险：需要审批
    "timeout": 1.0,
    "max_retries": 0,
    "idempotent": False,   # 写操作，默认不自动重试（配合幂等键才能安全重试）
    "output_model": RefundOutput,
}
list_recent_orders.metadata = {
    "permission": "order:read",
    "risk": "low",
    "timeout": 1.0,
    "max_retries": 2,
    "idempotent": True,    # 只读，可以自动重试
    "output_model": ListOrdersOutput,
}


# ============================================================
# 1.6　工具能力和 Agent Loop 解耦：Registry 单独管"有哪些工具"，
#      Agent Loop 只问 Registry 要，不硬编码具体工具
# ============================================================
class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, t: BaseTool) -> None:
        self._tools[t.name] = t

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def visible_tools(self, ctx: "ExecutionContext") -> list[BaseTool]:
        return [t for t in self._tools.values() if t.metadata["permission"] in ctx.permissions]


REGISTRY = ToolRegistry()


# ============================================================
# 1.5　Runtime 执行前的边界：
#      身份/权限只信 ExecutionContext，不信模型参数；
#      超时后能不能重试，看 idempotent，不能无脑重试
# ============================================================
@dataclass
class ExecutionContext:
    user_id: str
    permissions: set[str]
    approved_call_ids: set[str] = field(default_factory=set)


async def run_handler(tool_obj: BaseTool, args: BaseModel, ctx: ExecutionContext, call_id: str):
    rules = tool_obj.metadata
    max_attempts = (1 + rules["max_retries"]) if rules["idempotent"] else 1
    config = {
        "configurable": {
            "user_id": ctx.user_id,                    # 可信身份，模型碰不到
            "idempotency_key": f"idem_{call_id}",       # 同一条调用，重试也用同一个 key
        }
    }
    for attempt in range(1, max_attempts + 1):
        try:
            return await asyncio.wait_for(tool_obj.ainvoke(args.model_dump(), config=config), timeout=rules["timeout"])
        except asyncio.TimeoutError:
            if attempt == max_attempts:
                raise
            await asyncio.sleep(0.05)


def _error(call: dict, code: str, message: str, retryable: bool = False) -> ToolMessage:
    err = ToolError(code=code, message=message, retryable=retryable)
    return ToolMessage(
        content=json.dumps({"error": err.model_dump()}, ensure_ascii=False),
        tool_call_id=call["id"], name=call["name"], status="error",
    )


async def execute(call: dict, ctx: ExecutionContext) -> ToolMessage:
    """查找 → 参数(Input Schema) → 权限 → 审批 → 执行(超时/重试) → 校验输出(Output Schema) → 结果(Error Schema)"""
    tool_obj = REGISTRY.get(call["name"])
    if tool_obj is None:
        return _error(call, "INVALID_ARGUMENT", f"没有工具 {call['name']}")
    rules = tool_obj.metadata

    try:
        args = tool_obj.args_schema.model_validate(call["args"])         # Input Schema（1.3）
    except ValidationError as e:
        return _error(call, "INVALID_ARGUMENT", str(e))

    if rules["permission"] not in ctx.permissions:                       # 只信 ctx，不信模型（1.5）
        return _error(call, "PERMISSION_DENIED", f"缺少权限 {rules['permission']}")
    if rules["risk"] == "high" and call["id"] not in ctx.approved_call_ids:
        return _error(call, "APPROVAL_REQUIRED", "需要用户先确认这次调用")

    try:
        raw_output = await run_handler(tool_obj, args, ctx, call["id"])   # 超时/幂等重试（1.5）
    except asyncio.TimeoutError:
        return _error(call, "TIMEOUT", "执行超时", retryable=rules["idempotent"])
    except Exception as e:
        return _error(call, "UPSTREAM_ERROR", str(e))

    try:
        output = rules["output_model"].model_validate(raw_output)         # Output Schema（1.3）
    except ValidationError:
        return _error(call, "INVALID_OUTPUT", "工具返回的数据不符合 Output Schema")

    return ToolMessage(content=output.model_dump_json(), tool_call_id=call["id"], name=call["name"])


# ============================================================
# 1.1　并行执行：同时发出多条 Tool Call，各自带 tool_call_id，
#      完成顺序可能和发出顺序不一样，靠 id 对应回去，不会配错
# ============================================================
async def execute_batch(calls: list[dict], ctx: ExecutionContext) -> list[ToolMessage]:
    tasks = [execute(call, ctx) for call in calls]
    results = []
    for task in asyncio.as_completed(tasks):
        results.append(await task)
    return results


# ============================================================
# 1.4　模型选工具、生成参数 + 最小 Agent Loop：
#      模型决策 → 执行 → 写回(带 tool_call_id) → 再决策，直到不再调用工具
# ============================================================
MAX_STEPS = 5


async def run_agent(question: str, ctx: ExecutionContext, llm: ChatOpenAI) -> str:
    messages = [
        SystemMessage("你是订单助手。需要订单数据就调用工具，不要编造；工具报错时如实告诉用户。"),
        HumanMessage(question),
    ]
    for step in range(1, MAX_STEPS + 1):
        tools = REGISTRY.visible_tools(ctx)                               # 向 Registry 要，不硬编码（1.6）
        ai_msg: AIMessage = await llm.bind_tools(tools, tool_choice="auto").ainvoke(messages)
        messages.append(ai_msg)

        if not ai_msg.tool_calls:
            return ai_msg.content

        print(f"[第 {step} 轮] 模型选择调用：", [(c["name"], c["args"]) for c in ai_msg.tool_calls])
        messages.extend(await execute_batch(ai_msg.tool_calls, ctx))      # 并行执行 + 写回（1.1）

    raise RuntimeError(f"超过最大轮数 {MAX_STEPS}，停止")


def tool_call(call_id: str, name: str, **args) -> dict:
    return {"id": call_id, "name": name, "args": args}


# ============================================================
# demo：把 1.1～1.6 串起来跑一遍
# ============================================================
async def main() -> None:
    REGISTRY.register(refund_order)
    REGISTRY.register(list_recent_orders)
    ctx = ExecutionContext(
        user_id="u_1001",
        permissions={"order:read", "order:refund"},
        approved_call_ids={"call_2"},
    )

    print("===== 1.2 模型能看到的工具定义（只有 name/description/Input Schema）=====")
    print(convert_to_openai_tool(refund_order))

    print("\n===== 1.3 + 1.5 一次正常执行 =====")
    print(await execute(tool_call("call_1", "list_recent_orders"), ctx))

    print("\n===== 高风险操作，未审批 → APPROVAL_REQUIRED =====")
    print(await execute(tool_call("call_x", "refund_order", order_id="o1", amount=100), ctx))

    print("\n===== 已审批（call_2）→ 正常执行 =====")
    print(await execute(tool_call("call_2", "refund_order", order_id="o1", amount=100), ctx))

    print("\n===== 1.1 并行执行两条调用，看 tool_call_id 怎么对应回去 =====")
    calls = [tool_call("call_A", "list_recent_orders"), tool_call("call_B", "list_recent_orders")]
    results = await execute_batch(calls, ctx)
    print("发出顺序：", [c["id"] for c in calls])
    print("完成顺序：", [r.tool_call_id for r in results])

    print("\n===== 1.6 撤销工具，Agent Loop 代码不用改 =====")
    REGISTRY.unregister("refund_order")
    print("撤销后可见工具：", [t.name for t in REGISTRY.visible_tools(ctx)])
    REGISTRY.register(refund_order)

    print("\n===== 1.4 接真实模型，走一遍最小 Agent Loop =====")
    if not os.environ.get("OPENAI_API_KEY"):
        print("没有设置 OPENAI_API_KEY，跳过")
        return
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    answer = await run_agent("帮我查一下我最近的订单，然后把金额最小的那笔退款", ctx, llm)
    print("最终回答：", answer)


if __name__ == "__main__":
    asyncio.run(main())