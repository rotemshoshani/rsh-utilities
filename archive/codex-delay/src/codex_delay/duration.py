from __future__ import annotations


def parse_duration(value: str) -> int:
    """Parse the same seconds, MM:SS, or HH:MM:SS forms accepted by slp."""
    text = value.strip()
    parts = text.split(":")
    if not 1 <= len(parts) <= 3 or any(not part.isdigit() for part in parts):
        raise ValueError("use seconds, MM:SS, or HH:MM:SS")

    numbers = [int(part, 10) for part in parts]
    if len(numbers) == 1:
        return numbers[0]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]


def format_duration(total_seconds: int) -> str:
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
