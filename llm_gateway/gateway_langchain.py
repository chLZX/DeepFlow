"""
LLM Gateway —— LangChain 简化版

在保留原版核心能力的前提下（统一请求协议 / 模型别名路由 / 主备降级重试 /
结构化输出 / 流式返回 / 调用审计），把"供应商适配 + 重试 + 降级"这部分
交给 LangChain 自带的能力去做（with_retry / with_fallbacks），
代码量大幅减少，方便理解和复现。

依赖安装：
    pip install fastapi uvicorn "langchain-openai>=0.2" jsonschema

环境变量（用 DeepSeek 举例，OpenAI 兼容接口；换供应商只需改 base_url / key）：
    export DEEPSEEK_API_KEY=sk-你的主模型key
    export DEEPSEEK_BACKUP_API_KEY=sk-你的备用模型key   # 没有备用账号，写和主模型一样的 key 也能跑通演示

运行：
    uvicorn gateway_langchain:app --reload --port 8000

快速测试：
    curl -X POST http://127.0.0.1:8000/v1/llm \
      -H "Content-Type: application/json" \
      -d '{"model": "general-primary", "messages": [{"role": "user", "content": "你好"}]}'
"""

import json
import logging
import os
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from jsonschema import ValidationError as JsonSchemaError
from jsonschema import validate
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel, Field, model_validator

# 只重试"瞬时的、换个时间点大概率会成功"的故障：
# - APIConnectionError / TimeoutError / ConnectionError：网络连不上/连接被打断
# - APITimeoutError：等了 timeout 秒都没响应（耗时约等于你设置的 timeout 值）
# - RateLimitError：触发限流，等一下大概率恢复
# - InternalServerError（500）：上游服务自己临时出错，不是你的请求有问题
#
# 不放进来的（AuthenticationError 401 / BadRequestError 400）：
# 这两种通常是服务端还没开始真正跑模型、光靠校验请求头/参数就直接拒绝了，
# 说明问题出在"这次请求本身"（Key 错了 / 参数不对），重试多少次结果都一样，
# 应该让它尽快失败——它依然会被 with_fallbacks 捕获并立刻切到备用模型，
# 只是不会在同一个模型上白白多等待、多重试一次。
RETRYABLE_EXCEPTIONS = (
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    InternalServerError,
    TimeoutError,
    ConnectionError,
)

logger = logging.getLogger("llm_gateway")


# ========== 1. 统一请求 / 响应协议 ==========
# 和原版思路一致：调用方只跟这几个字段打交道，不用关心背后具体是哪家供应商。

class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=20_000)


class LLMRequest(BaseModel):
    model: str  # "general-primary" / "general-backup"
    messages: list[Message]
    stream: bool = False
    response_schema: dict[str, Any] | None = None  # 传了就要求模型返回符合此 schema 的 JSON
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    prompt_name: str | None = None  # 简化版：只按名字选模板，不做版本号管理
    prompt_variables: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_supported_combination(self) -> "LLMRequest":
        if self.stream and self.response_schema is not None:
            raise ValueError("stream 与 response_schema 不能同时使用")
        return self


class LLMResponse(BaseModel):
    request_id: str
    model: str  # 实际生效的模型别名（可能因为降级而不等于请求的 model）
    content: str
    parsed: dict[str, Any] | list[Any] | None = None
    latency_ms: int = Field(ge=0)


# ========== 2. 受控 Prompt 模板 ==========
# 简化：去掉版本号管理，只保留"按名字取模板 + 变量替换"，
# 核心目的没变——调用方不能自己传模板正文，只能选受控好的模板。

PROMPT_TEMPLATES = {
    "knowledge_decision": "你是${product_name}的知识库决策器。资料不足时搜索，资料充分时结束回答。不得编造制度内容。",
}


def render_system_prompt(name: str, variables: dict[str, str]) -> str:
    from string import Template

    template = PROMPT_TEMPLATES.get(name)
    if template is None:
        raise HTTPException(400, {"code": "unknown_prompt_template", "message": "Prompt 模板不存在"})
    try:
        return Template(template).substitute(variables)
    except KeyError as exc:
        raise HTTPException(
            400, {"code": "missing_prompt_variable", "message": f"缺少 Prompt 变量: {exc.args[0]}"}
        ) from exc


def to_lc_messages(request: LLMRequest) -> list:
    # 把网关统一的 Message 转成 LangChain 认识的消息对象。
    role_map = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}
    lc_messages = [role_map[m.role](content=m.content) for m in request.messages]
    if request.prompt_name:
        system_text = render_system_prompt(request.prompt_name, request.prompt_variables)
        lc_messages = [SystemMessage(content=system_text), *lc_messages]
    return lc_messages


# ========== 3. 模型路由 ==========
# 把"平台模型别名"映射到具体的 LangChain 模型实例。
# 原版这里自己写了 Provider Protocol + OpenAICompatibleProvider 去屏蔽供应商差异，
# LangChain 的 ChatOpenAI 本身就是这层适配，所以直接用它，不用再自己封装一遍。

