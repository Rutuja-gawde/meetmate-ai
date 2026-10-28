"""Offline check of the task-structuring step (no accounts or network needed).
Run from the backend folder:  python test_pipeline.py

Testing the real Notion/Slack/Google delivery now needs a signed-in user's saved
connections, so do that through the website (Connections > then upload a short recording)."""

from task_agent import structure_tasks

sample_output = '''
{
  "action_items": [
    {"task": "Send final ad creatives to design team", "owner": "Speaker 2 (Sushant)", "due_date": "Thursday"},
    {"task": "Set up campaign tracking dashboard", "owner": "Speaker 3 (Shruti)", "due_date": "Monday"}
  ]
}
'''

if __name__ == "__main__":
    for task in structure_tasks(sample_output):
        print(task)
