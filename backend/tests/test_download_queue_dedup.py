"""併發同 btih 去重。

sent-hash 快取只在送出成功後追加,兩個 dedup key 不同的 job(同一作品的
兩種番號拼法,如 NHDTC-3603 / NHDTC-03603)同時解析到同一磁力時,舊行為
會雙送 PikPak(2026-08-04 實況:兩任務相差 0.5ms)。修法=送出期間宣告
btih 在途:code 分支在挑磁力時把在途 hash 併入 skip 集,direct 分支在
sent 檢查時一併看在途集。
"""

import asyncio
from types import SimpleNamespace

from app.schemas import SendAllOptions
from app.services import download_queue as dq

BTIH = "D967088166B41DEBE83A89606F3D2AAFF2B5D6AB"
MAGNET = f"magnet:?xt=urn:btih:{BTIH}&dn=test"


def _magnet_obj(link: str = MAGNET) -> SimpleNamespace:
    return SimpleNamespace(
        link=link, name="test", size="5.0GB", is_hd=True, has_subtitle=False
    )


def _job(code: str, **kw) -> dq.Job:
    return dq.Job(
        code=code, options=SendAllOptions(), source="test", **kw
    )


def _patch_env(monkeypatch, submit_stub):
    """空快取 + 假 scraper/pick/pikpak/DB log,回傳送出呼叫記錄 list。"""
    monkeypatch.setattr(dq, "_sent_hashes_cache", set())
    monkeypatch.setattr(dq, "_inflight_hashes", set())

    async def fetch(code):
        await asyncio.sleep(0)
        return SimpleNamespace(magnets=[_magnet_obj()])

    monkeypatch.setattr(
        dq, "scraper", SimpleNamespace(fetch_detail_resolved=fetch)
    )

    def pick(magnets, **kw):
        skip = kw.get("skip_hashes") or set()
        for m in magnets:
            if dq.extract_btih(m.link) not in skip:
                return m
        return None

    monkeypatch.setattr(dq, "pick_best_magnet", pick)
    monkeypatch.setattr(
        dq, "pikpak_service", SimpleNamespace(offline_download=submit_stub)
    )

    async def log(*, code, magnet, **kw):
        dq._note_sent_hash(magnet)

    monkeypatch.setattr(dq, "_log_offline_task", log)


async def test_concurrent_same_btih_submits_once(monkeypatch):
    calls = []

    async def submit(payload):
        calls.append(payload.magnet)
        await asyncio.sleep(0.02)  # 停在送出中,讓另一個 job 進到挑磁力
        return SimpleNamespace(
            id="task1", file_id="f1", name="test", phase="PENDING", message=""
        )

    _patch_env(monkeypatch, submit)
    q = dq.DownloadQueue(concurrency=2)
    r1, r2 = await asyncio.gather(
        q._process(_job("NHDTC-3603")), q._process(_job("NHDTC-03603"))
    )
    assert len(calls) == 1, "同 btih 併發必須只送一次"
    assert sorted([r1.status, r2.status]) == ["sent", "skipped_already_sent"]
    assert not dq._inflight_hashes, "宣告必須在收尾釋放"


async def test_claim_released_on_submit_failure(monkeypatch):
    attempts = []

    async def submit(payload):
        attempts.append(payload.magnet)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        return SimpleNamespace(
            id="task2", file_id="f2", name="test", phase="PENDING", message=""
        )

    _patch_env(monkeypatch, submit)
    q = dq.DownloadQueue(concurrency=1)
    first = await q._process(_job("NHDTC-3603"))
    assert first.status == "failed"
    assert not dq._inflight_hashes, "送出失敗必須釋放宣告"
    retry = await q._process(_job("NHDTC-3603"))
    assert retry.status == "sent"
    assert len(attempts) == 2


async def test_direct_submit_skips_inflight_hash(monkeypatch):
    calls = []

    async def submit(payload):
        calls.append(payload.magnet)
        return SimpleNamespace(
            id="task3", file_id="f3", name="test", phase="PENDING", message=""
        )

    _patch_env(monkeypatch, submit)
    dq._inflight_hashes.add(BTIH)  # 模擬 code job 正以同 btih 送出中
    q = dq.DownloadQueue(concurrency=1)
    r = await q._process(_job("ABC-123", direct_magnet=MAGNET))
    assert r.status == "skipped_already_sent"
    assert calls == []
    # force=true 維持可強制再送
    forced = await q._process(_job("ABC-123", direct_magnet=MAGNET, force=True))
    assert forced.status == "sent"
    assert len(calls) == 1
