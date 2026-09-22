"""
Run 三接口版：创建 / 订阅 / 取消 分离 + RunEvent 协议。

跟 main.py 的极简单接口版相比，这个文件多做两件事（课件里强调的重点）：

1. 三个接口职责分离，互不重叠：
   - POST /v1/runs                创建任务，立即返回 run_id（不等模型跑完）
   - GET  /v1/runs/{id}/events    只做 SSE 推送，支持 Last-Event-ID 断线重连
   - POST /v1/runs/{id}/cancel    只负责取消，跟订阅接口无关

2. RunEvent 协议：用 run_id + seq 定位一条事件，schema_version 留作协议演化；
   五类事件：run.started / text.delta / run.completed / run.failed / run.cancelled。

控制面语义：
   - 断开连接 ≠ 取消任务：浏览器断开只停止推送，后台任务继续跑。
   - 流结束 ≠ 任务成功：模型输出被截断（finish_reason == "length"）
     要归一化成 run.failed，而不是当成 run.completed。

为了保持简单：Run 状态存在内存 dict 里（进程重启即丢失），订阅用轮询
（每 200ms 检查一次有没有新事件），没有做心跳、没有做工具调用。
"""

import asyncio
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

load_dotenv()

app = FastAPI(title="DeepSeek Streaming Gateway (Run API)")

model = ChatOpenAI(
    model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com/v1",
    streaming=True,
)

SYSTEM_PROMPT = "你是一个乐于助人的中文助手。"


# ---------------------------------------------------------------------------
# RunEvent 协议
# ---------------------------------------------------------------------------

EventType = Literal[
    "run.started",
    "text.delta",
    "run.completed",
    "run.failed",
    "run.cancelled",
]
TERMINAL_TYPES = {"run.completed", "run.failed", "run.cancelled"}


@dataclass(frozen=True)
class RunEvent:
    run_id: str
    seq: int
    type: EventType
    data: dict[str, Any]
    schema_version: str = "1"


@dataclass
class RunState:
    run_id: str
    status: str = "running"
    events: list[RunEvent] = field(default_factory=list)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    def append(self, event_type: EventType, data: dict[str, Any]) -> RunEvent:
        event = RunEvent(
            run_id=self.run_id,
            seq=len(self.events),  # 事件在这个 Run 内的稳定位置
            type=event_type,
            data=data,
        )
        self.events.append(event)
        return event


RUNS: dict[str, RunState] = {}
TASKS: dict[str, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# 后台任务：真正驱动模型，产出事件
# ---------------------------------------------------------------------------

async def run_model(state: RunState, prompt: str) -> None:
    state.append("run.started", {})
    messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)]

    text = ""
    finish_reason = None

    try:
        async for chunk in model.astream(messages):
            if state.cancelled.is_set():
                state.status = "cancelled"
                state.append(
                    "run.cancelled",
                    {"reason": "user_requested", "partial_text": text},
                )
                return

            if chunk.content:
                text += chunk.content
                state.append("text.delta", {"delta": chunk.content})

            finish_reason = chunk.response_metadata.get("finish_reason") or finish_reason

        # 流结束 ≠ 任务成功：输出被截断也要算失败
        if finish_reason == "length":
            state.status = "failed"
            state.append(
                "run.failed",
                {"code": "OUTPUT_TRUNCATED", "partial_text": text},
            )
            return

        state.status = "completed"
        state.append(
            "run.completed",
            {"text": text, "finish_reason": finish_reason},
        )

    except asyncio.CancelledError:
        state.status = "cancelled"
        state.append(
            "run.cancelled",
            {"reason": "task_cancelled", "partial_text": text},
        )
        raise
    except Exception as exc:
        state.status = "failed"
        state.append(
            "run.failed",
            {"code": "MODEL_STREAM_FAILED", "error": str(exc), "partial_text": text},
        )


# ---------------------------------------------------------------------------
# SSE 编码 + 订阅（简单轮询，好懂优先于优雅）
# ---------------------------------------------------------------------------

def encode_sse(event: RunEvent) -> str:
    payload = json.dumps(asdict(event), ensure_ascii=False)
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"


async def subscribe(request: Request, state: RunState, after_seq: int):
    cursor = after_seq + 1
    while True:
        while cursor < len(state.events):
            event = state.events[cursor]
            cursor += 1
            yield encode_sse(event)
            if event.type in TERMINAL_TYPES:
                return

        if await request.is_disconnected():
            # 断开连接只停止推送，不取消后台任务
            return

        await asyncio.sleep(0.2)


# ---------------------------------------------------------------------------
# 三个接口
# ---------------------------------------------------------------------------

class CreateRunRequest(BaseModel):
    prompt: str = Field(min_length=1)


@app.post("/v1/runs")
async def create_run(body: CreateRunRequest) -> dict:
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    state = RunState(run_id=run_id)
    RUNS[run_id] = state

    task = asyncio.create_task(run_model(state, body.prompt))
    TASKS[run_id] = task

    return {"run_id": run_id, "status": state.status}


@app.get("/v1/runs/{run_id}/events")
async def get_events(
    run_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    state = RUNS.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="run not found")

    # 支持断线重连：客户端带着上次收到的 Last-Event-ID 回来，从下一条继续推
    after_seq = int(last_event_id) if last_event_id is not None else -1

    return StreamingResponse(
        subscribe(request, state, after_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


# ---------------------------------------------------------------------------
# 用法（保持简单，不额外写前端页面）：
#
#   uvicorn runs_api:app --reload --port 8001
#
#   1) 创建:
#      curl -s -X POST localhost:8001/v1/runs \
#           -H "Content-Type: application/json" \
#           -d '{"prompt":"用一句话介绍你自己"}'
#      => {"run_id": "run_xxxx", "status": "running"}
#
#   2) 订阅 (SSE，命令行看原始事件流):
#      curl -N localhost:8001/v1/runs/run_xxxx/events
#
#   3) 取消:
#      curl -s -X POST localhost:8001/v1/runs/run_xxxx/cancel
#
#   4) 断线重连（从 seq=3 之后继续推）:
#      curl -N -H "Last-Event-ID: 3" localhost:8001/v1/runs/run_xxxx/events
# ---------------------------------------------------------------------------