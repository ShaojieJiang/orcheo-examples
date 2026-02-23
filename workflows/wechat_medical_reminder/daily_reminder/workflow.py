"""WeChat Medical Reminder - Daily Reminder workflow.

Cron-triggered workflow that sends personalised health reminders to all
active registered users daily at 9:00 AM Asia/Shanghai.

Configurable inputs (workflow_config.json):
- corp_id (WeCom corp ID)
- reminder_database (MongoDB database name)
- registered_users_collection (collection for user profiles)

Orcheo vault secrets required:
- wecom_app_secret_medical_reminder: WeCom app secret for access token
- mdb_connection_string: MongoDB connection string
"""

from collections.abc import Mapping
from typing import Any
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from orcheo.edges import Condition, While
from orcheo.graph.state import State
from orcheo.nodes.base import TaskNode
from orcheo.nodes.mongodb import MongoDBFindNode
from orcheo.nodes.sub_workflow import SubWorkflowNode
from orcheo.nodes.triggers import CronTriggerNode
from orcheo.nodes.wecom import WeComAccessTokenNode


class PrepareIterationNode(TaskNode):
    """Read the user list from MongoDBFindNode and initialise iteration state."""

    async def run(self, state: State, config: RunnableConfig) -> dict[str, Any]:
        """Return the user list and total count for the While loop."""
        results = state.get("results", {})
        find_result = results.get("find_active_users", {})
        if not isinstance(find_result, Mapping):
            return {"users": [], "total": 0}
        users = find_result.get("data", [])
        if not isinstance(users, list):
            users = []
        return {"users": users, "total": len(users)}


class SelectCurrentUserNode(TaskNode):
    """Pick the user at the current loop index and format a reminder message."""

    async def run(self, state: State, config: RunnableConfig) -> dict[str, Any]:
        """Return the personalised message and user identifiers."""
        results = state.get("results", {})
        users = results.get("prepare_iteration", {}).get("users", [])

        # iteration is written by the increment node (name="loop_users") via
        # the LangGraph reducer.  Before the first increment it defaults to 0.
        loop_state = results.get("loop_users", {})
        iteration = 0
        if isinstance(loop_state, Mapping):
            raw = loop_state.get("iteration", 0)
            iteration = int(raw) if raw is not None else 0
        index = max(iteration, 0)

        if index >= len(users):
            return {
                "message": "",
                "external_userid": "",
                "open_kf_id": "",
            }

        user = users[index]
        username = user.get("external_username", "")
        items = user.get("reminder_items", [])
        if isinstance(items, list) and items:
            items_text = "\n".join(f"- {item}" for item in items)
        else:
            items_text = "- 您的健康任务"

        greeting = f"{username}，早上好！" if username else "早上好！"
        message = (
            f"{greeting}今日健康提醒，请完成以下任务：\n"
            f"{items_text}\n"
            f"完成后请回复您的状态。"
        )
        return {
            "message": message,
            "external_userid": user.get("external_userid", ""),
            "open_kf_id": user.get("open_kf_id", ""),
        }


class IncrementCounterNode(TaskNode):
    """Advance the While loop counter via the LangGraph reducer.

    The node **must** be instantiated with ``name="loop_users"`` so that its
    output is stored in ``results["loop_users"]``, which is the state key the
    ``While`` edge reads for its iteration counter.
    """

    async def run(self, state: State, config: RunnableConfig) -> dict[str, Any]:
        """Increment and persist the iteration counter."""
        results = state.get("results", {})
        loop_state = results.get("loop_users", {})
        iteration = 0
        if isinstance(loop_state, Mapping):
            raw = loop_state.get("iteration", 0)
            iteration = int(raw) if raw is not None else 0
        return {"iteration": iteration + 1}


async def orcheo_workflow() -> StateGraph:
    """Build the Daily Reminder workflow."""
    graph = StateGraph(State)

    # --- Trigger ---
    graph.add_node(
        "cron_trigger",
        CronTriggerNode(
            name="cron_trigger",
            expression="0 9 * * *",
            timezone="Asia/Shanghai",
        ),
    )

    # --- Fetch data ---
    graph.add_node(
        "get_access_token",
        WeComAccessTokenNode(
            name="get_access_token",
            corp_id="{{config.configurable.corp_id}}",
            app_secret="[[wecom_app_secret_medical_reminder]]",
        ),
    )
    # NOTE: Fetches all active users at once. For large deployments (1000+
    # users), consider adding pagination with limit/skip.
    graph.add_node(
        "find_active_users",
        MongoDBFindNode(
            name="find_active_users",
            database="{{config.configurable.reminder_database}}",
            collection="{{config.configurable.registered_users_collection}}",
            filter={"status": "active"},
        ),
    )

    # --- Iteration setup ---
    graph.add_node(
        "prepare_iteration",
        PrepareIterationNode(name="prepare_iteration"),
    )

    # --- Per-user nodes ---
    graph.add_node(
        "select_current_user",
        SelectCurrentUserNode(name="select_current_user"),
    )
    graph.add_node(
        "send_reminder",
        SubWorkflowNode(
            name="send_reminder",
            steps=[
                {
                    "type": "WeComCustomerServiceSendNode",
                    "name": "send_message",
                    "open_kf_id": "{{select_current_user.open_kf_id}}",
                    "external_userid": ("{{select_current_user.external_userid}}"),
                    "message": "{{select_current_user.message}}",
                },
            ],
        ),
    )
    graph.add_node(
        "increment_counter",
        IncrementCounterNode(name="loop_users"),
    )

    # --- Edges ---
    graph.set_entry_point("cron_trigger")
    graph.add_edge("cron_trigger", "get_access_token")
    graph.add_edge("get_access_token", "find_active_users")
    graph.add_edge("find_active_users", "prepare_iteration")

    # While loop: entry check after prepare_iteration
    loop_entry = While(
        name="loop_users",
        conditions=[
            Condition(
                operator="less_than",
                right="{{prepare_iteration.total}}",
            ),
        ],
    )
    graph.add_conditional_edges(
        "prepare_iteration",
        loop_entry,
        {
            "continue": "select_current_user",
            "exit": END,
        },
    )

    graph.add_edge("select_current_user", "send_reminder")
    graph.add_edge("send_reminder", "increment_counter")

    # While loop: continuation check after increment_counter
    loop_continue = While(
        name="loop_users",
        conditions=[
            Condition(
                operator="less_than",
                right="{{prepare_iteration.total}}",
            ),
        ],
    )
    graph.add_conditional_edges(
        "increment_counter",
        loop_continue,
        {
            "continue": "select_current_user",
            "exit": END,
        },
    )

    return graph
