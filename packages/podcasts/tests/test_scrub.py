import os
import xml.etree.ElementTree as ET
from collections.abc import Callable

import httpx
import numpy as np
import pytest
from podcasts import scrub
from podcasts.library import clock, key
from podcasts.rss import ITUNES
from podcasts.scrub import Scrubber, ScrubError
from test_avio import make_mp3
from test_fingerprint import episode, write_wav
from test_library import FEED_URL, Remote, feed, served, subscribe


def wav(path, seed: int, ad_at: float | None):
    return write_wav(path, episode(seed, ad_at, length=40))


def length(spans) -> float:
    return sum(b - a for a, b in spans)


def test_scrubber_finds_what_episodes_share(tmp_path):
    s = Scrubber(tmp_path / "prints")
    one, two, three = (
        wav(tmp_path / f"{n}.wav", n, at) for n, at in ((1, 10), (2, 25), (3, None))
    )
    before = two.read_bytes()
    assert s.scrub("show", "1", one) is None  # nothing to compare with yet
    s.fingerprint("show", "3", three)
    spans = s.scrub("show", "2", two)
    assert spans is not None
    assert (
        len(spans) == 1
        and spans[0][0] == pytest.approx(25, abs=0.5)
        and length(spans) == pytest.approx(12, abs=0.5)
    )
    assert two.read_bytes() == before  # the scrubber only finds; nothing is cut here
    assert length(s.scrub("show", "1", one)) == pytest.approx(12, abs=0.5)
    assert s.scrub("show", "3", three) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "1.wav",
        "2.wav",
        "3.wav",
        "prints",
    ]


def test_scrubber_keeps_long_repeats(tmp_path, monkeypatch):
    monkeypatch.setattr(scrub, "MAX_REPEAT", 5)
    s = Scrubber(tmp_path / "prints")
    one, two = wav(tmp_path / "1.wav", 1, 10), wav(tmp_path / "2.wav", 2, 25)
    s.fingerprint("show", "2", two)
    assert s.scrub("show", "1", one) == []


def test_scrubber_reports_unreadable_audio(tmp_path):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"ID3 not audio")
    with pytest.raises(ScrubError):
        Scrubber(tmp_path / "prints").fingerprint("show", "x", bad)


def test_prune_keeps_wanted_and_newest_up_to_peers(tmp_path, monkeypatch):
    monkeypatch.setattr(scrub, "PEERS", 3)
    s = Scrubber(tmp_path / "prints")
    folder = tmp_path / "prints" / "show"
    folder.mkdir(parents=True)
    for i, k in enumerate("abcde"):
        f = folder / f"{k}.npy"
        np.save(f, np.zeros((1, 8), np.uint8))
        os.utime(f, (i, i))
    s.prune("show", {"a"})
    assert sorted(p.name for p in folder.iterdir()) == [f"{k}.npy" for k in "ade"]
    s.forget("show")
    assert not folder.exists()


SECONDS = 30
MP3 = make_mp3(SECONDS)


def tagged(name: str, mp3: bytes = MP3) -> bytes:
    """`mp3` under an ID3 tag naming the episode, so each episode's bytes (and hash) differ."""
    frame = b"TIT2" + (len(name) + 1).to_bytes(4, "big") + b"\0\0\0" + name.encode()
    n = len(frame)
    return b"ID3\x03\x00\x00" + bytes([0, 0, n >> 7, n & 0x7F]) + frame + mp3


class Mp3Remote(Remote):
    """Serves each episode as `mp3`, tagged with its name."""

    def __init__(self, body: bytes, mp3: bytes = MP3):
        super().__init__(body)
        self.mp3 = mp3

    def handler(self, request):
        r = super().handler(request)
        if r.headers.get("content-type") == "audio/mpeg":
            body = tagged(request.url.path.strip("/"), self.mp3)
            return httpx.Response(
                200, content=body, headers={"content-type": "audio/mpeg"}
            )
        return r


class FakeScrubber:
    """Finds `spans` in every episode; the first episode of a show has none to compare with."""

    def __init__(self, spans=((5.0, 15.0),)):
        self.spans = list(spans)
        self.printed: list[str] = []  # the guids, from the title tag in the file
        self.scrubbed: list[str] = []
        self.pruned: set[str] = set()
        self.forgot: list[str] = []
        self.seen_in_feed: list[list[str]] = []
        self.feed: Callable[[], list[str]] = list  # the feed's guids

    @staticmethod
    def guid(audio) -> str:
        """The episode's guid, from the title `tagged` gave it ("<guid>.mp3")."""
        return audio.read_bytes()[21:80].split(b".mp3")[0].decode()

    def fingerprint(self, slug, key, audio):
        if b"broken" in audio.read_bytes()[:100]:
            raise ScrubError("cannot read audio")
        self.printed.append(self.guid(audio))

    def scrub(self, slug, key, audio):
        self.seen_in_feed.append(self.feed())
        self.scrubbed.append(self.guid(audio))
        return self.spans

    def prune(self, slug, keys):
        self.pruned = keys

    def forget(self, slug):
        self.forgot.append(slug)


