from datetime import UTC, datetime


def utcnow() -> datetime:
    # MongoDB keeps milliseconds and drops the rest. Doing the same here means a
    # document returned straight after it's saved has the same timestamps as it
    # will when read back.
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)
