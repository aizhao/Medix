import json
import tempfile
import unittest
from pathlib import Path

from backend.storage import SQLiteStore
from scripts.build_minibench_corpus import _openfda_to_text, _pmc_to_text, _select_fda_result, verify_corpus


class MiniBenchTests(unittest.TestCase):
    def test_openfda_record_is_normalized_with_provenance(self):
        text, metadata = _openfda_to_text(
            "metformin",
            {
                "effective_time": "20260101",
                "openfda": {
                    "generic_name": ["METFORMIN"],
                    "brand_name": ["Example brand"],
                    "manufacturer_name": ["Example manufacturer"],
                    "spl_set_id": ["set-123"],
                },
                "indications_and_usage": ["For glycemic control."],
            },
        )
        self.assertIn("INDICATIONS AND USAGE", text)
        self.assertEqual(metadata["external_id"], "set-123")
        self.assertEqual(metadata["published_at"], "20260101")

    def test_openfda_selection_rejects_combination_label(self):
        selected = _select_fda_result("lisinopril", [
            {"openfda": {"generic_name": ["LISINOPRIL AND HYDROCHLOROTHIAZIDE"]}},
            {"openfda": {"generic_name": ["LISINOPRIL"]}},
        ])
        self.assertEqual(selected["openfda"]["generic_name"], ["LISINOPRIL"])

    def test_pmc_requires_and_extracts_creative_commons_article(self):
        xml = b"""<pmc-articleset><article><front><journal-meta><journal-title>Test Journal</journal-title></journal-meta><article-meta><article-id pub-id-type="pmcid">PMC123</article-id><title-group><article-title>Systematic review</article-title></title-group><pub-date><year>2025</year></pub-date><permissions><license xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="https://creativecommons.org/licenses/by/4.0/"><license-p>Creative Commons Attribution license.</license-p></license></permissions><abstract><p>""" + (b"A" * 800) + b"""</p></abstract></article-meta></front><body><sec><p>""" + (b"B" * 800) + b"""</p></sec></body></article></pmc-articleset>"""
        text, metadata = _pmc_to_text(xml)
        self.assertIn("PMC123", text)
        self.assertIn("creativecommons.org", metadata["license"])
        self.assertEqual(metadata["external_id"], "PMC123")

    def test_corpus_verification_and_source_metadata_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"medical evidence"
            source = root / "evidence.txt"
            source.write_bytes(payload)
            import hashlib

            entry = {
                "filename": source.name,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            self.assertEqual(verify_corpus(root, {"documents": [entry]}), [])

            store = SQLiteStore(str(root / "medix.db"))
            created = store.add_document(
                source.name,
                str(source),
                source_url="https://example.org/evidence",
                publisher="Example Authority",
                license="CC BY 4.0",
                external_id="DOC-1",
                corpus_id="medix-minibench-v0.1",
                language="en",
                published_at="2025",
                tags=["evaluation"],
            )
            saved = store.get_document(created["id"])
            self.assertEqual(saved["source_url"], "https://example.org/evidence")
            self.assertEqual(saved["corpus_id"], "medix-minibench-v0.1")
            self.assertEqual(saved["tags"], ["evaluation"])


if __name__ == "__main__":
    unittest.main()
