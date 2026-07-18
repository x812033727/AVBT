"""Genres on JavBus detail pages live OUTSIDE the span.header pattern:
a bare ``<p class="header">類別:</p>`` followed by a sibling ``<p>`` of
``<span class="genre">`` checkboxes. The header-span loop can never see
them — every cached detail shipped with genres=[] (live: whole cache,
15,330 rows). Parse the spans directly; uncensored pages reuse
span.genre for ACTRESS links (href /star/…), which must not be counted.
"""

from app.scrapers.javbus import _parse_detail

_HTML = """
<div class="container">
  <h3>PRED-865 パワハラ上司に寝取られる妻</h3>
  <div class="row movie">
    <div class="col-md-9"><a class="bigImage" href="/cover.jpg"><img src="/cover.jpg"></a></div>
    <div class="col-md-3 info">
      <p><span class="header">識別碼:</span> PRED-865</p>
      <p><span class="header">發行日期:</span> 2026-06-01</p>
      <p><span class="header">長度:</span> 123分鐘</p>
      <p><span class="header">導演:</span> <a href="https://www.javbus.com/director/3c1">モルツくん</a></p>
      <p><span class="header">製作商:</span> <a href="https://www.javbus.com/studio/i6">プレミアム</a></p>
      <p><span class="header">發行商:</span> <a href="https://www.javbus.com/label/6k8">エレガンス</a></p>
      <p class="header">類別:<span id="genre-toggle" class="glyphicon glyphicon-plus"></span></p>
      <p>
        <span class="genre"><label><input type="checkbox" name="gr_sel" value="1f"><a href="https://www.javbus.com/genre/1f">苗條</a></label></span>
        <span class="genre"><label><input type="checkbox" name="gr_sel" value="4"><a href="https://www.javbus.com/genre/4">中出</a></label></span>
        <span class="genre"><label><input type="checkbox" name="gr_sel" value="4o"><a href="https://www.javbus.com/genre/4o">高畫質</a></label></span>
      </p>
      <p class="star-show"><span class="header">演員:</span></p>
      <p>
        <span class="genre"><a href="https://www.javbus.com/star/abc">女優名</a></span>
      </p>
    </div>
  </div>
</div>
"""


def test_genres_parsed_from_genre_spans():
    d = _parse_detail(_HTML, "PRED-865")
    names = [g.name for g in d.genres]
    ids = [g.id for g in d.genres]
    assert names == ["苗條", "中出", "高畫質"]
    assert ids == ["1f", "4", "4o"]
    # actress link reusing span.genre (uncensored layout) must NOT count
    assert "女優名" not in names
    # the rest of the info loop still works
    assert d.studio and d.studio.id == "i6"
    assert d.label and d.label.id == "6k8"
    assert d.release_date == "2026-06-01"


def test_no_genres_yields_empty():
    d = _parse_detail("<div class='container'><h3>X-1 t</h3><div class='info'></div></div>", "X-1")
    assert d.genres == []


async def test_empty_genre_cache_hit_refetches_and_heals(tmp_path, monkeypatch):
    """A cached detail with genres=[] predates the genre-parse fix — a
    view must fall through to one refetch and heal the cached row."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.scrapers.javbus as jb
    import app.services.detail_cache as dc
    from app.database import Base
    from app.schemas import GenreRef, MovieDetail

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/c.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(dc, "SessionLocal", sm)
    jb._detail_cache.clear()

    # Seed a pre-fix row: title present, genres empty.
    await dc.put("HEAL-1", MovieDetail(code="HEAL-1", title="t", release_date="2020-01-01"))

    calls = {"n": 0}

    async def fake_fetch(cli, url, **kw):
        calls["n"] += 1
        return "<html>stub</html>"

    def fake_parse(html, code):
        return MovieDetail(code=code, title="t", release_date="2020-01-01",
                           genres=[GenreRef(name="中出", id="4")])

    monkeypatch.setattr(jb, "_fetch", fake_fetch)
    monkeypatch.setattr(jb, "_parse_detail", fake_parse)
    monkeypatch.setattr(jb, "_get_client", lambda: object())
    async def fake_magnets(*a, **kw):
        return []
    if hasattr(jb, "_fetch_magnets"):
        monkeypatch.setattr(jb, "_fetch_magnets", fake_magnets)

    d = await jb.fetch_detail("HEAL-1")
    assert calls["n"] == 1                    # stale hit fell through
    assert [g.name for g in d.genres] == ["中出"]
    healed = await dc.get("HEAL-1")
    assert healed and healed.genres           # DB row healed
    # Second view: no more fetches.
    d2 = await jb.fetch_detail("HEAL-1")
    assert calls["n"] == 1
    assert d2.genres
    await engine.dispose()


async def test_backfill_picks_genre_stale_rows(tmp_path, monkeypatch):
    """When no truly-missing codes remain, backfill slots fill with
    pre-fix rows (genres=[]) so the whole cache heals over time."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.services.detail_backfill as db_mod
    import app.services.detail_cache as dc
    from app.database import Base
    from app.schemas import GenreRef, MovieDetail

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/b.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(dc, "SessionLocal", sm)
    monkeypatch.setattr(db_mod, "SessionLocal", sm)
    monkeypatch.setattr(db_mod, "_attempted", set())

    await dc.put("STALE-1", MovieDetail(code="STALE-1", title="t"))
    await dc.put("FRESH-1", MovieDetail(code="FRESH-1", title="t",
                                        genres=[GenreRef(name="g", id="1")]))

    class _P:
        def peek(self):
            return {"STALE-1", "FRESH-1"}   # all downloaded & cached

    monkeypatch.setattr(db_mod, "presence_index", _P())
    picked = await db_mod._pick_missing_codes(5)
    assert "STALE-1" in picked              # pre-fix row queued to heal
    assert "FRESH-1" not in picked          # healthy row untouched
    await engine.dispose()
