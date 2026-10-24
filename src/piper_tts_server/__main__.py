import argparse
from pathlib import Path

from . import __version__
from . import i18n
from . import util
from .server import TTSServer, run

_ = i18n.gettext


def build_parser():
    parser = argparse.ArgumentParser(
        prog="piper-tts-server",
        description=_("Piper TTS server over a Unix socket with LRU cache and PCM streaming"),
    )
    parser.add_argument(
        "--socket",
        default=None,
        help=_("Unix socket path (default $PIPER_SOCKET or /run/piper.sock)"),
    )
    parser.add_argument(
        "--voices-dir",
        default=None,
        help=_("Voices directory (default $PIPER_VOICES_DIR or /usr/share/piper-voices)"),
    )
    parser.add_argument(
        "--idle-seconds",
        type=float,
        default=None,
        help=_("Idle seconds before exiting (default $PIPER_IDLE_SECONDS)"),
    )
    parser.add_argument(
        "--max-languages",
        type=int,
        default=None,
        help=_("Maximum voices in LRU cache (default $PIPER_MAX_LANGUAGES)"),
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv=None):
    i18n.setup()
    args = build_parser().parse_args(argv)
    util.setup_logging()
    app = TTSServer(
        voices_dir=Path(args.voices_dir).expanduser() if args.voices_dir else util.voices_dir(),
        idle_seconds=args.idle_seconds if args.idle_seconds is not None else util.idle_seconds(),
        max_languages=args.max_languages if args.max_languages is not None else util.max_languages(),
        sentence_silence=util.sentence_silence(),
        default_lang=util.default_lang(),
    )
    socket_path = Path(args.socket).expanduser() if args.socket else util.socket_path()
    return run(app, socket_path)


if __name__ == "__main__":
    raise SystemExit(main())
