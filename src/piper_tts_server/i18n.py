import gettext as _gettext
import os


DOMAIN = "piper-tts-server"
DEFAULT_LOCALEDIR = "/usr/share/locale"

_translation = _gettext.NullTranslations()


def setup():
    global _translation
    localedir = os.environ.get("PIPER_LOCALEDIR", DEFAULT_LOCALEDIR)
    _translation = _gettext.translation(DOMAIN, localedir=localedir, fallback=True)


def gettext(message):
    return _translation.gettext(message)


def ngettext(singular, plural, number):
    return _translation.ngettext(singular, plural, number)
