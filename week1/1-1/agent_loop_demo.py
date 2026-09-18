"""
Mini Coding Agent —— LangChain / LangGraph 单文件版。

这是一个能直接跑起来的小项目，而不是几个互相不认识的 demo 文件。
把之前拆开的几块东西，按"实际会被用到"的原则合到了一起：

    模型工厂（切换 DeepSeek / OpenAI）
    错误分类 + 有边界重试（对应课件第 19 页那张表）
    结构化输出契约 + 输出格式错时的重试
    LangGraph 一节点决策 Loop

这一版按最新要求做了简化：
    1. 不再有 "Adapter" 这个抽象类。直接用 LangChain 的聊天模型，
       "切供应商"就是换一个模型类，不需要专门包一层类去做协议翻译。
    2. 重试只做两件事，别的都不管：
       a) 调用 API 报错时，按下面的表分类，只重试"暂时性故障"；
       b) 模型返回的结构化输出解析失败（JSON / Schema 不合法）时，
          把错误原因写成一句话喂回给模型，让它自己修正后重新生成。

对照表（课件第 19 页）：

    阶段      典型问题              是否重试   正确处置
    ------   -----------------    --------  --------------------------
    请求前    参数 / Schema 不合法    否       本地修复，不要发起请求
    认证      Key 无效 / 权限不足     否       终止并提示配置问题
    传输      网络中断 / 连接超时     有边界    退避 + 控制次数后重试
    服务端    429 限流 / 5xx 过载    有边界    指数退避 + jitter 后重试
    模型输出   拒答                  否       终止，交给人工判断（本版未特殊处理，
                                              少数模型会把拒答也包成解析失败，
                                              这里统一按"格式错"重试几次兜底）
    模型输出   被截断 / 格式错(JSON)  有边界    把原因喂回模型，重新生成

运行方式：
    export MODEL_PROVIDER=deepseek   # 或 openai
    export DEEPSEEK_API_KEY=sk-xxx   # 对应 provider 的 key
    python agent_loop_demo.py
"""

from __future__ import annotations

import os
import random
import time
from typing import Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel, Field


# ============================================================
# 1) 模型工厂 —— 不用 Adapter，切供应商就是换一个 LangChain 模型类
# ============================================================

def get_chat_model(provider: str | None = None) -> BaseChatModel:
    provider = provider or os.getenv("MODEL_PROVIDER", "deepseek")

    if provider == "deepseek":
        from langchain_deepseek import ChatDeepSeek

        return ChatDeepSeek(
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            api_key=os.environ["DEEPSEEK_API_KEY"],
            timeout=30.0,
            max_retries=0,  # SDK 自带的重试关掉，重试统一交给下面的 call_with_retry
        )

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=os.getenv("OPENAI_MODEL", "gpt-5.5"),
            api_key=os.environ["OPENAI_API_KEY"],
            timeout=30.0,
            max_retries=0,
        )

    raise ValueError(f"未知 MODEL_PROVIDER: {provider}")


# ============================================================
# 2) 错误分类 + 有边界重试 —— 只重试"暂时性故障"，其余直接报错
# ============================================================

def is_retryable(exc: Exception) -> bool:
    """按上面的表格判断：这个错误值不值得重试。"""

    # 认证失败 / 参数或 Schema 不合法：本地就能确定，重试也没用
    if isinstance(exc, (AuthenticationError, BadRequestError)):
        return False

    # 网络中断、连接超时、429 限流、5xx 过载：都是"暂时性故障"，值得重试
    if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)):
        return True

    # 没见过的错误类型，保守起见不重试，别把真正的 bug 掩盖掉
    return False


def call_with_retry(operation, *, max_attempts: int = 3, base_delay: float = 0.5, max_delay: float = 8.0):
    """指数退避 + jitter，最多重试 max_attempts 次。"""

    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if not is_retryable(exc) or attempt == max_attempts:
                raise
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.25)  # jitter：避免大家同时重试
            print(f"[retry] 第 {attempt} 次调用失败（{type(exc).__name__}），{delay:.1f}s 后重试")
            time.sleep(delay)


# ============================================================
# 3) 结构化输出契约 —— 输出格式不对时，把错误喂回模型重试
# ============================================================

class AgentAction(BaseModel):
    """Agent 下一步动作的输出契约：先定义好格式，模型的输出才能被程序直接使用。"""

    step: Literal["inspect_logs", "run_tests", "read_code", "ask_user"] = Field(
        description="Agent 下一步要执行的动作"
    )
    reason: str = Field(description="选择这个动作的原因")
    needs_user_input: bool = Field(description="是否需要向用户补充提问")
    confidence: float = Field(ge=0, le=1, description="当前判断的置信度，范围 0 到 1")


def ask_for_action(model: BaseChatModel, messages: list, *, max_json_retries: int = 3) -> AgentAction:
    """调用模型拿一个结构化的 AgentAction。

    两层重试叠在一起：
    - call_with_retry 处理"网络 / 限流 / 过载"这类 API 报错；
    - 这里的 for 循环处理"模型输出格式不对"——不是盲目重试，而是把上一次
      的错误原因写成一条消息喂回给模型，让它照着提示修正，这就是课件
      里说的 retry-with-feedback，比"什么都不说、再问一遍"更容易成功。
    """

    structured_model = model.with_structured_output(AgentAction, include_raw=True)
    history = list(messages)

    for attempt in range(1, max_json_retries + 1):
        result = call_with_retry(lambda: structured_model.invoke(history))

        parsed = result["parsed"]
        error = result["parsing_error"]

        if parsed is not None and error is None:
            return parsed

        if attempt == max_json_retries:
            raise RuntimeError(
                f"MODEL_SCHEMA_INVALID: 重试 {max_json_retries} 次后输出仍不合法：{error}"
            )

        print(f"[json-retry] 第 {attempt} 次输出解析失败，把错误喂回模型重新生成：{error}")
        history = [
            *history,
            (
                "user",
                "你上一次的输出没有通过格式校验，请严格按照要求的字段和取值范围重新输出。"
                f"校验错误如下：\n{error}",
            ),
        ]

    raise AssertionError("unreachable")  # 上面的循环要么 return，要么 raise


# ============================================================
# 4) LangGraph —— 一个节点的决策 Loop
# ============================================================

SYSTEM_PROMPT = (
    "你是编码 Agent 的决策器。根据目标选择 inspect_logs、run_tests、"
    "read_code 或 ask_user。不要声称执行了尚未执行的动作。"
)


class LoopState(TypedDict):
    goal: str
    action: AgentAction | None


def build_agent_loop(model: BaseChatModel):
    """START -> decide -> END。以后要加工具调用 / 人工审批节点，
    在这里继续 add_node / add_edge 即可，decide 内部逻辑不用重写。
    """

    def decide(state: LoopState) -> LoopState:
        messages = [("system", SYSTEM_PROMPT), ("user", state["goal"])]
        action = ask_for_action(model, messages)
        return {"goal": state["goal"], "action": action}

    graph = StateGraph(LoopState)
    graph.add_node("decide", decide)
    graph.add_edge(START, "decide")
    graph.add_edge("decide", END)
    return graph.compile()


def main() -> None:
    provider = os.getenv("MODEL_PROVIDER", "deepseek")
    model = get_chat_model(provider)
    app = build_agent_loop(model)

    goal = "检查 tests/test_api.py 失败原因，当前尚未运行测试。"
    final_state = app.invoke({"goal": goal, "action": None})
    print(final_state["action"])


if __name__ == "__main__":
    main()
