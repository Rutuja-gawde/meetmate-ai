import json
from date_parser import parse_due_date

def structure_tasks(summary_json_str):
    data = json.loads(summary_json_str)
    return [
        {
            "owner": item.get("owner"),
            "task": item.get("task"),
            "due_date_raw": item.get("due_date"),
            "due_date": parse_due_date(item.get("due_date")),
            "priority": "medium"
        }
        for item in data.get("action_items", [])
    ]