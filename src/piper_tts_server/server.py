import io
import json
import logging
import os
import signal
import socket
import socketserver
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from piper.config import SynthesisConfig

from . import __version__
from .util import rss_mb
from .voices import VoiceCache, discover_voices, load_voice, select_voice

_LOGGER = logging.getLogger(__name__)


class TTSServer:
    def __init__(
        self, voices_dir, idle_seconds, max_languages, sentence_silence, default_lang, default_rate
    ):
        self.voices_dir = Path(voices_dir)
        self.idle_seconds = idle_seconds
        self.sentence_silence = sentence_silence
        self.default_lang = default_lang
        self.default_rate = default_rate
        self.voices = discover_voices(self.voices_dir)
        self.cache = VoiceCache(max_languages)
        self.synth_lock = threading.Lock()
        self.load_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active = 0
        self._last_activity = time.monotonic()
        self.started = time.monotonic()
        _LOGGER.info(
            "Voices available in %s: %s (RSS %.0f MB, default rate %s)",
            self.voices_dir,
            ", ".join(v.locale for v in self.voices) or "none",
            rss_mb(),
            self.default_rate,
        )

    def request_started(self):
        with self._state_lock:
            self._active += 1
            self._last_activity = time.monotonic()

    def request_finished(self):
        with self._state_lock:
            self._active = max(0, self._active - 1)
            self._last_activity = time.monotonic()

    def touch(self):
        with self._state_lock:
            self._last_activity = time.monotonic()

    def idle_for(self):
        with self._state_lock:
            return time.monotonic() - self._last_activity, self._active

    def resolve(self, lang, voice=None):
        info = None
        if voice:
            info = select_voice(self.voices, voice=voice)
        if info is None:
            info = select_voice(self.voices, lang=lang or self.default_lang)
        if info is None:
            return None, None
        key = info.voice_id
        voice_obj = self.cache.get(key)
        if voice_obj is None:
            with self.load_lock:
                voice_obj = self.cache.get(key)
                if voice_obj is None:
                    voice_obj = load_voice(info)
                    self.cache.put(key, voice_obj)
        return voice_obj, key


def _length_scale(rate):
    rate = max(-100.0, min(100.0, float(rate)))
    if rate >= 0:
        return 1.0 / (1.0 + rate / 100.0)
    return 1.0 + (-rate) / 100.0


def _volume(volume):
    return max(0.0, min(2.0, 1.0 + float(volume) / 100.0))


