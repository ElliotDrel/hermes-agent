"""Real discord.py serializers, isolated from the gateway suite's global stubs."""
import json
import subprocess
import sys
import pytest

_SCRIPT = r'''
"""Discord-populated install defaults must not cause endless command recreation."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import discord
from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter

async def test_sync_respects_managed_install_types(desired_install, remote_install, recreated):
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)
    async def callback(interaction):
        pass
    command = discord.app_commands.Command(name="example", description="Example command", callback=callback)
    tree.add_command(command)
    desired = command.to_dict(tree)
    if desired_install is not None:
        desired["integration_types"] = desired_install
    raw = {**desired, "id":"123", "application_id":"456", "version":"789",
           "integration_types": remote_install}
    existing = discord.app_commands.AppCommand(data=raw,state=client._connection)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="unused"))
    http = SimpleNamespace(delete_global_command=AsyncMock(),upsert_global_command=AsyncMock(),edit_global_command=AsyncMock())
    adapter._client = SimpleNamespace(application_id=456, http=http, tree=SimpleNamespace(
        get_commands=lambda: [SimpleNamespace(to_dict=lambda tree: desired)],
        fetch_commands=AsyncMock(return_value=[existing])))
    adapter._sleep_between_command_sync_mutations = AsyncMock()
    result = await adapter._safe_sync_slash_commands()
    assert result["recreated"] == recreated, (result, adapter._existing_command_to_payload(existing), desired)
    assert result["unchanged"] == 1-recreated
    assert http.delete_global_command.await_count == recreated
    assert http.upsert_global_command.await_count == recreated
    http.edit_global_command.assert_not_awaited()
    await client.close()

import asyncio
import json
import sys
asyncio.run(test_sync_respects_managed_install_types(*json.loads(sys.argv[1])))
'''

@pytest.mark.parametrize("desired,remote,recreated", [(None,[0],0),(None,[0,1],0),([0],[0,1],0),([1],[0,1],1)])
def test_sync_respects_managed_install_types(desired, remote, recreated):
    result = subprocess.run([sys.executable, "-c", _SCRIPT, json.dumps([desired,remote,recreated])],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
