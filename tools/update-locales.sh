#!/bin/bash
# Regenera po/*.po y los .mo estáticos de Piper.
# Se ejecuta antes de commitear; los .po y locale/*.mo se versionan.
set -euo pipefail

cd "$(dirname "$0")/.."

DOMAIN_SERVER="piper-tts-server"
DOMAIN_ZERO="zero-lliurex-piper-tts-server"
POT="$(mktemp)"
trap 'rm -f "$POT"' EXIT

# Cadenas de Python.
xgettext \
    --language=Python \
    --keyword=_ \
    --from-code=UTF-8 \
    --force-po \
    --output="$POT" \
    src/piper_tts_server/*.py

# Cadenas de los scripts de shell (gettext directo y _()).
xgettext \
    --language=Shell \
    --keyword=_ \
    --keyword=gettext \
    --from-code=UTF-8 \
    --join-existing \
    --output="$POT" \
    tools/piper-tts-test \
    zero-lliurex-piper-tts-server.install-files/usr/share/zero-lliurex-piper-tts-server/piper-tts-server_script

for lang in $(sed -e 's/#.*//' -e '/^$/d' po/LINGUAS); do
    msgmerge --update --no-fuzzy-matching "po/$lang.po" "$POT"
    mkdir -p "locale/$DOMAIN_SERVER/$lang/LC_MESSAGES"
    mkdir -p "locale/$DOMAIN_ZERO/$lang/LC_MESSAGES"
    msgfmt --check -o "locale/$DOMAIN_SERVER/$lang/LC_MESSAGES/$DOMAIN_SERVER.mo" "po/$lang.po"
    msgfmt --check -o "locale/$DOMAIN_ZERO/$lang/LC_MESSAGES/$DOMAIN_ZERO.mo" "po/$lang.po"
done

echo "Updated po/ and generated locale/."
