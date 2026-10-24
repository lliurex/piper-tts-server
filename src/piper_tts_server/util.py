import logging
import os
from pathlib import Path


def setup_logging(level=None):
    level = level or os.environ.get("PIPER_LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def socket_path():
    path = os.environ.get("PIPER_SOCKET")
    if path:
        return Path(path)
    return Path("/run/piper.sock")


def voices_dir():
    path = os.environ.get("PIPER_VOICES_DIR")
    if path:
        return Path(path).expanduser()
    return Path("/usr/share/piper-voices")


def idle_seconds():
    return float(os.environ.get("PIPER_IDLE_SECONDS", "900"))


def sentence_silence():
    return float(os.environ.get("PIPER_SENTENCE_SILENCE", "0.2"))


def max_languages():
    return int(os.environ.get("PIPER_MAX_LANGUAGES", "2"))


def default_lang():
    lang = os.environ.get("PIPER_DEFAULT_LANG")
    if lang and lang.lower() != "auto":
        return lang
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value and value not in ("C", "POSIX"):
            code = value.split(".")[0].split("_")[0].split("@")[0]
            if code:
                return code.lower()
    return None


def rss_mb():
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return 0.0
