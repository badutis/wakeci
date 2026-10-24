"""The documentation site stays complete and in step with the code. The build itself (with
--strict, which fails on a broken link or anchor) runs in CI's docs job."""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
sys.path.insert(0, str(ROOT / "scripts"))

import config_reference  # noqa: E402


def nav_pages() -> list:
    """The local pages in zensical.toml's nav, read without a TOML parser (3.9 has none)."""
    text = (ROOT / "zensical.toml").read_text(encoding="utf-8")
    nav = text.split("nav = [", 1)[1].split("\n]", 1)[0]
    return [p for p in re.findall(r'=\s*"([^"]+)"', nav) if not p.startswith("http")]


class DocsTest(unittest.TestCase):
    def test_the_configuration_tables_match_the_schema(self):
        self.assertEqual(config_reference.main(["--check"]), 0, "run python3 scripts/config_reference.py")

    def test_every_schema_key_has_a_description(self):
        schema = json.loads((DOCS / "config.schema.json").read_text(encoding="utf-8"))
        props = schema["properties"]
        for where, block in (
            ("", props),
            ("notify.", props["notify"]["properties"]),
            ("sources[].", props["sources"]["items"]["properties"]),
            ("machines[].", props["machines"]["items"]["properties"]),
        ):
            for key, prop in block.items():
                self.assertTrue(prop.get("description"), f"{where}{key} has no description")

    def test_the_generated_tables_list_every_key(self):
        page = (DOCS / "configuration.md").read_text(encoding="utf-8")
        schema = json.loads((DOCS / "config.schema.json").read_text(encoding="utf-8"))
        for key in schema["properties"]:
            if key not in config_reference.HIDDEN:
                self.assertIn(f"| `{key}` |", page, key)
        for key in schema["properties"]["machines"]["items"]["properties"]:
            self.assertIn(f"| `{key}` |", page, key)

    def test_every_page_is_in_the_nav_and_every_nav_page_exists(self):
        pages = nav_pages()
        on_disk = sorted(p.name for p in DOCS.glob("*.md"))
        self.assertEqual(sorted(pages), on_disk)

    def test_the_readme_links_to_pages_that_exist(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        links = re.findall(r"\]\((docs/[^)#]+)", readme)
        self.assertTrue(links)
        for link in links:
            self.assertTrue((ROOT / link).exists(), link)

    def test_the_docs_tools_are_pinned_by_hash(self):
        lines = (ROOT / "requirements-docs.txt").read_text(encoding="utf-8").splitlines()
        pins = [line for line in lines if re.match(r"^[a-z0-9]", line)]
        self.assertTrue(any(p.startswith("zensical==") for p in pins))
        for pin in pins:
            self.assertRegex(pin, r"^[a-z0-9._-]+==[^ ]+ \\$", pin)
        self.assertIn("--hash=sha256:", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
