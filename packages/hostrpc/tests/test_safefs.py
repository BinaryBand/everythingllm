import os

import pytest
from hostrpc import safefs


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    (root / "a" / "f.txt").write_text("inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    return root, outside


def test_reads_a_plain_file(tree):
    root, _ = tree
    assert safefs.read_regular(root, ("a", "f.txt"), 100) == b"inside"
    assert safefs.read_regular(root, ("a", "f.txt"), 3) == b"ins"


def test_a_symlinked_folder_on_the_way_is_refused(tree):
    root, outside = tree
    (root / "link").symlink_to(outside)
    assert safefs.read_regular(root, ("link", "secret.txt"), 100) is None
    with pytest.raises(OSError):
        safefs.open_dir(root, ("link",))


def test_a_symlinked_file_is_refused(tree):
    root, outside = tree
    (root / "a" / "s.txt").symlink_to(outside / "secret.txt")
    assert safefs.read_regular(root, ("a", "s.txt"), 100) is None


def test_a_fifo_is_refused_without_blocking(tree):
    root, _ = tree
    os.mkfifo(root / "a" / "pipe")
    assert safefs.read_regular(root, ("a", "pipe"), 100) is None


def test_bad_parts_are_refused(tree):
    root, _ = tree
    for bad in ("..", ".", "", "a/b"):
        with pytest.raises(OSError):
            safefs.open_dir(root, (bad,))


def test_make_makes_folders_but_not_through_a_symlink(tree):
    root, outside = tree
    with safefs.folder(root, ("x", "y"), make=True):
        pass
    assert (root / "x" / "y").is_dir()
    (root / "l").symlink_to(outside)
    with pytest.raises(OSError):
        safefs.open_dir(root, ("l", "new"), make=True)
    assert not (outside / "new").exists()


def test_create_free_never_writes_through_a_symlink(tree):
    root, outside = tree
    (root / "a" / "out.txt").symlink_to(outside / "planted.txt")  # dangling
    with safefs.folder(root, ("a",)) as d:
        fd, name = safefs.create_free(d, "out.txt")
        os.close(fd)
        assert name == "out-2.txt"
        fd, name = safefs.create_free(d, "out.txt")
        os.close(fd)
        assert name == "out-3.txt"
    assert not (outside / "planted.txt").exists()
    assert oct((root / "a" / "out-2.txt").stat().st_mode & 0o777) == "0o644"


def test_replace_replaces_a_symlink_itself(tree):
    root, outside = tree
    (root / "a" / "card.png").symlink_to(outside / "secret.txt")
    with safefs.folder(root, ("a",)) as d:
        safefs.replace(d, "card.png", b"png")
    assert not (root / "a" / "card.png").is_symlink()
    assert (root / "a" / "card.png").read_bytes() == b"png"
    assert (outside / "secret.txt").read_text() == "secret"
    assert [p.name for p in (root / "a").iterdir() if p.name.startswith(".")] == []
