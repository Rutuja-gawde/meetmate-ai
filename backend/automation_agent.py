import os
from dotenv import load_dotenv
from notion_client import Client
from slack_sdk import WebClient

load_dotenv()

# --- Notion setup ---
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
notion = Client(auth=NOTION_TOKEN)

# --- Slack setup ---
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")
slack_client = WebClient(token=SLACK_BOT_TOKEN)


def push_to_notion(tasks):
    for t in tasks:
        properties = {
            "Task": {"title": [{"text": {"content": t["task"]}}]},
            "Owner": {"rich_text": [{"text": {"content": t["owner"]}}]}
        }
        if t.get("due_date"):
            properties["Due Date"] = {"date": {"start": t["due_date"]}}

        notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties=properties
        )
    print(f"Pushed {len(tasks)} tasks to Notion.")


def push_to_slack(tasks, channel=None):
    channel = channel or SLACK_CHANNEL_ID
    message = "*New Meeting Action Items:*\n"
    for t in tasks:
        due = t.get("due_date") or t.get("due_date_raw", "unspecified")
        message += f"• *{t['task']}* — {t['owner']} (due {due})\n"

    slack_client.chat_postMessage(channel=channel, text=message)
    print(f"Posted {len(tasks)} tasks to Slack.")