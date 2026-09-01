"""`search_files` fallback when pikpakapi has no server-side search.

The installed pikpakapi exposes no `file_search`, so every search goes
through the client-side fallback. That fallback used to call
`file_list(parent_id, size=500)` once: a single unpaged page of the
parent's *direct* children. Two consequences, both silent:

- a folder with more than 500 children hid its tail from search;
- a whole-drive lookup (`parent_id=""`) could never match anything,
  because AVBT files live under `AVBT/製作商/<studio>/<系列>/` and the
  root has only folders. Seven rounds of the backfill rotation logged
  "this endpoint returns 0 for files that certainly exist" without the
  cause ever being pinned down.

So: page through the level (via `list_all_files`), and offer an explicit
`recursive` walk for existence checks, which fails loudly rather than
silently truncating when the subtree is too big.
"""

import pytest

from app.services.pikpak import PikPakError, PikPakService


def _raw(fid: str, name: str, *, folder: bool = False, parent: str = "") -> dict:
    return {
        "id": fid,
        "name": name,
        "kind": "drive#folder" if folder else "drive#file",
        "parent_id": parent,
        "size": "1",
    }


class FakeClient:
    """file_list over a fixed tree, honouring next_page_token paging."""

    def __init__(self, tree: dict[str, list[dict]], page: int = 2):
        self.tree = tree
        self.page = page
        self.calls: list[tuple[str, str]] = []

    async def file_list(self, parent_id="", size=100, next_page_token=""):
        self.calls.append((parent_id, next_page_token))
        children = self.tree.get(parent_id, [])
        start = int(next_page_token or 0)
        chunk = children[start : start + self.page]
        nxt = start + self.page
        return {
            "files": chunk,
            "next_page_token": str(nxt) if nxt < len(children) else "",
        }


@pytest.fixture()
def service(monkeypatch):
    svc = PikPakService()

    def _install(tree, page=2):
        client = FakeClient(tree, page=page)

        async def fake_ensure(*a, **k):
            return client

        async def fake_call(fn, *a, **k):
            return await fn(client)

        monkeypatch.setattr(svc, "_ensure", fake_ensure)
        monkeypatch.setattr(svc, "_call", fake_call)
        return client

    svc.install = _install  # type: ignore[attr-defined]
    return svc


async def test_pages_past_the_first_page(service):
    """The match sits beyond page 1 — the old single-page call missed it."""
    tree = {"dir": [_raw(f"f{i}", f"OTHER-{i}.mp4") for i in range(5)]}
    tree["dir"].append(_raw("hit", "DTT-086.mp4"))
    service.install(tree, page=2)

    found = await service.search_files("dtt-086", parent_id="dir")

    assert [f.name for f in found] == ["DTT-086.mp4"]


async def test_non_recursive_stays_on_the_level(service):
    """Default contract is unchanged: the frontend's in-folder filter."""
    tree = {
        "": [_raw("sub", "AVBT", folder=True)],
        "sub": [_raw("hit", "DTT-086.mp4", parent="sub")],
    }
    service.install(tree)

    assert await service.search_files("DTT-086") == []


async def test_recursive_finds_files_below_the_root(service):
    """The existence check the rotation actually needs."""
    tree = {
        "": [_raw("a", "AVBT", folder=True)],
        "a": [_raw("b", "製作商", folder=True)],
        "b": [_raw("hit", "DTT-086.mp4", parent="b"), _raw("x", "OTHER.mp4")],
    }
    service.install(tree)

    found = await service.search_files("DTT-086", recursive=True)

    assert [f.name for f in found] == ["DTT-086.mp4"]


async def test_recursive_matches_folder_names_too(service):
    """Wrapper folders are what hygiene reports; search must see them."""
    tree = {
        "": [_raw("a", "AVBT", folder=True)],
        "a": [_raw("w", "[Thz.la]ap-417", folder=True, parent="a")],
        "w": [],
    }
    service.install(tree)

    found = await service.search_files("ap-417", recursive=True)

    assert [f.name for f in found] == ["[Thz.la]ap-417"]


async def test_recursive_raises_instead_of_truncating(service):
    """A too-large subtree must fail loudly — a short result list here
    reads as 'not in the cloud', which is how bad decisions get made."""
    tree = {"": [_raw(f"d{i}", f"dir{i}", folder=True) for i in range(4)]}
    for i in range(4):
        tree[f"d{i}"] = []
    service.install(tree)

    with pytest.raises(PikPakError, match="範圍過大"):
        await service.search_files("nope", recursive=True, folder_cap=3)
