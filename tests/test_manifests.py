"""Plugin manifest validation: syntax, required fields, naming schema, consistency and referenced files."""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin.json"
CODEX = ROOT / ".codex-plugin" / "plugin.json"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")      # ChatGPT's importer: lowercase letters, numbers, dots, hyphens
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


@pytest.fixture(scope="module")
def plugin():
    return json.loads(PLUGIN.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def codex():
    return json.loads(CODEX.read_text(encoding="utf-8"))


def iface(plugin):
    return plugin["extensions"]["com.openai"]["interface"]


def test_both_manifests_are_valid_json_objects(plugin, codex):
    assert isinstance(plugin, dict) and isinstance(codex, dict)


@pytest.mark.parametrize("which", ["plugin", "codex"])
def test_required_top_level_fields(which, plugin, codex):
    manifest = {"plugin": plugin, "codex": codex}[which]
    for key in ("name", "version", "description", "author", "keywords"):
        assert manifest.get(key), f"{which}: missing {key}"
    assert isinstance(manifest["keywords"], list) and all(isinstance(k, str) for k in manifest["keywords"])
    assert manifest["author"].get("name")


@pytest.mark.parametrize("which", ["plugin", "codex"])
def test_plugin_name_follows_the_identifier_schema(which, plugin, codex):
    name = {"plugin": plugin, "codex": codex}[which]["name"]
    assert NAME_RE.match(name), f"{name!r} must be lowercase letters, numbers, dots or hyphens"


@pytest.mark.parametrize("which", ["plugin", "codex"])
def test_version_is_semantic(which, plugin, codex):
    assert VERSION_RE.match({"plugin": plugin, "codex": codex}[which]["version"])


def test_manifests_agree_on_identity_and_text(plugin, codex):
    for key in ("name", "version", "description", "author", "keywords"):
        assert plugin[key] == codex[key], key
    a, b = iface(plugin), codex["interface"]
    assert a == b, "the two interface blocks must be identical"


def test_interface_fields_present(plugin):
    i = iface(plugin)
    for key in ("displayName", "shortDescription", "longDescription", "developerName", "category",
                "capabilities", "defaultPrompt", "brandColor", "composerIcon", "logo"):
        assert i.get(key), key
    assert i["displayName"] == "tubeGPT"                  # the human-facing name keeps its capitalisation
    assert re.match(r"^#[0-9A-Fa-f]{6}$", i["brandColor"])
    assert len(i["shortDescription"]) <= 60
    assert all(isinstance(p, str) and p for p in i["defaultPrompt"])


@pytest.mark.parametrize("which", ["plugin", "codex"])
def test_referenced_images_exist_and_are_pngs(which, plugin, codex):
    i = iface(plugin) if which == "plugin" else codex["interface"]
    for key in ("composerIcon", "logo"):
        path = ROOT / i[key]
        assert path.is_file(), f"{i[key]} is missing"
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", f"{i[key]} is not a PNG"


def test_skills_folder_and_skill_file(codex):
    folder = ROOT / codex["skills"]
    assert folder.is_dir()
    skills = [d for d in folder.iterdir() if d.is_dir()]
    assert skills, "no skill folders found"
    for skill in skills:
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        front = re.match(r"^---\nname: (?P<name>[a-z0-9-]+)\ndescription: (?P<desc>.+)\n---\n", text)
        assert front, f"{skill.name}: bad SKILL.md front matter"
        assert front["name"] == skill.name, "skill name must equal its folder name"
        assert len(front["desc"]) <= 1024
        assert (skill / "scripts" / "transcript.py").is_file()


def test_attribution_and_disclaimers_in_long_description(plugin):
    text = iface(plugin)["longDescription"]
    assert "not affiliated with or endorsed by YouTube or Google" in text
    assert "timestamp" in text.lower()
