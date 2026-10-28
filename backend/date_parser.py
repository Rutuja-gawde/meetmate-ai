import re
from datetime import datetime, timedelta

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# A date that is only a little in the past (e.g. "October 14th" said in an old recording that
# is processed two weeks later) still belongs to THIS year. Only a date that is clearly long
# past ("by January 5th" said in December) is moved to next year.
ROLL_TO_NEXT_YEAR_AFTER_DAYS = 60


def _month_day(month_str, day_str, year):
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(f"{month_str} {day_str} {year}", fmt)
        except ValueError:
            continue
    return None


def _explicit_date(phrase_clean, reference_date):
    """'before August 5th', 'by 12th October', 'October 14th' -> datetime, or None."""
    month_first = re.search(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b", phrase_clean)
    day_first = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)", phrase_clean)
    candidates = []
    if month_first:
        candidates.append((month_first.group(1), month_first.group(2)))
    if day_first:
        candidates.append((day_first.group(2), day_first.group(1)))

    for month_str, day_str in candidates:
        parsed = _month_day(month_str, day_str, reference_date.year)
        if parsed is None:
            continue
        if parsed < reference_date - timedelta(days=ROLL_TO_NEXT_YEAR_AFTER_DAYS):
            parsed = _month_day(month_str, day_str, reference_date.year + 1) or parsed
        return parsed
    return None


def parse_due_date(phrase, reference_date=None):
    """
    Converts phrases like 'Thursday', 'Monday', 'end of this week',
    'before August 5th' into an actual date (YYYY-MM-DD).
    Falls back to None if it can't confidently parse.
    """
    if not phrase:
        return None

    reference_date = reference_date or datetime.now()
    phrase_clean = str(phrase).lower().strip()

    # Case 1: an explicit date wins over a weekday name ("Monday, October 12th" -> Oct 12)
    explicit = _explicit_date(phrase_clean, reference_date)
    if explicit:
        return explicit.strftime("%Y-%m-%d")

    # Case 2: plain weekday name, e.g. "Thursday"
    for i, day in enumerate(WEEKDAYS):
        if day in phrase_clean:
            days_ahead = (i - reference_date.weekday() + 7) % 7
            days_ahead = days_ahead if days_ahead != 0 else 7  # always next occurrence, not today
            return (reference_date + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    # Case 3: "end of this week" -> upcoming Friday
    if "end of" in phrase_clean and "week" in phrase_clean:
        days_ahead = (4 - reference_date.weekday() + 7) % 7  # Friday = index 4
        days_ahead = days_ahead if days_ahead != 0 else 7
        return (reference_date + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    return None  # couldn't parse confidently


if __name__ == "__main__":
    print(parse_due_date("Thursday"))
    print(parse_due_date("Monday"))
    print(parse_due_date("end of this week"))
    print(parse_due_date("before August 5th"))
