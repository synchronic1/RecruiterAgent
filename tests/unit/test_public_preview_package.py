"""The public packager must reject the private corpus variant."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("public_preview", ROOT / "tools/build_public_preview.py")
packager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packager)


class PublicPreviewPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("offline_preview", ROOT / "tools/build_design_preview.py")
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        with tempfile.TemporaryDirectory() as folder:
            generator.OUTPUT = Path(folder) / "preview.html"
            generator.build(generator.payload())
            cls.source = generator.OUTPUT.read_bytes().decode("utf-8")

    def test_public_packaging_is_stable(self):
        public = packager.public_preview(self.source)
        self.assertEqual(packager.public_preview(public), public)

    def test_real_corpus_is_rejected_before_packaging(self):
        with self.assertRaises(ValueError):
            packager.public_preview(self.source.replace('"dataset": "synthetic_fixtures"', '"dataset": "real_technology_resumes"'))

    def test_missing_declaration_is_rejected(self):
        with self.assertRaises(ValueError):
            packager.public_preview("<!doctype html><title>Unqualified preview</title>")
