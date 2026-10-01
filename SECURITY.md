# Security Policy

## Reporting a vulnerability

Please do not open a public issue for security vulnerabilities.

Report privately via GitHub Security Advisories:
<https://github.com/0xzerolight/anki_miner_game/security/advisories/new>

Anki Miner Game is maintained by a single person on a best-effort basis. You can expect an acknowledgement within a reasonable time.

## Scope

In scope:

- Handling of the OBS WebSocket password, which the app reads from OBS's own settings and never logs.
- The text feed's page and WebSocket servers, which listen on this machine only. The WebSocket accepts apps and scripts that send no Origin, pages from `http://127.0.0.1` or `http://localhost`, and the hosted texthooker-ui (`https://renji-xd.github.io`); any other web page is refused.
- The WebSocket clients that connect to text hookers.
- Add-on downloads and the environments they install into (VAD, OCR).
- Bundled installers (PyInstaller, AppImage, `.deb`, `.tar.gz`, Inno Setup).

Out of scope:

- Vulnerabilities in third-party software (OBS, owocr, text hookers). owocr's websocket server listening on `0.0.0.0` is its own behaviour and is documented in the README.
- Issues requiring local filesystem write access already granted to the user.

## Supported versions

The latest release on GitHub is supported. Older versions may receive critical patches at maintainer discretion.
