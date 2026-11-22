"""Convert local scheduling times and format real calendar instants."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_instant(value: str | datetime) -> datetime:
    """Normalize a datetime with an explicit offset to UTC."""
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    if instant.utcoffset() is None:
        raise ValueError("A calendar instant needs a UTC offset.")
    return instant.astimezone(UTC)


def local_datetime(day: date, at: time, time_zone: str) -> datetime:
    """Resolve a wall time, requiring a matching offset during a repeated hour."""
    zone = ZoneInfo(time_zone)
    requested = datetime.combine(day, at)
    wall_time = requested.replace(tzinfo=None)
    instances: dict[datetime, datetime] = {}
    for fold in (0, 1):
        instant = wall_time.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        local = instant.astimezone(zone)
        if local.replace(tzinfo=None) == wall_time:
            instances[instant] = local

    if not instances:
        raise ValueError(f"{wall_time.isoformat()} does not exist in {time_zone}.")

    offset = requested.utcoffset()
    if offset is not None:
        for local in instances.values():
            if local.utcoffset() == offset:
                return local
        raise ValueError(f"The UTC offset does not match {time_zone} at {wall_time.isoformat()}.")

    if len(instances) > 1:
        raise ValueError(f"{wall_time.isoformat()} is ambiguous in {time_zone}. Supply its UTC offset.")
    return next(iter(instances.values()))


def _day_boundary(day: date, zone: ZoneInfo) -> datetime:
    midnight = datetime.combine(day, time.min, zone)
    earlier, later = sorted(midnight.replace(fold=fold).astimezone(UTC) for fold in (0, 1))
    if earlier.astimezone(zone).date() >= day:
        return earlier

    # A gap can span midnight. The two folds bracket the first instant of the day.
    while later - earlier > timedelta(microseconds=1):
        midpoint = earlier + (later - earlier) // 2
        if midpoint.astimezone(zone).date() < day:
            earlier = midpoint
        else:
            later = midpoint
    return later


def local_bounds(start_date: date, end_date: date, time_zone: str) -> tuple[datetime, datetime]:
    """Return half-open UTC bounds for an inclusive range of local dates."""
    if start_date > end_date:
        raise ValueError("The start date must be on or before the end date.")
    zone = ZoneInfo(time_zone)
    return _day_boundary(start_date, zone), _day_boundary(end_date + timedelta(days=1), zone)


def _date_label(local: datetime) -> str:
    return f"{local:%a} {local.day} {local:%b} {local.year}"


def _time_label(local: datetime, show_offset: bool) -> str:
    clock = local.strftime("%H:%M")
    if not show_offset:
        return clock
    offset = local.strftime("%z")
    offset_parts = [offset[:3], offset[3:5]]
    if offset[5:]:
        offset_parts.append(offset[5:])
    return f"{clock} UTC{':'.join(offset_parts)}"


def slot_labels(start: str | datetime, end: str | datetime, time_zone: str) -> tuple[str, str]:
    """Format the local date and time range of a pair of real instants."""
    zone = ZoneInfo(time_zone)
    start_utc, end_utc = parse_instant(start), parse_instant(end)
    if start_utc >= end_utc:
        raise ValueError("A slot must end after it starts.")

    local_start, local_end = start_utc.astimezone(zone), end_utc.astimezone(zone)
    dates = _date_label(local_start)
    if local_start.date() != local_end.date():
        dates = f"{dates} – {_date_label(local_end)}"

    show_offsets = local_start.utcoffset() != local_end.utcoffset() or any(
        local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset()
        for local in (local_start, local_end)
    )
    times = f"{_time_label(local_start, show_offsets)}–{_time_label(local_end, show_offsets)}"
    return dates, times
