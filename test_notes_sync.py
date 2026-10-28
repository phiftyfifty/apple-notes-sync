import tempfile
import unittest
from pathlib import Path

import notes_sync as ns


def note(id, name, folder="Writing", created="2024-01-01T00:00:00.000Z"):
    return ns.Note(id=id, name=name, modified="2024-01-02T00:00:00.000Z", created=created, locked=False, folder=folder)


class NotesSyncTest(unittest.TestCase):
    def test_unsafe_filename_characters_are_replaced(self):
        self.assertEqual(ns.safe_name('a/b: c? "d"'), "a b c d")

    def test_same_title_in_a_folder_gets_an_id_suffix_and_oldest_keeps_the_bare_name(self):
        paths = ns.note_paths([note("x/p2", "Shopping", created="2024-02-01"), note("x/p1", "Shopping")])
        self.assertEqual(paths["x/p1"], "Writing/Shopping.md")
        self.assertEqual(paths["x/p2"], "Writing/Shopping (p2).md")

    def test_untitled_note_falls_back_to_its_created_date(self):
        self.assertEqual(ns.note_paths([note("x/p1", "  ")])["x/p1"], "Writing/Untitled 2024-01-01.md")

    def test_split_headings_merge_and_duplicate_title_is_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            md, n = ns.html_to_md("<div><h1>Laos</h1><h1> secret war</h1></div>\n<div><br></div>\n<div>Body.</div>",
                                  "Laos secret war", Path(d))
        self.assertEqual(md, "# Laos secret war\n\nBody.\n")
        self.assertEqual(n, 0)

    def test_inline_image_is_saved_and_embedded(self):
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        with tempfile.TemporaryDirectory() as d:
            md, n = ns.html_to_md(f'<div><img src="data:image/png;base64,{png}"></div>', "Pic", Path(d))
            self.assertEqual(len(list(Path(d).iterdir())), 1)
        self.assertEqual(n, 1)
        self.assertIn("![[Attachments/Apple Notes/", md)

    def test_vault_edit_is_detected_through_the_synced_hash(self):
        n = note("x/p1", "A")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "A.md"
            p.write_text(ns.render(n, "# A\n\nbody\n"))
            self.assertFalse(ns.scan_vault(Path(d))["x/p1"][2])
            p.write_text(p.read_text() + "my edit\n")
            self.assertTrue(ns.scan_vault(Path(d))["x/p1"][2])


if __name__ == "__main__":
    unittest.main()
