"""Behavior checks using fictional, temporary data only; no network or user library."""
import base64
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import unquote, urlsplit


SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jG9sAAAAASUVORK5CYII=")
MARKDOWN = """---
name: Example
homepage: https://example.com
tokens:
  colors:
    canvas: '#ffffff'
    foreground: '#000000'
  typography:
    heading:
      family: Example Sans
      size: 32px
    body:
      family: Example Serif
      size: 16px
  spacing:
    small: 8px
    medium: 16px
---
# Example

## Layout
Use one primary content column for this fictional invitation form.
"""


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []
        self.attributes = []

    def handle_starttag(self, tag, attrs):
        self.attributes.extend(attrs)
        for name, value in attrs:
            if name in {"href", "src"} and value:
                parsed = urlsplit(value)
                if not parsed.scheme and parsed.path:
                    self.paths.append(unquote(parsed.path))


class PortableLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ux-synthetic-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / "library").resolve()
        self.run_cli("references.py", "init")

    def run_cli(self, script, *args, root=None, success=True, use_root=True, environment=None):
        command = [sys.executable, str(SCRIPTS / script)]
        if use_root:
            command.extend(["--root", str(root or self.root)])
        command.extend(str(a) for a in args)
        result = subprocess.run(command, cwd=self.temp.name, text=True, capture_output=True,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(environment or {})})
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result.stderr

    def write_json(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def record(self, identity="website:example:invite:v1"):
        image = self.root / "sources/website/screen.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(PNG)
        return {"schema_version": 1, "id": identity, "source": "website", "kind": "page",
                "product": "Example", "platform": "web", "title": "Invitation permissions",
                "source_url": "https://example.com/invite", "site_url": "https://example.com",
                "page_type": "landing", "revision": "v1", "collected_at": "2026-01-01T00:00:00Z",
                "tags": ["landing"], "screens": [{"path": "sources/website/screen.png"}],
                "notes": [{"observation": "Synthetic invitation form", "scope": "one fictional image"}]}

    def import_record(self, record, name="batch.jsonl", success=True):
        path = Path(self.temp.name) / name
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        return self.run_cli("references.py", "import", path, success=success)

    def manifest(self, source="authored", snapshot="2026-01-01", markdown=MARKDOWN):
        directory = self.root / "sources" / source / snapshot
        directory.mkdir(parents=True, exist_ok=True)
        document = directory / "DESIGN.md"
        document.write_text(markdown, encoding="utf-8")
        manifest = {"complete": True, "collected_at": "2026-01-01T00:00:00Z", "documents": [{
            "slug": "example", "path": document.relative_to(self.root).as_posix(),
            "source_url": "https://example.com/design", "sha256": hashlib.sha256(document.read_bytes()).hexdigest()}]}
        self.write_json((directory / "manifest.json").relative_to(self.root), manifest)
        return document, manifest

    def test_empty_library_and_explicit_root(self):
        stats = self.run_cli("references.py", "stats")
        self.assertEqual(stats["records"], 0)
        self.assertFalse(stats["index_rebuilt"])
        self.assertEqual(self.run_cli("references.py", "search", "invitation")["total"], 0)
        self.assertEqual(self.run_cli("references.py", "site", "Example")["total"], 0)
        self.assertEqual(self.run_cli("design_graph.py", "stats")["rules"], 0)
        self.assertEqual(self.run_cli("site_registry.py", "stats")["entities"], 0)
        self.assertEqual(self.run_cli("build_site_view.py")["entities"], 0)
        config = self.root / "catalog/taxonomy.json"
        config.write_text('{"tags": [], "retained": true}\n', encoding="utf-8")
        self.run_cli("references.py", "init")
        self.assertTrue(json.loads(config.read_text())["retained"])
        for script in ("references.py", "design_graph.py", "site_registry.py", "build_site_view.py"):
            args = () if script == "build_site_view.py" else ("stats",)
            self.assertIn("--root", self.run_cli(script, *args, success=False, use_root=False))
        self.assertIn("outside", self.run_cli("references.py", "init", root=SKILL / "data", success=False))
        copied_skill = Path(self.temp.name) / "copied-skill"
        copied_skill.mkdir()
        (copied_skill / "SKILL.md").write_text("# Another skill installation\n", encoding="utf-8")
        for location in (copied_skill, copied_skill / "private-library"):
            self.assertIn("outside", self.run_cli("references.py", "init", root=location, success=False))
            self.assertFalse((location / "catalog").exists())

    def test_jsonl_search_identity_and_failed_import_rollback(self):
        self.write_json("catalog/taxonomy.json", {"tags": [
            {"id": "invitation", "pattern": "invitation|invite", "aliases": ["초대", "invite"]}]})
        record = self.record()
        first = self.import_record(record)
        self.assertFalse(first["already_present"])
        self.assertTrue(self.import_record(record)["already_present"])
        search = self.run_cli("references.py", "search", "초대", "--tag", "landing")
        self.assertEqual(search["total"], 1)
        self.assertEqual(search["results"][0]["id"], record["id"])
        detail = self.run_cli("references.py", "show", record["id"])
        self.assertEqual(detail["library_root"], str(self.root))
        self.assertEqual(Path(detail["screens"][0]["path"]), self.root / "sources/website/screen.png")
        site = self.run_cli("references.py", "site", "example.com")
        self.assertEqual(site["match_status"], "ux-only")
        self.assertEqual(self.run_cli("references.py", "search", "--entity", "Example")["total"], 1)
        preflight = self.run_cli("references.py", "check-site", "--site-url", "https://www.example.com/?utm_source=test")
        self.assertTrue(preflight["skip_site"])
        imports = set((self.root / "imports").rglob("*.jsonl"))
        collision = self.record("website:duplicate:v1")
        self.assertIn("Duplicate landing", self.import_record(collision, "collision.jsonl", success=False))
        self.assertEqual(set((self.root / "imports").rglob("*.jsonl")), imports)
        self.assertEqual(self.run_cli("references.py", "stats")["records"], 1)
        invalid = self.record("website:invalid:v1")
        invalid["screens"] = [{"path": "../outside.png"}]
        self.assertIn("escapes library", self.import_record(invalid, "invalid.jsonl", success=False))

    def test_generic_graph_evidence_variants_and_draft(self):
        document, _ = self.manifest()
        self.manifest("second-source", markdown=MARKDOWN.replace("32px", "48px"))
        self.import_record(self.record())
        stats = self.run_cli("design_graph.py", "build")
        self.assertEqual(stats["documents"], 2)
        self.assertEqual(stats["brands"], 1)
        self.assertEqual(stats["linked_ux_visuals"], 1)
        colors = self.run_cli("design_graph.py", "search", "color-pair", "--brand", "Example")
        self.assertEqual(colors["total"], 2)
        self.assertEqual(colors["results"][0]["value"]["contrast_ratio"], 21)
        fonts = self.run_cli("design_graph.py", "search", "font-pair", "--brand", "Example")
        self.assertEqual(fonts["total"], 2)
        ratios = self.run_cli("design_graph.py", "search", "type-ratio", "--source", "authored")
        self.assertEqual(ratios["results"][0]["value"]["ratio"], 2)
        rule = self.run_cli("design_graph.py", "show", ratios["results"][0]["id"])
        self.assertEqual(Path(rule["document"]["absolute_path"]), document)
        self.assertEqual(len(rule["input_rules"]), 2)
        self.assertTrue(self.run_cli("design_graph.py", "compare", "Example")["variants"])
        self.assertTrue(self.run_cli("design_graph.py", "packet", "--brand", "Example")["dimensions"]["typography"])
        draft = Path(self.temp.name) / "DESIGN.draft.md"
        self.run_cli("design_graph.py", "draft", "--brief", "Fictional form", "--rule", rule["id"], "--output", draft)
        original = draft.read_bytes()
        self.run_cli("design_graph.py", "draft", "--brief", "Changed", "--rule", rule["id"], "--output", draft, success=False)
        self.assertEqual(draft.read_bytes(), original)
        site = self.run_cli("references.py", "site", "Example")
        self.assertEqual(site["match_status"], "matched")
        self.run_cli("site_registry.py", "build")
        for html in (self.root / "catalog/sites").rglob("*.html"):
            parser = Links()
            parser.feed(html.read_text())
            for link in parser.paths:
                self.assertTrue((html.parent / link).is_file(), (html, link))
        graph = self.root / "knowledge/design-rules/rules.sqlite"
        before = graph.read_bytes()
        self.write_json("sources/authored/2026-02-01/manifest.json", {"complete": False})
        self.assertIn("Incomplete", self.run_cli("design_graph.py", "build", success=False))
        self.assertEqual(graph.read_bytes(), before)
        second_root = Path(self.temp.name) / "second-library"
        self.run_cli("references.py", "init", root=second_root)
        self.assertEqual(self.run_cli("design_graph.py", "search", "color-pair", root=second_root)["total"], 0)
        self.assertEqual(self.run_cli("design_graph.py", "search", "color-pair")["total"], 2)

    def test_existing_viewer_changes_only_with_explicit_flag(self):
        # A synthetic app-archive-format archive, with no third-party contents.
        base = "archives/fixture"
        self.write_json(base + "/selection.json", {"createdAt": "2026-01-01T00:00:00Z", "apps": [
            {"archiveDir": "example", "name": "Example", "platform": "web", "url": "https://example.com"}]})
        self.write_json(base + "/example/app.json", {"version": "v1", "captureLabel": "synthetic", "sourceUrl": "https://example.com"})
        self.write_json(base + "/example/all-flows.json", {"status": "complete", "flows": [{
            "flowId": "invite-v1", "index": 0, "parentIndex": None, "path": ["Invite"],
            "name": "Invite", "screenIndices": [0], "url": "https://example.com/invite"}],
            "screens": [{"path": "screen.png", "sourceUrl": "https://example.com/screen.png",
                         "sha256": hashlib.sha256(PNG).hexdigest()}]})
        self.write_json(base + "/example/flow-analysis.json", {})
        (self.root / base / "example/screen.png").write_bytes(PNG)
        viewer = self.root / base / "example/index.html"
        viewer.write_text("<!doctype html><header>Fictional archive</header>", encoding="utf-8")
        gallery = self.root / base / "index.html"
        gallery.write_text("<!doctype html><p>Fictional collection</p>", encoding="utf-8")
        original = viewer.read_bytes()
        self.run_cli("references.py", "stats")
        flow = self.run_cli("references.py", "show", "app-archive:v1:invite-v1")
        self.assertEqual(flow["source"], "app-archive")
        self.assertEqual(flow["screen_count"], 1)
        self.run_cli("build_site_view.py")
        self.assertEqual(viewer.read_bytes(), original)
        self.assertIn("../../archives/fixture/index.html", (self.root / "catalog/sites/index.html").read_text(encoding="utf-8"))
        self.run_cli("build_site_view.py", "--link-viewers")
        updated = viewer.read_bytes()
        self.assertIn(b'site-entity-link', updated)
        self.run_cli("build_site_view.py", "--link-viewers")
        self.assertEqual(viewer.read_bytes(), updated)

    def test_manifest_path_escape_rejected(self):
        document, manifest = self.manifest()
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text(MARKDOWN, encoding="utf-8")
        manifest["documents"][0]["path"] = "../outside.md"
        self.write_json(document.parent.relative_to(self.root) / "manifest.json", manifest)
        self.assertIn("escapes library", self.run_cli("design_graph.py", "build", success=False))

    def test_arbitrary_design_ids_have_safe_distinct_viewer_paths(self):
        identities = ['\" onmouseover=\"alert(1)', '../../outside', 'a/b', 'a-b', '<svg onload=alert(1)>']
        for index, design_id in enumerate(identities):
            record = self.record(f"provider-a:fixture-{index}:v1")
            record.update(source="provider-a", reference_type="design-gallery", kind="screen", page_type="", design_id=design_id)
            self.import_record(record, f"design-{index}.jsonl")
        # Without a design ID, two different full record IDs sharing a revision remain separate.
        for name in ("first", "second"):
            record = self.record(f"provider-a:{name}:v1")
            record.update(source="provider-a", reference_type="design-gallery", kind="screen", page_type="")
            self.import_record(record, f"{name}.jsonl")
        registry = json.loads((self.root / "catalog/site-matches.json").read_text(encoding="utf-8"))
        entities = registry["entities"]
        self.assertEqual(len(entities), 7)
        paths = [entity["local_viewer"] for entity in entities]
        self.assertEqual(len(set(paths)), 7)
        for entity in entities:
            for field in ("local_viewer", "analysis_path"):
                path = self.root / entity[field]
                self.assertEqual(path.parent, self.root / "catalog/sites")
                self.assertRegex(path.name, r"^design--gallery--[a-f0-9]{64}\.(html|md)$")
                self.assertTrue(path.is_file())
            html = self.root / entity["local_viewer"]
            parser = Links()
            parser.feed(html.read_text(encoding="utf-8"))
            self.assertFalse(any(name.startswith("on") for name, _ in parser.attributes))
            for relative in parser.paths:
                self.assertTrue((html.parent / relative).is_file(), relative)
        self.run_cli("site_registry.py", "build")
        rebuilt = json.loads((self.root / "catalog/site-matches.json").read_text(encoding="utf-8"))
        self.assertEqual(paths, [entity["local_viewer"] for entity in rebuilt["entities"]])

    def test_provider_names_do_not_suppress_visual_records(self):
        for source in ("provider-a", "provider-b"):
            record = self.record(f"{source}:example:screen:v1")
            record.update(source=source, kind="screen", page_type="")
            self.import_record(record, f"{source}.jsonl")
        stats = self.run_cli("site_registry.py", "stats")
        self.assertEqual(stats["ux_records"], 2)
        self.assertEqual(self.run_cli("references.py", "site", "Example")["ux_record_count"], 2)

    def test_optional_design_id_falls_back_without_breaking_direct_or_imported_data(self):
        for index, value in enumerate(("", "   ", 1.5, True, {"unexpected": "shape"})):
            record = self.record(f"provider-a:invalid-{index}:v1")
            record.update(source="provider-a", reference_type="design-gallery", kind="screen",
                          page_type="", design_id=value)
            if index % 2:
                # A user can provide a JSONL file directly under configured inputs.
                path = self.root / "imports/provider-a" / f"direct-{index}.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            else:
                self.import_record(record, f"invalid-optional-{index}.jsonl")
            result = self.run_cli("references.py", "show", record["id"])
            self.assertIsNone(result["design_id"])
            self.assertIn("full reference ID", result["design_id_warning"])
        stats = self.run_cli("site_registry.py", "stats")
        self.assertEqual(stats["independent_designs"], 5)
        self.assertEqual(self.run_cli("references.py", "stats")["records"], 5)

    def test_gallery_identity_is_source_scoped_and_requires_domain_evidence(self):
        self.manifest()
        self.run_cli("design_graph.py", "build")
        for source in ("provider-a", "provider-b"):
            record = self.record(f"{source}:item:v1")
            record.update(source=source, reference_type="design-gallery", kind="screen", page_type="",
                          design_id="same-item", site_identity_status="verified-domain", identity_evidence=[])
            self.import_record(record, f"{source}.jsonl")
        stats = self.run_cli("site_registry.py", "stats")
        self.assertEqual(stats["independent_designs"], 2)
        self.assertEqual(stats["matched_ux_records"], 0)
        verified = self.record("provider-c:verified:v1")
        verified.update(source="provider-c", reference_type="design-gallery", kind="screen", page_type="",
                        design_id="same-item", site_identity_status="verified-domain",
                        identity_evidence=["https://example.com"])
        self.import_record(verified, "verified.jsonl")
        self.assertEqual(self.run_cli("site_registry.py", "stats")["matched_ux_records"], 1)

    def test_unicode_product_names_without_domains_remain_distinct(self):
        entities = []
        for index, name in enumerate(("가나", "다라")):
            record = self.record(f"website:unicode-{index}:v1")
            record.update(product=name, kind="screen", page_type="", site_url="")
            self.import_record(record, f"unicode-{index}.jsonl")
            entity = self.run_cli("references.py", "site", name)
            self.assertEqual(entity["name"], name)
            self.assertEqual(entity["ux_record_count"], 1)
            entities.append(entity)
        self.assertNotEqual(entities[0]["id"], entities[1]["id"])
        self.assertNotEqual(entities[0]["local_viewer"], entities[1]["local_viewer"])

    def test_utf8_files_work_under_ascii_locale(self):
        # Keep stdout UTF-8 while forcing text-file defaults to ASCII.
        environment = {"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "PYTHONIOENCODING": "utf-8"}
        self.run_cli("references.py", "init", environment=environment)
        record = self.record()
        record["product"] = "예제"
        batch = Path(self.temp.name) / "utf8.jsonl"
        batch.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        self.run_cli("references.py", "import", batch, environment=environment)
        (self.root / "relations.json").write_text(json.dumps(
            [{"primary_id": record["id"], "note": "한글 근거"}], ensure_ascii=False), encoding="utf-8")
        detail = self.run_cli("references.py", "show", record["id"], environment=environment)
        self.assertEqual(detail["cross_source_searches"][0]["note"], "한글 근거")
        self.manifest(markdown=MARKDOWN.replace("name: Example", "name: 예제"))
        self.run_cli("design_graph.py", "build", environment=environment)
        self.run_cli("site_registry.py", "build", environment=environment)
        self.run_cli("build_site_view.py", environment=environment)
        registry = json.loads((self.root / "catalog/site-matches.json").read_text(encoding="utf-8"))
        self.assertEqual(registry["entities"][0]["name"], "예제")
        page = self.root / registry["entities"][0]["local_viewer"]
        self.assertIn("예제", page.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
