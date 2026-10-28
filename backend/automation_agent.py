"""Notion and Slack delivery. Each call uses the credentials of the user who owns the
meeting (stored encrypted in the database), never a shared account.

Notion: the user creates an "internal integration" at notion.so/profile/integrations,
        shares their tasks database with it, and pastes the token + database id in Settings.
        The database needs the properties: Task (title), Owner (text), Due Date (date).
Slack:  the user creates an Incoming Webhook for their workspace and pastes its URL.
"""

from notion_client import Client
from notion_client.errors import APIResponseError
from slack_sdk.webhook import WebhookClient

# The properties the Notion database must have, and what to call their types for the user.
REQUIRED_NOTION_PROPERTIES = {"Task": "title", "Owner": "rich_text", "Due Date": "date"}
_TYPE_LABELS = {"title": "Title", "rich_text": "Text", "date": "Date"}


def push_to_notion(tasks, notion_conn: dict):
    notion = Client(auth=notion_conn["token"])
    database_id = notion_conn["database_id"]
    for t in tasks:
        properties = {
            "Task": {"title": [{"text": {"content": t.get("task") or "Untitled task"}}]},
            "Owner": {"rich_text": [{"text": {"content": t.get("owner") or "unassigned"}}]},
        }
        if t.get("due_date"):
            properties["Due Date"] = {"date": {"start": t["due_date"]}}

        notion.pages.create(parent={"database_id": database_id}, properties=properties)
    print(f"Pushed {len(tasks)} tasks to Notion.")


def push_to_slack(tasks, slack_conn: dict, meeting_title: str = ""):
    heading = f"*New meeting action items{' — ' + meeting_title if meeting_title else ''}:*\n"
    if tasks:
        message = heading
        for t in tasks:
            due = t.get("due_date") or t.get("due_date_raw") or "unspecified"
            message += f"• *{t.get('task') or 'Untitled task'}* — {t.get('owner') or 'unassigned'} (due {due})\n"
    else:
        message = heading + "No action items were detected.\n"

    response = WebhookClient(slack_conn["webhook_url"]).send(text=message)
    if response.status_code != 200:
        raise RuntimeError(f"Slack rejected the message ({response.status_code}): {response.body}")
    print(f"Posted {len(tasks)} tasks to Slack.")


# ---------------------------------------------------------------------------
# Checks run when a user saves a connection, so a mistake shows up right away in
# Settings instead of as a failed sync after their next meeting.
# Both raise ValueError with a message that is safe to show to the user.
# ---------------------------------------------------------------------------

def check_notion(notion_conn: dict) -> None:
    notion = Client(auth=notion_conn["token"])
    try:
        database = notion.databases.retrieve(database_id=notion_conn["database_id"])
    except APIResponseError as e:
        code = str(getattr(e, "code", ""))
        if code == "unauthorized":
            raise ValueError("Notion did not accept that token. Copy the integration's secret again.")
        if code in ("object_not_found", "restricted_resource"):
            raise ValueError("Notion cannot see that database. Open it in Notion, click the ... menu, "
                             "choose Connections, add your integration, and check the database link.")
        raise ValueError(f"Notion refused the request: {e}")
    except Exception as e:
        raise ValueError(f"Could not reach Notion: {e}")

    # Best effort: newer Notion versions keep the properties on the database's data source.
    properties = database.get("properties")
    if properties is None:
        try:
            sources = database.get("data_sources") or []
            if sources:
                properties = notion.data_sources.retrieve(data_source_id=sources[0]["id"]).get("properties")
        except Exception:
            properties = None
    if not properties:
        return  # could not read the layout; the real sync will report any mismatch

    problems = []
    for name, kind in REQUIRED_NOTION_PROPERTIES.items():
        found = properties.get(name)
        if found is None:
            problems.append(f'a property named "{name}" (type {_TYPE_LABELS[kind]})')
        elif found.get("type") != kind:
            problems.append(f'"{name}" must be type {_TYPE_LABELS[kind]} (it is {found.get("type")})')
    if problems:
        raise ValueError("The Notion database needs " + "; ".join(problems) + ".")


def send_slack_test(slack_conn: dict) -> None:
    try:
        response = WebhookClient(slack_conn["webhook_url"], timeout=10).send(
            text=":white_check_mark: MeetMate is connected. Meeting action items will be posted here."
        )
    except Exception as e:
        raise ValueError(f"Could not reach Slack: {e}")
    if response.status_code != 200:
        raise ValueError("Slack did not accept that webhook URL. Create a new Incoming Webhook "
                         "and paste its full URL.")