def _silence_bytes(sample_rate, seconds, sample_width=2):
    frames = int(round(max(0.0, seconds) * sample_rate))
    return b"\x00" * (frames * sample_width)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = f"piper-tts-server/{__version__}"
    timeout = 300

    def log_message(self, fmt, *args):
        _LOGGER.debug("%s", fmt % args)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        app = self.server.app
        app.request_started()
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._json(
                    200,
                    {
                        "status": "ok",
                        "version": __version__,
                        "uptime_s": round(time.monotonic() - app.started, 1),
                    },
                )
            elif parsed.path == "/voices":
                self._json(
                    200,
                    {
                        "available": [v.as_dict() for v in app.voices],
                        "loaded": app.cache.loaded(),
                    },
                )
            elif parsed.path == "/say":
                self._say(method, parse_qs(parsed.query))
            else:
                self._json(404, {"error": "not_found"})
        except (BrokenPipeError, ConnectionResetError):
            _LOGGER.debug("Client disconnected during response")
        except Exception:
            _LOGGER.exception("Error handling request")
            try:
                self._json(500, {"error": "internal_error"})
            except Exception:
                pass
        finally:
            app.request_finished()

    def _say(self, method, params):
        app = self.server.app
        text = params.get("text", [""])[0]
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length > 0:
                text = self.rfile.read(length).decode("utf-8", errors="replace")
        text = text.strip()
        if not text:
            self._json(400, {"error": "empty_text"})
            return
        lang = params.get("lang", [None])[0] or self.headers.get("X-Lang")
        voice_name = params.get("voice", [None])[0] or self.headers.get("X-Voice")
        rate = params.get("rate", [None])[0]
        if rate is None or rate == "":
            rate = app.default_rate
        volume = params.get("volume", ["0"])[0]
        fmt = params.get("format", ["raw"])[0].lower()
        voice, key = app.resolve(lang, voice_name)
        if voice is None:
            self._json(
                404,
                {
                    "error": "unknown_voice" if voice_name else "unknown_language",
                    "lang": lang or "",
                    "voice": voice_name or "",
                    "available": [v.locale for v in app.voices],
                },
            )
            return
        syn_config = SynthesisConfig(
            length_scale=_length_scale(rate),
            volume=_volume(volume),
        )
        if fmt == "wav":
            self._send_wav(voice, text, syn_config, key)
        else:
            self._send_raw(voice, text, syn_config, key)

    def _send_raw(self, voice, text, syn_config, key):
        app = self.server.app
        sample_rate = voice.config.sample_rate
        self.send_response(200)
        self.send_header("Content-Type", f"audio/L16; rate={sample_rate}; channels=1")
        self.send_header("X-Sample-Rate", str(sample_rate))
        self.send_header("X-Bits", "16")
        self.send_header("X-Channels", "1")
        self.send_header("X-Voice", key)
        self.send_header("Connection", "close")
        self.end_headers()
        silence = _silence_bytes(sample_rate, app.sentence_silence)
        started = time.monotonic()
        total = 0
        first = True
        iterator = voice.synthesize(text, syn_config=syn_config)
        while True:
            with app.synth_lock:
                try:
                    chunk = next(iterator)
                except StopIteration:
                    break
            if not first and silence:
                self.wfile.write(silence)
                total += len(silence)
            data = chunk.audio_int16_bytes
            self.wfile.write(data)
            self.wfile.flush()
            total += len(data)
            first = False
            app.touch()
        _LOGGER.info(
            "say voice=%s chars=%d bytes=%d ms=%d (RSS %.0f MB)",
            key,
            len(text),
            total,
            int((time.monotonic() - started) * 1000),
            rss_mb(),
        )

    def _send_wav(self, voice, text, syn_config, key):
        app = self.server.app
        started = time.monotonic()
        chunks = []
        iterator = voice.synthesize(text, syn_config=syn_config)
        while True:
            with app.synth_lock:
                try:
                    chunk = next(iterator)
                except StopIteration:
                    break
            chunks.append(chunk)
        sample_rate = voice.config.sample_rate
        silence = _silence_bytes(sample_rate, app.sentence_silence)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            for index, chunk in enumerate(chunks):
                if index and silence:
                    wav_file.writeframes(silence)
                wav_file.writeframes(chunk.audio_int16_bytes)
        data = buffer.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Voice", key)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()
        _LOGGER.info(
            "say(wav) voice=%s chars=%d bytes=%d ms=%d",
            key,
            len(text),
            len(data),
            int((time.monotonic() - started) * 1000),
        )

    def _json(self, code, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()


class _UnixHTTPServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, app, socket_path):
        self.app = app
        super().__init__(str(socket_path), _Handler)
        os.chmod(socket_path, 0o600)


class _ActivatedHTTPServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, app, sock):
        socketserver.BaseServer.__init__(self, sock.getsockname(), _Handler)
        self.app = app
        self.socket = sock
        self.server_activate()


class _IdleWatchdog(threading.Thread):
    def __init__(self, app, httpd):
        super().__init__(name="idle-watchdog", daemon=True)
        self.app = app
        self.httpd = httpd

    def run(self):
        interval = min(30.0, max(1.0, self.app.idle_seconds / 4.0))
        while True:
            time.sleep(interval)
            idle, active = self.app.idle_for()
            if active == 0 and idle >= self.app.idle_seconds:
                _LOGGER.info(
                    "Idle for %.0f s (limit %.0f s), exiting (RSS %.0f MB)",
                    idle,
                    self.app.idle_seconds,
                    rss_mb(),
                )
                self.httpd.shutdown()
                return


def _activated_socket():
    if os.environ.get("LISTEN_FDS") != "1":
        return None
    pid = os.environ.get("LISTEN_PID")
    if pid and pid != str(os.getpid()):
        return None
    names = os.environ.get("LISTEN_FDNAMES", "")
    if names and names != "piper-tts.socket":
        return None
    return socket.fromfd(3, socket.AF_UNIX, socket.SOCK_STREAM)


def run(app, socket_path=None):
    activated = _activated_socket()
    if activated is not None:
        httpd = _ActivatedHTTPServer(app, activated)
        _LOGGER.info("Started via socket activation (systemd)")
    else:
        path = Path(socket_path) if socket_path else None
        if path is None:
            raise ValueError("Missing socket path")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        httpd = _UnixHTTPServer(app, path)
        _LOGGER.info("Listening on %s", path)
    _IdleWatchdog(app, httpd).start()
    stopping = threading.Event()

    def _shutdown(signum, frame):
        if stopping.is_set():
            return
        stopping.set()
        _LOGGER.info("Signal %s received, stopping", signum)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
        if activated is None and socket_path:
            try:
                Path(socket_path).unlink()
            except OSError:
                pass
    return 0
