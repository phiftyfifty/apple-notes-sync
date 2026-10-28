# apple-notes-sync

One-way sync from Apple Notes into the zettelkasten vault.
Apple Notes is only read, never written.

```sh
python3 notes_sync.py --dry-run              # what would change, writes nothing
python3 notes_sync.py                        # sync everything
python3 notes_sync.py --folder Writing       # one Apple Notes folder (repeatable)
python3 -m unittest test_notes_sync          # tests
```

Needs `pandoc` (brew) and Automation permission for the terminal to control Notes.

## Where things go

Each note becomes `<vault>/Apple Notes/<folder path>/<title>.md`, nested folders as subfolders.
Images inside notes are saved to `Attachments/Apple Notes/` and embedded with `![[...]]`; HEIC and TIFF are converted to JPEG.
`Recently Deleted` is skipped.

## How it stays in sync

The vault is the state.
Each synced file's frontmatter holds `apple_id`, `modified` and `synced_hash` (a hash of the body as written).
A rerun only fetches notes whose Apple modified time changed, and moves files when a note is renamed or changes folder.

## What it refuses to do

- Overwrite a file you edited in Obsidian. It is listed as a conflict and the run exits 1. Move your edits elsewhere, or delete the file to take the Apple version again.
- Delete anything. Notes deleted in Apple Notes are listed as gone and stay in the vault.
- Export locked notes, or attachments that are not inline images (PDFs, scans, drawings). Both are listed by name.