def make_model(model_env: str, base_url_env: str, api_key_env: str) -> ChatOpenAI:
    return ChatOpenAI(
        model=os.getenv(model_env, "deepseek-chat"),
        base_url=os.getenv(base_url_env, "https://api.deepseek.com"),
        api_key=os.getenv(api_key_env),
        timeout=60,
    )


primary_model = make_model("PRIMARY_PROVIDER_MODEL", "PRIMARY_BASE_URL", "DEEPSEEK_API_KEY")
backup_model = make_model("BACKUP_PROVIDER_MODEL", "BACKUP_BASE_URL", "DEEPSEEK_BACKUP_API_KEY")

MODEL_REGISTRY = {
    "general-primary": primary_model,
    "general-backup": backup_model,
}

PRICE_PER_MILLION = {
    "general-primary": {"input": 1.0, "output": 4.0},
    "general-backup": {"input": 0.8, "output": 3.2},
}


def build_runnable(requested_alias: str):
    # 核心简化点：重试和降级都交给 LangChain 内置能力，不用自己手写循环。
    # - with_retry：网络类瞬时故障（超时/连接失败/限流）自动重试，并带指数退避
    #   （wait_exponential_jitter=True：第1次重试前等约1秒，第2次约2秒……上限10秒，
    #    再叠加随机抖动，避免上游刚恢复就被大量重试请求瞬间打满）
    # - with_fallbacks：主模型重试耗尽后仍失败，自动换成备用模型再试一遍
    if requested_alias not in MODEL_REGISTRY:
        raise HTTPException(400, {"code": "unknown_model", "message": "模型不在 Gateway 允许列表中"})

    primary = MODEL_REGISTRY[requested_alias].with_retry(
        retry_if_exception_type=RETRYABLE_EXCEPTIONS,
        wait_exponential_jitter=True,
        stop_after_attempt=2,
    )
    fallback_alias = "general-backup" if requested_alias != "general-backup" else None
    if fallback_alias is None:
        return primary
    fallback = MODEL_REGISTRY[fallback_alias].with_retry(
        retry_if_exception_type=RETRYABLE_EXCEPTIONS,
        wait_exponential_jitter=True,
        stop_after_attempt=2,
    )
    return primary.with_fallbacks([fallback])


def resolve_actual_alias(requested_alias: str, actual_provider_model: str) -> str:
    # LangChain 不会直接告诉你"这次是不是走了 fallback"，
    # 用返回结果里的真实供应商模型名，反推这次实际用的是主模型还是备用模型。
    if requested_alias != "general-backup" and actual_provider_model == backup_model.model_name:
        return "general-backup"
    return requested_alias


# ========== 4. 调用审计 ==========
# 简化：不再单独定义 CallTrace 这个 Pydantic 模型，直接用 dict，字段一目了然。

TRACES: list[dict[str, Any]] = []


def record_trace(
    request_id: str,
    requested_alias: str,
    actual_alias: str | None,
    usage: dict,
    latency_ms: int,
    status: str,
    error_code: str | None = None,
) -> None:
    input_tokens = usage.get("prompt_tokens", 0) if usage else 0
    output_tokens = usage.get("completion_tokens", 0) if usage else 0
    price = PRICE_PER_MILLION.get(actual_alias, {"input": 0, "output": 0})
    trace = {
        "request_id": request_id,
        "requested_model": requested_alias,
        "actual_model": actual_alias,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": (input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000,
        "latency_ms": latency_ms,
        "status": status,
        "error_code": error_code,
    }
    TRACES.append(trace)
    logger.info("llm_call_trace=%s", json.dumps(trace, ensure_ascii=False))


# ========== 5. 核心业务逻辑 ==========

