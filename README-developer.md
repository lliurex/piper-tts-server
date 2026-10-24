# piper-tts-server developer guide

Development, packaging, and operational notes for `piper-tts-server`. For the
user-facing introduction and usage examples, see [README.md](README.md).

## Requirements

- Python >= 3.9 and the Debian package `piper-tts >= 1.8.0`, which provides
  the `piper` library, the `/usr/bin/piper` command and its dependencies
  (`python3-onnxruntime`, `python3-numpy` and `python3-pathvalidate`).
- `gettext-base` for translated messages in `piper-tts-test`.
- `paplay`, provided by `pulseaudio-utils`, for playback. A running PulseAudio
  or PipeWire sound server is still required.
- speech-dispatcher with the `sd_generic` module.
- `speech-dispatcher-espeak-ng` for the espeak-ng fallback.
- systemd for the socket/service units and `journalctl`.

## Installation

### Debian package

Build dependencies: `debhelper-compat`, `dh-sequence-python3`,
`pybuild-plugin-pyproject`, `python3-all`, `python3-setuptools`, `gettext`.

```bash
gbp buildpackage -us -uc
sudo apt install ../build-area/piper-tts-server_<version>_all.deb
```

The package installs:

- `/usr/bin/piper-tts-{server,client,test}`
- **system** units `/usr/lib/systemd/system/piper-tts.{socket,service}`
  (socket at `/run/piper.sock`, mode `0666`, socket activation)
- the conffile `/etc/speech-dispatcher/modules/piper_sd.conf`
- translations in `/usr/share/locale/{es,ca}/LC_MESSAGES/piper-tts-server.mo`

The package depends on `piper-tts`, `speech-dispatcher`,
`speech-dispatcher-espeak-ng`, `pulseaudio-utils`, `systemd` and
`gettext-base`. The postinst adds the piper module to
`/etc/speech-dispatcher/speechd.conf`, leaving a `.bkp` backup.
`dh_installsystemd` enables and starts the socket; the service is activated by
the socket on the first client.

On removal or purge, the `.bkp` files are restored and the configuration
returns to its previous state. Full package documentation is available at
`/usr/share/doc/piper-tts-server/README.Debian`.

## Package layout

- `src/piper_tts_server/`: Python server, client, voice discovery and i18n.
- `tools/piper-tts-test`: installed test command.
- `po/`: gettext sources for Spanish (`es`) and Valencian (`ca`).
- `debian/`: native Debian packaging, units, speech-dispatcher module and
  maintainer scripts.
- `debian/README.Debian`: documentation installed with the package.

## Development workflow

Changes to code and general documentation go through `master` and are merged
into the distribution branches:

- `debian/jammy`: Ubuntu jammy.
- `debian/noble`: Ubuntu noble.

Changes that only affect `debian/` are made directly on the corresponding
distribution branch.

Typical flow:

```bash
git checkout master
# edit code, tools, po/ or README files
git commit

git checkout debian/jammy
git merge master
# edit debian/ if needed
git commit

git checkout debian/noble
git merge master
# edit debian/ if needed
git commit
```

## Building and testing

```bash
gbp buildpackage -us -uc
lintian ../build-area/piper-tts-server_<version>_all.deb
```

Runtime checks:

```bash
piper-tts-test
piper-tts-test -v thorsten
piper-tts-test -d
piper-tts-test -d -v thorsten
```

For a source-tree smoke test without installing:

```bash
PYTHONPATH=src python3 -m piper_tts_server --help
PYTHONPATH=src python3 -m piper_tts_server.client --help
```

## Translations

Source messages are in English. `piper-tts-test`, the client and the server
command-line help use gettext. Catalogs live in `po/` and are compiled during
the package build with `msgfmt --check --check-format`.

To test catalogs from a staging directory without installing them:

