"""
CASE MCP server - wires case_tools.py's read-only file/memory tools (and,
only when explicitly enabled, case_write_tools.py's write/delete tools)
into Bionic's own MCP integration (Program -> Integrations), for CASE's
LOCAL chat on the PC (Bionic's native chat window specifically -
case_cli.py/case_gui_web.py never touch this file, they call case_tools.py
functions directly in-process instead).

Write tools default OFF here (case_agent.get_mcp_write_enabled(), toggled
in case_gui_web.py's Settings > General): this path's write safety would
rest ENTIRELY on Bionic's own tool-confirmation prompt
(~/.lmstudio/settings.json -> chat.neverAskForToolConfirmation = false,
skipToolConfirmationPatterns = [] - confirmed empty, so no tool is
exempted). Unlike case_gui_web.py, which has its OWN independent
confirm_callback + native OS dialog as a CASE-controlled fallback, this
server has no such backstop - if Bionic's dialog doesn't fire the way the
setting implies, nothing here would catch it. Also confirmed live on this
machine: Bionic's own agent harness has a separate, DIFFERENT settings
namespace (~/.lmstudio/apps/bionic/.internal/settings.json) with its own
"sessions.firstShellWarning" key - i.e. Bionic can run native shell
commands entirely outside this MCP server, gated by whatever "first
warning" means there, not by the classic chat.neverAskForToolConfirmation
flag above. That's a separate, unresolved risk this file's own on/off
switch does nothing about - if Sati wants shell-command safety in Bionic-
native sessions, that needs checking/disabling directly in Bionic's own
Program -> Integrations / session settings, not here.

Reasoning runs on the Mac mini; this stays on the PC where the files live.

Run manually to sanity-check:  python case_mcp_server.py
Wired into LM Studio/Bionic via ~/.lmstudio/mcp.json.
"""

from mcp.server.mcpserver import MCPServer

import case_agent
import case_tools
import case_write_tools

_write_enabled = case_agent.get_mcp_write_enabled()

_instructions = (
    "Access to the user's Claude Code project files and memory notes, for "
    "CASE to use as a local fallback assistant. list_directory / "
    "read_file / search_files / memory_reflect are read-only, safe to "
    "use freely."
)
if _write_enabled:
    _instructions += (
        " write_file and delete_file MODIFY files under those same "
        "folders - Bionic will ask the user to confirm before either actually "
        "runs, so always explain clearly what you're about to write/delete "
        "and why before calling them."
    )

server = MCPServer(name="case-context", instructions=_instructions)

server.add_tool(case_tools.list_directory)
server.add_tool(case_tools.read_file)
server.add_tool(case_tools.search_files)
server.add_tool(case_tools.memory_reflect)
if _write_enabled:
    server.add_tool(case_write_tools.write_file)
    server.add_tool(case_write_tools.delete_file)
    server.add_tool(case_write_tools.restore_last_backup)


if __name__ == "__main__":
    server.run()  # stdio - local only, spawned directly by Bionic on the PC