async def call_with_fallback(request: LLMRequest) -> LLMResponse:
    request_id = str(uuid4())
    started = time.perf_counter()
    messages = to_lc_messages(request)
    runnable = build_runnable(request.model)

    # 需要结构化输出时，直接在提示词里要求模型只返回符合 schema 的 JSON。
    # 这种写法牺牲一点"强约束"（不像 OpenAI 原生 json_schema 模式那样强制），
    # 换来的是几乎所有 OpenAI 兼容供应商都能直接跑通，简单、好复现。
    if request.response_schema is not None:
        schema_hint = SystemMessage(
            content=(
                "只返回一个合法 JSON 对象，必须严格符合下列 JSON Schema，不要返回 Markdown 或额外文字：\n"
                f"{json.dumps(request.response_schema, ensure_ascii=False)}"
            )
        )
        messages = [schema_hint, *messages]

    try:
        result = await runnable.ainvoke(messages)
    except AuthenticationError as exc:
        # 主备模型的 Key 全都无效/未配置——这是网关自己的配置问题，不是上游偶发故障，
        # 重试和降级都救不了，应该让运维/开发者立刻看到"是 Key 配错了"，而不是笼统的 502。
        # 注意：with_fallbacks 内部失败后重新抛出的是"最后一个候选（backup）的异常"，
        # 所以这里能捕获到，说明 backup 的 Key 也失败了（primary 可能同样失败，也可能
        # 是别的问题，但这里已经无法回天，统一按"鉴权失败"上报）。
        latency_ms = int((time.perf_counter() - started) * 1000)
        record_trace(request_id, request.model, None, {}, latency_ms, "failed", "invalid_api_key")
        raise HTTPException(
            401, {"code": "invalid_api_key", "message": "模型服务的 API Key 无效或未配置，请检查网关部署环境的密钥配置"}
        ) from exc
    except BadRequestError as exc:
        # 请求本身被上游拒绝（参数不合法、消息超长、模型名在供应商侧不存在等）。
        # 换主模型还是备用模型结果都一样，是调用方这次请求的内容有问题，应归为 4xx。
        latency_ms = int((time.perf_counter() - started) * 1000)
        record_trace(request_id, request.model, None, {}, latency_ms, "failed", "upstream_bad_request")
        raise HTTPException(
            400, {"code": "upstream_bad_request", "message": f"上游模型服务拒绝了这次请求：{exc}"}
        ) from exc
    except Exception as exc:
        # 其余情况（网络问题、限流、上游临时故障等，且重试/降级都已经用尽）才归为
        # "模型服务不可用"，用 502 表示问题出在网关和上游之间，不是调用方的请求有问题。
        latency_ms = int((time.perf_counter() - started) * 1000)
        record_trace(request_id, request.model, None, {}, latency_ms, "failed", "model_unavailable")
        raise HTTPException(502, {"code": "model_unavailable", "message": "主模型和备用模型均不可用"}) from exc

    content = result.content
    usage = result.response_metadata.get("token_usage", {}) or {}
    actual_provider_model = result.response_metadata.get("model_name", "")
    actual_alias = resolve_actual_alias(request.model, actual_provider_model)

    parsed = None
    if request.response_schema is not None:
        try:
            parsed = json.loads(content)
            validate(instance=parsed, schema=request.response_schema)
        except json.JSONDecodeError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            record_trace(request_id, request.model, actual_alias, usage, latency_ms, "failed", "invalid_json")
            raise HTTPException(502, {"code": "invalid_json", "message": "模型没有返回合法 JSON"}) from exc
        except JsonSchemaError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            record_trace(
                request_id, request.model, actual_alias, usage, latency_ms, "failed", "schema_validation_failed"
            )
            raise HTTPException(502, {"code": "schema_validation_failed", "message": "模型结果不符合 response_schema"}) from exc

    latency_ms = int((time.perf_counter() - started) * 1000)
    record_trace(request_id, request.model, actual_alias, usage, latency_ms, "success")

    return LLMResponse(
        request_id=request_id,
        model=actual_alias,
        content=content,
        parsed=parsed,
        latency_ms=latency_ms,
    )


def encode_sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def stream_with_fallback(request: LLMRequest):
    messages = to_lc_messages(request)
    runnable = build_runnable(request.model)
    started = time.perf_counter()
    request_id = str(uuid4())
    try:
        async for chunk in runnable.astream(messages):
            if chunk.content:
                yield encode_sse({"type": "content.delta", "delta": chunk.content})
        latency_ms = int((time.perf_counter() - started) * 1000)
        record_trace(request_id, request.model, request.model, {}, latency_ms, "success")
        yield encode_sse({"type": "response.completed", "model": request.model})
    except Exception:
        logger.exception("upstream stream failed")
        latency_ms = int((time.perf_counter() - started) * 1000)
        record_trace(request_id, request.model, None, {}, latency_ms, "failed", "upstream_stream_failed")
        yield encode_sse({"type": "response.failed", "error": "upstream_stream_failed"})


# ========== 6. FastAPI 路由 ==========
# 和原版保持一致的三个接口，方便对照复现。

app = FastAPI(title="Agent LLM Gateway (LangChain)", version="0.0.1")


@app.post("/v1/llm", response_model=LLMResponse)
async def create_llm_response(request: LLMRequest) -> LLMResponse:
    if request.stream:
        raise HTTPException(400, {"code": "use_stream_endpoint", "message": "流式请求请使用 /v1/llm/stream"})
    return await call_with_fallback(request)


@app.post("/v1/llm/stream")
async def create_stream(request: LLMRequest) -> StreamingResponse:
    if request.response_schema is not None:
        raise HTTPException(400, {"code": "unsupported_combination", "message": "流式输出不支持 response_schema"})
    if request.model not in MODEL_REGISTRY:
        raise HTTPException(400, {"code": "unknown_model", "message": "模型不在 Gateway 允许列表中"})
    return StreamingResponse(stream_with_fallback(request), media_type="text/event-stream")


@app.get("/v1/traces")
async def list_traces() -> list[dict[str, Any]]:
    return TRACES
