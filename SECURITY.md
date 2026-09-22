# Security Policy

## Reporting a vulnerability

Please do not open a public issue for security vulnerabilities.

Report privately via GitHub Security Advisories:
<https://github.com/0xzerolight/anki_miner_game/security/advisories/new>

Anki Miner Game is maintained by a single person on a best-effort basis. You can expect an acknowledgement within a reasonable time.

## Scope

In scope:

- Handling of the OBS websocket password, which the app reads from OBS's own settings and never logs.
- The text feed's page and websocket servers, which listen on this machine only.
- The websocket clients that connect to text hookers.
- Add-on downloads and the environments they install into (VAD, OCR).
- Bundled installers (PyInstaller, AppImage, `.deb`, `.tar.gz`, Inno Setup).

Out of scope:

- Vulnerabilities in third-party software (OBS, owocr, text hookers). owocr's websocket server listening on `0.0.0.0` is its own behaviour and is documented in the README.
- Issues requiring local filesystem write access already granted to the user.

## Supported versions

The latest release on GitHub is supported. Older versions may receive critical patches at maintainer discretion.
