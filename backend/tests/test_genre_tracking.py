from app.config import _TRACKED_KINDS, all_kind_paths, kind_base_path
from app.routers.tracked import _ALLOWED
from app.services.jav_code import KIND_LABELS_CH, clean_listing_name
from app.services.tracker import _KIND_LABELS


def test_genre_is_first_class_tracked_kind():
    assert "genre" in _TRACKED_KINDS
    assert "genre" in _ALLOWED
    assert KIND_LABELS_CH["genre"] == "類別"
    assert _KIND_LABELS["genre"] == "類別"


def test_genre_base_path_uses_chinese_label():
    assert kind_base_path("genre").endswith("/類別")
    assert dict(all_kind_paths())["genre"] == kind_base_path("genre")


def test_clean_listing_name_strips_genre_suffix():
    assert clean_listing_name("女教師 - 類別 - 影片") == "女教師"
    # Existing kinds keep working.
    assert clean_listing_name("回胴錄 - 系列 - 影片") == "回胴錄"
    assert clean_listing_name("已經乾淨") == "已經乾淨"
