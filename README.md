# piper-tts-server

Resident Piper TTS server over a Unix socket for speech-dispatcher and direct
use.

## Why a separate server?

Piper is a local neural text-to-speech engine. Its command-line interface is
designed for one-shot synthesis: each invocation starts a process, loads the
model, generates audio, and exits. Interactive consumers such as
speech-dispatcher produce many short utterances, so that approach repeats
model loading and pays startup latency on every phrase.

Upstream Piper also includes an optional Flask-based HTTP server
(`python3 -m piper.http_server`) for browser and HTTP API use.
`piper-tts-server` is not a replacement for it: it is a local,
socket-activated service with raw PCM streaming, an LRU voice cache, idle
shutdown, systemd isolation, and an espeak-ng fallback.

Piper remains the synthesis engine. The server does not replace it.

| Task | Recommended tool |
|---|---|
| One-off file or batch synthesis | `piper` |
| Browser UI or remote HTTP API | Piper HTTP server |
| Local low-latency playback and speech-dispatcher | `piper-tts-server` |

## Features

- **Raw PCM streaming**: synthesizes and plays sentence by sentence, without
  waiting for the whole audio to be generated.
- **Voice selection**: the `voice` parameter (sent by `spd-say -y` and
  Firefox's `SET_SYNTHESIS_VOICE`, among others) takes precedence over the
  language; it accepts the model name (`upc_pau`) or a short alias (`pau`).
- **Per-voice LRU cache**: keeps the last N voices in memory (2 by default)
  and releases the rest.
- **Idle auto-shutdown**: if there are no requests for 15 minutes
  (configurable), the process exits. With the `piper-tts.socket` systemd unit,
  the next client starts it again automatically (socket activation).
- **Memory-aware logs**: every voice load/unload, synthesis, start and exit
  records the current RSS.
- **Configurable default rate**: `PIPER_DEFAULT_RATE` sets the speed used when
  a request does not include an explicit `rate`; the default is `-20`.
- **Rate presets**: the client accepts `slow` (-40), `normal` (-20), `fast`
  (0), or a numeric value from -100 to +100.
- **espeak-ng fallback**: if the server does not respond, the client
  synthesizes with `libespeak-ng` (ctypes) and plays through paplay, reporting
  the reason on stderr; if the library is missing, it uses
  `spd-say -o espeak-ng`.

## Architecture

```
speech-dispatcher (sd_generic, user unit)
      |  echo "$DATA" | piper-tts-client --voice $VOICE --lang ca --rate 0
      v
piper-tts-client  --(HTTP/1.0 over /run/piper.sock)-->  piper-tts-server
      |                                                    |
      |  s16le PCM                                         |  PiperVoice.synthesize()
      v                                                     v
   paplay  <----------------------------------------  piper-tts (piper1-gpl + onnxruntime)
      ^
      |  (if the server does not respond or the voice is missing) -> libespeak-ng (ctypes)

systemd (system): piper-tts.socket (/run/piper.sock, 0666) activates
piper-tts.service on demand as a dynamic user
```

- HTTP endpoints (Unix socket): `POST /say?lang=XX&voice=VV&rate=0&volume=0[&format=wav]`
  (text in the body), `GET /voices`, `GET /health`.
- `/say` resolution: voice > language > default language
  (`PIPER_DEFAULT_LANG`, `auto` by default = system locale). If the requested
  voice does not exist, the language is used; if that does not match either,
  it returns 404 (`unknown_voice` / `unknown_language`) with the list of
  available locales.
- If `rate` is omitted, the server uses `PIPER_DEFAULT_RATE` (default `-20`).
- By default `/say` returns PCM data with `X-Sample-Rate`, `X-Bits: 16`,
  `X-Channels: 1` and `X-Voice` (resolved voice); the client runs
  `paplay --raw` with those parameters.
- `format=wav` returns a complete WAV file (not streaming), useful with `curl`.

## Usage

```bash
piper-tts-client "Hello"                         # play through paplay
echo "Hello" | piper-tts-client                  # text from stdin
piper-tts-client --lang es --rate 20 "Hola"      # language and speed (-100..100)
piper-tts-client --rate slow "Hola"               # preset: slow (-40)
piper-tts-client -r fast "Hello"                 # preset: fast (0)
piper-tts-client --volume 50 "Hello"             # volume -100..100 (0 = normal)
piper-tts-client --voice thorsten "Hallo"        # specific voice (wins over language)
piper-tts-client --stdout "Hello" > voice.pcm    # raw s16le PCM (no paplay)
piper-tts-client --no-fallback "Hello"           # no espeak-ng fallback
piper-tts-test                                   # all voices + rate sweep
piper-tts-test -v thorsten                       # test a specific voice
piper-tts-test --rate fast                       # test all voices at fast
piper-tts-test -r slow -v thorsten               # specific voice at slow
piper-tts-test -d                                # also show the server journal
piper-tts-test -d -v thorsten                    # journal + specific voice
```

### Speech rate

`PIPER_DEFAULT_RATE` controls the speed used when neither the client nor the
application requests a specific rate. The initial value is `-20`
(approximately 20 % slower than the historical normal speed).

To change it temporarily, start the server directly with:

```bash
PIPER_DEFAULT_RATE=-30 piper-tts-server
```

For the systemd service, use a drop-in:

```bash
sudo systemctl edit piper-tts.service
```

```ini
[Service]
Environment=PIPER_DEFAULT_RATE=-30
```

Then reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart piper-tts.socket piper-tts.service
```

Explicit values still take priority:

```bash
piper-tts-client --rate fast "Hello"     # 0, normal historical speed
piper-tts-client --rate -50 "Hello"      # slower than the default
```

Via speech-dispatcher (the user daemon starts at login):

```bash
spd-say "prova de veu amb piper"
spd-say -l es "prueba en castellano"
spd-say -y lessac "Hello from Lessac"
spd-say -y pau "Hola, soc en Pau"    # alias for upc_pau
spd-say -L                           # available voices
```

Direct server use (for example with `curl`):

```bash
curl --unix-socket /run/piper.sock http://localhost/voices
curl --unix-socket /run/piper.sock -X POST \
     "http://localhost/say?lang=ca&voice=pau&format=wav" -d "Hola mon" -o voice.wav
```

## Service and configuration

```bash
systemctl status piper-tts.socket piper-tts.service
journalctl -u piper-tts.service -e          # logs (including RSS)
systemctl restart piper-tts.service         # e.g. after installing voices
```

All variables are set in the unit; use a drop-in to change them:

```bash
sudo systemctl edit piper-tts.service
```

```ini
[Service]
Environment=PIPER_MAX_LANGUAGES=3
Environment=PIPER_IDLE_SECONDS=1800
```

`PIPER_DEFAULT_LANG=auto` derives the language from the system locale
(`LANG`/`LC_*`); a specific code (`ca`, `es`, ...) can also be set.

| Variable | Default | Description |
| --- | --- | --- |
| `PIPER_SOCKET` | `/run/piper.sock` | Unix socket path |
| `PIPER_VOICES_DIR` | `/usr/share/piper-voices` | Directory with `.onnx` and `.onnx.json` files |
| `PIPER_IDLE_SECONDS` | `900` | Idle time before the server exits |
| `PIPER_MAX_LANGUAGES` | `2` | Maximum voices in the LRU cache |
| `PIPER_SENTENCE_SILENCE` | `0.2` | Silence between sentences, in seconds |
| `PIPER_DEFAULT_LANG` | `auto` (locale) | Language when the client does not specify one |
| `PIPER_LOG_LEVEL` | `INFO` | Log level (`DEBUG`, `INFO`, ...) |

## Voices, languages and names

Voices are discovered from the file name:
`ca_ES-upc_pau-x_low.onnx` -> locale `ca_ES`, language `ca`, name `upc_pau`,
quality `x_low`. If several qualities exist for a language, the highest one is
chosen (`high` > `medium` > `low` > `x_low`).

The `voice` parameter accepts the model name (`upc_pau`, `thorsten`, ...) or a
unique suffix alias (`pau` -> `upc_pau`), case-insensitively. The names
published by `spd-say -L` are those from `AddVoice` in `piper_sd.conf`
(kareem, pau, thorsten, lessac, sharvard, siwis, paola, denis) and match the
installed models. `GET /voices` exposes the `name` and `id` of each voice.

## Messages and translations

Source messages are in English. The client, `piper-tts-test` and the server
command-line help use gettext; Spanish (`es`) and Valencian (`ca`) catalogs are
available. The service journal remains in English so it is stable for
debugging.

```bash
LANG=es_ES.UTF-8 piper-tts-test --help
LANG=ca_ES@valencia piper-tts-test --help
LANG=C piper-tts-test --help
```

`piper-tts-test -d` follows the `piper-tts.service` journal and prints
timestamped journal lines; without `-d` no journal output is shown.

## License

The code in this repository is licensed under GPL-3.0-or-later:
https://www.gnu.org/licenses/gpl-3.0.html

The voice models installed by the piper-tts-voices-* packages are
third-party content and keep their own licenses; they are not covered by
the GPL (see the MODEL_CARD of each voice package).

## Development

Development, packaging, testing and operational instructions are in
[README-developer.md](README-developer.md).
