"""Native image scaling and shared image byte budget (HERMES-FORK-031)."""

import base64
import json
from io import BytesIO
from pathlib import Path
from PIL import Image
from agent.anthropic_adapter import build_anthropic_kwargs
from agent.image_routing import _file_to_data_url


def test_nous_wire_request_fits_with_large_user_and_tool_images():
    image = "data:image/png;base64," + "A" * 3_000_000
    messages = [{"role": "system", "content": "s" * 300_000}]
    for n in range(5):
        messages.extend(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": str(n)},
                        {"type": "image_url", "image_url": {"url": image + str(n)}},
                    ],
                },
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call" + str(n),
                            "type": "function",
                            "function": {"name": "vision_analyze", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call" + str(n),
                    "content": [
                        {"type": "text", "text": "crop"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64," + "B" * 200_000
                            },
                        },
                    ],
                },
            ]
        )
    before = json.dumps(messages)
    body = build_anthropic_kwargs(
        "test",
        messages,
        None,
        1024,
        None,
        base_url="https://inference-api.nousresearch.com/v1",
    )
    assert len(json.dumps(body).encode()) < 10_000_000
    assert json.dumps(messages) == before
    assert body["system"] == "s" * 300_000


def test_native_image_size_is_bounded_before_history_and_replay(tmp_path, monkeypatch):
    path = tmp_path / "photo.png"
    Image.new("RGB", (4096, 3072), "blue").save(path)
    url = _file_to_data_url(path)
    resized = Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert max(resized.size) <= 1568
    assert _file_to_data_url(path) == url
    from agent.image_routing import build_native_content_parts, split_image_notes

    parts, skipped = build_native_content_parts("photo", [str(path)])
    assert skipped == []
    _, refs = split_image_notes(parts[0]["text"])
    retained = Path(refs[0])
    assert max(Image.open(retained).size) <= 1568
    assert _file_to_data_url(retained) == url
    assert Image.open(path).size == (4096, 3072)
    from gateway.session_api_media import (
        commit_api_images,
        restore_api_images,
        _compacted,
    )

    original_url = (
        "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
    )
    original_parts = [{"type": "image_url", "image_url": {"url": original_url}}]
    media = commit_api_images(original_parts)
    restored = restore_api_images(original_parts, media)
    assert restored[1]["image_url"]["url"] == url

    def no_resize_in_compaction(*args, **kwargs):
        raise AssertionError("settlement must use the recorded normalized digest")

    monkeypatch.setattr(
        "tools.vision_tools._resize_image_for_vision", no_resize_in_compaction
    )
    compacted = _compacted(original_parts, media)
    assert compacted[0]["image_url"]["url"].startswith("hermes-retained:")
    assert _compacted(original_parts, [{**media[0], "sha256": "0" * 64}]) is None


def test_user_and_tool_images_share_the_budget_and_latched_retirement():
    import copy
    from agent.context_compressor import evict_stale_outbound_tool_images
    from agent.image_eviction_policy import OUTBOUND_IMAGE_BUDGET_BYTES

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"photo {n}; original retained reference"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + str(n) * 3_000_000},
                },
            ],
        }
        for n in range(4)
    ]
    messages.append(
        {
            "role": "tool",
            "tool_call_id": "crop",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + "B" * 200_000},
                }
            ],
        }
    )
    before = json.dumps(messages)
    wire = copy.deepcopy(messages)
    retired = set()
    assert evict_stale_outbound_tool_images(wire, retired) > 0
    remaining = sum(
        len(p["image_url"]["url"])
        for m in wire
        for p in m["content"]
        if p["type"] == "image_url"
    )
    assert remaining <= OUTBOUND_IMAGE_BUDGET_BYTES
    assert [m["role"] for m in wire] == [m["role"] for m in messages]
    assert wire[0]["content"][0] == messages[0]["content"][0]
    assert wire[0]["content"][1]["text"] == "[image removed to save context]"
    later = copy.deepcopy(messages[:-1])
    evict_stale_outbound_tool_images(later, retired)
    assert later[0]["content"] == wire[0]["content"]
    assert json.dumps(messages) == before


def test_corrupt_photo_does_not_drop_the_good_photo(tmp_path, monkeypatch):
    from agent.image_routing import build_native_content_parts
    from hermes_state_runtime import RuntimeStoreError

    good = tmp_path / "good.jpg"
    Image.new("RGB", (4096, 3072), "blue").save(good)
    corrupt = tmp_path / "corrupt.jpg"
    corrupt_bytes = good.read_bytes()[:2048]
    corrupt.write_bytes(corrupt_bytes)
    reads = {good: 0, corrupt: 0}
    read_bytes = Path.read_bytes

    def counted_read(path):
        if path in reads:
            reads[path] += 1
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read)

    def unavailable(*args, **kwargs):
        raise RuntimeStoreError("storage_unavailable")

    monkeypatch.setattr("gateway.session_api_media.commit_api_images", unavailable)
    parts, skipped = build_native_content_parts("compare", [str(corrupt), str(good)])
    assert skipped == []
    assert reads == {good: 1, corrupt: 1}
    images = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
    assert len(images) == 2
    assert base64.b64decode(images[0].partition(",")[2]) == corrupt_bytes
    assert (
        max(Image.open(BytesIO(base64.b64decode(images[1].partition(",")[2]))).size)
        <= 1568
    )


def test_resending_a_retired_photo_keeps_the_newest_copy():
    import copy
    from agent.context_compressor import evict_stale_outbound_tool_images

    def photo(value):
        return {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + value * 3_000_000},
                }
            ],
        }

    history = [photo(value) for value in "ABCD"]
    retired = set()
    first = copy.deepcopy(history)
    evict_stale_outbound_tool_images(first, retired)
    assert first[0]["content"][0]["type"] == "text"
    history.append(photo("A"))
    resent = copy.deepcopy(history)
    evict_stale_outbound_tool_images(resent, retired)
    assert resent[0]["content"][0]["type"] == "text"
    assert resent[-1]["content"][0]["type"] == "image_url"


def test_normalization_applies_exif_orientation_before_resizing(tmp_path):
    photo = tmp_path / "oriented.jpg"
    image = Image.new("RGB", (2048, 3072), "blue")
    exif = Image.Exif()
    exif[274] = 6
    image.save(photo, exif=exif)
    url = _file_to_data_url(photo)
    result = Image.open(BytesIO(base64.b64decode(url.partition(",")[2])))
    assert max(result.size) <= 1568
    assert result.width > result.height
    assert result.width * image.width == result.height * image.height
    assert 274 not in result.getexif()


def test_normalization_preserves_alpha_when_downscaling(tmp_path):
    photo = tmp_path / "transparent.png"
    Image.new("RGBA", (3200, 2400), (0, 80, 160, 128)).save(photo)
    url = _file_to_data_url(photo)
    result = Image.open(BytesIO(base64.b64decode(url.partition(",")[2])))
    assert max(result.size) <= 1568
    assert url.startswith("data:image/png;base64,")
    assert "A" in result.getbands()
    assert result.getchannel("A").getextrema() == (128, 128)

    palette = Image.new("P", (3200, 2400), 0)
    palette_path = tmp_path / "transparent.gif"
    palette.save(palette_path, transparency=0)
    palette_url = _file_to_data_url(palette_path)
    palette_result = Image.open(
        BytesIO(base64.b64decode(palette_url.partition(",")[2]))
    )
    assert max(palette_result.size) <= 1568
    assert palette_url.startswith("data:image/png;base64,")
    assert palette_result.convert("RGBA").getchannel("A").getextrema() == (0, 0)
