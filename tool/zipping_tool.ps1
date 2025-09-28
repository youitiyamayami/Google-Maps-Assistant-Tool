<# ========================================================================
 zipping_tool.ps1
 JSON で指定した対象（ファイル/フォルダ）を ZIP 化するツール
 追加機能:
  - wrap_folder_name: ZIP 内を指定名のトップレベルフォルダでラップ
  - include_empty_dirs: 空ディレクトリも含める
  - exclude_globs: 除外する相対パスのワイルドカード（__pycache__ や *.pyc 等）
 既存:
  - mode: "overwrite" | "update"
  - フォルダ構造は root_dir を起点に相対パスで保持
 PowerShell 5.1 以降（Windows標準）で動作
========================================================================= #>

[CmdletBinding()]
param(
    [Parameter(Mandatory=$false)]
    [string]$ConfigPath
)

$ErrorActionPreference = 'Stop'

function New-WildcardMatcher {
    param([string[]]$Patterns)
    $list = @()
    foreach($p in $Patterns){
        if([string]::IsNullOrWhiteSpace($p)){ continue }
        # 相対表記の区切りのゆらぎに強くするため / と \ のどちらでもマッチするように整形
        $pp = $p -replace '/', '\'
        $list += New-Object System.Management.Automation.WildcardPattern($pp, [System.Management.Automation.WildcardOptions]::IgnoreCase)
    }
    return $list
}
function Test-MatchAny {
    param(
        [string]$RelativePath,
        [System.Management.Automation.WildcardPattern[]]$Matchers
    )
    if(-not $Matchers -or $Matchers.Count -eq 0){ return $false }
    $rp = $RelativePath -replace '/', '\'
    foreach($m in $Matchers){
        if($m.IsMatch($rp)){ return $true }
    }
    return $false
}

