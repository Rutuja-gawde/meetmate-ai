import os
import base64
import html
import threading
from datetime import datetime, timedelta
from email.message import EmailMessage

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send"
]

# Calendar and email now run at the same time, so only one of them may
# refresh or write token.json at once.
_creds_lock = threading.Lock()


def get_google_creds():
    with _creds_lock:
        creds = None
        if os.path.exists("token.json"):
            creds = Credentials.from_authorized_user_file("token.json", SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
                creds = flow.run_local_server(port=0)
            with open("token.json", "w") as token:
                token.write(creds.to_json())
        return creds


def push_to_calendar(tasks, calendar_id="primary"):
    creds = get_google_creds()
    service = build("calendar", "v3", credentials=creds)

    created = 0
    for t in tasks:
        due = t.get("due_date")
        if not due:
            continue  # no reliable date, so don't schedule it

        # All-day events need an exclusive end date (the day after).
        end = (datetime.strptime(due, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        event = {
            "summary": t.get("task") or "Untitled task",
            "description": (
                f"Owner: {t.get('owner') or 'unassigned'} "
                f"(originally due: {t.get('due_date_raw') or 'unspecified'})"
            ),
            "start": {"date": due},
            "end": {"date": end},
        }
        service.events().insert(calendarId=calendar_id, body=event).execute()
        created += 1

    print(f"Created {created} events in Google Calendar.")


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _item_text(item) -> str:
    """Agenda/decision items are normally strings, but a model can return objects."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("title") or item.get("topic") or item.get("text") or item)
    return str(item)


def _split_topic(text: str):
    """'Budget review: discussed Q3' -> ('Budget review', 'discussed Q3')"""
    i = text.find(":")
    if 0 < i <= 80:
        return text[:i].strip(), text[i + 1:].strip()
    return "", text


def build_summary_email(meeting_title: str, summary_data: dict, tasks: list):
    """Returns (plain_text, html) versions of the meeting-minutes email."""
    esc = html.escape
    today = datetime.now().strftime("%A, %d %B %Y")

    summary = summary_data.get("summary") or "No summary was generated."
    agenda = [_item_text(a) for a in (summary_data.get("agenda") or [])]
    decisions = [_item_text(d) for d in (summary_data.get("decisions") or [])]

    # ---- plain text version (shown by clients that don't render HTML) ----
    lines = [f"Meeting minutes: {meeting_title}", today, "", "SUMMARY", summary, ""]
    if agenda:
        lines.append("AGENDA")
        lines += [f"{n}. {a}" for n, a in enumerate(agenda, 1)]
        lines.append("")
    if decisions:
        lines.append("DECISIONS")
        lines += [f"- {d}" for d in decisions]
        lines.append("")
    lines.append("ACTION ITEMS")
    if tasks:
        for t in tasks:
            due = t.get("due_date") or t.get("due_date_raw") or "not specified"
            lines.append(f"- {t.get('task') or 'Untitled task'} "
                         f"(owner: {t.get('owner') or 'unassigned'}, due: {due})")
    else:
        lines.append("No action items were detected.")
    lines += ["", "Sent automatically by MeetMate AI."]
    plain = "\n".join(lines)

    # ---- HTML version (inline styles and tables, so it works in Gmail/Outlook) ----
    def section(title, inner):
        return f"""
        <tr><td style="padding:24px 32px 0 32px;">
          <h2 style="margin:0 0 10px 0;font-family:Georgia,'Times New Roman',serif;font-size:18px;color:#10263b;">{esc(title)}</h2>
          {inner}
        </td></tr>"""

    agenda_html = ""
    if agenda:
        items = ""
        for text in agenda:
            topic, note = _split_topic(text)
            label = f"<strong style='color:#10263b;'>{esc(topic)}</strong> &ndash; {esc(note)}" if topic else esc(note)
            items += f"<li style='margin:0 0 8px 0;'>{label}</li>"
        agenda_html = section(
            "Agenda",
            f"<ol style='margin:0;padding-left:20px;font-size:15px;line-height:1.55;color:#3b4a5c;'>{items}</ol>",
        )

    decisions_html = ""
    if decisions:
        items = "".join(f"<li style='margin:0 0 8px 0;'>{esc(d)}</li>" for d in decisions)
        decisions_html = section(
            "Decisions",
            f"<ul style='margin:0;padding-left:20px;font-size:15px;line-height:1.55;color:#3b4a5c;'>{items}</ul>",
        )

    if tasks:
        rows = ""
        for t in tasks:
            due = t.get("due_date") or t.get("due_date_raw") or "Not specified"
            rows += f"""
            <tr>
              <td style="padding:10px 12px;border-top:1px solid #dde3ea;font-size:14px;color:#14202e;">{esc(t.get('task') or 'Untitled task')}</td>
              <td style="padding:10px 12px;border-top:1px solid #dde3ea;font-size:14px;color:#3b4a5c;">{esc(t.get('owner') or 'Unassigned')}</td>
              <td style="padding:10px 12px;border-top:1px solid #dde3ea;font-size:14px;color:#3b4a5c;white-space:nowrap;">{esc(str(due))}</td>
            </tr>"""
        tasks_inner = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #dde3ea;border-radius:8px;border-collapse:separate;">
          <tr style="background:#f2f5f8;">
            <th align="left" style="padding:10px 12px;font-size:13px;color:#667589;">Task</th>
            <th align="left" style="padding:10px 12px;font-size:13px;color:#667589;">Owner</th>
            <th align="left" style="padding:10px 12px;font-size:13px;color:#667589;">Due</th>
          </tr>{rows}
        </table>"""
    else:
        tasks_inner = "<p style='margin:0;font-size:15px;color:#667589;'>No action items were detected.</p>"
    tasks_html = section("Action items", tasks_inner)

    html_body = f"""<!DOCTYPE html>
<html lang="en">
<body style="margin:0;padding:0;background:#f2f5f8;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;">{esc(summary[:120])}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f5f8;">
    <tr><td align="center" style="padding:24px 12px;">
      <table role="presentation" width="640" cellpadding="0" cellspacing="0"
             style="width:100%;max-width:640px;background:#ffffff;border-radius:12px;overflow:hidden;font-family:'Segoe UI',Roboto,Arial,sans-serif;">
        <tr><td style="background:#10263b;padding:28px 32px;">
          <div style="font-family:Georgia,'Times New Roman',serif;font-size:24px;color:#ffffff;">Meeting minutes</div>
          <div style="margin-top:6px;font-size:15px;color:#b9c8d8;">{esc(meeting_title)}</div>
          <div style="margin-top:2px;font-size:13px;color:#8ba1b8;">{esc(today)}</div>
        </td></tr>
        {section("Summary", f"<p style='margin:0;font-size:15px;line-height:1.65;color:#14202e;'>{esc(summary)}</p>")}
        {agenda_html}
        {decisions_html}
        {tasks_html}
        <tr><td style="padding:28px 32px;font-size:12px;color:#8393a6;">
          Sent automatically by MeetMate AI. Action items have also been added to Notion, Slack and Google Calendar where available.
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    return plain, html_body


def _normalize_recipients(recipients) -> list[str]:
    if isinstance(recipients, str):
        recipients = recipients.split(",")
    return [r.strip() for r in recipients if r and r.strip()]


def send_summary_email(recipients, subject, body_text, body_html=None):
    """recipients can be one address, a comma-separated string, or a list."""
    to_list = _normalize_recipients(recipients)
    if not to_list:
        raise ValueError("No email recipients configured. Set EMAIL_RECIPIENTS in the backend .env file.")

    creds = get_google_creds()
    service = build("gmail", "v1", credentials=creds)

    message = EmailMessage()
    message["To"] = ", ".join(to_list)
    message["Subject"] = subject
    message.set_content(body_text)
    if body_html:
        message.add_alternative(body_html, subtype="html")

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"Sent summary email to {len(to_list)} recipient(s).")