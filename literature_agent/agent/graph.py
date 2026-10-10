"""LangGraph 状态图定义：Plan → Act → Observe → Reflect ⇄ Verify → Finish。

循环保护：
- Act 每执行一个工具 current_step +1，超过 max_steps 后 Reflect 强制进入 Verify；
- Verify 不通过可走 Refine 生成补救动作回 Act；
- Refine 无动作可做时直接 Finish，杜绝 Refine↔Verify 死循环。

Checkpoint：默认编译为无持久化图（轻量、确定性，适合一次性解析）；
需要同进程内中断恢复时传入共享 MemorySaver 与 thread_id（见 agent.__init__）。
"""
from langgraph.graph import END, START, StateGraph

from agent.nodes.act_node import act_node
from agent.nodes.finish_node import finish_node
from agent.nodes.observe_node import observe_node
from agent.nodes.plan_node import plan_node
from agent.nodes.refine_node import refine_node, refine_router
from agent.nodes.reflect_node import reflect_node, reflect_router
from agent.nodes.verify_node import verify_node, verify_router
from agent.state import (
    AgentState,
    NODE_ACT,
    NODE_FINISH,
    NODE_OBSERVE,
    NODE_PLAN,
    NODE_REFINE,
    NODE_REFLECT,
    NODE_VERIFY,
    ROUTE_ACT,
    ROUTE_FINISH,
    ROUTE_REFINE,
    ROUTE_VERIFY,
)


def build_parse_graph(checkpointer=None):
    """编译文献解析 Agent 状态图。

    Args:
        checkpointer: 可选 LangGraph checkpointer（如 MemorySaver）。
            传入后可用 thread_id 做同进程中断恢复；None 表示无持久化。
    Returns:
        编译后的 LangGraph 可执行图（invoke / stream / get_state）。
    """
    graph = StateGraph(AgentState)

    graph.add_node(NODE_PLAN, plan_node)
    graph.add_node(NODE_ACT, act_node)
    graph.add_node(NODE_OBSERVE, observe_node)
    graph.add_node(NODE_REFLECT, reflect_node)
    graph.add_node(NODE_VERIFY, verify_node)
    graph.add_node(NODE_REFINE, refine_node)
    graph.add_node(NODE_FINISH, finish_node)

    graph.add_edge(START, NODE_PLAN)
    graph.add_edge(NODE_PLAN, NODE_ACT)
    graph.add_edge(NODE_ACT, NODE_OBSERVE)
    graph.add_edge(NODE_OBSERVE, NODE_REFLECT)

    # Reflect：队列不空 → 继续 Act；清空 → Verify 校验
    graph.add_conditional_edges(
        NODE_REFLECT, reflect_router,
        {ROUTE_ACT: NODE_ACT, ROUTE_VERIFY: NODE_VERIFY},
    )
    # Verify：通过 → Finish；不通过 → Refine 补证
    graph.add_conditional_edges(
        NODE_VERIFY, verify_router,
        {ROUTE_FINISH: NODE_FINISH, ROUTE_REFINE: NODE_REFINE},
    )
    # Refine：有补救动作 → Act；无动作 → Finish（防死循环）
    graph.add_conditional_edges(
        NODE_REFINE, refine_router,
        {ROUTE_ACT: NODE_ACT, ROUTE_FINISH: NODE_FINISH},
    )
    graph.add_edge(NODE_FINISH, END)

    return graph.compile(checkpointer=checkpointer)
