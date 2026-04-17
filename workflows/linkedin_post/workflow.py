"""LinkedIn posting workflow backed by Orcheo vault credentials."""

from langgraph.graph import END, START, StateGraph
from orcheo.graph.state import State
from orcheo.nodes.linkedin import LinkedInPostNode


def build_graph() -> StateGraph:
    """Build the LinkedIn posting workflow."""
    graph = StateGraph(State)
    graph.add_node(
        "post_linkedin",
        LinkedInPostNode(
            name="post_linkedin",
        ),
    )
    graph.add_edge(START, "post_linkedin")
    graph.add_edge("post_linkedin", END)
    return graph
