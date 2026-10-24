import json

from pen_library import PEN_COUNT, PenLibrary, clamp_speed, normalize_hex


def test_defaults_and_roundtrip(tmp_path):
    path = tmp_path / "pens.json"
    lib = PenLibrary(str(path))
    assert len(lib.all()) == PEN_COUNT
    lib.update_from_form({"name_1": "Micron 03", "color_1": "#123456", "speed_1": "8", "name_2": "Red", "speed_2": "999"})
    assert path.exists()
    lib2 = PenLibrary(str(path))
    assert lib2.get(1) == {"name": "Micron 03", "color": "#123456", "speed": 8}
    assert lib2.get(2)["speed"] == 42  # clamped


def test_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "pens.json"
    path.write_text("{not json")
    lib = PenLibrary(str(path))
    assert lib.get(1)["name"] == "Black"


def test_suggest_nearest_colour_and_memory(tmp_path):
    lib = PenLibrary(str(tmp_path / "pens.json"))
    assert lib.suggest("#000000") == 1
    assert lib.suggest("#ff0000") == 2     # nearest to default red
    assert lib.suggest("#0000ff") == 3     # nearest to default blue
    lib.remember({"#0000ff": 7})
    assert lib.suggest("#0000FF") == 7
    data = json.loads((tmp_path / "pens.json").read_text())
    assert data["color_memory"] == {"#0000ff": 7}


def test_helpers():
    assert normalize_hex("#ABC") == "#aabbcc"
    assert normalize_hex("garbage") == "#000000"
    assert clamp_speed("0") == 1 and clamp_speed(50) == 42 and clamp_speed("x") == 10
