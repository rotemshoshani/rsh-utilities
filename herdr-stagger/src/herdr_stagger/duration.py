"""Duration grammar shared by every herdr-stagger option that takes a time."""

import re

MAX_SECONDS = 24 * 60 * 60

_SUFFIXED = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_COLON = re.compile(r"^(\d+)(?::(\d+)){1,2}$")


class DurationError(ValueError):
    """Raised when a duration string cannot be parsed."""


def parse_duration(text, allow_zero=False):
    """Parse a duration into whole seconds.

    Accepts bare seconds (``90``), suffixed units (``90s``, ``10m``, ``2h``,
    ``1h30m``), and slp-compatible clock forms (``MM:SS``, ``HH:MM:SS``).

    ``allow_zero`` admits ``0``, which is how an operator says "no delay
    before this one" for a start delay or a gap.
    """
    if text is None:
        raise DurationError("duration is required")
    raw = str(text).strip().lower()
    if not raw:
        raise DurationError("duration is empty")

    if raw.isdigit():
        return _checked(int(raw), raw, allow_zero)

    if ":" in raw:
        if not _COLON.match(raw):
            raise DurationError("bad clock duration: %s" % text)
        parts = [int(p) for p in raw.split(":")]
        if len(parts) == 2:
            hours, minutes, seconds = 0, parts[0], parts[1]
        else:
            hours, minutes, seconds = parts
        if len(parts) == 3 and (minutes > 59 or seconds > 59):
            raise DurationError("bad clock duration: %s" % text)
        if len(parts) == 2 and seconds > 59:
            raise DurationError("bad clock duration: %s" % text)
        return _checked(hours * 3600 + minutes * 60 + seconds, raw, allow_zero)

    match = _SUFFIXED.match(raw)
    if not match or not any(match.groups()):
        raise DurationError("bad duration: %s (try 90s, 10m, 2h, 1h30m)" % text)
    hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return _checked(hours * 3600 + minutes * 60 + seconds, raw, allow_zero)


def _checked(seconds, raw, allow_zero=False):
    if seconds < 0 or (seconds == 0 and not allow_zero):
        raise DurationError("duration must be positive: %s" % raw)
    if seconds > MAX_SECONDS:
        raise DurationError(
            "duration exceeds the 24h ceiling: %s" % raw
        )
    return seconds


def format_duration(seconds):
    """Render seconds compactly: 45s, 2m30s, 1h05m12s."""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%dh%02dm%02ds" % (hours, minutes, secs)
    if minutes:
        return "%dm%02ds" % (minutes, secs)
    return "%ds" % secs
