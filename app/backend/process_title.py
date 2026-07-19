"""Best-effort friendly process title for macOS Activity Monitor."""

PROCESS_TITLE = "Video Studio Mac"


def apply_process_title() -> bool:
    try:
        import setproctitle

        setproctitle.setproctitle(PROCESS_TITLE)
        return True
    except Exception as exc:
        print(f"[process] friendly title unavailable: {exc}", flush=True)
        return False
