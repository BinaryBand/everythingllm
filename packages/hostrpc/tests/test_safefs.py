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


def test_copy_tree_copies_plain_files_and_folders_only(tree, tmp_path):
    root, outside = tree
    (root / "a" / "b").mkdir()
    (root / "a" / "b" / "g.txt").write_text("deep")
    (root / "a" / ".hidden").write_text("no")
    (root / "a" / "s.txt").symlink_to(outside / "secret.txt")
    (root / "a" / "l").symlink_to(outside)
    os.mkfifo(root / "a" / "pipe")
    dest = tmp_path / "copy"
    with safefs.folder(root, ("a",)) as d:
        assert safefs.copy_tree(d, dest, 1000) == len("inside") + len("deep")
    assert sorted(str(p.relative_to(dest)) for p in dest.rglob("*")) == [
        "b",
        "b/g.txt",
        "f.txt",
    ]
    with safefs.folder(root, ("a",)) as d, pytest.raises(OSError, match="over 5"):
        safefs.copy_tree(d, tmp_path / "small", 5)


def test_trim_deletes_the_oldest_files_and_leaves_the_rest(tmp_path):
    for age, name in enumerate(("c", "b", "a")):  # a is the oldest
        (tmp_path / name).write_bytes(b"x" * 100)
        os.utime(tmp_path / name, (100 - age, 100 - age))
    (tmp_path / ".writing.tmp").write_bytes(b"x" * 1000)  # another's write in progress
    (tmp_path / "folder").mkdir()
    os.symlink("/etc/passwd", tmp_path / "link")
    with safefs.folder(tmp_path) as d:
        safefs.trim(d, 250)
    assert sorted(os.listdir(tmp_path)) == [".writing.tmp", "b", "c", "folder", "link"]


def test_keep_names_bytes_by_their_hash_and_only_marks_them_new_when_kept(tmp_path):
    with safefs.folder(tmp_path) as d:
        name = safefs.keep(d, b"picture", "jpg")
        os.utime(tmp_path / name, (1, 1))
        assert safefs.keep(d, b"picture", "jpg") == name
        assert (tmp_path / name).stat().st_mtime > 1  # marked new: trim keeps it longer
        assert safefs.keep(d, b"another", "png") != name
    assert name.endswith(".jpg") and len(os.listdir(tmp_path)) == 2
