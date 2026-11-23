# Update Feed and Release Process

Desktop Icon Billiards checks for updates once automatically on every launch; the window also provides a “Check Updates” button. The update feed is an
HTTPS JSON file. The client only *notifies*: clicking “Go update” opens the download URL in the default browser;
it **never auto-downloads, replaces or runs the exe**. To ship a new version, just update this JSON as documented here.

The check runs on a background thread and never blocks the game. Automatic check failures (no network, timeout, invalid JSON, non-HTTPS, etc.)
are skipped silently and written to the run log; manual “Check Updates” failures pop up “Check failed, please try again later”.
Either way the game starts and runs normally.

The default URL is the `DEFAULT_UPDATE_URL` constant in `dib/config.py`, compiled into the exe; changing it requires repackaging.
For temporary pre-release verification only, override it with `DIB_UPDATE_URL` — no rebuild needed. Before release, make sure the default address
returns the format documented here.

## Feed Address

- The client requests `https://go.liuxiaogang.com/desktop-icon-billiards-update` by default.
- Query parameters include `app=DesktopIconBilliards`, `version=<currently installed version>` and `source`. Automatic checks on startup use `source=startup`; manual checks use `source=manual`; the short-link entry can count request sources. Other existing query parameters in the address are preserved by the client.
- The short link uses HTTP 302 to point to [update.json on the main branch of CNB](https://cnb.cool/liuxiaogang/desktop-icon-billiards/-/git/raw/main/update.json), which returns UTF-8 JSON. Update the CNB file to ship.
- For pre-release/staging you can temporarily override: set the `DIB_UPDATE_URL` environment variable, read at process start — the exe does not need repackaging.

```powershell
# Applies to the current PowerShell session only; for validating a pre-release JSON
$env:DIB_UPDATE_URL = 'https://staging.example.com/desktop-icon-billiards/update.json'
.\dist\DesktopIconBilliards-0.1.0-win-x64.exe
```

`DIB_UPDATE_URL` likewise only accepts `https://` addresses; close the session after verifying so production runs don’t keep pointing at the pre-release feed.

## JSON Format

The top level has three required fields; `history` is optional. See `update.example.json` in the repo root for a complete example.

```json
{
  "version": "0.1.1",
  "download_url": "https://example.com/DesktopIconBilliards-0.1.1-win-x64.exe",
  "notes": "Release notes; use \n for line breaks.",
  "history": [
    {"version": "0.1.0", "notes": "First public release."}
  ]
}
```

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `version` | yes | string | Latest version; must be `major.minor.patch`, three non-negative integer segments, e.g. `0.1.1`. |
| `download_url` | yes | string | HTTPS download URL of the latest exe; the client uses only this single address. |
| `notes` | yes | string | Release notes for the latest version; at most 4000 characters; use `\n` for line breaks. |
| `history` | no | array | Notes for older versions, display-only; each item has `version` and `notes` strings. |
| `date` | no | string | Release time, format `YYYY-MM-DD HH:MM`, recorded in Beijing time; `YYYY-MM-DD` (date only) is also accepted. History entries may carry it too. Missing or invalid values are not shown; old clients ignore the field. |

Conventions:

- The top-level `version` is always the latest release; the download URL is taken only from top-level `download_url`; do not put the latest release in `history`.
- `history` holds older versions only; do not put anything newer than the top level there; keep them ordered newest to oldest, retaining about the last 20 entries.
- Version comparison is numeric (`0.10.0` is newer than `0.9.0`), not string ordering.
- Since 0.2.3, notes support lightweight Markdown: `#`–`###` headings, `**bold**`, unordered/ordered lists, inline code and `> quote`. The window shows this release plus all fetched history, segmented by version, with scrollable body.
- HTML is not parsed, images are not loaded, scripts are not executed; unsupported syntax is shown as plain text. Old clients still show notes as plain text; field formats and length limits are unchanged.
- Do not change the top-level structure to an array, and do not change field names or types, or old clients will not recognize the feed.

## Hard Limits

1. **HTTPS only**: both the feed URL and `download_url` must be `https://`; HTTP is rejected.
2. **Whole file at most 65536 bytes (64 KiB)**: counted in bytes after UTF-8 encoding. Non-ASCII characters usually take more than 1 byte,
   so do not estimate by character count; if content is too large, trim `history` or shorten `notes`, and keep some headroom.
3. **`notes` ≤ 4000 characters**: an overlong top-level note makes the client reject the feed; each history entry is likewise limited to 4000 characters,
   overlong history entries are ignored.
4. **Version must be valid**: `version` must be three non-negative integer segments (`major.minor.patch`, each at most 6 digits).
5. **Download URL must be a complete HTTPS URL**: must include a host name and must not contain username or password.

## Release Checklist

1. Change `__version__` in `dib/__init__.py`; this is the only version source in the repo.
   The spec file, `version_info.txt` and the build script do not hard-code the version; do not edit them by hand.
2. Build in a clean Windows environment:

   ```powershell
   pwsh -File build.ps1
   ```

   Output is `dist/DesktopIconBilliards-<version>-win-x64.exe`; the script prints the size and SHA-256.
   Old build outputs are not deleted, making rollback easy.
3. First upload the exe as a GitHub Release asset; CNB assets are uploaded via the release-branch pipeline, not via CLI-created releases.
   The release branch’s `.cnb.yml` downloads the same exe from GitHub, verifies it with `SHA256SUMS.txt`, then creates the version via `git:release`,
   and uploads attachments via `cnbcool/attachments` (`ttl: 0` keeps them permanently). Release notes are read from `RELEASE.md`.
   See the CNB `codex/publish-desktop-icon-billiards-v0.1.4` branch; later releases need the version numbers and checksums updated together.
   After the pipeline succeeds, confirm both sides allow anonymous HTTPS downloads and the SHA-256 matches the local artifact.
   Keep the file name in the `DesktopIconBilliards-<version>-win-x64.exe` format.
4. Update the JSON: move the previous top-level `version` and `notes` to the front of `history`, then write the new
   `version`, `download_url` and `notes`. Set `download_url` to this release’s CNB mirror download link.
5. Validate the JSON: parses cleanly, version is valid, whole file at most 65536 bytes.
6. Verify with an old-version client: on launch it should see the new version and notes, “Go update” should open the right download URL,
   and cancelling should return to the game normally. Use `DIB_UPDATE_URL` for staging; do not change code for this.
7. Keep the historical notes after release; do not clear `history`.

## Local Validation

```powershell
# Must be valid JSON
$feed = Get-Content -Raw -LiteralPath update.example.json | ConvertFrom-Json
$feed.version
$feed.download_url

# UTF-8 byte count must not exceed 65536
(Get-Item -LiteralPath update.example.json).Length

# Top-level notes at most 4000 characters
$feed.notes.Length
```

## Compatibility Notes

- Old clients only read top-level `version`, `download_url` and `notes`, and ignore `history`, so adding history entries
  does not affect their ability to discover and download the latest release.
- New clients also work fine against old-format JSON without `history`.
- The 64 KiB limit comes from shipped clients. Do not raise the client read limit to accommodate oversized JSON; trim the content instead.
- Automatic check failures (no network, timeout, invalid JSON, non-HTTPS, etc.) only skip the prompt and write to the log;
  manual check failures prompt “Check failed, please try again later”; neither affects starting the game.
