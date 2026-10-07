"""Recipient-local sending windows using the system IANA time zone database."""
import math
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT = {'enabled': False, 'start': '09:00', 'end': '11:00',
           'weekdays_only': True, 'not_before': 0, 'fallback_timezone': 'UTC'}


def valid_zone(value, optional=False):
    if optional and value == '':
        return ''
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError('Use an IANA time zone, such as America/New_York.')
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError('Unknown time zone. Use an IANA name, such as America/New_York.') from None
    return value


def validate(value):
    if not isinstance(value, dict):
        raise ValueError('Choose sending-window settings.')
    result = {**DEFAULT, **{key: value[key] for key in DEFAULT if key in value}}
    if type(result['enabled']) is not bool or type(result['weekdays_only']) is not bool:
        raise ValueError('Choose whether to enable scheduling and skip weekends.')
    for key in ('start', 'end'):
        if not isinstance(result[key], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', result[key]):
            raise ValueError('Use valid start and end times.')
    if result['start'] >= result['end']:
        raise ValueError('End time must be after start time on the same day.')
    timestamp = result['not_before']
    if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or not 0 <= timestamp < 4102444800:
        raise ValueError('Choose a valid earliest start date.')
    valid_zone(result['fallback_timezone'])
    return result


def next_window(now, recipient_zone, settings):
    if not settings['enabled']:
        return now
    now = max(now, settings['not_before'])
    zone = ZoneInfo(recipient_zone or settings['fallback_timezone'])
    local = datetime.fromtimestamp(now, zone)
    minute = local.hour * 60 + local.minute
    start = int(settings['start'][:2]) * 60 + int(settings['start'][3:])
    end = int(settings['end'][:2]) * 60 + int(settings['end'][3:])
    if (not settings['weekdays_only'] or local.weekday() < 5) and start <= minute < end:
        return now
    # Round-trip candidate wall times so DST gaps never invent a send time.
    # Check both folds so repeated local times choose the earliest future slot.
    for offset in range(8):
        day = local.date() + timedelta(days=offset)
        if offset == 0 and local.hour * 60 + local.minute >= end:
            # Keep repeated-hour windows available after the first DST fold.
            if local.replace(fold=0).utcoffset() == local.replace(fold=1).utcoffset():
                continue
        if settings['weekdays_only'] and day.weekday() >= 5:
            continue
        earliest = None
        for minute in range(start, end):
            wall = datetime(day.year, day.month, day.day, minute // 60, minute % 60)
            for fold in (0, 1):
                candidate = wall.replace(tzinfo=zone, fold=fold).timestamp()
                if candidate < now:
                    continue
                restored = datetime.fromtimestamp(candidate, zone)
                if restored.replace(tzinfo=None) == wall:
                    earliest = candidate if earliest is None else min(earliest, candidate)
            if earliest is not None:
                return earliest
    raise ValueError('No available sending window within the next week.')
