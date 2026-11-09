"""Tests for trending.py with FAKE data only: no network, no key. Run: python -m unittest test_trending -v"""
import io, json, os, tempfile, unittest, contextlib
from unittest import mock
import trending

FAKE_KEY = "FAKEKEY" + "x" * 25          # a made-up 32-character test key, not a real one

class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); trending.OUT = os.path.join(self.d, "site", "trending.json")

    def test_fake_writes_eight_alternating_items(self):
        self.assertEqual(trending.main(["--fake"]), 0)
        data = json.load(open(trending.OUT, encoding="utf-8"))
        self.assertEqual(data["v"], 1); self.assertEqual(len(data["items"]), 8)
        self.assertEqual([i["kind"] for i in data["items"]], ["movie", "show"] * 4)
        self.assertEqual([i["rank"] for i in data["items"]], list(range(1, 9)))
        self.assertTrue(all(i["pic"].startswith("https://image.tmdb.org/t/p/w780/") for i in data["items"]))
        self.assertRegex(data["date"], r"^\d{4}-\d{2}-\d{2}$"); self.assertGreater(data["generated"], 1_700_000_000_000)

    def test_no_key_writes_nothing_and_succeeds(self):
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(trending.main([]), 0)
        self.assertFalse(os.path.exists(trending.OUT)); self.assertIn("not set", out.getvalue())

    def test_titles_without_a_picture_or_adult_are_skipped(self):
        res = [{"title": "A", "backdrop_path": None}, {"title": "B", "backdrop_path": "/ok-picture1.jpg", "adult": True},
               {"title": "C", "backdrop_path": "/../evil.jpg"}, {"title": "D", "backdrop_path": "/good-picture2.jpg", "release_date": "2026-05-01"}]
        got = trending.pick(res, "movie")
        self.assertEqual([g["title"] for g in got], ["D"]); self.assertEqual(got[0]["year"], "2026")

    def test_order_is_tmdbs_own_and_capped_at_four(self):
        res = [{"name": f"S{i}", "backdrop_path": f"/pic-number{i}.jpg", "first_air_date": "2024-01-01"} for i in range(9)]
        self.assertEqual([g["title"] for g in trending.pick(res, "show")], ["S0", "S1", "S2", "S3"])

    def test_error_never_contains_the_key(self):
        boom = Exception("failed for https://api.themoviedb.org/3/x?api_key=" + FAKE_KEY)
        with mock.patch("urllib.request.urlopen", side_effect=boom):
            with self.assertRaises(RuntimeError) as cm: trending.send("/trending/movie/day", FAKE_KEY)
        self.assertNotIn(FAKE_KEY, str(cm.exception)); self.assertNotIn("api_key", str(cm.exception))

    def test_api_failure_writes_nothing_and_never_prints_the_key(self):
        with mock.patch.dict(os.environ, {"TMDB_API_KEY": FAKE_KEY}), \
             mock.patch("urllib.request.urlopen", side_effect=Exception("https://x?api_key=" + FAKE_KEY)), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(trending.main([]), 0)
        self.assertFalse(os.path.exists(trending.OUT)); self.assertNotIn(FAKE_KEY, out.getvalue())

    def test_real_path_with_mocked_tmdb_answers(self):
        def fake_send(path, key):
            if "movie" in path: return {"results": [{"title": f"M{i}", "backdrop_path": f"/mpic-pic{i}.jpg", "release_date": "2026-01-01"} for i in range(6)]}
            return {"results": [{"name": f"T{i}", "backdrop_path": f"/tpic-pic{i}.jpg", "first_air_date": "2025-01-01"} for i in range(6)]}
        with mock.patch.dict(os.environ, {"TMDB_API_KEY": FAKE_KEY}), mock.patch.object(trending, "send", fake_send), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(trending.main([]), 0)
        data = json.load(open(trending.OUT)); self.assertEqual([i["title"] for i in data["items"]], ["M0", "T0", "M1", "T1", "M2", "T2", "M3", "T3"])
        self.assertNotIn(FAKE_KEY, out.getvalue()); self.assertNotIn(FAKE_KEY, open(trending.OUT).read())

    def test_fewer_than_two_titles_writes_nothing(self):
        with mock.patch.dict(os.environ, {"TMDB_API_KEY": FAKE_KEY}), mock.patch.object(trending, "send", lambda p, k: {"results": []}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(trending.main([]), 0)
        self.assertFalse(os.path.exists(trending.OUT))

if __name__ == "__main__": unittest.main()
