#!/usr/bin/env python3
"""Digital Veil art review gallery — Token Annihilator theme, / and /m."""
from __future__ import annotations

import io
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from urllib.parse import parse_qs, quote, unquote, urlparse
import urllib.request

GALLERY_DIR = Path(__file__).resolve().parent
ART_ROOT = GALLERY_DIR.parent
MANIFEST = ART_ROOT / "manifest.json"
CONFIG_PATH = GALLERY_DIR / "config.json"
CLICKS = GALLERY_DIR / "clicks.jsonl"
WEBHOOK_CURSOR = GALLERY_DIR / "webhook_cursor.json"
HOST = os.environ.get("ART_GALLERY_HOST", "0.0.0.0")
PORT = int(os.environ.get("ART_GALLERY_PORT", "11490"))
HTTPS_PORT = int(os.environ.get("ART_GALLERY_HTTPS_PORT", "8443"))
# Batched webhooks: clicks save instantly; notify routine at most once per BATCH_INTERVAL_SEC.
BATCH_INTERVAL_SEC = int(os.environ.get("GALLERY_WEBHOOK_BATCH_SEC", "300"))  # ~5 min
FINAL_QUIET_SEC = int(os.environ.get("GALLERY_WEBHOOK_FINAL_QUIET_SEC", "90"))
FINAL_MIN_GAP_SEC = int(os.environ.get("GALLERY_WEBHOOK_FINAL_MIN_GAP_SEC", "180"))  # 3 min
BATCH_POLL_SEC = float(os.environ.get("GALLERY_WEBHOOK_POLL_SEC", "5"))

STATUSES = ("needs-review", "finalist", "approved", "rejected", "training", "in-game", "archived", "hidden-empty")
RECENT_REJECT_HOURS = 24
EMPTY_OPAQUE_FRAC = float(os.environ.get("GALLERY_EMPTY_OPAQUE_FRAC", "0.02"))
THUMB_MAX = int(os.environ.get("GALLERY_THUMB_MAX", "256"))

# Asset class / act derived from asset or job names (rule-based, no model).
ASSET_CLASSES = (
    "floor", "wall", "door", "prop", "decal", "rubble",
    "backdrop", "character", "portrait", "ui", "relic", "fx", "other",
)
ASSET_CLASS_LABELS = {
    "floor": "floor",
    "wall": "wall",
    "door": "door",
    "prop": "prop",
    "decal": "decal",
    "rubble": "rubble",
    "backdrop": "backdrop",
    "character": "characters/enemies",
    "portrait": "portraits",
    "ui": "UI",
    "relic": "relic",
    "fx": "effects",
    "other": "other",
}
_CHARACTER_EXACT = frozenset({
    "mecha-fox", "turret", "echo-shade", "data-courier", "relay-pylon",
})
_CHARACTER_PREFIXES = ("biomech-",)
_UI_PREFIXES = (
    "elem-", "weapon-", "terminal-", "hexslot-", "shield-", "supply-", "tooltip-",
)
_ACT_RE = re.compile(r"(?:^|[-\s_/])act[_-]?(\d+)\b", re.I)
_MAP_KIND_RE = re.compile(r"^map-act(\d+)-([a-z]+)", re.I)


def classify_asset(asset: str = "", source_job: str = "", round_name: str = "") -> tuple[str, int | None]:
    """Return (asset_class, act_or_None) from naming conventions."""
    name = (asset or "").strip().lower()
    blob = " ".join([name, (source_job or "").lower(), (round_name or "").lower()])
    act = None
    m = _ACT_RE.search(blob)
    if m:
        act = int(m.group(1))
    mm = _MAP_KIND_RE.match(name)
    if mm:
        act = int(mm.group(1))
        kind = mm.group(2).lower()
        if kind in ("floor", "wall", "door", "prop", "decal", "rubble"):
            return kind, act
        return "other", act
    if name.startswith("backdrop"):
        return "backdrop", act
    if name.startswith("relic"):
        return "relic", act
    if name.startswith("portrait") or "portrait" in name:
        return "portrait", act
    if name.startswith("fx-") or name.startswith("vfx-") or name.endswith("-fx"):
        return "fx", act
    if name in _CHARACTER_EXACT or any(name.startswith(p) for p in _CHARACTER_PREFIXES):
        return "character", act
    if any(name.startswith(p) for p in _UI_PREFIXES) or "-icon" in name:
        return "ui", act
    return "other", act


def ensure_class_fields(entry: dict) -> dict:
    """Fill asset_class / act on an entry if missing; mutate and return entry."""
    if not isinstance(entry, dict):
        return entry
    cls = entry.get("asset_class")
    act = entry.get("act")
    if cls not in ASSET_CLASSES or (act is None and "act" not in entry):
        derived_cls, derived_act = classify_asset(
            entry.get("asset") or "",
            entry.get("source_job") or "",
            entry.get("round") or "",
        )
        if cls not in ASSET_CLASSES:
            entry["asset_class"] = derived_cls
        if "act" not in entry:
            entry["act"] = derived_act
    return entry


def archive_reject_hours() -> int:
    """Hours before a reject moves to archived. Config key archive_reject_hours, default 24."""
    cfg = load_config()
    try:
        v = cfg.get("archive_reject_hours")
        if v is not None:
            return max(1, int(v))
    except Exception:
        pass
    env = os.environ.get("GALLERY_ARCHIVE_REJECT_HOURS")
    if env:
        try:
            return max(1, int(env))
        except Exception:
            pass
    return 24


def is_recently_rejected(entry: dict) -> bool:
    if entry.get("status") != "rejected":
        return False
    ts = parse_iso(entry.get("rejected_at") or entry.get("reviewed_at") or "")
    if not ts:
        return False
    hours = archive_reject_hours()
    return datetime.now(timezone.utc) - ts.astimezone(timezone.utc) <= timedelta(hours=hours)


