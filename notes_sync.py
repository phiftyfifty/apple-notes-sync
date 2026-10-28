#!/usr/bin/env python3
"""One-way sync from Apple Notes into an Obsidian vault.

Apple Notes is never written to. Each note becomes
<vault>/<dest>/<folder path>/<title>.md with its Apple id and a hash of what
was written in the frontmatter, so the vault itself is the sync state.
A note edited in Obsidian is never overwritten: it is reported as a conflict
and the run exits nonzero.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_VAULT = Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents/zettelkasten"
DEFAULT_DEST = "Apple Notes"
ATTACH_DIR = "Attachments/Apple Notes"
SKIP_FOLDERS = {"Recently Deleted"}
BAD_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]')
IMG_RE = re.compile(r'<img[^>]*?src="data:([^;"]+);base64,([^"]+)"[^>]*>', re.S)
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/heic": "jpg",
       "image/tiff": "jpg", "image/webp": "webp"}

INDEX_JS = r"""
function run() {
  const Notes = Application("Notes");
  const out = [];
  for (const acct of Notes.accounts()) {
    const folders = acct.folders();
    const byId = {};
    for (const f of folders) byId[f.id()] = {name: f.name(), parent: f.container().id()};
    for (const f of folders) {
      const parts = [];
      for (let id = f.id(); byId[id]; id = byId[id].parent) parts.unshift(byId[id].name);
      if (acct.name() !== "iCloud") parts.unshift(acct.name());
      const ns = f.notes;
      const ids = ns.id();
      if (!ids.length) continue;
      const names = ns.name(), mods = ns.modificationDate(), created = ns.creationDate(), locked = ns.passwordProtected();
      for (let i = 0; i < ids.length; i++) {
        out.push({id: ids[i], name: names[i], modified: mods[i].toISOString(), created: created[i].toISOString(),
                  locked: locked[i], folder: parts.join("/")});
      }
    }
  }
  return JSON.stringify(out);
}
"""

BODY_JS = r"""
function run(argv) {
  const n = Application("Notes").notes.byId(argv[0]);
  return JSON.stringify({body: n.body(), attachments: n.attachments().length});
}
"""


@dataclass
class Note:
    id: str
    name: str
    modified: str
    created: str
    locked: bool
    folder: str


@dataclass
class Report:
    new: list = field(default_factory=list)
    updated: list = field(default_factory=list)
    moved: list = field(default_factory=list)
    unchanged: int = 0
    conflicts: list = field(default_factory=list)
    locked: list = field(default_factory=list)
    gone: list = field(default_factory=list)
    lost_attachments: list = field(default_factory=list)


def osascript(js, *args):
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", js, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"osascript failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


def fetch_index():
    return [Note(**n) for n in osascript(INDEX_JS) if n["folder"].split("/")[-1] not in SKIP_FOLDERS]


def safe_name(s, limit=120):
    s = BAD_CHARS.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:limit].rstrip(" .")


def note_paths(notes):
    """Relative .md path per note id. Same-title notes in a folder: the oldest keeps the bare name."""
    paths, taken = {}, set()
    for n in sorted(notes, key=lambda n: (n.created, n.id)):
        folder = "/".join(safe_name(p) or "Untitled folder" for p in n.folder.split("/"))
        title = safe_name(n.name) or f"Untitled {n.created[:10]}"
        rel = f"{folder}/{title}.md"
        if rel.lower() in taken:
            rel = f"{folder}/{title} ({n.id.rsplit('/', 1)[-1]}).md"
        taken.add(rel.lower())
        paths[n.id] = rel
    return paths


def split_frontmatter(text):
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    fm = {}
    for line in text[4:end].splitlines():
        k, sep, v = line.partition(": ")
        if sep:
            fm[k] = v
    return fm, text[end + 5:]


def body_hash(body):
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def render(note, body):
    fm = (f"---\ntags: [apple-notes]\ncreated: {note.created[:10]}\nmodified: {note.modified}\n"
          f"apple_id: {note.id}\napple_folder: {note.folder}\nsynced_hash: {body_hash(body)}\n---\n")
    return fm + body


def scan_vault(root):
    """apple_id -> (relative path, frontmatter, edited) for every synced file under root."""
    found = {}
    if not root.exists():
        return found
    for p in root.rglob("*.md"):
        fm, body = split_frontmatter(p.read_text())
        if "apple_id" in fm:
            found[fm["apple_id"]] = (p.relative_to(root).as_posix(), fm, body_hash(body) != fm.get("synced_hash"))
    return found


def save_image(mime, data, attach_root):
    raw = base64.b64decode(data)
    ext = EXT.get(mime, mime.split("/")[-1])
    name = f"{hashlib.sha256(raw).hexdigest()[:16]}.{ext}"
    out = attach_root / name
    if not out.exists():
        attach_root.mkdir(parents=True, exist_ok=True)
        if mime in ("image/heic", "image/tiff"):
            with tempfile.NamedTemporaryFile(suffix="." + mime.split("/")[-1]) as tmp:
                tmp.write(raw)
                tmp.flush()
                r = subprocess.run(["sips", "-s", "format", "jpeg", tmp.name, "--out", str(out)], capture_output=True, text=True)
                if r.returncode != 0:
                    raise SystemExit(f"sips could not convert a {mime} image: {r.stderr.strip()}")
        else:
            out.write_bytes(raw)
    return name


def html_to_md(html, title, attach_root):
    """Markdown body and the number of images extracted."""
    images = []

    def repl(m):
        images.append(save_image(m.group(1), m.group(2), attach_root))
        return f"<p>APPLENOTESIMG{len(images) - 1}X</p>"

    html = IMG_RE.sub(repl, html)
    html = re.sub(r"</h([1-6])>(\s*)<h\1>", "", html)
    html = re.sub(r"<div>\s*<br\s*/?>\s*</div>", "", html)
    r = subprocess.run(["pandoc", "-f", "html", "-t", "gfm-raw_html", "--wrap=none"], input=html, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"pandoc failed: {r.stderr.strip()}")
    md = r.stdout
    for i, name in enumerate(images):
        md = md.replace(f"APPLENOTESIMG{i}X", f"![[{ATTACH_DIR}/{name}]]")
    md = re.sub(r"^(\s*[-*]|\s*\d+\.)?\s*\\$", "", md, flags=re.M)
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = strip_title(md, title)
    return f"# {title.strip()}\n\n{md.strip()}\n" if title.strip() else md.strip() + "\n", len(images)


def strip_title(md, title):
    lines = md.lstrip("\n").split("\n")
    norm = lambda s: re.sub(r"[\s*_#\\]+", "", s).lower()
    if lines and title.strip() and norm(lines[0]) == norm(title):
        lines = lines[1:]
    return "\n".join(lines)


def sync(vault, dest, folders, dry_run, out=sys.stdout):
    root = vault / dest
    attach_root = vault / ATTACH_DIR
    notes = fetch_index()
    if folders:
        notes = [n for n in notes if n.folder in folders or n.folder.split("/")[-1] in folders]
    paths = note_paths(notes)
    synced = scan_vault(root)
    rep = Report()
    live = {n.id for n in notes}

    for n in notes:
        rel = paths[n.id]
        if n.locked:
            rep.locked.append(f"{n.folder}/{n.name}")
            continue
        prev = synced.get(n.id)
        if prev and prev[2]:
            rep.conflicts.append(prev[0])
            continue
        if prev and prev[1].get("modified") == n.modified:
            if prev[0] == rel:
                rep.unchanged += 1
            else:
                rep.moved.append(f"{prev[0]} -> {rel}")
                if not dry_run:
                    (root / rel).parent.mkdir(parents=True, exist_ok=True)
                    os.rename(root / prev[0], root / rel)
            continue
        target = root / rel
        if target.exists() and not (prev and prev[0] == rel):
            rep.conflicts.append(f"{rel} (file exists and is not this note)")
            continue
        (rep.updated if prev else rep.new).append(rel)
        if dry_run:
            continue
        data = osascript(BODY_JS, n.id)
        body, n_images = html_to_md(data["body"], n.name, attach_root)
        if data["attachments"] > n_images:
            rep.lost_attachments.append(f"{rel} ({data['attachments'] - n_images} not exported)")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(n, body))
        if prev and prev[0] != rel:
            (root / prev[0]).unlink()

    rep.gone = sorted(p for i, (p, _, _) in synced.items() if i not in live and not folders)
    print_report(rep, dry_run, out)
    return 1 if rep.conflicts else 0


def print_report(rep, dry_run, out):
    verb = "would " if dry_run else ""
    print(f"{verb}add {len(rep.new)}, {verb}update {len(rep.updated)}, {verb}move {len(rep.moved)}, unchanged {rep.unchanged}", file=out)
    for label, items in [("Moved", rep.moved), ("Conflicts (edited in Obsidian, left alone)", rep.conflicts),
                         ("Locked notes (not exported)", rep.locked),
                         ("Attachments that did not come through", rep.lost_attachments),
                         ("Gone from Apple Notes (kept in the vault)", rep.gone)]:
        if items:
            print(f"\n{label}: {len(items)}", file=out)
            for i in items:
                print(f"  {i}", file=out)
    if dry_run:
        print("\nDry run: nothing written. Attachment losses are only known on a real run.", file=out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    ap.add_argument("--dest", default=DEFAULT_DEST, help="folder inside the vault (default: %(default)s)")
    ap.add_argument("--folder", action="append", default=[], help="only sync this Apple Notes folder (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    a = ap.parse_args()
    if not a.vault.is_dir():
        raise SystemExit(f"vault not found: {a.vault}")
    sys.exit(sync(a.vault, a.dest, set(a.folder), a.dry_run))


if __name__ == "__main__":
    main()
