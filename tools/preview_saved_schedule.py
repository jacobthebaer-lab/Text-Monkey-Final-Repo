#!/usr/bin/env python3
"""Print fictional proposals only. Never open a database or initialize runtime."""
import argparse
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.demo_schedule import preview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings-json', type=Path, required=True,
                        help='Saved setup JSON, or just its timezone/service_times fields.')
    parser.add_argument('--sunday', required=True, help='Explicit fictional date, YYYY-MM-DD.')
    parser.add_argument('--duration-minutes', type=int, required=True,
                        help='Explicit operator choice; setup does not save service duration.')
    parser.add_argument('--role-id', type=int, required=True, help='Existing reviewed role ID.')
    args = parser.parse_args()
    try:
        data = json.loads(args.settings_json.read_text())
        if not isinstance(data, dict):
            raise ValueError()
        # Ignore private contact/profile fields; only schedule fields are emitted.
        result = preview(data.get('details', data), args.sunday, args.duration_minutes, args.role_id)
    except (OSError, ValueError, TypeError, KeyError, ZoneInfoNotFoundError):
        # Do not echo private input or filenames in an error receipt.
        parser.error('Invalid saved Sunday settings, date, duration or role. Nothing was created.')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