```bash
msgfmt -o /tmp/locale/es/LC_MESSAGES/piper-tts-server.mo po/es.po
PIPER_LOCALEDIR=/tmp/locale LANG=es_ES.UTF-8 piper-tts-test --help
```

`PIPER_LOCALEDIR` is a development-only override for the catalog directory.
The service journal remains in English.

## systemd service

```bash
systemctl status piper-tts.socket piper-tts.service
journalctl -u piper-tts.service -e
systemctl restart piper-tts.service
```

Use a drop-in to override environment variables:

```bash
sudo systemctl edit piper-tts.service
```

```ini
[Service]
Environment=PIPER_MAX_LANGUAGES=3
Environment=PIPER_IDLE_SECONDS=1800
```

## Why not use Piper's HTTP server?

Upstream Piper includes a Flask-based HTTP server:

```bash
python3 -m piper.http_server -m <voice>
```

It provides a browser UI, `/info`, `/voices`, `/synthesize` and voice
downloads over TCP (port 5000 by default). `piper-tts-server` targets a
different deployment.

| Need | Piper HTTP server | piper-tts-server |
|---|---|---|
| Browser UI | Yes | No |
| HTTP over TCP | Yes | No |
| Unix socket | No | Yes |
| speech-dispatcher integration | No | Yes |
| Raw PCM streaming | No; returns WAV | Yes |
| LRU voice cache | No; loaded voices accumulate | Yes, configurable |
| Idle shutdown | No | Yes |
| Socket activation | No | Yes |
| `DynamicUser` / `NoNewPrivileges` | No | Yes |
| Voice aliases and language resolution | Limited | Yes |
| espeak-ng fallback | No | Yes |
| Flask dependency | Yes | No |

Use Piper's HTTP server for browser access, remote HTTP clients, or voice
downloads. Use `piper-tts-server` for local low-latency playback,
speech-dispatcher, and an on-demand system service.

## Troubleshooting

- **No sound**: check `curl --unix-socket /run/piper.sock http://localhost/health`;
  if it fails, use `systemctl status piper-tts.socket piper-tts.service` and
  `journalctl -u piper-tts.service -e`.
- **`spd-say -y` does not change the voice**: the speech-dispatcher daemon
  keeps the module configuration in memory; restart it so it rereads
  `piper_sd.conf`. Note: `pkill -x speech-dispatcher` does not work (the
  process name is truncated to 15 characters, `speech-dispatche`); use
  `pkill -f '[s]peech-dispatcher'` or kill the PID. It is reactivated by the
  socket on the next request.
- **spd-say uses espeak-ng**: verify that `AddModule "piper" ...` and
  `DefaultModule piper` are present in
  `/etc/speech-dispatcher/speechd.conf` and restart the daemon. A
  `speechd.conf` in the user's home takes precedence and is outside the
  package.
- **The piper module does not initialize**: this version of `sd_generic`
  rejects `GenericPunct*` with an empty value (`""`); do not include those
  lines. An `sd_generic` module needs at least one `AddVoice` line
  (`DefaultVoice` is optional).
- **The espeak-ng voice is heard**: the requested language or voice is not in
  `PIPER_VOICES_DIR`; the client fell back and reports it on stderr. Check
  `GET /voices` or install the missing voice.
- **Memory usage**: each voice takes approximately the model size + 40-60 MB.
  Adjust `PIPER_MAX_LANGUAGES` if you need a larger or smaller cache.

## Uninstallation

```bash
sudo dpkg -r piper-tts-server   # removes units and restores the previous configuration (.bkp)
sudo dpkg -P piper-tts-server   # purge
```

`piper-tts` is not removed when uninstalling; it is an independent system
package and may be used by other software.

## License

The code in this repository is licensed under GPL-3.0-or-later:
https://www.gnu.org/licenses/gpl-3.0.html

The voice models installed by the piper-tts-voices-* packages are
third-party content and keep their own licenses; they are not covered by
the GPL (see the MODEL_CARD of each voice package).
