import gc
import logging
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from piper.voice import PiperVoice

from .util import rss_mb

_LOGGER = logging.getLogger(__name__)

_QUALITY_ORDER = {"x_low": 0, "low": 1, "medium": 2, "high": 3}


@dataclass(frozen=True)
class VoiceInfo:
    locale: str
    lang: str
    name: str
    quality: str
    voice_id: str
    model_path: Path
    config_path: Path

    @property
    def quality_rank(self):
        return _QUALITY_ORDER.get(self.quality, -1)

    def as_dict(self):
        return {
            "locale": self.locale,
            "lang": self.lang,
            "name": self.name,
            "quality": self.quality,
            "id": self.voice_id,
            "model": str(self.model_path),
        }


def _parse_stem(stem, model_path, config_path):
    parts = stem.split("-")
    locale = parts[0]
    if "_" not in locale:
        return None
    lang = locale.split("_")[0].lower()
    quality = parts[-1].lower() if len(parts) > 1 else ""
    if quality in _QUALITY_ORDER:
        name_parts = parts[1:-1]
    else:
        quality = ""
        name_parts = parts[1:]
    name = "-".join(name_parts) if name_parts else locale
    return VoiceInfo(
        locale=locale,
        lang=lang,
        name=name,
        quality=quality,
        voice_id=stem,
        model_path=model_path,
        config_path=config_path,
    )


def discover_voices(path):
    voices = []
    for model_path in sorted(path.glob("*.onnx")):
        config_path = model_path.with_suffix(".onnx.json")
        if not config_path.is_file():
            _LOGGER.warning("Missing configuration file for %s", model_path.name)
            continue
        info = _parse_stem(model_path.name[: -len(".onnx")], model_path, config_path)
        if info is None:
            _LOGGER.warning("Unrecognized voice name: %s", model_path.name)
            continue
        voices.append(info)
    return voices


def _best(matches):
    matches.sort(key=lambda v: (v.quality_rank, v.locale), reverse=True)
    return matches[0]


def select_voice(voices, lang=None, voice=None):
    if voice:
        wanted = voice.lower()
        exact = [v for v in voices if v.name.lower() == wanted]
        if exact:
            return _best(exact)
        suffix = [v for v in voices if v.name.lower().endswith("_" + wanted)]
        if suffix and len({v.name.lower() for v in suffix}) == 1:
            return _best(suffix)
        return None
    if lang:
        wanted = lang.lower()
        matches = [v for v in voices if v.lang == wanted or v.locale.lower() == wanted]
        if not matches:
            return None
        return _best(matches)
    if not voices:
        return None
    return _best(list(voices))


def load_voice(info):
    _LOGGER.info("Loading voice %s (%s)", info.locale, info.model_path.name)
    voice = PiperVoice.load(
        str(info.model_path),
        config_path=str(info.config_path),
        include_alignments=False,
    )
    _LOGGER.info("Voice %s loaded (RSS %.0f MB)", info.locale, rss_mb())
    return voice


def _trim_memory():
    gc.collect()
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


class VoiceCache:
    def __init__(self, max_languages=2):
        self._lock = Lock()
        self._max = max(1, int(max_languages))
        self._voices = OrderedDict()

    def get(self, key):
        with self._lock:
            voice = self._voices.get(key)
            if voice is not None:
                self._voices.move_to_end(key)
            return voice

    def put(self, key, voice):
        dropped = None
        with self._lock:
            if key in self._voices:
                self._voices.move_to_end(key)
                return
            self._voices[key] = voice
            while len(self._voices) > self._max:
                old_key, old_voice = self._voices.popitem(last=False)
                _LOGGER.info("Unloading voice %s (LRU, max %d voices)", old_key, self._max)
                del old_voice
                dropped = old_key
        _trim_memory()
        if dropped is not None:
            _LOGGER.info("Voice %s unloaded (RSS %.0f MB)", dropped, rss_mb())

    def loaded(self):
        with self._lock:
            return list(self._voices.keys())
