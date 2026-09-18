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

## 已知的取舍（教学版，不是生产版）

- "拒答"（模型主动拒绝回答）没有单独识别和处理，目前会和"格式错"一样
  走 retry-with-feedback 兜底重试几次，如果真的是拒答，重试大概率还是
  会失败，最终会抛出 `MODEL_SCHEMA_INVALID`。生产环境如果需要区分拒答，
  可以检查 `finish_reason == "content_filter"`，单独判定为不重试。
- 结构化输出场景下 `temperature` / `top_p` 之类采样参数没有暴露出来，
  因为这一版的重点是"错误分类 + 重试"，不是"参数调节"；如果需要，
  在 `model.with_structured_output(...)` 之前对 `model` 调用
  `.bind(temperature=..., top_p=...)` 即可加回来。
