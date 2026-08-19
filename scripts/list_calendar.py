"""List scheduled Garmin workouts for Aug 2026 (calendar view)."""
import json
from garminconnect import Garmin

g = Garmin()
g.login("~/.garminconnect")

for month in (8, 9):
    try:
        data = g.get_scheduled_workouts(2026, month)
    except Exception as e:
        print(f"month {month} error: {e}")
        continue
    items = data.get("calendarItems", [])
    print(f"=== {month}/2026: {len(items)} items ===")
    for it in items:
        print(json.dumps({
            "date": it.get("date"),
            "itemType": it.get("itemType"),
            "sportTypeKey": it.get("sportTypeKey"),
            "workoutId": it.get("workoutId"),
            "scheduledWorkoutId": it.get("scheduledWorkoutId"),
            "title": it.get("title") or (it.get("workout") or {}).get("workoutName"),
        }, default=str))