@pytest.fixture
def fake():
    return FakeScrubber()


@pytest.fixture
def lib(lib, fake, tmp_path):
    lib.scrubber = fake
    fake.feed = lambda: feed_guids(tmp_path)
    return lib


def feed_guids(tmp_path) -> list[str]:
    try:
        root = ET.fromstring((tmp_path / "site" / "the-show" / "feed.xml").read_bytes())
    except FileNotFoundError:
        return []
    return [i.findtext("guid", "") for i in root.iterfind("channel/item")]


def test_new_episodes_join_the_feed_once_cut(lib, fake, tmp_path):
    subscribe(lib, Mp3Remote(feed(("a", "1"), ("b", "2"))), keep=2)
    # Both were read before either was cut, and neither was in the feed while it was cut.
    assert fake.printed == ["b", "a"]
    assert fake.scrubbed == ["b", "a"]
    assert fake.seen_in_feed == [[], ["b"]]
    eps = lib.record("the-show")["show"].episodes
    assert fake.pruned == {key(e) for e in eps}

    left = SECONDS - 10
    for e in eps:
        m = lib.audio.manifest("the-show", e.file)
        assert (e.scrubbed, e.ads_cut, e.duration, e.bytes) == (
            True,
            10.0,
            clock(left),
            m.size,
        )
        assert m.seconds == pytest.approx(left, abs=0.1)
        assert e.file.startswith(
            f"2026-10-0{'2' if e.guid == 'b' else '1'}-ep-{e.guid}-more-"
        )
        assert len(e.file.split(".")) == 3  # <stem>.<cut hash>.mp3
        # The original is untouched, and the sidecar says what was left out.
        assert lib.audio.path(e.audio).read_bytes() == tagged(f"{e.guid}.mp3")
        assert [
            (c["start"], c["end"], c["source"], c["active"])
            for c in lib.audio.cuts(e.audio)
        ] == [(5.0, 15.0, "repeat", True)]
    root = ET.fromstring((tmp_path / "site" / "the-show" / "feed.xml").read_bytes())
    item = root.find("channel/item")
    assert item is not None
    enc = item.find("enclosure")
    assert enc is not None
    assert item.findtext(f"{{{ITUNES}}}duration") == clock(left)
    assert enc.get("length") == str(eps[0].bytes)
    assert enc.get("url", "").endswith("/the-show/" + eps[0].file)
    assert lib.record("the-show")["downloading"] == ""


def test_scrubbed_episodes_are_not_cut_again(lib, fake):
    remote = Mp3Remote(feed(("a", "1")))
    subscribe(lib, remote, keep=2)
    with remote.client() as client:
        remote.feed = feed(("a", "1"), ("b", "2"))
        lib.sync(client)
    assert fake.scrubbed == ["a", "b"]
    eps = lib.record("the-show")["show"].episodes
    assert [(e.guid, e.ads_cut) for e in eps] == [("b", 10.0), ("a", 10.0)]


def test_quiet_hours_download_but_leave_the_ads_for_the_next_sync(lib, fake, capsys):
    """At night the fans stay quiet: a sync still downloads, but reads nothing for ads and
    cuts nothing, and the episodes wait unpublished for the morning's sync."""
    lib.quiet = lambda: True
    remote = Mp3Remote(feed(("a", "1"), ("b", "2")))
    subscribe(lib, remote, keep=2)
    assert (fake.printed, fake.scrubbed) == ([], [])
    eps = lib.record("the-show")["show"].episodes
    assert all(e.audio and not e.scrubbed for e in eps)
    assert feed_guids(lib.site.parent) == []
    assert "the-show: quiet hours; 2 episodes wait" in capsys.readouterr().out
    lib.quiet = lambda: False
    with remote.client() as client:
        lib.sync(client)
    assert fake.scrubbed == ["b", "a"]
    assert sorted(feed_guids(lib.site.parent)) == ["a", "b"]


def test_quiet_hours_starting_mid_sync_stop_before_the_next_scrub(lib, fake):
    calls = iter([False, False, False])  # read both, cut one, then quiet
    lib.quiet = lambda: next(calls, True)
    subscribe(lib, Mp3Remote(feed(("a", "1"), ("b", "2"))), keep=2)
    assert (fake.printed, fake.scrubbed) == (["b", "a"], ["b"])


