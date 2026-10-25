import re
from datetime import datetime, timedelta

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

def parse_due_date(phrase, reference_date=None):
    """
    Converts phrases like 'Thursday', 'Monday', 'end of this week',
    'before August 5th' into an actual date (YYYY-MM-DD).
    Falls back to None if it can't confidently parse.
    """
    if not phrase:
        return None

    reference_date = reference_date or datetime.now()
    phrase_clean = phrase.lower().strip()

    # Case 1: plain weekday name, e.g. "Thursday"
    for i, day in enumerate(WEEKDAYS):
        if day in phrase_clean:
            days_ahead = (i - reference_date.weekday() + 7) % 7
            days_ahead = days_ahead if days_ahead != 0 else 7  # always next occurrence, not today
            return (reference_date + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    # Case 2: "end of this week" -> upcoming Friday
    if "end of" in phrase_clean and "week" in phrase_clean:
        days_ahead = (4 - reference_date.weekday() + 7) % 7  # Friday = index 4
        days_ahead = days_ahead if days_ahead != 0 else 7
        return (reference_date + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    # Case 3: explicit date mentioned, e.g. "before August 5th", "by August 5"
    match = re.search(r"([A-Za-z]+)\s+(\d{1,2})(st|nd|rd|th)?", phrase_clean)
    if match:
        month_str, day_str = match.group(1), match.group(2)
        try:
            year = reference_date.year
            parsed = datetime.strptime(f"{month_str} {day_str} {year}", "%B %d %Y")
            if parsed < reference_date:
                parsed = parsed.replace(year=year + 1)  # roll to next year if already passed
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None  # couldn't parse confidently

if __name__ == "__main__":
    print(parse_due_date("Thursday"))
    print(parse_due_date("Monday"))
    print(parse_due_date("end of this week"))
    print(parse_due_date("before August 5th"))