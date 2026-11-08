import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate_backup.py"
SPEC = importlib.util.spec_from_file_location("validate_backup", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


SOURCE = """<!doctype html><html><head><title>Favorites!</title>
<link rel="canonical" href="https://fav.ju.mp/">
<link rel="icon" href="assets/icon.png?v=1">
<style>@font-face {font-family: test; src: url('assets/font.woff2');}</style>
</head><body><div class="site-wrapper"><div class="site-main" role="main">
<section id="home-section"><a href="https://example.com/?a=1&amp;b=2">Bookmark</a>
<a href="#home-section">Home</a><img src="assets/image.png">
<script src="/cdn-cgi/email.js"></script><script>const preserved = true;</script>
</section></div></div></body></html>"""
MIRROR = SOURCE.replace("assets/icon.png?v=1", "assets/icon.png@v=1").replace(
    'href="#home-section"', 'href="index.html#home-section"'
).replace('src="/cdn-cgi/email.js"', 'src="cdn-cgi/email.js"')


class BackupValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.html"
        self.mirror = self.root / "mirror"
        self.mirror.mkdir()
        self.source.write_text(SOURCE, encoding="utf-8")
        (self.mirror / "index.html").write_text(MIRROR, encoding="utf-8")
        for name in ("assets/icon.png@v=1", "assets/font.woff2", "assets/image.png", "cdn-cgi/email.js"):
            path = self.mirror / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test resource")

    def replace_mirror(self, old, new):
        (self.mirror / "index.html").write_text(MIRROR.replace(old, new), encoding="utf-8")

    def test_valid_source_and_mirror(self):
        validator.validate(self.source)
        validator.validate(self.source, self.mirror)

    def test_truncated_html(self):
        self.source.write_text(SOURCE.replace("</html>", ""), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Incomplete HTML"):
            validator.validate(self.source)

    def test_interstitial_title(self):
        self.source.write_text(SOURCE.replace("Favorites!", "Just a moment..."), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unexpected page title"):
            validator.validate(self.source)

    def test_error_hidden_behind_expected_title(self):
        self.source.write_text(SOURCE.replace('id="home-section"', 'id="challenge-form"'), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "interstitial"):
            validator.validate(self.source)

    def test_missing_structure(self):
        self.source.write_text(SOURCE.replace("site-main", "partial-content"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Missing site structure"):
            validator.validate(self.source)

    def test_missing_bookmark(self):
        self.replace_mirror('href="https://example.com/?a=1&amp;b=2"', 'href="https://other.example/"')
        with self.assertRaisesRegex(ValueError, "bookmark"):
            validator.validate(self.source, self.mirror)

    def test_missing_font_image_and_script(self):
        for name in ("assets/font.woff2", "assets/image.png", "cdn-cgi/email.js"):
            with self.subTest(resource=name):
                path = self.mirror / name
                resource = path.read_bytes()
                path.unlink()
                with self.assertRaisesRegex(ValueError, "Missing or empty rendering resource"):
                    validator.validate(self.source, self.mirror)
                path.write_bytes(resource)

    def test_remote_rendering_resource(self):
        self.replace_mirror('src="assets/image.png"', 'src="https://fav.ju.mp/assets/image.png"')
        with self.assertRaisesRegex(ValueError, "still requires the network"):
            validator.validate(self.source, self.mirror)

    def test_dropped_resource_element(self):
        self.replace_mirror('<script src="cdn-cgi/email.js"></script>', "")
        with self.assertRaisesRegex(ValueError, "rendering resource elements"):
            validator.validate(self.source, self.mirror)

    def test_dropped_font_reference(self):
        self.replace_mirror("src: url('assets/font.woff2');", "")
        with self.assertRaisesRegex(ValueError, "CSS rendering resources"):
            validator.validate(self.source, self.mirror)

    def test_changed_visible_content(self):
        self.replace_mirror(">Bookmark</a>", ">Incorrect content</a>")
        with self.assertRaisesRegex(ValueError, "visible site content"):
            validator.validate(self.source, self.mirror)

    def test_changed_inline_script(self):
        self.replace_mirror("const preserved = true;", "")
        with self.assertRaisesRegex(ValueError, "inline site scripts"):
            validator.validate(self.source, self.mirror)

    def test_nested_stylesheet_resource(self):
        source = SOURCE.replace("</head>", '<link rel="stylesheet" href="assets/style.css"></head>')
        mirror = MIRROR.replace("</head>", '<link rel="stylesheet" href="assets/style.css"></head>')
        self.source.write_text(source, encoding="utf-8")
        (self.mirror / "index.html").write_text(mirror, encoding="utf-8")
        (self.mirror / "assets/style.css").write_text('@import "nested.css";', encoding="utf-8")
        (self.mirror / "assets/nested.css").write_text("body {background: url('missing.png')}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing.png"):
            validator.validate(self.source, self.mirror)
        (self.mirror / "assets/missing.png").write_bytes(b"image")
        validator.validate(self.source, self.mirror)

    def test_resource_path_escape(self):
        self.replace_mirror('src="assets/image.png"', 'src="../source.html"')
        with self.assertRaisesRegex(ValueError, "escapes the mirror"):
            validator.validate(self.source, self.mirror)


if __name__ == "__main__":
    unittest.main()
