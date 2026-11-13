"""Tests for genre_tiles.py with FAKE data only (no network, no key). python -m unittest test_genre_tiles -v"""
import datetime as dt
import json
import os
import shutil
import tempfile
import unittest

from PIL import Image

import genre_tiles as G


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        G.OUTDIR = os.path.join(self.tmp, "site", "genre")
        G.OUT = os.path.join(self.tmp, "site", "genre.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fake_run_writes_nine_tiles_and_list(self):
        self.assertEqual(G.main(["--fake"]), 0)
        d = json.load(open(G.OUT, encoding="utf-8"))
        self.assertEqual(d["v"], 1)
        self.assertEqual(len(d["tiles"]), 9)  # 2026-11-12: + Kids & Family, History, Science & Nature
        self.assertEqual(d["size"], [392, 220])
        for t in d["tiles"]:
            p = os.path.join(self.tmp, "site", t["pic"])
            im = Image.open(p)
            self.assertEqual(im.size, (392, 220))
            self.assertLess(os.path.getsize(p), 120_000)

    def test_candidates_need_primary_genre_and_skip_family_for_dark_genres(self):
        res = [{"id": 1, "title": "A", "genre_ids": [27, 53]}, {"id": 2, "title": "B", "genre_ids": [53, 27]}, {"id": 3, "title": "C", "genre_ids": [27, 16]}, {"id": 4, "title": "D", "genre_ids": [27], "adult": True}]
        got = G.candidates(res, 27, "horror", set())
        self.assertEqual([r["id"] for r in got], [1])
        self.assertEqual([r["id"] for r in G.candidates(res, 27, "horror", {1})], [])

    def test_textless_backdrop_picks_language_free_and_wide(self):
        imgs = {"backdrops": [{"iso_639_1": "en", "width": 1920, "file_path": "/aaaaaaaa.jpg", "vote_average": 9}, {"iso_639_1": None, "width": 1920, "file_path": "/bbbbbbbb.jpg", "vote_average": 5},
                              {"iso_639_1": None, "width": 800, "file_path": "/cccccccc.jpg", "vote_average": 8}, {"iso_639_1": None, "width": 3840, "file_path": "/dddddddd.jpg", "vote_average": 6}]}
        self.assertEqual(G.textless_backdrop(imgs)["file_path"], "/dddddddd.jpg")
        self.assertIsNone(G.textless_backdrop({"backdrops": [{"iso_639_1": "en", "width": 1920, "file_path": "/aaaaaaaa.jpg"}]}))

    def test_mood_rules(self):
        dark = Image.new("RGB", (64, 36), (20, 24, 22))
        bright = Image.new("RGB", (64, 36), (240, 240, 230))
        self.assertTrue(G.reads_as("horror", dark))
        self.assertFalse(G.reads_as("horror", bright))
        self.assertFalse(G.reads_as("comedy", dark))

    def test_key_never_in_errors(self):
        key = "K" * 32
        try:
            G.send("/nonexistent", key)
        except RuntimeError as e:
            self.assertNotIn(key, str(e))
            self.assertNotIn("api_key", str(e))

    def test_missing_key_still_succeeds(self):
        old = os.environ.pop("TMDB_API_KEY", None)
        G.carry_forward = lambda slug, name: None
        try:
            self.assertEqual(G.main([]), 0)
        finally:
            if old:
                os.environ["TMDB_API_KEY"] = old


    def test_family_accepts_animation_first_but_not_other_genres(self):
        res = [{"id": 1, "title": "A", "genre_ids": [16, 10751]}, {"id": 2, "title": "B", "genre_ids": [28]}]
        self.assertEqual([r["id"] for r in G.candidates(res, 10751, "family", set())], [1])

    def test_new_genres_are_in_the_list(self):
        self.assertEqual([g[1] for g in G.GENRES][-3:], ["family", "history", "nature"])
        self.assertTrue(len(G.GENRES[-1]) == 5 and "18330" in G.GENRES[-1][4])


    def test_nature_tile_uses_a_wide_discover_window_and_makes_a_tile(self):
        """2026-11-12: the first live run made NO nature tile. Cause: the default discover filter (releases of the last 3 years with >= 300 votes) leaves almost no nature documentaries. Nature now asks 12 years back with >= 100 votes and carries the keyword list; a fake TMDb that answers ONLY such a wide window must still give a nature tile (the old query would get nothing)."""
        seen = []
        def fake_send(path, key):
            seen.append(path)
            if path.startswith("/discover/movie"):
                wide = "vote_count.gte=100" in path and "primary_release_date.gte=" in path
                year = int(path.split("primary_release_date.gte=")[1][:4])
                if wide and year <= dt.date.today().year - 10:
                    return {"results": [{"id": 11, "title": "Wild Planet", "genre_ids": [99], "release_date": "2020-05-01"}]}
                return {"results": []}                  # the old 3-year / 300-vote window: nothing
            return {"backdrops": [{"iso_639_1": None, "width": 1920, "file_path": "/aaaaaaaaaa.jpg", "vote_average": 5}]}
        pic = Image.new("RGB", (1920, 1080), (90, 120, 100))
        old_send, old_fetch = G.send, G.fetch_image
        G.send, G.fetch_image = fake_send, lambda p: pic
        try:
            entry, img = G.pick_for(G.GENRES[-1], "k", set())
            e2, _ = G.pick_for(G.GENRES[0], "k", set())          # a normal genre keeps the 3-year / 300-vote window (and here gets nothing)
        finally:
            G.send, G.fetch_image = old_send, old_fetch
        self.assertIsNotNone(entry)
        self.assertEqual(entry["slug"], "nature")
        self.assertIsNotNone(img)
        self.assertIsNone(e2)
        disc = [p for p in seen if p.startswith("/discover/movie")][0]
        self.assertIn("with_genres=99", disc)
        self.assertIn("with_keywords=18330%7C221355", disc)

if __name__ == "__main__":
    unittest.main()
