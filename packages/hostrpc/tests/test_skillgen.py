import importlib
import inspect
import json
from pathlib import Path
from typing import Annotated, Literal

import hostrpc
import pytest
from hostrpc import skillgen

# The generator imports the fronts, so it's tested with the whole workspace (make test).
Field = pytest.importorskip("pydantic").Field
pytest.importorskip("mcp")

ROOT = Path(__file__).resolve().parents[3]


def test_the_generated_skills_are_up_to_date():
    assert skillgen.stale(ROOT) == [], "run `make skills`"


def test_every_front_with_skills_is_found():
    assert {"sites.server", "podcasts.server", "audit.server"} <= set(
        skillgen.fronts(ROOT)
    )


@pytest.mark.parametrize("module", skillgen.fronts(ROOT))
def test_a_skill_takes_what_its_op_takes(module):
    """Each skill is an op of its runner (<package>.tools.OPS), with the same parameters
    and defaults, as the generated handler leaves a param out to get the op's default."""
    skills = importlib.import_module(module).skills
    tools = importlib.import_module(module.replace(".server", ".tools"))
    ops = {f.__name__: f for f in tools.OPS}
    for skill in skills:
        op = ops.get(skill.__name__)
        assert op is not None, f"{skill.__name__} isn't in {tools.__name__}.OPS"
        declared = inspect.signature(skill).parameters
        actual = inspect.signature(op).parameters
        assert list(declared) == list(actual), skill.__name__
        for name, p in declared.items():
            assert p.default == actual[name].default, f"{skill.__name__}.{name}"


async def example(
    url: Annotated[str, Field(description="A URL.")],
    keep: Annotated[int | str, Field(description="A number or 'all'.")] = 5,
    flag: Annotated[bool | None, Field(description="On or off.")] = None,
    mode: Annotated[Literal["a", "b"] | None, Field(description="Which.")] = None,
    extra: Annotated[dict[str, str] | None, Field(description="Fields.")] = None,
) -> str:
    """Do the example
    thing."""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    skills = hostrpc.Skills("demo", "DEMO_SOCKET")
    skills.add(example)
    monkeypatch.setattr(skillgen, "declared", lambda root: [("demo.server", skills)])
    (tmp_path / skillgen.SKILLS_DIR).mkdir(parents=True)
    return tmp_path


def test_render_writes_a_manifest_and_a_handler(repo):
    files = skillgen.render(repo)
    folder = repo / skillgen.SKILLS_DIR / "example"
    manifest = json.loads(files[folder / "plugin.json"])
    assert manifest["name"] == "Example"
    assert manifest["hubId"] == "example"
    assert manifest["description"] == "Do the example thing."
    assert manifest["entrypoint"]["params"] == {
        "url": {"type": "string", "description": "A URL."},
        "keep": {"type": "string", "description": "A number or 'all'."},
        "flag": {"type": "boolean", "description": "On or off."},
        "mode": {"type": "string", "enum": ["a", "b"], "description": "Which."},
        "extra": {"type": "object", "description": "Fields."},
    }
    handler = files[folder / "handler.js"]
    assert handler.startswith(skillgen.GENERATED)
    assert (
        '"service": "demo", "env": "DEMO_SOCKET", "op": "example", "params": {"url": "string", '
        '"keep": "integer-or-string", "flag": "boolean", "mode": "enum", "extra": "object"}'
    ) in handler


def test_write_then_check(repo):
    assert skillgen.stale(repo)
    assert skillgen.write(repo)
    assert skillgen.stale(repo) == []
    assert skillgen.write(repo) == []


def test_write_never_replaces_a_hand_written_skill(repo):
    folder = repo / skillgen.SKILLS_DIR / "example"
    folder.mkdir()
    (folder / "handler.js").write_text("// mine\n")
    with pytest.raises(ValueError, match="hand-written"):
        skillgen.write(repo)
    assert (folder / "handler.js").read_text() == "// mine\n"


def test_a_skill_no_longer_declared_is_removed(repo, monkeypatch):
    skillgen.write(repo)
    hand = repo / skillgen.SKILLS_DIR / "mine"
    hand.mkdir()
    (hand / "handler.js").write_text("// mine\n")
    monkeypatch.setattr(skillgen, "declared", lambda root: [])
    assert skillgen.stale(repo) == [f"{skillgen.SKILLS_DIR}/example/"]
    skillgen.write(repo)
    assert not (repo / skillgen.SKILLS_DIR / "example").exists()
    assert hand.exists()


def test_a_param_type_a_skill_cant_take_is_refused():
    with pytest.raises(ValueError, match="no skill param type"):
        skillgen._param("x.y", {"type": "array", "items": {"type": "string"}})
