import datetime
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_tz_name = os.getenv("TIMEZONE", "UTC")
try:
    TZ: datetime.tzinfo = ZoneInfo(_tz_name)
    TZ_NAME: str = _tz_name
except ZoneInfoNotFoundError:
    TZ = datetime.timezone.utc
    TZ_NAME = "UTC"