def opaque_fraction(path: Path) -> float | None:
    try:
        from PIL import Image
        im = Image.open(path).convert("RGBA")
        px = im.getdata()
        step = max(1, len(px) // 4000)
        vals = [px[i] for i in range(0, len(px), step)]
        if not vals:
            return 0.0
        return sum(1 for p in vals if p[3] > 10) / len(vals)
    except Exception:
        return None


def make_checker(size: int):
    from PIL import Image
    tile = 8
    im = Image.new("RGB", (size, size), (24, 28, 32))
    px = im.load()
    for y in range(size):
        for x in range(size):
            if ((x // tile) + (y // tile)) % 2 == 0:
                px[x, y] = (40, 44, 48)
    return im


def ensure_thumb(entry: dict, force: bool = False) -> str:
    """Ensure a <=THUMB_MAX webp thumb exists; composite transparent tiles on checker."""
    from PIL import Image
    sp = Path(entry.get("spark_path") or "")
    if not sp.is_file():
        return entry.get("thumb_path") or ""
    tp = Path(entry.get("thumb_path") or "")
    round_dir = sp.parent
    thumb_dir = round_dir / "thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    cache = thumb_dir / (sp.stem + "_t" + str(THUMB_MAX) + ".webp")
    if cache.is_file() and not force:
        entry["thumb_path"] = str(cache)
        return str(cache)
    if tp.is_file() and not force:
        try:
            with Image.open(tp) as im:
                # reuse small thumbs that already have content; still prefer checker cache for sparse tiles
                if max(im.size) <= THUMB_MAX + 2 and opaque_fraction(tp) not in (None, 0.0) and (opaque_fraction(tp) or 0) >= 0.02:
                    entry["thumb_path"] = str(tp)
                    return str(tp)
        except Exception:
            pass
    try:
        im = Image.open(sp).convert("RGBA")
        of = opaque_fraction(sp)
        w, h = im.size
        scale = min(THUMB_MAX / max(w, 1), THUMB_MAX / max(h, 1), 1.0)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        im = im.resize((nw, nh), Image.Resampling.LANCZOS)
        if of is not None and of < 0.98:
            bg = make_checker(max(nw, nh)).convert("RGBA").resize((nw, nh))
            composed = Image.alpha_composite(bg, im)
        else:
            composed = im
        composed.convert("RGB").save(cache, "WEBP", quality=72, method=4)
        entry["thumb_path"] = str(cache)
        return str(cache)
    except Exception as e:
        print(f"thumb failed {sp}: {e}", flush=True)
        return entry.get("thumb_path") or str(sp)


def classify_empty_and_archive(entries: list) -> dict:
    """Backfill rejected_at, archive old rejects, mark empty tiles hidden-empty. Never deletes files."""
    stats = {"archived": 0, "hidden_empty": 0, "backfilled_rejected_at": 0, "thumbs_fixed": 0, "checked": 0}
    hours = archive_reject_hours()
    now = datetime.now(timezone.utc)
    click_reject_ts: dict[str, str] = {}
    if CLICKS.exists():
        try:
            with CLICKS.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    payload = row.get("payload") or {}
                    if (payload.get("action") or "").lower() != "reject":
                        continue
                    ts = row.get("ts") or ""
                    ids = list(payload.get("ids") or [])
                    if payload.get("id"):
                        ids.append(payload.get("id"))
                    for i in ids:
                        if i and i not in click_reject_ts:
                            click_reject_ts[i] = ts
        except Exception:
            pass

    tile_classes = {"floor", "wall", "door", "decal", "rubble", "prop"}
    changed = False
    for entry in entries:
        ensure_class_fields(entry)
        if entry.get("status") in ("rejected", "archived") and not entry.get("rejected_at"):
            ts = entry.get("reviewed_at") or click_reject_ts.get(entry.get("id") or "") or now_iso()
            entry["rejected_at"] = ts
            stats["backfilled_rejected_at"] += 1
            changed = True
        if entry.get("status") == "rejected":
            ts = parse_iso(entry.get("rejected_at") or entry.get("reviewed_at") or "")
            if ts and (now - ts.astimezone(timezone.utc) > timedelta(hours=hours)):
                entry["status"] = "archived"
                stats["archived"] += 1
                changed = True
        if entry.get("status") == "needs-review" and (entry.get("asset_class") in tile_classes):
            sp = Path(entry.get("spark_path") or "")
            if sp.is_file():
                stats["checked"] += 1
                of = opaque_fraction(sp)
                if of is not None and of < EMPTY_OPAQUE_FRAC:
                    entry["status"] = "hidden-empty"
                    entry["empty_opaque_frac"] = round(of, 4)
                    entry["hidden_at"] = now_iso()
                    stats["hidden_empty"] += 1
                    changed = True
                else:
                    old = entry.get("thumb_path")
                    ensure_thumb(entry, force=False)
                    if entry.get("thumb_path") != old:
                        stats["thumbs_fixed"] += 1
                        changed = True
    if changed:
        save_manifest(entries)
    return stats



# Prefer gallery venv segno
_VENV_SITE = GALLERY_DIR / ".venv" / "lib"
for p in _VENV_SITE.glob("python*/site-packages"):
    sys.path.insert(0, str(p))


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_iso(s: str):
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t
    except Exception:
        return None


def load_manifest() -> list:
    if not MANIFEST.exists():
        return []
    try:
        data = json.loads(MANIFEST.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_manifest(entries: list) -> None:
    MANIFEST.write_text(json.dumps(entries, indent=2) + "\n")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text() or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def config_ready(cfg: dict) -> bool:
    return bool(cfg.get("url") and cfg.get("key"))


def append_click(row: dict) -> None:
    with CLICKS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def post_webhook(cfg: dict, payload: dict, url_override: str | None = None) -> tuple[bool, str]:
    url = url_override or cfg.get("url")
    if not url or not (url_override or cfg.get("key")):
        return False, "webhook not configured"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "digital-veil-art-gallery/1.4-batch",
    }
    if cfg.get("key") and not url_override:
        headers["Authorization"] = f"Bearer {cfg['key']}"
        headers["X-Automation-Key"] = str(cfg["key"])
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read()[:500]
            return True, f"HTTP {resp.status} {body!r}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def load_webhook_cursor() -> dict:
    """byte_offset into clicks.jsonl of next unsent byte; last_sent_epoch wall time."""
    default = {"byte_offset": 0, "last_sent_epoch": 0.0}
    if not WEBHOOK_CURSOR.exists():
        return dict(default)
    try:
        data = json.loads(WEBHOOK_CURSOR.read_text() or "{}")
        if not isinstance(data, dict):
            return dict(default)
        return {
            "byte_offset": int(data.get("byte_offset") or 0),
            "last_sent_epoch": float(data.get("last_sent_epoch") or 0.0),
        }
    except Exception:
        return dict(default)


def save_webhook_cursor(byte_offset: int, last_sent_epoch: float) -> None:
    WEBHOOK_CURSOR.write_text(
        json.dumps(
            {
                "byte_offset": int(byte_offset),
                "last_sent_epoch": float(last_sent_epoch),
                "updated_at": now_iso(),
            },
            indent=2,
        )
        + "\n"
    )


def init_webhook_cursor_at_eof() -> None:
    """First boot after upgrade: do not re-blast historical clicks."""
    if WEBHOOK_CURSOR.exists():
        return
    offset = CLICKS.stat().st_size if CLICKS.exists() else 0
    save_webhook_cursor(offset, 0.0)
    print(f"webhook cursor initialized at EOF offset={offset}", flush=True)


def read_unsent_clicks(byte_offset: int) -> tuple[list, int]:
    """Return (rows, new_end_offset) for complete JSONL lines after byte_offset."""
    if not CLICKS.exists():
        return [], byte_offset
    size = CLICKS.stat().st_size
    if byte_offset > size:
        byte_offset = size
    rows = []
    with CLICKS.open("rb") as f:
        f.seek(byte_offset)
        while True:
            line = f.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                # incomplete trailing line — wait for next write
                break
            byte_offset += len(line)
            try:
                row = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows, byte_offset


def click_action(row: dict) -> str:
    return str(((row.get("payload") or {}).get("action") or "")).strip().lower()


def build_batch_payload(rows: list) -> dict:
    clicks = []
    for row in rows:
        payload = row.get("payload") or {}
        entry = {
            "ts": row.get("ts"),
            "action": payload.get("action"),
            "note": payload.get("note") or "",
            "destination": payload.get("destination") or "",
            "bulk": bool(row.get("bulk")),
        }
        if payload.get("ids"):
            entry["ids"] = list(payload.get("ids") or [])
            entry["paths"] = list(payload.get("paths") or [])
        else:
            entry["id"] = payload.get("id") or ""
            entry["path"] = payload.get("path") or ""
        clicks.append(entry)
    return {
        "batch": True,
        "count": len(clicks),
        "clicks": clicks,
        "queued_at": now_iso(),
    }


class WebhookBatcher:
    """Background sender: at most one webhook per BATCH_INTERVAL_SEC.

    Clicks are always appended to clicks.jsonl immediately by the request handler.
    This thread only notifies the gallery-click routine in batches.
    'Pick as final' (action=final) may flush after FINAL_QUIET_SEC of silence,
    still respecting FINAL_MIN_GAP_SEC since the last successful send.
    Failed sends leave the cursor unmoved and retry on the next window.
    """

    def __init__(self):
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_click_epoch = 0.0
        self._thread: threading.Thread | None = None
        self._url_override: str | None = None  # dry-run / tests only

    def notify_click(self) -> None:
        with self._lock:
            self._last_click_epoch = time.time()

    def set_url_override(self, url: str | None) -> None:
        self._url_override = url

    def start(self) -> None:
        init_webhook_cursor_at_eof()
        self._thread = threading.Thread(
            target=self._loop, name="gallery-webhook-batcher", daemon=True
        )
        self._thread.start()
        print(
            f"webhook batcher started batch={BATCH_INTERVAL_SEC}s "
            f"final_quiet={FINAL_QUIET_SEC}s final_min_gap={FINAL_MIN_GAP_SEC}s",
            flush=True,
        )

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        cur = load_webhook_cursor()
        unsent, end = read_unsent_clicks(cur["byte_offset"])
        with self._lock:
            last_click = self._last_click_epoch
        return {
            "byte_offset": cur["byte_offset"],
            "end_offset": end,
            "unsent": len(unsent),
            "last_sent_epoch": cur["last_sent_epoch"],
            "last_click_epoch": last_click,
            "batch_interval_sec": BATCH_INTERVAL_SEC,
            "final_quiet_sec": FINAL_QUIET_SEC,
            "final_min_gap_sec": FINAL_MIN_GAP_SEC,
            "has_final_pending": any(click_action(r) == "final" for r in unsent),
            "override": bool(self._url_override),
        }

    def flush_now_for_test(self) -> tuple[bool, str, int]:
        """Force one send attempt (tests). Returns (ok, detail, count)."""
        return self._try_send(force=True)

    def _loop(self) -> None:
        while not self._stop.wait(BATCH_POLL_SEC):
            try:
                self._try_send(force=False)
            except Exception as e:
                print(f"webhook batcher error: {type(e).__name__}: {e}", flush=True)

    def _try_send(self, force: bool = False) -> tuple[bool, str, int]:
        cur = load_webhook_cursor()
        unsent, end_offset = read_unsent_clicks(cur["byte_offset"])
        if not unsent:
            return False, "nothing to send", 0
        now = time.time()
        last_sent = float(cur.get("last_sent_epoch") or 0.0)
        gap = now - last_sent if last_sent else 10**9
        with self._lock:
            last_click = self._last_click_epoch
        quiet = (now - last_click) if last_click else 10**9
        has_final = any(click_action(r) == "final" for r in unsent)
        ready = force
        if not ready:
            if gap >= BATCH_INTERVAL_SEC:
                ready = True
            elif has_final and quiet >= FINAL_QUIET_SEC and gap >= FINAL_MIN_GAP_SEC:
                ready = True
        if not ready:
            return False, "waiting for batch window", len(unsent)

        cfg = load_config()
        payload = build_batch_payload(unsent)
        ok, detail = post_webhook(cfg, payload, url_override=self._url_override)
        # Never log the key; detail is HTTP status / error only.
        if ok:
            save_webhook_cursor(end_offset, now)
            print(
                f"webhook batch sent count={len(unsent)} offset={end_offset} detail={detail}",
                flush=True,
            )
            return True, detail, len(unsent)
        print(
            f"webhook batch FAILED count={len(unsent)} will retry detail={detail}",
            flush=True,
        )
        return False, detail, len(unsent)


WEBHOOK_BATCHER = WebhookBatcher()


def find_entry(entries: list, entry_id: str):
    for i, e in enumerate(entries):
        if e.get("id") == entry_id:
            return i, e
    return None, None


def safe_under(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def magicdns_host() -> str:
    """Optional Tailscale MagicDNS hostname; falls back to localhost."""
    try:
        raw = subprocess.check_output(["tailscale", "status", "--json"], timeout=5)
        d = json.loads(raw)
        name = ((d.get("Self") or {}).get("DNSName") or "").rstrip(".")
        if name:
            return name
    except Exception:
        pass
    return "localhost"


def public_https_url() -> str:
    """Public URL shown in the UI / QR. Override with ART_GALLERY_PUBLIC_URL or config public_url."""
    env = os.environ.get("ART_GALLERY_PUBLIC_URL", "").strip()
    if env:
        return env if env.endswith("/") else env + "/"
    cfg = load_config()
    cfg_url = str(cfg.get("public_url") or "").strip()
    if cfg_url:
        return cfg_url if cfg_url.endswith("/") else cfg_url + "/"
    host = os.environ.get("ART_GALLERY_PUBLIC_HOST") or magicdns_host()
    https_port = HTTPS_PORT
    scheme = os.environ.get("ART_GALLERY_PUBLIC_SCHEME", "http")
    if scheme == "https":
        return f"https://{host}:{https_port}/"
    return f"http://{host}:{PORT}/"

def qr_png_bytes(url: str) -> bytes:
    try:
        import segno
        buf = io.BytesIO()
        segno.make(url, error="m").save(buf, kind="png", scale=4, border=1)
        return buf.getvalue()
    except Exception as e:
        # 1x1 transparent PNG fallback
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )


def status_dir(status: str) -> Path:
    """Canonical on-disk folder for a status (under ART_ROOT)."""
    mapping = {
        "needs-review": ART_ROOT / "needs-review",
        "finalist": ART_ROOT / "finalists",
        "approved": ART_ROOT / "approved",
        "rejected": ART_ROOT / "rejected",
        "training": ART_ROOT / "training",
        "in-game": ART_ROOT / "in-game",
    }
    return mapping.get(status, ART_ROOT / "needs-review")


def move_entry_files(entry: dict, new_status: str) -> None:
    """Move spark + thumb into status/<round>/…; update paths. Sources stay put."""
    sp = Path(entry.get("spark_path") or "")
    if not sp.exists() or not sp.is_file():
        return
    round_name = entry.get("round") or "misc"
    dest_root = status_dir(new_status) / round_name
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / sp.name
    if sp.resolve() != dest.resolve():
        try:
            if dest.exists():
                dest.unlink()
            sp.replace(dest)
            entry["spark_path"] = str(dest)
        except Exception as e:
            print(f"move spark failed {sp} -> {dest}: {e}", flush=True)
            return
    tp = Path(entry.get("thumb_path") or "")
    if tp.exists() and tp.is_file():
        thumb_dir = dest_root / "thumbs"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        tdest = thumb_dir / tp.name
        try:
            if tp.resolve() != tdest.resolve():
                if tdest.exists():
                    tdest.unlink()
                tp.replace(tdest)
            entry["thumb_path"] = str(tdest)
        except Exception as e:
            print(f"move thumb failed {tp} -> {tdest}: {e}", flush=True)


def apply_action(entry: dict, action: str, note: str, destination: str) -> dict:
    # Legacy approve → finalist (shortlist), not ship
    if action == "approve":
        action = "finalist"
    if action == "finalist":
        entry["status"] = "finalist"
        if destination:
            entry["destination"] = destination
        entry["reviewed_at"] = now_iso()
        entry.pop("rejected_at", None)
        entry.pop("steer", None)
        if note:
            entry["note"] = note
        move_entry_files(entry, "finalist")
    elif action == "final":
        entry["status"] = "approved"
        if destination:
            entry["destination"] = destination
        entry["reviewed_at"] = now_iso()
        entry.pop("rejected_at", None)
        entry.pop("steer", None)
        if note:
            entry["note"] = note
        move_entry_files(entry, "approved")
    elif action == "reject":
        entry["status"] = "rejected"
        entry["reviewed_at"] = now_iso()
        entry["rejected_at"] = now_iso()
        entry.pop("steer", None)
    elif action == "steer":
        entry["status"] = "training"
        if destination:
            entry["destination"] = destination
        entry["reviewed_at"] = now_iso()
        entry["steer"] = True
        entry.pop("rejected_at", None)
        if note:
            entry["note"] = note
    elif action == "note":
        if note:
            entry["note"] = note
            entry["noted_at"] = now_iso()
    elif action in ("unreject", "undo", "back-to-review"):
        entry["status"] = "needs-review"
        entry.pop("rejected_at", None)
        entry.pop("steer", None)
        entry["reviewed_at"] = now_iso()
        move_entry_files(entry, "needs-review")
    return entry


def build_hook(action, bulk, ids, paths, note, destination, single_id="", single_path=""):
    # Normalize legacy approve in webhook payloads too
    if action == "approve":
        action = "finalist"
    if bulk:
        body = {"action": action, "ids": list(ids), "paths": list(paths)}
        if action != "reject":
            body["note"] = note
        if action in ("finalist", "final", "steer", "approve"):
            body["destination"] = destination or ""
        return body
    body = {"action": action, "id": single_id, "path": single_path}
    if action != "reject":
        body["note"] = note
    if action in ("finalist", "final", "steer", "approve"):
        body["destination"] = destination or ""
    return body


THEME_CSS = r"""
:root{
  --bg:#030505;--panel:#0a100c;--green:#39ff14;--green-bright:#c8ff9a;--green-mid:#6dff4a;
  --line:#2a6b2a;--line-strong:#3a9a3a;--unlit:#152218;--unlit-edge:#2a3e2a;
  --warn:#ffcc00;--redline:#ff3d1a;--muted:#a8d898;--label:#f0ffe8;--cyan:#7ef0ff;
  --mono:ui-monospace,"Cascadia Mono","SF Mono",Menlo,Consolas,monospace;
  --safe-t:env(safe-area-inset-top,0px);--safe-b:env(safe-area-inset-bottom,0px);
  --safe-l:env(safe-area-inset-left,0px);--safe-r:env(safe-area-inset-right,0px);
  --ship:var(--green);--steer:var(--cyan);--danger:var(--redline);
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:100%;min-height:100%;background:var(--bg);color:var(--green);overflow-x:hidden;}
body{
  font-family:var(--mono);font-size:15px;line-height:1.35;
  padding:calc(10px + var(--safe-t)) calc(12px + var(--safe-r)) calc(16px + var(--safe-b)) calc(12px + var(--safe-l));
}
body::before{content:"";pointer-events:none;position:fixed;inset:0;z-index:9999;
  background:repeating-linear-gradient(0deg,rgba(0,0,0,.08) 0,rgba(0,0,0,.08) 1px,transparent 1px,transparent 3px);opacity:.14;}
.cluster{
  position:relative;width:100%;margin:0 auto;display:flex;flex-direction:column;gap:12px;
  border:3px solid var(--line-strong);border-radius:12px;padding:12px 10px;
  background:linear-gradient(180deg,#07100a,#030505);
  box-shadow:inset 0 0 24px #39ff1412,0 0 12px #39ff1420;
}
.hdr{display:flex;flex-direction:column;gap:8px;}
.hdr-top{display:flex;align-items:flex-start;justify-content:space-between;gap:8px;flex-wrap:wrap;}
h1{margin:0;color:var(--green-bright);letter-spacing:.06em;text-transform:uppercase;
  font-size:15px;font-weight:800;line-height:1.25;
  text-shadow:0 0 5px rgba(57,255,20,.35),0 0 1px rgba(200,255,154,.55);}
.https-bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;
  border:2px solid var(--line);border-radius:8px;padding:8px;background:#050a07;}
.https-bar a{color:var(--cyan);font-size:12px;word-break:break-all;flex:1 1 140px;}
.copy-btn,.desk-link{
  appearance:none;cursor:pointer;border:1px solid var(--line);background:#0a120c;color:var(--muted);
  font:700 11px/1 var(--mono);letter-spacing:.04em;text-transform:uppercase;padding:8px 10px;border-radius:4px;
  text-decoration:none;white-space:nowrap;}
.copy-btn:active{color:var(--green-bright);}
.banner{display:none;background:#2a2208;color:var(--warn);border:1px solid #6a4c16;
  padding:8px 10px;border-radius:8px;font-size:12px;}
.banner.show{display:block;}
.range-tog{display:flex;flex-wrap:nowrap;gap:3px;width:100%;padding:2px;border:1px solid var(--line-strong);
  border-radius:4px;background:#040806;overflow-x:auto;-webkit-overflow-scrolling:touch;}
.rtab{appearance:none;cursor:pointer;border:1px solid #1a2e1a;background:#0a120c;color:#5a7a5a;
  font:700 10px/1 var(--mono);letter-spacing:.04em;text-transform:uppercase;padding:8px 10px;border-radius:3px;
  text-align:center;white-space:nowrap;text-decoration:none;flex:0 0 auto;min-width:44px;
  display:inline-flex;align-items:center;justify-content:center;}
.rtab.on{color:#041204;background:var(--green);border-color:var(--green-bright);}
.rtab.recent{border-color:var(--redline);color:#c07060;}
.rtab.recent.on{background:var(--redline);color:#fff;border-color:#ff8a6a;}
.rtab.rtab-link{color:var(--muted);}
.meta{color:var(--muted);font-size:12px;}
.legend{font-size:11px;color:var(--muted);}
.legend b.ship{color:var(--green-bright);} .legend b.final{color:var(--green-bright);} .legend b.steer{color:var(--cyan);} .legend b.reject{color:var(--redline);}
.view-controls label{display:inline-flex;align-items:center;gap:8px;font-size:12px;color:var(--muted);
  border:1px solid var(--line);border-radius:6px;padding:8px 10px;cursor:pointer;}
.view-controls input{width:18px;height:18px;accent-color:var(--green);}
.panel{border:2px solid var(--line);border-radius:8px;padding:10px;background:#050a07;}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px;}
.card{background:var(--panel);border:2px solid var(--line);border-radius:8px;overflow:hidden;
  display:flex;flex-direction:column;min-width:0;}
.card.selected{border-color:var(--green-bright);box-shadow:0 0 0 2px rgba(57,255,20,.35);}
.card img{width:100%;aspect-ratio:1/1;object-fit:contain;background:#030805;display:block;cursor:pointer;}
.card .body{padding:8px;display:flex;flex-direction:column;gap:6px;flex:1;}
.id{font-size:11px;color:var(--muted);word-break:break-all;}
.round{font-size:12px;font-weight:800;color:var(--label);}
.sel-row{display:flex;justify-content:flex-end;align-items:center;min-height:40px;}
.selbox{width:26px;height:26px;accent-color:var(--green);padding:8px;box-sizing:content-box;cursor:pointer;}
.actions{display:grid;grid-template-columns:1fr 1fr;gap:6px;}
button{border:0;border-radius:6px;padding:10px 8px;font:700 11px/1.1 var(--mono);cursor:pointer;text-transform:uppercase;letter-spacing:.03em;}
.approve,.finalist{background:var(--green);color:#041204;}
.pick-final{background:var(--green);color:#041204;border:2px solid var(--green-bright)!important;box-shadow:0 0 0 1px var(--green-bright);}
.reject{background:var(--redline);color:#fff;}
.steer{background:var(--cyan);color:#041018;grid-column:1/-1;}
.back-review{background:#1a3a2a;color:var(--green-bright);border:1px solid var(--line-strong);}
.note-btn{background:#1a3a2a;color:var(--green-bright);border:1px solid var(--line);}
.unreject{background:var(--warn);color:#1a1200;}
.asset-group{grid-column:1/-1;margin:12px 0 4px;padding:6px 2px;border-bottom:1px solid var(--line);color:var(--green-bright);font:700 13px/1.2 var(--mono);text-transform:uppercase;letter-spacing:.06em;}
.finalists-grid{display:contents;}
input[type=text],textarea{width:100%;border-radius:6px;border:1px solid var(--line);background:#040806;
  color:var(--green-bright);padding:8px 10px;font:12px/1.3 var(--mono);}
textarea{min-height:56px;resize:vertical;}
.note-row{display:flex;flex-direction:column;gap:6px;}
.note-row .row-btns{display:grid;grid-template-columns:1fr 1fr;gap:6px;}
.existing-note{font-size:11px;color:var(--muted);white-space:pre-wrap;}
.empty{color:var(--muted);text-align:center;padding:28px 8px;}
.toast{display:none;margin:0 auto;max-width:92%;background:#102018;border:1px solid var(--line);
  color:var(--green-bright);padding:10px 12px;border-radius:8px;font-size:12px;align-items:center;gap:10px;}
.toast.show{display:flex;}
.toast button{background:var(--cyan);color:#041018;padding:8px 10px;flex:0 0 auto;}
.bulkbar{border:2px solid var(--line-strong);border-radius:8px;padding:10px;background:#050a07;
  display:flex;flex-direction:column;gap:8px;position:sticky;bottom:0;}
.bulkbar .fields{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
.bulkbar .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
.bulkbar .count{font-weight:800;font-size:12px;color:var(--label);min-width:7rem;}
.bulk-actions{display:flex;gap:6px;flex:1;justify-content:flex-end;flex-wrap:wrap;}
.bulk-actions button{padding:12px 10px;}
.bulk-actions button:disabled{opacity:.35;cursor:not-allowed;}
.clear-sel{background:#12301a;color:var(--muted);border:1px solid var(--line);}
.foot{text-align:center;color:var(--muted);font-size:11px;}

.filter-row{display:flex;flex-wrap:wrap;gap:10px;margin-top:6px;align-items:flex-end;}
.filter-field{display:flex;flex-direction:column;gap:4px;min-width:140px;flex:1;}
.filter-label{font-size:11px;color:var(--muted);letter-spacing:.04em;text-transform:uppercase;}
.filter-select{appearance:none;cursor:pointer;border:2px solid var(--line-strong);background:#0a120c;color:var(--green-bright);
  font:700 14px/1.2 var(--mono);padding:12px 14px;border-radius:6px;min-height:48px;width:100%;
  background-image:linear-gradient(45deg,transparent 50%,var(--cyan) 50%),linear-gradient(135deg,var(--cyan) 50%,transparent 50%);
  background-position:calc(100% - 18px) calc(50% - 3px),calc(100% - 12px) calc(50% - 3px);
  background-size:6px 6px,6px 6px;background-repeat:no-repeat;padding-right:32px;}
.pager{display:flex;gap:10px;align-items:center;justify-content:center;flex-wrap:wrap;margin-top:8px;}
.page-btn{appearance:none;cursor:pointer;border:2px solid var(--line-strong);background:#0a120c;color:var(--green-bright);
  font:800 14px/1 var(--mono);padding:12px 18px;border-radius:6px;min-height:48px;min-width:88px;}
.page-btn:disabled{opacity:.35;cursor:not-allowed;}
.page-meta{color:var(--muted);font-size:12px;}
.card .actions{display:grid;grid-template-columns:1fr 1fr;gap:6px;}
.card .actions .steer{grid-column:1/-1;}
.card .actions button{min-height:44px;font-size:13px;font-weight:800;padding:10px 8px;}
.card img{background:
  linear-gradient(45deg,#1a1f22 25%,transparent 25%),
  linear-gradient(-45deg,#1a1f22 25%,transparent 25%),
  linear-gradient(45deg,transparent 75%,#1a1f22 75%),
  linear-gradient(-45deg,transparent 75%,#1a1f22 75%);
  background-size:16px 16px;background-position:0 0,0 8px,8px -8px,-8px 0;background-color:#0b0e10;}
"""

DESKTOP_EXTRA = r"""
.cluster{max-width:none; width:96vw; margin:0 auto;}
body{padding-left:max(1vw, var(--safe-l)); padding-right:max(1vw, var(--safe-r));}
.grid{grid-template-columns:repeat(auto-fill,minmax(280px,1fr));}
@media(min-width:1600px){.grid{grid-template-columns:repeat(auto-fill,minmax(300px,1fr));}}
@media(min-width:2000px){.grid{grid-template-columns:repeat(auto-fill,minmax(320px,1fr));}}
@media(max-width:520px){.bulkbar .fields{grid-template-columns:1fr;}}
/* Desktop-only: roomy status tabs + large action buttons */
.range-tog{
  flex-wrap:wrap; gap:8px; padding:10px; min-height:64px;
  align-items:center; margin:4px 0 6px;
}
.rtab{
  font-size:18px; line-height:1.15; font-weight:800;
  min-height:48px; padding:12px 18px; border-radius:6px;
  letter-spacing:.05em;
}
.rtab.rtab-link{font-size:16px;}
.card .actions{gap:8px; margin-top:4px;}
.card .actions button,
.card .note-row .row-btns button{
  font-size:16px; line-height:1.2; min-height:40px; padding:12px 10px;
  border-radius:8px;
}
.card .note-row textarea{font-size:14px; min-height:72px; padding:10px 12px;}
.bulkbar{padding:14px 12px; gap:12px;}
.bulkbar .count{font-size:16px; min-width:9rem;}
.bulk-actions{gap:10px;}
.bulk-actions button{
  font-size:16px; line-height:1.2; min-height:44px; padding:14px 16px;
  border-radius:8px;
}
.bulkbar .fields input{font-size:14px; padding:12px 12px; min-height:44px;}
.view-controls label{font-size:15px; padding:10px 14px;}
.view-controls input{width:20px; height:20px;}
.legend{font-size:13px; margin-top:6px;}
.meta{font-size:13px;}
.filter-label{font-size:13px;}
.filter-select{font-size:16px; min-height:52px; padding:14px 16px;}
.card .actions button{font-size:15px; min-height:48px;}
"""

MOBILE_EXTRA = r"""
.cluster{max-width:480px;}
.grid{grid-template-columns:1fr 1fr;gap:8px;}
.card .body .actions{display:grid;} /* per-card Reject/Steer restored */
.card .sel-row{min-height:36px;}
.card .actions button{min-height:48px;font-size:14px;}
.filter-select{min-height:52px;font-size:15px;}
.sheet-scrim{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:40;}
.sheet-scrim.open{display:block;}
.sheet{
  display:none;position:fixed;left:0;right:0;bottom:0;z-index:50;
  max-height:92vh;background:#050a07;border:3px solid var(--line-strong);
  border-radius:14px 14px 0 0;padding:10px 10px calc(12px + var(--safe-b));
  box-shadow:0 -8px 28px rgba(0,0,0,.65);flex-direction:column;gap:8px;
}
.sheet.open{display:flex;}
.sheet-x{position:absolute;top:6px;right:8px;width:40px;height:40px;border:0;background:transparent;
  color:var(--green-bright);font:700 28px/1 var(--mono);cursor:pointer;}
.sheet-stage{position:relative;flex:1;min-height:40vh;display:flex;align-items:center;justify-content:center;
  touch-action:pan-y;overflow:hidden;border:2px solid var(--line);border-radius:8px;background:#030805;}
.sheet-stage img{max-width:100%;max-height:55vh;object-fit:contain;user-select:none;-webkit-user-select:none;}
.sheet-meta{font-size:12px;color:var(--muted);text-align:center;}
.sheet-actions{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
.sheet-actions .steer{grid-column:1/-1;}
.bulkbar{position:static;} /* never floating fixed on mobile either */
"""

SHARED_JS = r"""
const STATUS_TABS = ['needs-review','finalists','recently-rejected','archived','approved','rejected','training','in-game','all'];
const CLASS_ORDER = ['floor','wall','door','prop','decal','rubble','backdrop','character','portrait','ui','relic','fx','other'];
const CLASS_LABELS = {floor:'floor',wall:'wall',door:'door',prop:'prop',decal:'decal',rubble:'rubble',backdrop:'backdrop',character:'characters/enemies',portrait:'portraits',ui:'UI',relic:'relic',fx:'effects',other:'other'};
const STATUS_DROP_OPTS = [
  {v:'all', label:'all'},
  {v:'needs-review', label:'unreviewed'},
  {v:'training', label:'steered'},
  {v:'rejected', label:'rejected'},
  {v:'recently-rejected', label:'recently rejected'},
  {v:'finalist', label:'finalist'},
  {v:'archived', label:'archived'},
  {v:'hidden-empty', label:'hidden empty'},
];
const PER_PAGE_OPTS = [10,25,50,100];
const ARCHIVE_REJECT_HOURS = (window.__ARCHIVE_REJECT_HOURS__|0) || 24;
const RECENT_MS = ARCHIVE_REJECT_HOURS*60*60*1000;
let current = 'needs-review';
let classFilter = 'all';
let actFilter = 'all';
let statusDrop = 'all';
let pageNum = 1;
let perPage = IS_MOBILE_DEFAULT();
let items = [];
let configured = false;
let classCounts = {};
let actCounts = {};
let statusDropCounts = {};
const selected = new Set();
let undoState = null;
const IS_MOBILE = !!window.__MOBILE__;

function IS_MOBILE_DEFAULT(){ return (window.__MOBILE__ || (window.matchMedia && matchMedia('(max-width:720px)').matches)) ? 10 : 50; }

function readQuery(){
  const q=new URLSearchParams(location.search);
  const st=q.get('status'); if(st && STATUS_TABS.includes(st)) current=st;
  const cl=q.get('class'); if(cl) classFilter=cl;
  const ac=q.get('act'); if(ac) actFilter=ac;
  const sd=q.get('status_drop'); if(sd) statusDrop=sd;
  const pp=parseInt(q.get('per_page')||'',10); if(PER_PAGE_OPTS.includes(pp)) perPage=pp;
  else perPage=IS_MOBILE_DEFAULT();
  const pg=parseInt(q.get('page')||'1',10); pageNum=Number.isFinite(pg)&&pg>0?pg:1;
}
function writeQuery(){
  const q=new URLSearchParams();
  if(current && current!=='needs-review') q.set('status', current);
  if(classFilter && classFilter!=='all') q.set('class', classFilter);
  if(actFilter && actFilter!=='all') q.set('act', actFilter);
  if(statusDrop && statusDrop!=='all') q.set('status_drop', statusDrop);
  if(perPage !== IS_MOBILE_DEFAULT()) q.set('per_page', String(perPage));
  if(pageNum>1) q.set('page', String(pageNum));
  const qs=q.toString();
  const next=location.pathname+(qs?('?'+qs):'');
  if(next!==location.pathname+location.search) history.replaceState(null,'',next);
}
readQuery();

function parseTs(s){ if(!s) return null; const d=new Date(s); return isNaN(d)?null:d; }
function isRecentlyRejected(it){
  if(it.status!=='rejected') return false;
  const t=parseTs(it.rejected_at||it.reviewed_at); if(!t) return false;
  return (Date.now()-t.getTime())<=RECENT_MS;
}
function toast(msg, undo){
  const el=document.getElementById('toast'); const m=document.getElementById('toastMsg');
  const u=document.getElementById('toastUndo');
  m.textContent=msg; undoState=undo||null;
  u.style.display=undo?'inline-block':'none';
  el.classList.add('show'); clearTimeout(toast._t);
  toast._t=setTimeout(()=>el.classList.remove('show'), undo?8000:3200);
}
document.addEventListener('click',e=>{
  if(e.target && e.target.id==='toastUndo' && undoState){
    const {ids,paths}=undoState; undoState=null;
    api('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'unreject',ids,paths})}).then(()=>load()).catch(err=>toast('Undo failed: '+err.message));
  }
});
async function api(path, opts){
  const r=await fetch(path, opts); const j=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(j.error||r.statusText); return j;
}
function statusMatch(it){
  if(current==='all') return it.status!=='hidden-empty';
  if(current==='recently-rejected') return isRecentlyRejected(it);
  if(current==='finalists') return it.status==='finalist';
  if(current==='rejected') return it.status==='rejected' && !isRecentlyRejected(it);
  return it.status===current;
}
function statusDropMatch(it){
  if(statusDrop==='all') return true;
  if(statusDrop==='recently-rejected') return isRecentlyRejected(it);
  if(statusDrop==='needs-review') return it.status==='needs-review';
  if(statusDrop==='training') return it.status==='training';
  if(statusDrop==='rejected') return it.status==='rejected';
  if(statusDrop==='finalist') return it.status==='finalist';
  if(statusDrop==='archived') return it.status==='archived';
  if(statusDrop==='hidden-empty') return it.status==='hidden-empty';
  return true;
}
function classMatch(it){
  if(classFilter==='all') return true;
  return (it.asset_class||'other')===classFilter;
}
function actMatch(it){
  if(actFilter==='all') return true;
  if(actFilter==='none') return it.act==null || it.act==='' || typeof it.act==='undefined';
  return String(it.act)===String(actFilter);
}
function visibleItems(){
  return items.filter(it=>statusMatch(it) && classMatch(it) && actMatch(it) && statusDropMatch(it));
}
function pagedItems(){
  const list=visibleItems();
  const pages=Math.max(1, Math.ceil(list.length/perPage));
  if(pageNum>pages) pageNum=pages;
  if(pageNum<1) pageNum=1;
  const start=(pageNum-1)*perPage;
  return {list, page:list.slice(start, start+perPage), pages, start, total:list.length};
}
function recountFilters(){
  classCounts={}; actCounts={all:0}; statusDropCounts={all:0};
  // Class counts: within current tab + act + statusDrop filters (not class)
  for(const it of items){
    if(!statusMatch(it) || !actMatch(it) || !statusDropMatch(it)) continue;
    const c=it.asset_class||'other';
    classCounts[c]=(classCounts[c]||0)+1;
  }
  // Act counts: within tab + class + statusDrop
  for(const it of items){
    if(!statusMatch(it) || !classMatch(it) || !statusDropMatch(it)) continue;
    actCounts.all=(actCounts.all||0)+1;
    if(it.act==null || it.act==='' || typeof it.act==='undefined') actCounts.none=(actCounts.none||0)+1;
    else { const k=String(it.act); actCounts[k]=(actCounts[k]||0)+1; }
  }
  // Status-drop counts: within tab + class + act
  for(const it of items){
    if(!statusMatch(it) || !classMatch(it) || !actMatch(it)) continue;
    statusDropCounts.all=(statusDropCounts.all||0)+1;
    const key = it.status==='needs-review'?'needs-review'
      : it.status==='training'?'training'
      : isRecentlyRejected(it)?'recently-rejected'
      : it.status==='rejected'?'rejected'
      : it.status==='finalist'?'finalist'
      : it.status==='archived'?'archived'
      : it.status==='hidden-empty'?'hidden-empty'
      : it.status;
    statusDropCounts[key]=(statusDropCounts[key]||0)+1;
  }
}
function updateSelUI(){
  const n=selected.size;
  document.getElementById('selCount').textContent=n+' selected';
  ['bulkApprove','bulkReject','bulkSteer'].forEach(id=>{
    const el=document.getElementById(id); if(el) el.disabled=!n;
  });
  const bn=document.getElementById('bulkNoteBtn'); const bu=document.getElementById('bulkUnreject');
  if(bn) bn.disabled=!n; if(bu) bu.disabled=!n;
  const bf=document.getElementById('bulkFinal'); const bb=document.getElementById('bulkBack');
  if(bf){ bf.disabled=!n; bf.style.display=current==='finalists'?'':'none'; }
  if(bb){ bb.disabled=!n; bb.style.display=current==='finalists'?'':'none'; }
  const sa=document.getElementById('selectAll');
  if(sa){
    const page=pagedItems().page;
    sa.checked=page.length>0 && page.every(it=>selected.has(it.id));
  }
}
function toggleSelect(id, on){
  if(on) selected.add(id); else selected.delete(id);
  const card=document.querySelector('.card[data-id="'+CSS.escape(id)+'"]');
  if(card){ card.classList.toggle('selected',selected.has(id));
    const cb=card.querySelector('.selbox'); if(cb) cb.checked=selected.has(id); }
  updateSelUI();
}
function tabLabel(s){
  if(s==='needs-review') return 'Needs review';
  if(s==='finalists') return 'Finalists';
  if(s==='recently-rejected') return 'Recently rejected';
  if(s==='hidden-empty') return 'Hidden empty';
  return s.charAt(0).toUpperCase()+s.slice(1);
}
function renderTabs(){
  const tabs=document.getElementById('tabs'); tabs.innerHTML='';
  for(const s of STATUS_TABS){
    const b=document.createElement('button');
    b.className='rtab'+(s===current?' on':'')+(s==='recently-rejected'?' recent':'');
    const base = s==='all'?items.filter(i=>i.status!=='hidden-empty')
      : s==='recently-rejected'?items.filter(isRecentlyRejected)
      : s==='finalists'?items.filter(i=>i.status==='finalist')
      : s==='rejected'?items.filter(i=>i.status==='rejected'&&!isRecentlyRejected(i))
      : items.filter(i=>i.status===s);
    const count = base.filter(it=>classMatch(it)&&actMatch(it)&&statusDropMatch(it)).length;
    b.textContent=`${tabLabel(s)} (${count})`;
    b.onclick=()=>{ current=s; pageNum=1; selected.clear(); writeQuery(); recountFilters(); renderTabs(); renderFilters(); renderGrid(); updateSelUI(); };
    tabs.appendChild(b);
  }
}
function fillSelect(sel, options, currentVal, onChange){
  sel.innerHTML='';
  for(const opt of options){
    const o=document.createElement('option');
    o.value=opt.v; o.textContent=opt.label;
    if(String(opt.v)===String(currentVal)) o.selected=true;
    sel.appendChild(o);
  }
  sel.onchange=()=>onChange(sel.value);
}
function renderFilters(){
  recountFilters();
  const classSel=document.getElementById('filterClass');
  const actSel=document.getElementById('filterAct');
  const statusSel=document.getElementById('filterStatus');
  const perSel=document.getElementById('filterPerPage');
  if(!classSel||!actSel||!statusSel) return;
  const classOpts=[{v:'all', label:`all (${Object.values(classCounts).reduce((a,b)=>a+b,0)})`}];
  for(const c of CLASS_ORDER){
    const n=classCounts[c]||0;
    if(n===0 && classFilter!==c) continue;
    classOpts.push({v:c, label:`${CLASS_LABELS[c]||c} (${n})`});
  }
  fillSelect(classSel, classOpts, classFilter, v=>{ classFilter=v; pageNum=1; selected.clear(); writeQuery(); renderTabs(); renderFilters(); renderGrid(); updateSelUI(); });

  const actOpts=[{v:'all', label:`all (${actCounts.all||0})`}];
  for(const a of Object.keys(actCounts).filter(k=>k!=='all'&&k!=='none').sort((a,b)=>Number(a)-Number(b))){
    actOpts.push({v:a, label:`Act ${a} (${actCounts[a]||0})`});
  }
  if(actCounts.none) actOpts.push({v:'none', label:`No act (${actCounts.none})`});
  fillSelect(actSel, actOpts, actFilter, v=>{ actFilter=v; pageNum=1; selected.clear(); writeQuery(); renderTabs(); renderFilters(); renderGrid(); updateSelUI(); });

  const stOpts=STATUS_DROP_OPTS.map(o=>{
    const n = o.v==='all' ? (statusDropCounts.all||0) : (statusDropCounts[o.v]||0);
    return {v:o.v, label:`${o.label} (${n})`};
  }).filter(o=>o.v==='all' || (statusDropCounts[o.v]||0)>0 || statusDrop===o.v);
  fillSelect(statusSel, stOpts, statusDrop, v=>{ statusDrop=v; pageNum=1; selected.clear(); writeQuery(); renderTabs(); renderFilters(); renderGrid(); updateSelUI(); });

  if(perSel){
    fillSelect(perSel, PER_PAGE_OPTS.map(n=>({v:String(n), label:String(n)+'/page'})), String(perPage), v=>{
      perPage=parseInt(v,10)||IS_MOBILE_DEFAULT(); pageNum=1; writeQuery(); renderGrid(); updateSelUI();
    });
  }
}
function escapeHtml(s){ return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function renderPager(pages, total){
  const pager=document.getElementById('pager'); if(!pager) return;
  if(pages<=1 && total<=perPage){ pager.innerHTML=''; return; }
  pager.innerHTML=`
    <button type="button" class="page-btn" id="pagePrev" ${pageNum<=1?'disabled':''}>Prev</button>
    <span class="page-meta">Page ${pageNum} / ${pages} · ${total} items</span>
    <button type="button" class="page-btn" id="pageNext" ${pageNum>=pages?'disabled':''}>Next</button>`;
  const prev=document.getElementById('pagePrev'); const next=document.getElementById('pageNext');
  if(prev) prev.onclick=()=>{ if(pageNum>1){ pageNum--; writeQuery(); renderGrid(); updateSelUI(); window.scrollTo(0,0);} };
  if(next) next.onclick=()=>{ if(pageNum<pages){ pageNum++; writeQuery(); renderGrid(); updateSelUI(); window.scrollTo(0,0);} };
}
function cardActionsHtml(it, mode){
  if(mode==='recent'){
    return `<div class="actions">
      <button type="button" class="unreject" data-act="unreject" data-id="${it.id}">Undo reject</button>
      <button type="button" class="note-btn" data-act="note" data-id="${it.id}">Save note</button>
    </div>
    <div class="note-row"><textarea class="item-note" data-id="${it.id}" placeholder="Reject note (optional)">${escapeHtml(it.note||'')}</textarea></div>`;
  }
  if(mode==='finalist'){
    return `<div class="actions">
      <button type="button" class="pick-final" data-act="final" data-id="${it.id}">Pick as final</button>
      <button type="button" class="back-review" data-act="back-to-review" data-id="${it.id}">Back to review</button>
    </div>`;
  }
  return `<div class="actions">
    <button type="button" class="approve" data-act="finalist" data-id="${it.id}">Finalist</button>
    <button type="button" class="reject" data-act="reject" data-id="${it.id}">Reject</button>
    <button type="button" class="steer" data-act="steer" data-id="${it.id}">Steer</button>
  </div>
  <div class="note-row">
    <textarea class="item-note" data-id="${it.id}" placeholder="Note (optional)">${escapeHtml(it.note||'')}</textarea>
    <div class="row-btns"><button type="button" class="note-btn" data-act="note" data-id="${it.id}">Save note</button></div>
  </div>`;
}
function renderGrid(){
  const grid=document.getElementById('grid'); const empty=document.getElementById('empty');
  grid.innerHTML='';
  const {list, page, pages, total}=pagedItems();
  empty.style.display=total?'none':'block';
  const recentMode=current==='recently-rejected';
  const finalistMode=current==='finalists';
  let lastAsset=null;
  const ordered=finalistMode?[...page].sort((a,b)=>String(a.asset||'').localeCompare(String(b.asset||''))||String(a.id).localeCompare(String(b.id))):page;
  for(const it of ordered){
    if(finalistMode){
      const a=it.asset||'unknown';
      if(a!==lastAsset){
        lastAsset=a;
        const h=document.createElement('div');
        h.className='asset-group';
        h.textContent=a;
        grid.appendChild(h);
      }
    }
    const card=document.createElement('div');
    card.className='card'+(selected.has(it.id)?' selected':'');
    card.dataset.id=it.id;
    const label=it.label||((it.asset||'')+' · '+(it.round||''));
    const mode=recentMode?'recent':(finalistMode?'finalist':'review');
    card.innerHTML=`
      <img loading="lazy" decoding="async" width="256" height="256" src="${it.thumb_url}" alt="${escapeHtml(it.id)}" data-full="${it.image_url}"/>
      <div class="body">
        <div class="round">${escapeHtml(label)}</div>
        <div class="id">${it.cell||''} ${it.key?'('+it.key+')':''}<br/>${escapeHtml(it.id)}${recentMode?('<br/>rejected '+(it.rejected_at||'')):''}</div>
        ${it.note&&recentMode?`<div class="existing-note">Note: ${escapeHtml(it.note)}</div>`:''}
        <div class="sel-row"><input class="selbox" type="checkbox" ${selected.has(it.id)?'checked':''} aria-label="Select ${escapeHtml(it.id)}"/></div>
        ${cardActionsHtml(it, mode)}
      </div>`;
    const cb=card.querySelector('.selbox');
    cb.addEventListener('click',e=>{ e.stopPropagation(); toggleSelect(it.id, cb.checked); });
    const img=card.querySelector('img');
    img.addEventListener('click',e=>{
      e.preventDefault();
      if(IS_MOBILE){ openSheet(list.findIndex(x=>x.id===it.id)); return; }
      toggleSelect(it.id, !selected.has(it.id));
    });
    grid.appendChild(card);
  }
  renderPager(pages, total);
  document.getElementById('meta').textContent=
    `${items.length} total · ${total} match · page ${pageNum}/${pages} (${perPage}/page) · webhook ${configured?'ready':'not configured'}`;
  updateSelUI();
}
async function doAction(action,id){
  const item=items.find(x=>x.id===id);
  let note='', destination='';
  if(action==='note'){
    const ta=document.querySelector(`textarea.item-note[data-id="${CSS.escape(id)}"]`);
    note=ta?ta.value.trim():'';
    if(!note){ toast('Enter a note first'); return; }
  }
  try{
    await api('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action,id,path:item?item.spark_path:'',note,destination})});
    selected.delete(id);
    const undoable=['finalist','final','approve','reject','steer','back-to-review'].includes(action);
    toast(action+': ok', undoable?{ids:[id],paths:[item?item.spark_path||'':'']}:null);
    await load();
  }catch(e){ toast('Error: '+e.message); }
}
async function doBulk(action){
  const ids=[...selected]; if(!ids.length){ toast('Nothing selected'); return; }
  let note=document.getElementById('bulkNote').value.trim();
  const destination=document.getElementById('bulkDest').value.trim();
  if(action==='note' && !note){ toast('Enter a shared note first'); return; }
  if(action==='reject') note='';
  const paths=ids.map(id=>{ const it=items.find(x=>x.id===id); return it?it.spark_path||'':''; });
  try{
    await api('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action,ids,paths,note,destination})});
    const undoable=['finalist','final','approve','reject','steer','back-to-review'].includes(action);
    toast('bulk '+action+': '+ids.length, undoable?{ids:[...ids],paths:[...paths]}:null);
    selected.clear(); await load();
  }catch(e){ toast('Error: '+e.message); }
}

/* Mobile bottom-sheet viewer */
let sheetIdx=-1;
function openSheet(idx){
  if(!IS_MOBILE) return;
  const list=visibleItems(); if(idx<0||idx>=list.length) return;
  sheetIdx=idx; const it=list[idx];
  const sheet=document.getElementById('sheet'); const scrim=document.getElementById('sheetScrim');
  document.getElementById('sheetImg').src=it.image_url;
  document.getElementById('sheetMeta').textContent=(it.label||it.asset||'')+' · '+(it.cell||'')+' · '+it.id;
  sheet.dataset.id=it.id;
  const rev=document.getElementById('sheetActionsReview');
  const fin=document.getElementById('sheetActionsFinalist');
  if(rev&&fin){
    const fm=current==='finalists';
    rev.style.display=fm?'none':'';
    fin.style.display=fm?'':'none';
  }
  sheet.classList.add('open'); scrim.classList.add('open');
}
function closeSheet(){
  document.getElementById('sheet').classList.remove('open');
  document.getElementById('sheetScrim').classList.remove('open');
  sheetIdx=-1;
}
function sheetNav(dir){
  const list=visibleItems(); if(!list.length) return;
  openSheet((sheetIdx+dir+list.length)%list.length);
}
async function sheetAct(action){
  const id=document.getElementById('sheet').dataset.id; if(!id) return;
  await doAction(action, id);
  const list=visibleItems();
  if(!list.length){ closeSheet(); return; }
  if(sheetIdx>=list.length) sheetIdx=list.length-1;
  openSheet(sheetIdx);
}

function wire(){
  document.getElementById('grid').addEventListener('click',e=>{
    const btn=e.target.closest('button[data-act]'); if(!btn) return;
    e.stopPropagation(); doAction(btn.dataset.act, btn.dataset.id);
  });
  document.getElementById('selectAll').addEventListener('change',e=>{
    const on=e.target.checked; for(const it of pagedItems().page) toggleSelect(it.id,on);
  });
  document.getElementById('clearSel').onclick=()=>{
    selected.clear();
    document.querySelectorAll('.card.selected').forEach(c=>c.classList.remove('selected'));
    document.querySelectorAll('.selbox').forEach(c=>{c.checked=false;});
    updateSelUI();
  };
  document.getElementById('bulkApprove').onclick=()=>doBulk('finalist');
  const bf=document.getElementById('bulkFinal'); if(bf) bf.onclick=()=>doBulk('final');
  const bb=document.getElementById('bulkBack'); if(bb) bb.onclick=()=>doBulk('back-to-review');
  document.getElementById('bulkReject').onclick=()=>doBulk('reject');
  document.getElementById('bulkSteer').onclick=()=>doBulk('steer');
  const bn=document.getElementById('bulkNoteBtn'); if(bn) bn.onclick=()=>doBulk('note');
  const bu=document.getElementById('bulkUnreject'); if(bu) bu.onclick=()=>doBulk('unreject');
  const copy=document.getElementById('copyHttps');
  if(copy) copy.onclick=async()=>{
    const url=document.getElementById('httpsLink').href;
    try{ await navigator.clipboard.writeText(url); toast('Copied HTTPS URL'); }
    catch(e){ toast('Copy failed'); }
  };
  if(IS_MOBILE){
    document.getElementById('sheetClose').onclick=closeSheet;
    document.getElementById('sheetScrim').onclick=closeSheet;
    document.getElementById('sheetApprove').onclick=()=>sheetAct('finalist');
    const sheetFinal=document.getElementById('sheetFinal'); if(sheetFinal) sheetFinal.onclick=()=>sheetAct('final');
    const sheetBack=document.getElementById('sheetBack'); if(sheetBack) sheetBack.onclick=()=>sheetAct('back-to-review');
    document.getElementById('sheetReject').onclick=()=>sheetAct('reject');
    document.getElementById('sheetSteer').onclick=()=>sheetAct('steer');
    const stage=document.getElementById('sheetStage');
    let x0=null;
    stage.addEventListener('touchstart',e=>{ x0=e.changedTouches[0].clientX; },{passive:true});
    stage.addEventListener('touchend',e=>{
      if(x0==null) return; const dx=e.changedTouches[0].clientX-x0; x0=null;
      if(Math.abs(dx)<40) return; sheetNav(dx<0?1:-1);
    },{passive:true});
  }
}
async function load(){
  const data=await api('/api/items');
  items=data.items||[]; configured=!!data.configured;
  for(const id of [...selected]){ if(!items.some(i=>i.id===id)) selected.delete(id); }
  document.getElementById('banner').classList.toggle('show', !configured);
  recountFilters(); renderTabs(); renderFilters(); renderGrid();
}
wire(); load(); setInterval(load, 30000);

"""


def page(mobile: bool) -> str:
    https = public_https_url()
    mode = "true" if mobile else "false"
    hours = archive_reject_hours()
    link_other = '<a class="desk-link" href="/">Desktop</a>' if mobile else '<a class="desk-link" href="/m">Mobile</a>'
    extra = MOBILE_EXTRA if mobile else DESKTOP_EXTRA
    sheet = ""
    if mobile:
        sheet = """
<div id="sheetScrim" class="sheet-scrim"></div>
<div id="sheet" class="sheet" role="dialog" aria-modal="true">
  <button type="button" class="sheet-x" id="sheetClose" aria-label="Close">×</button>
  <div class="sheet-stage" id="sheetStage"><img id="sheetImg" alt=""/></div>
  <div class="sheet-meta" id="sheetMeta"></div>
  <div class="sheet-actions" id="sheetActionsReview">
    <button type="button" class="approve" id="sheetApprove">Finalist</button>
    <button type="button" class="reject" id="sheetReject">Reject</button>
    <button type="button" class="steer" id="sheetSteer">Steer this direction</button>
  </div>
  <div class="sheet-actions" id="sheetActionsFinalist" style="display:none">
    <button type="button" class="pick-final" id="sheetFinal">Pick as final</button>
    <button type="button" class="back-review" id="sheetBack">Back to review</button>
  </div>
</div>"""
    title = "Digital Veil — Art Review" + (" · Mobile" if mobile else "")
    return f"""<!doctype html><html lang=en><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#030505">
<title>{title}</title>
<style>{THEME_CSS}{extra}</style>
</head><body>
<div class=cluster>
  <div class=hdr>
    <div class=hdr-top>
      <h1>Digital Veil — Art Review</h1>
      {link_other}
    </div>
    <div class="https-bar">
      <a id="httpsLink" href="{https}">{https}</a>
      <button type="button" class="copy-btn" id="copyHttps">Copy</button>
    </div>
    <div id="banner" class="banner">Webhook not configured — clicks logged locally. Add url to gallery/config.json (key already set).</div>
    <nav class="range-tog" id="tabs" role="tablist" aria-label="Status"></nav>
    <div class="filter-row" id="filterRow">
      <div class="filter-field">
        <label class="filter-label" for="filterStatus">Status</label>
        <select class="filter-select" id="filterStatus" aria-label="Status"></select>
      </div>
      <div class="filter-field">
        <label class="filter-label" for="filterClass">By asset type</label>
        <select class="filter-select" id="filterClass" aria-label="Asset type"></select>
      </div>
      <div class="filter-field">
        <label class="filter-label" for="filterAct">By Act</label>
        <select class="filter-select" id="filterAct" aria-label="Act"></select>
      </div>
      <div class="filter-field" style="max-width:140px">
        <label class="filter-label" for="filterPerPage">Per page</label>
        <select class="filter-select" id="filterPerPage" aria-label="Per page"></select>
      </div>
    </div>
    <div class="view-controls"><label><input type="checkbox" id="selectAll"/> Select all on page</label></div>
    <div class="meta" id="meta"></div>
    <div class="legend"><b class="ship">Finalist</b> = shortlist · <b class="final">Pick as final</b> = ship · <b class="steer">Steer</b> = train this way · <b class="reject">Reject</b> = no</div>
  </div>
  <div class="panel">
    <div class="grid" id="grid"></div>
    <div class="pager" id="pager"></div>
    <div class="empty" id="empty" style="display:none">No items in this filter.</div>
  </div>
  <div class="toast" id="toast"><span id="toastMsg"></span><button type="button" id="toastUndo" style="display:none">Undo</button></div>
  <div class="bulkbar" id="bulkbar">
    <div class="fields">
      <input type="text" id="bulkNote" placeholder="Shared note (optional)"/>
      <input type="text" id="bulkDest" placeholder="Shared destination (optional)"/>
    </div>
    <div class="row">
      <div class="count" id="selCount">0 selected</div>
      <div class="bulk-actions">
        <button type="button" class="clear-sel" id="clearSel">Clear</button>
        <button type="button" class="steer" id="bulkSteer" disabled>Steer this direction</button>
        <button type="button" class="approve" id="bulkApprove" disabled>Finalist</button>
        <button type="button" class="reject" id="bulkReject" disabled>Reject all</button>
        <button type="button" class="pick-final" id="bulkFinal" disabled style="display:none">Pick as final</button>
        <button type="button" class="back-review" id="bulkBack" disabled style="display:none">Back to review</button>
        <button type="button" class="note-btn" id="bulkNoteBtn" disabled style="display:none">Add note to selected</button>
        <button type="button" class="unreject" id="bulkUnreject" disabled style="display:none">Un-reject selected</button>
      </div>
    </div>
  </div>
  <p class="foot">Sources read-only · actions apply instantly</p>
</div>
{sheet}
<script>window.__MOBILE__={mode};window.__ARCHIVE_REJECT_HOURS__={hours};</script>
<script>{SHARED_JS}</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "ArtGallery/1.5-paging"

    def log_message(self, fmt, *args):
        print(f"[{now_iso()}] {self.address_string()} " + (fmt % args), flush=True)

    def _send(self, code: int, body: bytes, content_type: str = "text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path in ("/", "/index.html"):
            self._send(200, page(False).encode("utf-8"), "text/html; charset=utf-8")
            return
        if path in ("/m", "/mobile"):
            self._send(200, page(True).encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/qr.png":
            data = qr_png_bytes(public_https_url())
            self._send(200, data, "image/png")
            return
        if path == "/api/items":
            qs = parse_qs(parsed.query)
            status = (qs.get("status") or [None])[0]
            class_f = (qs.get("class") or [None])[0]
            act_f = (qs.get("act") or [None])[0]
            entries = load_manifest()
            for e in entries:
                ensure_class_fields(e)
            cfg = load_config()
            items = []
            for e in entries:
                if status and status != "all":
                    if status == "recently-rejected":
                        if not is_recently_rejected(e):
                            continue
                    elif status == "finalists":
                        if e.get("status") != "finalist":
                            continue
                    elif status == "rejected":
                        # non-recent rejects (recent ones live under recently-rejected)
                        if e.get("status") != "rejected" or is_recently_rejected(e):
                            continue
                    elif e.get("status") != status:
                        continue
                else:
                    # default listing still includes everything; UI hides hidden-empty on Needs review
                    pass
                if class_f and class_f != "all":
                    if (e.get("asset_class") or "other") != class_f:
                        continue
                if act_f and act_f != "all":
                    if act_f == "none":
                        if e.get("act") is not None:
                            continue
                    else:
                        try:
                            want = int(act_f)
                        except Exception:
                            want = act_f
                        if e.get("act") != want and str(e.get("act")) != str(act_f):
                            continue
                sp = e.get("spark_path") or ""
                tp = e.get("thumb_path") or ""
                items.append({
                    **{k: e.get(k) for k in (
                        "id", "round", "asset", "status", "spark_path", "source_path",
                        "source_job", "model", "size", "added_at", "cell", "key",
                        "seed", "note", "destination", "steer", "rejected_at", "reviewed_at",
                        "noted_at", "label", "asset_class", "act",
                    )},
                    "recently_rejected": is_recently_rejected(e),
                    "thumb_url": f"/file?p={quote(tp)}" if tp else f"/file?p={quote(sp)}",
                    "image_url": f"/file?p={quote(sp)}",
                })
            class_counts = {c: 0 for c in ASSET_CLASSES}
            act_counts: dict = {}
            for e in entries:
                cls = e.get("asset_class") or "other"
                if cls not in class_counts:
                    class_counts[cls] = 0
                class_counts[cls] += 1
                a = e.get("act")
                key = "none" if a is None else str(a)
                act_counts[key] = act_counts.get(key, 0) + 1
            # needs-review breakdowns for callers
            nr_class = {c: 0 for c in ASSET_CLASSES}
            nr_act: dict = {}
            for e in entries:
                if e.get("status") != "needs-review":
                    continue
                cls = e.get("asset_class") or "other"
                nr_class[cls] = nr_class.get(cls, 0) + 1
                a = e.get("act")
                key = "none" if a is None else str(a)
                nr_act[key] = nr_act.get(key, 0) + 1
            self._json(200, {
                "items": items,
                "configured": config_ready(cfg),
                "https_url": public_https_url(),
                "counts": {s: sum(1 for e in entries if e.get("status") == s) for s in STATUSES},
                "recently_rejected": sum(1 for e in entries if is_recently_rejected(e)),
                "class_counts": class_counts,
                "act_counts": act_counts,
                "needs_review_by_class": nr_class,
                "needs_review_by_act": nr_act,
                "class_labels": ASSET_CLASS_LABELS,
            })
            return
        if path == "/file":
            qs = parse_qs(parsed.query)
            p = Path((qs.get("p") or [""])[0])
            if not p.is_absolute():
                self._json(400, {"error": "absolute path required"})
                return
            if not safe_under(ART_ROOT, p):
                self._json(403, {"error": "path outside art root"})
                return
            if not p.exists() or not p.is_file():
                self._json(404, {"error": "not found"})
                return
            ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
            self._send(200, p.read_bytes(), ctype)
            return
        if path == "/api/health":
            self._json(200, {"ok": True, "time": now_iso(), "port": PORT, "https_url": public_https_url(), "archive_reject_hours": archive_reject_hours()})
            return
        if path == "/api/webhook-batch":
            self._json(200, {"ok": True, "time": now_iso(), **WEBHOOK_BATCHER.status()})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/action":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._json(400, {"error": "invalid json"})
            return
        action = (payload.get("action") or "").strip().lower()
        note = payload.get("note") or ""
        destination = payload.get("destination") or ""
        if action not in ("approve", "finalist", "final", "reject", "note", "steer", "unreject", "undo", "back-to-review"):
            self._json(400, {"error": "action must be finalist|final|reject|note|steer|unreject|undo|back-to-review"})
            return
        if action == "reject":
            note = ""
        ids = payload.get("ids")
        paths = payload.get("paths")
        bulk = isinstance(ids, list) and len(ids) > 0
        entries = load_manifest()
        updated = []
        if bulk:
            if not isinstance(paths, list):
                paths = []
            path_by_id = {}
            for i, eid in enumerate(ids):
                if i < len(paths) and paths[i]:
                    path_by_id[eid] = paths[i]
            resolved_paths = []
            for entry_id in ids:
                idx, entry = find_entry(entries, entry_id)
                if entry is None:
                    continue
                path = path_by_id.get(entry_id) or entry.get("spark_path") or ""
                apply_action(entry, action, note, destination)
                entries[idx] = entry
                updated.append(entry)
                resolved_paths.append(path)
            save_manifest(entries)
            hook_body = build_hook(action, True, ids, resolved_paths, note, destination)
        else:
            entry_id = payload.get("id") or ""
            path = payload.get("path") or ""
            if not entry_id:
                self._json(400, {"error": "id or ids required"})
                return
            idx, entry = find_entry(entries, entry_id)
            if entry is None:
                self._json(404, {"error": "unknown id"})
                return
            if not path:
                path = entry.get("spark_path") or ""
            apply_action(entry, action, note, destination)
            entries[idx] = entry
            save_manifest(entries)
            updated.append(entry)
            hook_body = build_hook(action, False, [], [], note, destination, entry_id, path)
        cfg = load_config()
        # Persist click immediately; webhook is batched by background thread.
        append_click({
            "ts": now_iso(),
            "payload": hook_body,
            "webhook_ok": None,
            "webhook_detail": "queued",
            "configured": config_ready(cfg),
            "bulk": bulk,
            "webhook_queued": True,
        })
        WEBHOOK_BATCHER.notify_click()
        self._json(200, {
            "ok": True,
            "webhook": "queued",
            "webhook_detail": "batched",
            "bulk": bulk,
            "updated": len(updated),
            "entry": updated[0] if len(updated) == 1 else None,
            "entries": updated if bulk else None,
        })


def main():
    ART_ROOT.mkdir(parents=True, exist_ok=True)
    (ART_ROOT / "training").mkdir(parents=True, exist_ok=True)
    (ART_ROOT / "finalists").mkdir(parents=True, exist_ok=True)
    (ART_ROOT / "approved").mkdir(parents=True, exist_ok=True)
    (ART_ROOT / "archived").mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text("{}\n")
    if not CLICKS.exists():
        CLICKS.write_text("")
    try:
        entries = load_manifest()
        stats = classify_empty_and_archive(entries)
        print(
            f"gallery startup maintenance archive_hours={archive_reject_hours()} "
            f"archived={stats['archived']} hidden_empty={stats['hidden_empty']} "
            f"backfilled_rejected_at={stats['backfilled_rejected_at']} thumbs_fixed={stats['thumbs_fixed']}",
            flush=True,
        )
    except Exception as e:
        print(f"startup maintenance failed: {e}", flush=True)
    WEBHOOK_BATCHER.start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"art-gallery listening on http://{HOST}:{PORT}/  https_url={public_https_url()}  art_root={ART_ROOT}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        WEBHOOK_BATCHER.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
