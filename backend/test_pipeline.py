import os
from task_agent import structure_tasks
from automation_agent import push_to_notion, push_to_slack
from google_agent import push_to_calendar, send_summary_email

sample_output = '''
{
  "action_items": [
    {"task": "Send final ad creatives to design team", "owner": "Speaker 2 (Sushant)", "due_date": "Thursday"},
    {"task": "Set up campaign tracking dashboard", "owner": "Speaker 3 (Shruti)", "due_date": "Monday"}
  ]
}
'''

if __name__ == "__main__":
    tasks = structure_tasks(sample_output)
    push_to_notion(tasks)
    push_to_slack(tasks)
    push_to_calendar(tasks)
    send_summary_email(os.getenv("EMAIL_RECIPIENTS", ""), "Meeting Summary", "Here are today's action items.")