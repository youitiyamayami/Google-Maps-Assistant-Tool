@echo off
setlocal
REM このBATのあるフォルダを作業ディレクトリに
cd /d "%~dp0"

REM PowerShell と ps1 のパス
set "POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "PS1=%~dp0run_server.ps1"

REM 実行ポリシーを一時バイパスしてps1を起動（PowerShellウィンドウは非表示）
"%POWERSHELL%" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%PS1%"

endlocal
