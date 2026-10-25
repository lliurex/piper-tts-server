import argparse
import ctypes
import math
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

from . import __version__
from . import i18n
from . import util

_ = i18n.gettext

_RATE_PRESETS = {"slow": -40.0, "normal": -20.0, "fast": 0.0}

_ESPEAK_LIBS = ("libespeak-ng.so.1", "libespeak-ng.so")
_ESPEAK_OUTPUT_SYNCHRONOUS = 2
_ESPEAK_CHARS_UTF8 = 1
_ESPEAK_POS_CHARACTER = 1

_SYNTH_CALLBACK = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p
)


class _EspeakVoice(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("languages", ctypes.c_char_p),
        ("identifier", ctypes.c_char_p),
        ("gender", ctypes.c_ubyte),
        ("age", ctypes.c_ubyte),
        ("variant", ctypes.c_ubyte),
        ("xx1", ctypes.c_ubyte),
        ("score", ctypes.c_int),
        ("spare", ctypes.c_void_p),
    ]


class ServerUnavailable(Exception):
    pass


def _request(path, target, body, timeout):
    payload = body.encode("utf-8")
    request = (
        f"POST {target} HTTP/1.0\r\n"
        "Host: localhost\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
    except OSError as exc:
        sock.close()
        raise ServerUnavailable(_("could not connect to %s: %s") % (path, exc)) from exc
    try:
        sock.sendall(request + payload)
        reader = sock.makefile("rb")
        status_line = reader.readline().decode("latin-1").strip()
        if not status_line:
            raise ServerUnavailable(_("empty response from server"))
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise ServerUnavailable(_("invalid response: %r") % status_line)
        status = int(parts[1])
        headers = {}
        while True:
            line = reader.readline()
            if line in (b"", b"\r\n", b"\n"):
                break
            name, _sep, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        return sock, reader, status, headers
    except Exception:
        sock.close()
        raise


def _play(sock, reader, headers, to_stdout):
    sample_rate = headers.get("x-sample-rate", "22050")
    proc = None
    if to_stdout:
        out = sys.stdout.buffer
    else:
        paplay = shutil.which("paplay")
        if paplay is None:
            reader.close()
            sock.close()
            raise ServerUnavailable(_("paplay not found in PATH"))
        try:
            proc = subprocess.Popen(
                [
                    paplay,
                    "--raw",
                    f"--rate={sample_rate}",
                    "--format=s16le",
                    "--channels=1",
                    "--client-name=piper-tts",
                ],
                stdin=subprocess.PIPE,
            )
        except OSError as exc:
            reader.close()
            sock.close()
            raise ServerUnavailable(_("could not execute paplay: %s") % exc) from exc
        out = proc.stdin

    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)

    def _terminate(signum, frame):
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    try:
        while True:
            try:
                data = reader.read(4096)
            except (socket.timeout, TimeoutError):
                print(_("piper-tts-client: timed out reading audio"), file=sys.stderr)
                break
            except (ConnectionResetError, OSError):
                break
            if not data:
                break
            try:
                out.write(data)
            except BrokenPipeError:
                break
        try:
            out.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        if proc is not None:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
            proc.wait()
        try:
            reader.close()
        except OSError:
            pass
        sock.close()
    return 0


def _load_espeak():
    for name in _ESPEAK_LIBS:
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


