"""The main window's parts (spec 16): status row, live list, banners, recent sessions, hand-off text."""


def clock_text(seconds: float) -> str:
    """``H:MM:SS`` of whole seconds, as the elapsed time and the list's offsets show it."""
    whole = max(0, int(seconds))
    return f"{whole // 3600}:{whole // 60 % 60:02d}:{whole % 60:02d}"
