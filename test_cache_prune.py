"""Test cache pruning — đảm bảo cache cũ bị xóa khi vượt MAX_CACHE_MB."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import prune_cache_dir


def _mk(dir_path, cache_id, ext, size, age_sec):
    path = os.path.join(dir_path, f"{cache_id}.{ext}")
    with open(path, "wb") as f:
        f.write(b"x" * size)
    ts = time.time() - age_sec
    os.utime(path, (ts, ts))
    return path


def _cid(ch):
    return ch * 32


def test_prune_noop_when_under_limit(tmp_path):
    _mk(str(tmp_path), _cid("a"), "mp3", 100, 100)
    freed = prune_cache_dir(str(tmp_path), max_bytes=1000)
    assert freed == 0
    assert os.path.exists(os.path.join(str(tmp_path), _cid("a") + ".mp3"))


def test_prune_evicts_oldest_group(tmp_path):
    d = str(tmp_path)
    # 3 cache nhóm, tổng 330 bytes; limit 300 → cần giải phóng tới target 270
    _mk(d, _cid("a"), "mp3", 100, 100)   # cũ nhất
    _mk(d, _cid("a"), "json", 10, 100)
    _mk(d, _cid("b"), "mp3", 100, 50)
    _mk(d, _cid("b"), "json", 10, 50)
    _mk(d, _cid("c"), "mp3", 100, 10)    # mới nhất
    _mk(d, _cid("c"), "json", 10, 10)

    freed = prune_cache_dir(d, max_bytes=300)

    assert freed == 110  # nhóm "a" bị xóa
    assert not os.path.exists(os.path.join(d, _cid("a") + ".mp3"))
    assert not os.path.exists(os.path.join(d, _cid("a") + ".json"))
    assert os.path.exists(os.path.join(d, _cid("b") + ".mp3"))
    assert os.path.exists(os.path.join(d, _cid("c") + ".mp3"))


def test_prune_skips_active_ids(tmp_path):
    d = str(tmp_path)
    _mk(d, _cid("a"), "mp3", 100, 100)
    _mk(d, _cid("b"), "mp3", 100, 10)

    # "a" cũ nhất nhưng đang được tạo → phải bỏ qua, xóa "b" cũng không được
    # vì "b" là mới nhất: total = 200 > max 150 → phải xóa gì đó; "a" bị skip
    # nên xóa "b" để về target 135
    freed = prune_cache_dir(d, max_bytes=150, skip_ids={_cid("a")})

    assert freed == 100
    assert os.path.exists(os.path.join(d, _cid("a") + ".mp3"))
    assert not os.path.exists(os.path.join(d, _cid("b") + ".mp3"))


def test_prune_ignores_unrelated_files(tmp_path):
    d = str(tmp_path)
    _mk(d, _cid("a"), "mp3", 100, 100)
    junk = os.path.join(d, "not-a-cache-file.txt")
    with open(junk, "wb") as f:
        f.write(b"y" * 1000)
    os.utime(junk, (time.time() - 1000, time.time() - 1000))

    # junk không được tính → tổng = 100 <= 200 → không xóa gì
    freed = prune_cache_dir(d, max_bytes=200)
    assert freed == 0
    assert os.path.exists(junk)
