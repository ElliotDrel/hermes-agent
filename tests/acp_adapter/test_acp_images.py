import pytest
from acp.schema import (
    ImageContentBlock,
    ResourceContentBlock,
    TextContentBlock,
)

from acp_adapter.server import HermesACPAgent, _content_blocks_to_openai_user_content

def test_acp_image_blocks_convert_to_openai_multimodal_content():
    content = _content_blocks_to_openai_user_content([
        TextContentBlock(type="text", text="What is in this image?"),
        ImageContentBlock(type="image", data="aGVsbG8=", mimeType="image/png"),
    ])

    assert content == [
        {"type": "text", "text": "What is in this image?"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,aGVsbG8="},
        },
    ]

def test_text_only_acp_blocks_stay_string_for_legacy_prompt_path():
    content = _content_blocks_to_openai_user_content([
        TextContentBlock(type="text", text="/help"),
    ])

    assert content == "/help"

def test_acp_resource_link_file_is_inlined_as_text(tmp_path):
    attached = tmp_path / "notes.md"
    attached.write_text("# Notes\n\nAttached file body", encoding="utf-8", newline="\n")

    content = _content_blocks_to_openai_user_content([
        TextContentBlock(type="text", text="Please read this file"),
        ResourceContentBlock(
            type="resource_link",
            name="notes.md",
            title="Project notes",
            uri=attached.as_uri(),
            mimeType="text/markdown",
        ),
    ])

    assert content == (
        "Please read this file\n"
        "[Attached file: Project notes (notes.md)]\n"
        f"URI: {attached.as_uri()}\n\n"
        "# Notes\n\nAttached file body"
    )

@pytest.mark.platforms("windows")
def test_native_drive_path_and_file_uri_refer_to_same_attachment(tmp_path):
    from acp_adapter.content import _path_from_file_uri
    path = tmp_path / "notes with spaces.md"
    path.write_text("body", encoding="utf-8")
    assert _path_from_file_uri(str(path)) == _path_from_file_uri(path.as_uri()) == path

@pytest.mark.asyncio
async def test_initialize_advertises_image_prompt_capability():
    response = await HermesACPAgent().initialize()

    assert response.agent_capabilities is not None
    assert response.agent_capabilities.prompt_capabilities is not None
    assert response.agent_capabilities.prompt_capabilities.image is True


def test_inline_and_file_images_use_native_normalization(tmp_path):
    import base64
    from io import BytesIO
    from PIL import Image

    photo = tmp_path / "photo.jpg"
    Image.new("RGB", (2160, 2880), "blue").save(photo)
    raw = photo.read_bytes()
    parts = _content_blocks_to_openai_user_content([
        ImageContentBlock(type="image", data=base64.b64encode(raw).decode(), mimeType="image/jpeg"),
        ResourceContentBlock(type="resource_link", name=photo.name, uri=photo.as_uri(), mimeType="image/jpeg"),
    ])
    images = [p for p in parts if p.get("type") == "image_url"]
    assert len(images) == 2
    for part in images:
        image = Image.open(BytesIO(base64.b64decode(part["image_url"]["url"].partition(",")[2])))
        assert max(image.size) <= 1568
    assert photo.read_bytes() == raw
