import base64
import unittest
from unittest.mock import AsyncMock, Mock, patch

from hackbot_agents.autowebcompat_repro.android_mcp import (
    AndroidTools,
    install_extension,
)
from mcp.types import CallToolResult, TextContent


def result(text, error=False):
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=error)


class ExtensionInstallationTests(unittest.IsolatedAsyncioTestCase):
    async def install(self, response):
        session = Mock(call_tool=AsyncMock(return_value=response))
        module = "hackbot_agents.autowebcompat_repro.setup_profile"
        with (
            patch(
                f"{module}.resolve_xpi_url",
                return_value=("https://example.com/addon.xpi", "1"),
            ),
            patch(
                f"{module}.download",
                side_effect=lambda url, path: path.write_bytes(b"signed-xpi"),
            ),
            patch(
                f"{module}.extract_extension_id", return_value="chrome-mask@example.com"
            ),
        ):
            await install_extension(session, "chrome-mask")
        return session

    async def test_uses_mcp_base64_permanent_install(self):
        session = await self.install(
            result(
                "Extension installed (permanent):\n  ID: chrome-mask@example.com\n  Type: base64"
            )
        )
        session.call_tool.assert_awaited_once_with(
            "install_extension",
            {
                "type": "base64",
                "value": base64.b64encode(b"signed-xpi").decode(),
                "permanent": True,
            },
        )

    async def test_rejects_tool_error(self):
        with self.assertRaisesRegex(RuntimeError, "installation failed"):
            await self.install(result("Signing verification failed", error=True))

    async def test_rejects_unexpected_extension(self):
        with self.assertRaisesRegex(RuntimeError, "installation failed"):
            await self.install(
                result("Extension installed:\n  ID: another@example.com")
            )


class AndroidToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_install_before_forwarding_and_preserve_tool_result(self):
        browser = Mock()
        response = result("snapshot")
        session = Mock(call_tool=AsyncMock(return_value=response))
        tools = AndroidTools(browser, session, ("chrome-mask",))

        async def install(upstream, slug):
            browser.start.assert_called_once_with(force=True)
            session.call_tool.assert_not_called()

        with patch(
            "hackbot_agents.autowebcompat_repro.android_mcp.install_extension",
            side_effect=install,
        ) as installer:
            self.assertIs(await tools.call("list_extensions", {}), response)
            await tools.call("navigate_page", {"url": "https://example.com"})
            installer.assert_awaited_once_with(session, "chrome-mask")
        session.call_tool.assert_any_await(
            "navigate_page", {"url": "https://example.com"}
        )

    async def test_failed_install_blocks_requested_tool_and_can_retry(self):
        session = Mock(call_tool=AsyncMock(return_value=result("ok")))
        tools = AndroidTools(Mock(), session, ("chrome-mask",))
        with patch(
            "hackbot_agents.autowebcompat_repro.android_mcp.install_extension",
            new_callable=AsyncMock,
        ) as installer:
            installer.side_effect = RuntimeError("install failed")
            with self.assertRaisesRegex(RuntimeError, "install failed"):
                await tools.call("navigate_page", {})
            session.call_tool.assert_not_called()
            installer.side_effect = None
            await tools.call("navigate_page", {})
            self.assertEqual(installer.await_count, 2)

    async def test_restart_reactivates_browser_without_reinstalling_permanent_addon(
        self,
    ):
        browser = Mock()
        tools = AndroidTools(
            browser,
            Mock(call_tool=AsyncMock(return_value=result("ok"))),
            ("chrome-mask",),
        )
        with patch(
            "hackbot_agents.autowebcompat_repro.android_mcp.install_extension",
            new_callable=AsyncMock,
        ) as installer:
            await tools.call("restart_firefox", {})
            await tools.call("list_extensions", {})
            self.assertEqual(
                browser.start.call_args_list,
                [unittest.mock.call(force=True), unittest.mock.call(force=True)],
            )
            installer.assert_awaited_once()