try {
    # ---------------------------------------------------------------
    # 1) コンフィグ読込
    # ---------------------------------------------------------------
    if (-not $ConfigPath -or [string]::IsNullOrWhiteSpace($ConfigPath)) {
        $ConfigPath = Join-Path -Path $PSScriptRoot -ChildPath 'zipping_tool.json'
    }
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        throw "Config JSON not found: $ConfigPath"
    }

    $cfgText = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8
    $cfg = $cfgText | ConvertFrom-Json

    $rootDir   = $cfg.root_dir
    $outputDir = $cfg.output_dir
    $zipName   = $cfg.zip_name
    $mode      = if ($cfg.mode) { $cfg.mode } else { 'overwrite' }
    $targets   = @(); if ($cfg.targets) { $targets = @($cfg.targets) }

    # 追加オプション
    $wrapFolderName    = $cfg.wrap_folder_name
    $includeEmptyDirs  = [bool]$cfg.include_empty_dirs
    $excludeGlobs      = @(); if($cfg.exclude_globs){ $excludeGlobs = @($cfg.exclude_globs) }
    $excludeMatchers   = New-WildcardMatcher -Patterns $excludeGlobs

    if ([string]::IsNullOrWhiteSpace($rootDir))   { throw "root_dir is required." }
    if ([string]::IsNullOrWhiteSpace($outputDir)) { $outputDir = '.' }
    if ([string]::IsNullOrWhiteSpace($zipName))   { $zipName   = 'archive.zip' }
    if (-not $targets -or $targets.Count -eq 0)   { throw "targets is empty." }

    $rootDir = (Resolve-Path -LiteralPath $rootDir).Path

    if ([System.IO.Path]::IsPathRooted($outputDir)) {
        $outDirAbs = $outputDir
    } else {
        $outDirAbs = Join-Path -Path $rootDir -ChildPath $outputDir
    }
    New-Item -ItemType Directory -Path $outDirAbs -Force | Out-Null
    $zipPath = Join-Path -Path $outDirAbs -ChildPath $zipName

    Write-Host "[INFO] root_dir   = $rootDir"
    Write-Host "[INFO] output_dir = $outDirAbs"
    Write-Host "[INFO] zip_path   = $zipPath"
    Write-Host "[INFO] mode       = $mode"
    Write-Host "[INFO] wrap       = $wrapFolderName"
    Write-Host "[INFO] empty-dirs = $includeEmptyDirs"
    if($excludeGlobs.Count -gt 0){
        Write-Host "[INFO] excludes  = $($excludeGlobs -join ', ')"
    }

    # ---------------------------------------------------------------
    # 2) 収集（root をカレントにし相対で詰める）
    # ---------------------------------------------------------------
    $relFilePaths = New-Object System.Collections.Generic.List[string]
    $relDirPaths  = New-Object System.Collections.Generic.HashSet[string]
    $seenFiles    = @{}

    Push-Location -LiteralPath $rootDir
    try {
        foreach ($t in $targets) {
            # 相対パスへ
            if ([System.IO.Path]::IsPathRooted($t)) {
                if (Test-Path -LiteralPath $t) { $rel = Resolve-Path -LiteralPath $t -Relative }
                else { Write-Warning "[WARN] Target not found (absolute): $t"; continue }
            } else {
                if (-not (Test-Path -LiteralPath $t)) { Write-Warning "[WARN] Target not found (relative): $t"; continue }
                $rel = Resolve-Path -LiteralPath $t -Relative
            }

            # ディレクトリ丸ごと指定時に、ディレクトリ自体が除外対象なら枝ごとスキップ
            if (Test-Path -LiteralPath $rel -PathType Container) {
                if (Test-MatchAny -RelativePath $rel -Matchers $excludeMatchers) { continue }

                [void]$relDirPaths.Add($rel)
                if ($includeEmptyDirs) {
                    Get-ChildItem -LiteralPath $rel -Recurse -Directory | ForEach-Object {
                        $drel = Resolve-Path -LiteralPath $_.FullName -Relative
                        if (-not (Test-MatchAny -RelativePath $drel -Matchers $excludeMatchers)) {
                            [void]$relDirPaths.Add($drel)
                        }
                    }
                }

                Get-ChildItem -LiteralPath $rel -Recurse -File | ForEach-Object {
                    $fr = Resolve-Path -LiteralPath $_.FullName -Relative
                    if (Test-MatchAny -RelativePath $fr -Matchers $excludeMatchers) { return }
                    if (-not $seenFiles.ContainsKey($fr)) {
                        $relFilePaths.Add($fr) | Out-Null
                        $seenFiles[$fr] = $true
                    }
                }
            }
            else {
                if (Test-MatchAny -RelativePath $rel -Matchers $excludeMatchers) { continue }
                if (-not $seenFiles.ContainsKey($rel)) {
                    $relFilePaths.Add($rel) | Out-Null
                    $seenFiles[$rel] = $true
                }
            }
        }

        if ($relFilePaths.Count -eq 0 -and -not $includeEmptyDirs) {
            throw "No files found under root for specified targets."
        }

        Write-Host ("[INFO] {0} file(s) to archive." -f $relFilePaths.Count)
        Write-Host ("[INFO] {0} dir(s)  to reflect." -f $relDirPaths.Count)

        # -----------------------------------------------------------
        # 3) 圧縮
        # -----------------------------------------------------------
        $useStaging = $includeEmptyDirs -or ([string]::IsNullOrWhiteSpace($wrapFolderName) -eq $false)

        if ($useStaging) {
            $guid = [guid]::NewGuid().ToString("N")
            $stagingRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("zipping_tool_"+$guid)
            New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null

            if ([string]::IsNullOrWhiteSpace($wrapFolderName)) { $wrapFolderName = "payload" }
            $base = Join-Path $stagingRoot $wrapFolderName
            New-Item -ItemType Directory -Path $base -Force | Out-Null

            try {
                foreach ($d in $relDirPaths) {
                    if (Test-MatchAny -RelativePath $d -Matchers $excludeMatchers) { continue }
                    $dstDir = Join-Path $base $d
                    New-Item -ItemType Directory -Path $dstDir -Force | Out-Null
                }

                foreach ($rf in $relFilePaths) {
                    if (Test-MatchAny -RelativePath $rf -Matchers $excludeMatchers) { continue }
                    $dst = Join-Path $base $rf
                    $dstDir = Split-Path -Parent $dst
                    if (-not (Test-Path -LiteralPath $dstDir)) {
                        New-Item -ItemType Directory -Path $dstDir -Force | Out-Null
                    }
                    Copy-Item -LiteralPath $rf -Destination $dst -Force
                }

                if ($mode -ieq 'overwrite') {
                    if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
                    Compress-Archive -Path $base -DestinationPath $zipPath -Force
                    Write-Host "[INFO] Created (overwrite): $zipPath"
                }
                elseif ($mode -ieq 'update') {
                    if (Test-Path -LiteralPath $zipPath) {
                        Compress-Archive -Path $base -DestinationPath $zipPath -Update
                        Write-Host "[INFO] Updated: $zipPath"
                    } else {
                        Compress-Archive -Path $base -DestinationPath $zipPath -Force
                        Write-Host "[INFO] Created (update->new): $zipPath"
                    }
                }
                else { throw "Unsupported mode: $mode (use 'overwrite' or 'update')" }
            }
            finally {
                try { Remove-Item -LiteralPath $stagingRoot -Recurse -Force -ErrorAction SilentlyContinue } catch {}
            }
        }
        else {
            if ($mode -ieq 'overwrite') {
                if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
                Compress-Archive -Path $relFilePaths -DestinationPath $zipPath -Force
                Write-Host "[INFO] Created (overwrite): $zipPath"
            }
            elseif ($mode -ieq 'update') {
                if (Test-Path -LiteralPath $zipPath) {
                    Compress-Archive -Path $relFilePaths -DestinationPath $zipPath -Update
                    Write-Host "[INFO] Updated: $zipPath"
                } else {
                    Compress-Archive -Path $relFilePaths -DestinationPath $zipPath -Force
                    Write-Host "[INFO] Created (update->new): $zipPath"
                }
            }
            else { throw "Unsupported mode: $mode (use 'overwrite' or 'update')" }
        }
    }
    finally {
        Pop-Location | Out-Null
    }

    exit 0
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
