# ADR-001：用 LangGraph 作为 Agent 编排框架

> 状态：✅ Accepted
> 日期：2026-05-18
> 关联：架构设计 v1.0 §3.1, §4.4
> 涉及代码：`src/orchestrator/graph.py`、`pyproject.toml`

## 背景

M2 起需要把 Director / Scriptwriter / Storyboarder / PromptSmith / ShotProducer / QA / Voice / Subtitle / Compositor 串成多 Agent 流水线，并支持：

- 顺序 + 分支 + 循环（QA 失败重试）
- 显式状态对象 in/out（PipelineState）
- 节点级别的可观测性（每个 Agent 的输入输出可单独 dump 用于调试）
- 后续 M3 加入断点续跑（checkpointer）的能力

## 候选

| 候选 | 优点 | 缺点 |
|---|---|---|
| **LangGraph** | 状态机原生、有 checkpointer、社区活跃、与 Pydantic schema 配合好 | 学习曲线、依赖较重 |
| CrewAI | 角色化 Agent 写法直观 | 流程控制弱（条件分支、循环不灵活） |
| AutoGen | 群聊式协作 | 难以控制确定性输出 |
| 自研轻量 Pipeline | 完全可控、零依赖 | 重复造轮子，断点续跑/HITL 都得自己写 |

## 决策

选 **LangGraph**。

## 落地约定

1. **每个 Agent = 一个 graph node**。Agent 的"业务逻辑"写在 `src/agents/<name>.py` 里（纯函数 + Pydantic 输入输出），`src/orchestrator/graph.py` 只负责编排（节点闭包、依赖注入、边定义）。
2. **节点签名统一为 `async def(state: PipelineState) -> dict[str, Any]`**，返回的 dict 由 LangGraph 自动 merge 到 state。
3. **依赖注入**：LLM / Enhancer / ComfyUI 客户端通过闭包注入，不放进 state（state 只装数据，不装连接对象）。
4. **schema-first**：先在 `src/orchestrator/state.py` 把所有数据模型定义齐，Agent 实现时直接 import，避免到处定义类型。
5. **本次（M2-A/B）只实现最小图**：`director → fanout_shots → prompt_smith`。Scriptwriter / Storyboarder / ShotProducer 等节点在后续迭代逐步替换 / 串接。

## 不做的事

- ❌ 暂不引入 LangChain Runnables / Chains（避免双层抽象；只用 LangGraph 核心）。
- ❌ 暂不接 LangSmith（追踪 / 评测 M3 再说）。
- ❌ 暂不开启 LangGraph checkpointer（断点续跑放到 M3 生产线模式时再开启 SQLite 后端）。

## 退路

如果 LangGraph 后续暴露重大缺陷（例如与 Pydantic v2 兼容性问题），可降级为自研 `Pipeline` 类（每个 step 是 async 函数，state 显式传递）。Agent 业务代码不会被绑定，迁移成本可控。