def _espeak_stream(text, lang, writer, on_rate):
    lib = _load_espeak()
    if lib is None:
        return False
    lib.espeak_Initialize.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    lib.espeak_Initialize.restype = ctypes.c_int
    lib.espeak_SetSynthCallback.argtypes = [_SYNTH_CALLBACK]
    lib.espeak_SetSynthCallback.restype = None
    lib.espeak_SetVoiceByName.argtypes = [ctypes.c_char_p]
    lib.espeak_SetVoiceByName.restype = ctypes.c_int
    lib.espeak_SetVoiceByProperties.argtypes = [ctypes.POINTER(_EspeakVoice)]
    lib.espeak_SetVoiceByProperties.restype = ctypes.c_int
    lib.espeak_Synth.argtypes = [
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    lib.espeak_Synth.restype = ctypes.c_int
    lib.espeak_Synchronize.argtypes = []
    lib.espeak_Synchronize.restype = ctypes.c_int
    sample_rate = lib.espeak_Initialize(_ESPEAK_OUTPUT_SYNCHRONOUS, 0, None, 0)
    if sample_rate <= 0:
        return False
    broken = [False]

    def _callback(wav, numsamples, events):
        if numsamples > 0:
            try:
                writer(ctypes.string_at(ctypes.cast(wav, ctypes.c_void_p), numsamples * 2))
            except (BrokenPipeError, OSError, ValueError):
                broken[0] = True
                return 1
        return 0

    callback = _SYNTH_CALLBACK(_callback)
    lib.espeak_SetSynthCallback(callback)
    if lang:
        if lib.espeak_SetVoiceByName(lang.encode("utf-8")) != 0:
            voice = _EspeakVoice()
            voice.languages = lang.encode("utf-8")
            lib.espeak_SetVoiceByProperties(ctypes.byref(voice))
    on_rate(sample_rate)
    data = text.encode("utf-8") + b"\x00"
    if lib.espeak_Synth(data, len(data), 0, _ESPEAK_POS_CHARACTER, 0, _ESPEAK_CHARS_UTF8, None, None) != 0:
        return False
    lib.espeak_Synchronize()
    return not broken[0]


def _fallback_stdout(text, lang):
    result = _espeak_stream(text, lang, sys.stdout.buffer.write, lambda rate: None)
    if not result:
        print(_("piper-tts-client: espeak-ng fallback not available"), file=sys.stderr)
        return 1
    return 0


def _fallback_speak(text, lang):
    paplay = shutil.which("paplay")
    if paplay is not None:
        proc_holder = {}

        def on_rate(rate):
            try:
                proc_holder["proc"] = subprocess.Popen(
                    [
                        paplay,
                        "--raw",
                        f"--rate={rate}",
                        "--format=s16le",
                        "--channels=1",
                        "--client-name=piper-tts-espeak",
                    ],
                    stdin=subprocess.PIPE,
                )
            except OSError:
                proc_holder["proc"] = None

        def writer(data):
            proc = proc_holder.get("proc")
            if proc is None:
                raise BrokenPipeError
            proc.stdin.write(data)

        if _espeak_stream(text, lang, writer, on_rate):
            proc = proc_holder.get("proc")
            if proc is None:
                return 1
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
            return proc.wait()
    spd_say = shutil.which("spd-say")
    if spd_say is None:
        print(_("piper-tts-client: server and espeak-ng fallback not available"), file=sys.stderr)
        return 1
    command = [spd_say, "-o", "espeak-ng", "-w"]
    if lang:
        command += ["-l", lang]
    command.append(text)
    try:
        return subprocess.run(command).returncode
    except OSError as exc:
        print(_("piper-tts-client: espeak-ng fallback failed: %s") % exc, file=sys.stderr)
        return 1


def _fallback(text, lang, to_stdout):
    if to_stdout:
        return _fallback_stdout(text, lang)
    return _fallback_speak(text, lang)


def _parse_rate(value):
    token = value.strip().lower()
    if token in _RATE_PRESETS:
        return (_RATE_PRESETS[token], True)
    try:
        rate = float(token)
    except ValueError:
        raise argparse.ArgumentTypeError(_("invalid speed: %s") % value)
    if not math.isfinite(rate) or rate < -100.0 or rate > 100.0:
        raise argparse.ArgumentTypeError(_("speed must be between -100 and +100"))
    return (rate, rate != 0)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="piper-tts-client",
        description=_("Client for piper-tts-server (speech-dispatcher or direct use)"),
    )
    parser.add_argument(
        "text", nargs="*", help=_("Text to synthesize (reads from stdin if omitted)")
    )
    parser.add_argument("--lang", default=None, help=_("Language code (ca, es, ...)"))
    parser.add_argument(
        "--voice",
        default=None,
        help=_("Voice name (thorsten, lessac, upc_pau, ...; takes precedence over --lang)"),
    )
    parser.add_argument(
        "-r",
        "--rate",
        type=_parse_rate,
        default=None,
        metavar="RATE",
        help=_(
            "Speed: slow (-40), normal (-20), fast (0), or a value from -100 to +100"
        ),
    )
    parser.add_argument(
        "--volume",
        type=float,
        default=0.0,
        help=_("Volume between -100 and +100 (0 = normal)"),
    )
    parser.add_argument("--socket", default=None, help=_("Unix socket path"))
    parser.add_argument(
        "--stdout",
        action="store_true",
        help=_("Write raw s16le PCM to stdout (no paplay)"),
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help=_("Do not use espeak-ng if the server does not respond"),
    )
    parser.add_argument(
        "--timeout", type=float, default=120.0, help=_("Socket timeout in seconds")
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv=None):
    i18n.setup()
    args = build_parser().parse_args(argv)
    text = " ".join(args.text).strip() if args.text else sys.stdin.read().strip()
    if not text:
        return 0
    lang = args.lang or util.default_lang()
    path = Path(args.socket).expanduser() if args.socket else util.socket_path()
    params = []
    if args.voice:
        params.append(("voice", args.voice))
    if lang:
        params.append(("lang", lang))
    if args.rate is not None:
        rate, send_rate = args.rate
        if send_rate:
            params.append(("rate", rate))
    if args.volume:
        params.append(("volume", args.volume))
    target = "/say"
    if params:
        target += "?" + urlencode(params)
    try:
        sock, reader, status, headers = _request(path, target, text, args.timeout)
    except ServerUnavailable as exc:
        if args.no_fallback:
            print(_("piper-tts-client: %s") % exc, file=sys.stderr)
            return 1
        print(
            _("piper-tts-client: server unavailable, falling back to espeak-ng: %s") % exc,
            file=sys.stderr,
        )
        return _fallback(text, lang, args.stdout)
    if status != 200:
        try:
            detail = reader.read().decode("utf-8", errors="replace").strip()
        except OSError:
            detail = ""
        try:
            reader.close()
        except OSError:
            pass
        sock.close()
        if args.no_fallback:
            print(
                _("piper-tts-client: server HTTP %s: %s") % (status, detail[:300]),
                file=sys.stderr,
            )
            return 1
        if status in (404, 500, 503):
            print(
                _("piper-tts-client: falling back to espeak-ng (server HTTP %s: %s)")
                % (status, detail[:200]),
                file=sys.stderr,
            )
            return _fallback(text, lang, args.stdout)
        print(
            _("piper-tts-client: server HTTP %s: %s") % (status, detail[:300]),
            file=sys.stderr,
        )
        return 1
    try:
        return _play(sock, reader, headers, args.stdout)
    except ServerUnavailable as exc:
        print(_("piper-tts-client: %s") % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
