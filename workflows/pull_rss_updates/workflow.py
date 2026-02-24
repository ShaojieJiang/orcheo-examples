"""Pull Updates – Orcheo equivalent of the n8n 'Pull Updates' workflow.

Fetches RSS feeds from a curated list every 30 minutes,
adds a ``read: false`` flag to each entry, and inserts them into MongoDB.

RSS source URLs are maintained in the companion ``workflow_config.json``
and uploaded via ``--config-file workflow_config.json``.
"""

import datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from orcheo.graph.state import State
from orcheo.nodes.base import TaskNode
from orcheo.nodes.data import HttpRequestNode
from orcheo.nodes.mongodb import MongoDBNode
from orcheo.nodes.triggers import CronTriggerNode


def extract_tag_text(xml: str, tag: str) -> str:
    """Extract text content of the first occurrence of <tag>...</tag>."""
    open_tag = "<" + tag
    close_tag = "</" + tag + ">"
    start = xml.find(open_tag)
    if start == -1:
        return ""
    # Skip past the opening tag (handles attributes)
    gt = xml.find(">", start)
    if gt == -1:
        return ""
    end = xml.find(close_tag, gt)
    if end == -1:
        return ""
    content = xml[gt + 1 : end]
    # Strip CDATA wrapper if present
    if content.strip().startswith("<![CDATA["):
        content = content.strip()[9:]
        if content.endswith("]]>"):
            content = content[:-3]
    return content.strip()


def extract_link(xml: str) -> str:
    """Extract link from RSS <link> or Atom <link href='...'>."""
    # Try Atom-style <link href="..."/>
    link_start = xml.find("<link")
    while link_start != -1:
        tag_end = xml.find(">", link_start)
        if tag_end == -1:
            break
        tag_content = xml[link_start : tag_end + 1]
        href_pos = tag_content.find('href="')
        if href_pos != -1:
            val_start = href_pos + 6
            val_end = tag_content.find('"', val_start)
            if val_end != -1:
                return tag_content[val_start:val_end]
        href_pos = tag_content.find("href='")
        if href_pos != -1:
            val_start = href_pos + 6
            val_end = tag_content.find("'", val_start)
            if val_end != -1:
                return tag_content[val_start:val_end]
        # RSS-style <link>URL</link>
        if tag_content.endswith("/>"):
            link_start = xml.find("<link", tag_end)
            continue
        close = xml.find("</link>", tag_end)
        if close != -1:
            return xml[tag_end + 1 : close].strip()
        break
    return ""


def parse_rss_items(body: str) -> list[dict[str, str]]:
    """Parse RSS/Atom items from XML body using string operations."""
    items = []

    # Split by RSS <item> or Atom <entry>
    for item_tag in ("item", "entry"):
        open_tag = "<" + item_tag
        close_tag = "</" + item_tag + ">"
        pos = 0
        while True:
            start = body.find(open_tag, pos)
            if start == -1:
                break
            end = body.find(close_tag, start)
            if end == -1:
                break
            fragment = body[start : end + len(close_tag)]

            title = extract_tag_text(fragment, "title")
            link = extract_link(fragment)
            description = extract_tag_text(fragment, "description")
            if not description:
                description = extract_tag_text(fragment, "summary")
            pub_date = extract_tag_text(fragment, "pubDate")
            if not pub_date:
                pub_date = extract_tag_text(fragment, "published")
            if not pub_date:
                pub_date = extract_tag_text(fragment, "updated")

            items.append({
                "title": title,
                "link": link,
                "description": description,
                "pubDate": pub_date,
            })

            pos = end + len(close_tag)

    return items


class FetchRSSNode(TaskNode):
    """Fetch all RSS feeds via HTTP and parse entries."""

    async def run(self, state: State, config: RunnableConfig) -> dict[str, Any]:
        sources = config.get("configurable", {}).get("rss_sources", [])

        documents: list[dict[str, Any]] = []
        now = datetime.datetime.utcnow().isoformat()

        for url in sources:
            try:
                fetcher = HttpRequestNode(name="http_fetch", url=url, timeout=15.0)
                result = await fetcher.run(state, config)
                body = result.get("content", "")
                if not body:
                    continue

                for item in parse_rss_items(body):
                    doc = {
                        "title": item.get("title", ""),
                        "link": item.get("link", ""),
                        "description": item.get("description", ""),
                        "pubDate": item.get("pubDate", ""),
                        "source": url,
                        "read": False,
                        "fetched_at": now,
                    }
                    documents.append(doc)
            except Exception:
                pass

        return {"documents": documents, "fetched_count": len(documents)}


class StoreRSSNode(TaskNode):
    """Insert RSS entries into MongoDB."""

    connection_string: str = "[[mdb_connection_string]]"
    database: str = "orcheo"
    collection: str = "rss_feeds"

    async def run(self, state: State, config: RunnableConfig) -> dict[str, Any]:
        documents = state["results"]["fetch_rss"]["documents"]

        inserted_count = 0
        if documents:
            node = MongoDBNode(
                name="mongo_insert",
                connection_string=self.connection_string,
                database=self.database,
                collection=self.collection,
                operation="insert_many",
                query=documents,
            )
            result = await node.run(state, config)
            inserted_count = len(result.get("data", []))

        return {"inserted_count": inserted_count}


async def orcheo_workflow() -> StateGraph:
    """Build and return the Pull Updates workflow."""
    graph = StateGraph(State)

    graph.add_node(
        "cron_trigger",
        CronTriggerNode(
            name="cron_trigger",
            expression="*/30 * * * *",
        ),
    )

    graph.add_node(
        "fetch_rss",
        FetchRSSNode(name="fetch_rss"),
    )

    graph.add_node(
        "store_rss",
        StoreRSSNode(name="store_rss"),
    )

    graph.add_edge(START, "cron_trigger")
    graph.add_edge("cron_trigger", "fetch_rss")
    graph.add_edge("fetch_rss", "store_rss")
    graph.add_edge("store_rss", END)

    return graph
