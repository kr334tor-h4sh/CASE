' Silent launcher for CASE's GUI - no console window flash.
' Double-click this (or the "CASE" desktop shortcut) to start CASE.
'
' WindowStyle must be 1 (SW_SHOWNORMAL), NOT 0 (SW_HIDE) - found 2026-09-13
' as the real cause of "double-click does nothing": pythonw.exe has no
' console to hide in the first place (that's the whole point of pythonw
' vs python), so WindowStyle here was never actually suppressing a console
' flash - but it turns out this SW_HIDE hint propagates via STARTUPINFO
' down to pywebview's own WinForms window, causing it to be created (right
' size, right position, correct "CASE" title - confirmed via direct Win32
' EnumWindows/IsWindowVisible inspection) but never actually shown.
' Confirmed by isolating launch method alone: identical code launched
' directly (no WScript.Shell.Run involved) always shows a visible window;
' launched through this script with WindowStyle 0 never does, regardless
' of any other setting. 1 shows the window normally - still no console
' flash either way, since pythonw.exe never had one.
' A bare "pythonw" (relying on PATH) was tried here and confirmed NOT to
' reliably resolve under WScript.Shell.Run's async mode (bWaitOnReturn=
' False) - it works fine synchronously but silently fails to actually
' launch anything in the async mode this script actually needs. So this
' uses an absolute path after all, built from %LOCALAPPDATA% instead of a
' hardcoded username - update the Python312 folder name below if you're
' on a different Python version, or replace the whole line with your own
' pythonw.exe path if it's installed somewhere else entirely (e.g. via
' the Microsoft Store or Anaconda).
Set objShell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
objShell.CurrentDirectory = scriptDir
pythonwPath = objShell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe")
objShell.Run """" & pythonwPath & """ ""case_gui_web.py""", 1, False
