"""
Business-hours arithmetic driven by the profession config, not a hardcoded
zone. Ported from the reference db.py lines 102-219 (add_biz_days,
_clamp_to_biz, add_biz_hours, elapsed_biz_hours, ack_urgency_score) with the
fixed UTC-6 offset replaced by zoneinfo and the 7am-5pm window replaced by
`business_hours` from the profession template:

    {"tz": "America/Denver", "start": 8, "end": 17, "days": [0,1,2,3,4]}

`days` are Python weekday numbers (0 = Monday).
"""

from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


def _zone(name):
    if ZoneInfo is not None and name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


class BusinessHours:
    def __init__(self, tz="UTC", start=8, end=17, days=(0, 1, 2, 3, 4)):
        self.tz_name = tz or "UTC"
        self.tz = _zone(self.tz_name)
        self.start = int(start)
        self.end = int(end)
        if self.end <= self.start:
            self.start, self.end = 8, 17
        self.days = tuple(sorted(set(int(d) for d in (days or (0, 1, 2, 3, 4)))))
        if not self.days:
            self.days = (0, 1, 2, 3, 4)

    @classmethod
    def from_profession(cls, prof):
        bh = (prof or {}).get("business_hours") or {}
        return cls(tz=bh.get("tz") or "UTC", start=bh.get("start", 8),
                   end=bh.get("end", 17), days=bh.get("days") or (0, 1, 2, 3, 4))

    @property
    def hours_per_day(self):
        return self.end - self.start

    def as_dict(self):
        return {"tz": self.tz_name, "start": self.start, "end": self.end,
                "days": list(self.days)}

    # -- clocks -----------------------------------------------------------------

    def now(self):
        return datetime.now(self.tz)

    def now_iso(self):
        return self.now().isoformat(timespec="seconds")

    def now_local_naive(self):
        """Wall-clock in the business zone with tzinfo dropped. The board parses
        no-offset ISO strings as local time, so SLA clocks are written in this
        shape (reference server.py:324-348)."""
        return self.now().replace(tzinfo=None)

    def today(self):
        return self.now().date().isoformat()

    def to_local(self, dt):
        """Any datetime -> aware datetime in the business zone. A naive input is
        taken as already local."""
        if isinstance(dt, str):
            dt = parse_dt(dt)
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=self.tz)
        return dt.astimezone(self.tz)

    def to_local_naive(self, dt):
        d = self.to_local(dt)
        return d.replace(tzinfo=None) if d else None

    def is_business_day(self, d):
        return d.weekday() in self.days

    # -- arithmetic ---------------------------------------------------------------

    def add_biz_days(self, from_date, days):
        if isinstance(from_date, str):
            d = date.fromisoformat(from_date[:10])
        elif isinstance(from_date, datetime):
            d = from_date.date()
        elif isinstance(from_date, date):
            d = from_date
        else:
            d = self.now().date()
        added = 0
        while added < days:
            d += timedelta(days=1)
            if self.is_business_day(d):
                added += 1
        return d.isoformat()

    def _next_day_start(self, dt):
        dt = dt.replace(hour=self.start, minute=0, second=0, microsecond=0) + timedelta(days=1)
        while not self.is_business_day(dt):
            dt += timedelta(days=1)
        return dt

    def clamp(self, dt):
        """Snap to the nearest open-hours boundary: before open -> open today;
        after close or a non-business day -> open on the next business day."""
        dt = self.to_local(dt)
        while not self.is_business_day(dt):
            dt = dt.replace(hour=self.start, minute=0, second=0, microsecond=0) + timedelta(days=1)
        if dt.hour < self.start:
            dt = dt.replace(hour=self.start, minute=0, second=0, microsecond=0)
        elif dt.hour >= self.end:
            dt = self._next_day_start(dt)
        return dt

    def add_biz_hours(self, from_dt, hours):
        """Add N business hours. Returns an ISO string in the business zone."""
        dt = self.clamp(from_dt)
        remaining = float(hours)
        while remaining > 0:
            end_of_day = dt.replace(hour=self.end, minute=0, second=0, microsecond=0)
            available = (end_of_day - dt).total_seconds() / 3600
            if available >= remaining:
                dt += timedelta(hours=remaining)
                remaining = 0
            else:
                remaining -= available
                dt = self._next_day_start(dt)
        return dt.isoformat(timespec="seconds")

    def elapsed_biz_hours(self, start_dt, end_dt=None):
        """Business hours between two instants. Float, rounded to 2 places."""
        start = self.clamp(start_dt)
        end = self.clamp(end_dt if end_dt is not None else self.now())
        if end <= start:
            return 0.0
        total = 0.0
        cursor = start
        while cursor < end:
            end_of_day = cursor.replace(hour=self.end, minute=0, second=0, microsecond=0)
            if end <= end_of_day:
                total += (end - cursor).total_seconds() / 3600
                break
            total += (end_of_day - cursor).total_seconds() / 3600
            cursor = self._next_day_start(cursor)
        return round(total, 2)

    def urgency_score(self, staged_at, sla_hours=4):
        """0 = just staged, 50 = at the SLA boundary, 100 = twice overdue."""
        elapsed = self.elapsed_biz_hours(staged_at)
        if elapsed <= 0 or not sla_hours:
            return 0
        ratio = elapsed / float(sla_hours)
        if ratio <= 1.0:
            return int(ratio * 50)
        return min(100, int(50 + (ratio - 1.0) * 50))


def parse_dt(raw):
    """Lenient ISO parse ('Z' accepted). None on failure."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return None


DEFAULT_HOURS = BusinessHours()
