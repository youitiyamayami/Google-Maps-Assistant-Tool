# PowerShell 実行スクリプト
# main.py を Python で起動し、GUI ウィンドウを表示させる

# Python 実行ファイルのパス（環境に合わせて修正してください）
$PythonPath = "C:\Users\Kaito_Tanaka\AppData\Local\Programs\Python\Python313\python.exe"

# main.py のパス
$ScriptPath = "C:\Users\Kaito_Tanaka\Desktop\private_file\programming_file\map_root\run_server.py"

& $PythonPath $ScriptPath
