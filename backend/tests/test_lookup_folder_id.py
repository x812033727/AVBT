"""``lookup_folder_id`` must be strict: pikpakapi's ``path_to_id``
resolves as far as it can and returns a PARTIAL list when a middle
segment is missing — the deepest existing ancestor. Treating that as a
hit silently redirects callers (video_count, cleanup, finalize) at the
ancestor folder, which for finalize means flattening a whole series
folder. Regression tests for the full/partial/file-leaf cases."""

from app.services.pikpak import PikPakService


def _seg(name, id, file_type="folder"):
    return {"id": id, "name": name, "file_type": file_type}


class StubSvc(PikPakService):
    def __init__(self, result):
        super().__init__()
        self._result = result
        self.calls = 0

    async def _call(self, fn):
        self.calls += 1
        return self._result


async def test_full_resolution_returns_leaf_and_caches():
    svc = StubSvc([_seg("AVBT", "a"), _seg("製作商", "b"), _seg("ROCKET", "c")])
    assert await svc.lookup_folder_id("AVBT/製作商/ROCKET") == "c"
    # Second hit comes from the cache — no extra API call.
    assert await svc.lookup_folder_id("AVBT/製作商/ROCKET") == "c"
    assert svc.calls == 1


async def test_partial_resolution_is_a_miss_not_the_ancestor():
    # 未分類/RCTD-999 missing → pikpakapi stops at 未分類 and returns
    # the partial chain. That must NOT resolve to the ancestor's id.
    svc = StubSvc([_seg("AVBT", "a"), _seg("製作商", "b"), _seg("未分類", "c")])
    assert await svc.lookup_folder_id("AVBT/製作商/未分類/RCTD-999") == ""
    # And the miss is not cached as anything.
    assert "AVBT/製作商/未分類/RCTD-999" not in svc._folder_cache


async def test_empty_result_is_a_miss():
    svc = StubSvc([])
    assert await svc.lookup_folder_id("AVBT/nope") == ""


async def test_file_leaf_is_a_miss():
    svc = StubSvc([_seg("AVBT", "a"), _seg("RCTD-740.mp4", "f", file_type="file")])
    assert await svc.lookup_folder_id("AVBT/RCTD-740.mp4") == ""
