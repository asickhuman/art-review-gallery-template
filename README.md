# Art Review Gallery

A small local web app for reviewing batches of generated images.

Reviewers open a desktop (`/`) or mobile (`/m`) page, flip through rounds, and mark each image:

| Action | Meaning |
| --- | --- |
| **Finalist** | Shortlist — moves the file into `finalists/<round>/` |
| **Pick as final** | Ship it — moves into `approved/<round>/` |
| **Steer this direction** | Keep reviewing, but flag the vibe for later training |
| **Reject** | No — moves into `rejected/<round>/` (recent rejects stay visible for 48h) |
| **Back to review / Un-reject** | Undo and return to `needs-review/` |

Clicks are appended to `gallery/clicks.jsonl`. If you configure a webhook, they are batched and POSTed in the background.

This repository is a **template**: it ships with four clearly labeled placeholder images so the UI works on first run. Replace them with your own art.

## Requirements

- Python 3.10+ (stdlib only for the server)
- Optional: [`segno`](https://pypi.org/project/segno/) for the `/qr.png` QR code (`pip install -r requirements.txt`)

No database. State lives in `manifest.json` plus the status folders on disk.

## Quick start

```bash
git clone <this-repo> art-review-gallery
cd art-review-gallery

# Optional: regenerate the placeholder samples + manifest paths for this machine
python3 scripts/seed_samples.py

# Optional webhook / public URL
cp config.example.json gallery/config.json
# edit gallery/config.json — or leave {} for local-only logging

# Run (default http://0.0.0.0:11490/)
python3 gallery/server.py
```

Open http://127.0.0.1:11490/ (desktop) or http://127.0.0.1:11490/m (mobile).

Health check: `GET /api/health`

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `ART_GALLERY_HOST` | `0.0.0.0` | Bind address |
| `ART_GALLERY_PORT` | `11490` | HTTP port |
| `ART_GALLERY_PUBLIC_URL` | _(derived)_ | URL shown in the header / QR |
| `ART_GALLERY_PUBLIC_HOST` | MagicDNS or `localhost` | Host used when deriving the public URL |
| `ART_GALLERY_PUBLIC_SCHEME` | `http` | `http` or `https` when deriving the URL |
| `ART_GALLERY_HTTPS_PORT` | `8443` | Port used when scheme is `https` |
| `GALLERY_WEBHOOK_BATCH_SEC` | `300` | Max seconds between webhook batches |


## Class and act filters

The gallery can filter by **asset class** and **act** in addition to status tabs.

- Class and act are stored on each manifest entry (`asset_class`, `act`).
- If missing, the server derives them from the asset / round / job name using rules in `gallery/class_rules.json`.
- Copy `gallery/class_rules.example.json` to `gallery/class_rules.json` and edit prefixes to match your naming.
- Filters combine with status tabs. The selection is kept in the URL, e.g. `/?class=prop&act=3` or `/m?class=ui`.

Default derivation (no custom rules file):

| Name pattern | Class | Act |
| --- | --- | --- |
| `map-act{N}-{kind}-…` | `kind` (`floor`, `wall`, `door`, `prop`, `decal`, `rubble`) | `N` |
| `backdrop…` / `relic…` / `portrait…` / `fx-…` | matching class | from `act{N}` if present |
| `*-icon` / `ui-…` / `icon-…` | `ui` | from `act{N}` if present |
| `char-…` / `enemy-…` / `npc-…` | `character` | from `act{N}` if present |
| anything else | `other` (or a configured fallback) | from `act{N}` if present |

## Layout

```
.
├── gallery/
│   ├── server.py              # HTTP UI + API
│   ├── config.json            # local only (gitignored) — copy from config.example.json
│   ├── class_rules.example.json
│   ├── class_rules.json       # optional; copy from example (gitignored)
│   └── art-gallery.service.example
├── manifest.json              # index of every reviewable image
├── needs-review/<round>/      # awaiting a decision (+ optional thumbs/)
├── finalists/<round>/         # shortlisted
├── approved/<round>/          # shipped
├── rejected/<round>/
├── training/                  # optional bucket
├── in-game/                   # optional bucket
├── scripts/seed_samples.py
├── config.example.json
└── README.md
```

`ART_ROOT` is the directory that contains `manifest.json` (the parent of `gallery/`).

## Pointing it at your own images

1. Drop files under `needs-review/<your-round-name>/`.
2. Optional thumbs under `needs-review/<your-round-name>/thumbs/`.
3. Append entries to `manifest.json` (or write a small importer). Minimum fields:

```json
{
  "id": "my-asset-round-1-A1",
  "round": "my-asset-round-1",
  "asset": "my-asset",
  "status": "needs-review",
  "spark_path": "needs-review/my-asset-round-1/A1__my-asset.png",
  "thumb_path": "needs-review/my-asset-round-1/thumbs/A1__my-asset_thumb.png",
  "label": "My asset — A1",
  "cell": "A1",
  "prompt": "optional",
  "added_at": "2026-01-01T12:00:00+00:00"
}
```

Paths may be **relative to the repo root** (preferred) or absolute, as long as they stay under the art root. The field name `spark_path` is historical and just means “path to the full-size image on this machine.”

4. Refresh the browser — `GET /api/items` reloads the manifest each request.

Actions move the image (and thumb, if present) into the matching status folder and update `manifest.json` in place.

## Webhook (optional)

`gallery/config.json`:

```json
{
  "url": "https://example.com/your-webhook-endpoint",
  "key": "YOUR_WEBHOOK_BEARER_TOKEN",
  "public_url": "http://localhost:11490/"
}
```

Clicks still work with an empty `{}` config; they only log locally.

## systemd user service (optional)

Copy `gallery/art-gallery.service.example` to `~/.config/systemd/user/art-gallery.service`, fix the paths, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now art-gallery.service
```

## License

MIT — see [LICENSE](LICENSE).
