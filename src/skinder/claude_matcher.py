"""Claude Code matching through the user's locally authenticated CLI."""

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
from typing import Any

from .matcher import MATCH_INSTRUCTIONS, MatchError, terminate
from .models import EngineStatus, MatchResult

SETTINGS_ARGS = (
    "--safe-mode", "--setting-sources", "", "--settings", '{"disableAllHooks":true}',
)
ISOLATION_ARGS = (
    *SETTINGS_ARGS, "--tools", "", "--strict-mcp-config",
    "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands",
    "--no-chrome", "--no-session-persistence",
)
REQUIRED_FLAGS = (
    "--print", "--output-format", "--json-schema", "--system-prompt",
    *(arg for arg in ISOLATION_ARGS if arg.startswith("--")),
)


def claude_environment() -> dict[str, str]:
    # Keep the normal login location, but not a parent agent session or credential override.
    return {
        key: value for key, value in os.environ.items()
        if key == "CLAUDE_CONFIG_DIR" or not (
            key.startswith(("ANTHROPIC_", "CLAUDE_")) or key == "CLAUDECODE"
        )
    }


async def invoke(
    executable: str, args: list[str], *, directory: str, timeout: float,
    limit: int, data: bytes | None = None,
) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        executable, *args,
        stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        cwd=directory, env=claude_environment(), start_new_session=True,
    )
    assert process.stdout is not None

    async def write() -> None:
        if process.stdin is not None:
            try:
                process.stdin.write(data)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

    async def discard() -> None:
        while await process.stdout.read(65536):
            pass

    writer = asyncio.create_task(write())
    try:
        async with asyncio.timeout(timeout):
            output = bytearray()
            while chunk := await process.stdout.read(min(65536, limit + 1 - len(output))):
                output.extend(chunk)
                if len(output) > limit:
                    raise MatchError("Claude Code returned too much output. Try again.")
            await writer
            return await process.wait(), bytes(output)
    finally:
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer
        # Drain while terminating: a full stdout pipe must not prevent process.wait().
        drainer = asyncio.create_task(discard())
        try:
            await terminate(process)
        finally:
            drainer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drainer


class ClaudeMatcher:
    def __init__(self, timeout: float = 180) -> None:
        self.timeout = timeout

    async def _supported(self, executable: str, directory: str) -> bool:
        code, output = await invoke(
            executable, ["--help"], directory=directory, timeout=10, limit=128_000,
        )
        help_text = output.decode("utf-8", errors="replace")
        return code == 0 and all(flag in help_text for flag in REQUIRED_FLAGS)

    async def health(self) -> EngineStatus:
        executable = shutil.which("claude")
        if not executable:
            return EngineStatus(
                installed=False, authenticated=False, ready=False,
                message="Install Claude Code, then run claude auth login.",
            )
        try:
            with tempfile.TemporaryDirectory(prefix="skinder-claude-health-") as directory:
                if not await self._supported(executable, directory):
                    return EngineStatus(
                        installed=True, authenticated=False, ready=False,
                        message="Update Claude Code with claude update to enable matching.",
                    )
                code, output = await invoke(
                    executable, [*SETTINGS_ARGS, "auth", "status", "--json"],
                    directory=directory, timeout=10, limit=32_000,
                )
                status = json.loads(output)
                authenticated = code == 0 and isinstance(status, dict) and status.get("loggedIn") is True
                return EngineStatus(
                    installed=True, authenticated=authenticated, ready=authenticated,
                    message="Claude Code is ready." if authenticated else "Run claude auth login.",
                )
        except (OSError, TimeoutError, ValueError, MatchError):
            return EngineStatus(
                installed=True, authenticated=False, ready=False,
                message="Could not check Claude Code login. Run claude auth status.",
            )

    async def match(self, request: dict[str, Any]) -> str:
        executable = shutil.which("claude")
        if not executable:
            raise MatchError(
                "Claude Code CLI is missing. Install it and run claude auth login, then try again.",
            )
        try:
            async with asyncio.timeout(self.timeout):
                with tempfile.TemporaryDirectory(prefix="skinder-claude-match-") as directory:
                    if not await self._supported(executable, directory):
                        raise MatchError("Update Claude Code with claude update to enable matching.")
                    code, output = await invoke(
                        executable,
                        ["--print", "--output-format", "json",
                         "--json-schema", json.dumps(MatchResult.model_json_schema()),
                         "--system-prompt", MATCH_INSTRUCTIONS, *ISOLATION_ARGS],
                        directory=directory, timeout=self.timeout, limit=1_048_576,
                        data=json.dumps(request, ensure_ascii=False).encode(),
                    )
                    if code:
                        raise MatchError(
                            "Claude Code could not complete this match. Check claude auth status "
                            "and your Claude usage limits, then try again.",
                        )
                    envelope = json.loads(output)
                    if not (
                        isinstance(envelope, dict) and envelope.get("type") == "result"
                        and envelope.get("subtype") == "success" and envelope.get("is_error") is False
                        and isinstance(envelope.get("structured_output"), dict)
                    ):
                        raise MatchError("Claude Code returned no usable result. Try again.")
                    result = json.dumps(envelope["structured_output"], ensure_ascii=False)
                    if len(result.encode()) > 100_000:
                        raise MatchError("Claude Code returned too much output. Try again.")
                    return result
        except TimeoutError as exc:
            raise MatchError(f"Matching timed out after {self.timeout:g} seconds. Try again.") from exc
        except (ValueError, UnicodeError) as exc:
            raise MatchError("Claude Code returned no usable result. Try again.") from exc
        except OSError as exc:
            raise MatchError("Could not start or read Claude Code. Check your local installation.") from exc