def test_changed_cuts_are_served_at_the_next_sync(lib, fake, tmp_path):
    remote = Mp3Remote(feed(("a", "1")))
    subscribe(lib, remote)
    with remote.client() as client:
        ep = lib.record("the-show")["show"].episodes[0]
        first = ep.file
        # Say the agent turns the repeat off and cuts elsewhere instead.
        lib.audio.set_source(ep.audio, "repeat", [(5.0, 15.0)], active=False)
        lib.audio.set_source(ep.audio, "agent", [(20.0, 22.0)])
        lib.sync(client)
    ep = lib.record("the-show")["show"].episodes[0]
    assert ep.file != first and ep.ads_cut == 2.0
    assert ep.possible_ads == []  # only ad reads left in are reported, not repeats
    # The old one stays a day, for apps mid-download.
    assert {first, ep.file} <= set(served(lib, "the-show"))
    lib.audio.gc({"the-show": {ep.file}}, {ep.audio}, grace=0)
    assert served(lib, "the-show") == [ep.file]


def test_published_episodes_stay_in_the_feed_while_cut(lib, fake):
    lib.scrubber = None
    remote = Mp3Remote(feed(("a", "1")))
    subscribe(lib, remote)
    lib.scrubber = fake
    with remote.client() as client:
        lib.sync(client)
    assert fake.seen_in_feed == [["a"]]
    rec = lib.record("the-show")
    assert rec is not None
    assert rec["show"].episodes[0].ads_cut == 10.0


def test_scrub_ads_off_leaves_episodes_alone(lib, fake):
    remote = Mp3Remote(feed(("a", "1")))
    subscribe(lib, remote, scrub_ads=False)
    with remote.client() as client:
        assert fake.printed == []
        assert [e.guid for e in lib.record("the-show")["show"].episodes] == ["a"]
        lib.add(client, FEED_URL, keep=3)  # leaving scrub_ads out keeps it off
        assert lib.feeds()["the-show"]["scrub_ads"] is False
        lib.add(client, FEED_URL, scrub_ads=True)
        lib.sync(client)
    assert fake.scrubbed == ["a"]


def test_episode_that_cannot_be_read_is_published_with_its_ads(lib, fake):
    subscribe(lib, Mp3Remote(feed(("broken", "1"), ("b", "2"))))
    rec = lib.record("the-show")
    assert [(e.guid, e.ads_cut) for e in rec["show"].episodes] == [
        ("b", 10.0),
        ("broken", 0.0),
    ]
    assert "couldn't read it for ads: cannot read audio" in rec["error"]
    assert fake.scrubbed == ["b"]


def test_an_episode_too_long_to_look_at_is_published_with_its_ads(
    lib, fake, monkeypatch
):
    """Past MAX_SCRUB_SECONDS, looking for ads would run the sync worker out of memory, at
    the same place on every sync."""
    from podcasts import library

    monkeypatch.setattr(library, "MAX_SCRUB_SECONDS", SECONDS - 1)
    subscribe(lib, Mp3Remote(feed(("a", "1"), ("b", "2"))))
    rec = lib.record("the-show")
    assert {(e.guid, e.scrubbed, e.ads_cut) for e in rec["show"].episodes} == {
        ("a", True, 0.0),
        ("b", True, 0.0),
    }
    assert "too long to look for ads in; published as it is" in rec["error"]
    assert fake.printed == fake.scrubbed == []


def test_nothing_to_compare_publishes_as_is(lib, fake):
    fake.scrub = lambda slug, key, audio: None
    subscribe(lib, Mp3Remote(feed(("a", "1"))))
    ep = lib.record("the-show")["show"].episodes[0]
    assert (ep.guid, ep.scrubbed, ep.ads_cut) == ("a", True, 0.0)
    assert (
        ep.file.startswith("2026-10-01-ep-a-more-") and ep.file.count(".") == 1
    )  # no cut hash
    assert lib.audio.manifest("the-show", ep.file).parts == [
        ["file", ep.audio, 0, len(tagged("a.mp3"))]
    ]


def test_video_is_not_cut(lib, fake):
    extra = """<item><title>Video</title><guid>v</guid><pubDate>9 Oct 2026 10:00:00 +0000</pubDate>
    <enclosure url="https://cdn.example/v.mp4" type="video/mp4" length="0"/></item>"""
    subscribe(lib, Mp3Remote(feed(("a", "1"), extra=extra)))
    assert fake.printed == ["a"]
    assert [e.guid for e in lib.record("the-show")["show"].episodes] == ["v", "a"]


def test_remove_forgets_fingerprints_and_deletes_the_audio(lib, fake, tmp_path):
    subscribe(lib, Mp3Remote(feed(("a", "1"))))
    lib.remove("the-show")
    assert fake.forgot == ["the-show"]
    state = tmp_path / "state"
    assert [list((state / d).iterdir()) for d in ("audio", "cuts", "manifests")] == [
        [],
        [],
        [],
    ]


def test_clock():
    assert [clock(s) for s in (0, 59.6, 754, 3600, 4027.66)] == [
        "0:00",
        "1:00",
        "12:34",
        "1:00:00",
        "1:07:08",
    ]
