# 在 GitLab Windows Runner 上构建并上传 Uni-Lab-OS Windows 安装器。
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Repo,
    [Parameter(Mandatory = $true)][string]$Ref,
    [Parameter(Mandatory = $true)][string]$ExpectedCommit,
    [Parameter(Mandatory = $true)][string]$BuildRoot,
    [Parameter(Mandatory = $true)][string]$Builder,
    [Parameter(Mandatory = $true)][string]$ArtifactDir,
    [Parameter(Mandatory = $true)][string]$Uploader,
    [Parameter(Mandatory = $true)][string]$VersionPreparer,
    [Parameter(Mandatory = $true)][string]$OssPrecheck,
    [Parameter(Mandatory = $true)][string]$Notifier,
    [Parameter(Mandatory = $true)][string]$OssPublicUrl,
    [Parameter(Mandatory = $true)][string]$ReleaseTag,
    [switch]$CondaOnly,
    [switch]$SkipUpload,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path -LiteralPath $Repo).Path
$BuildRoot = [IO.Path]::GetFullPath($BuildRoot)
$ArtifactDir = [IO.Path]::GetFullPath($ArtifactDir)
$Uploader = [IO.Path]::GetFullPath($Uploader)
$VersionPreparer = [IO.Path]::GetFullPath($VersionPreparer)
$OssPrecheck = [IO.Path]::GetFullPath($OssPrecheck)
$Notifier = [IO.Path]::GetFullPath($Notifier)
$OssPublicUrl = [IO.Path]::GetFullPath($OssPublicUrl)
$commit = (& git -C $Repo rev-parse --verify "$Ref^{commit}").Trim()
if ($LASTEXITCODE -ne 0 -or $commit -ne $ExpectedCommit) {
    throw "Ref $Ref does not resolve to expected commit $ExpectedCommit."
}
if ((& git -C $Repo rev-parse HEAD).Trim() -ne $commit) {
    throw 'CI checkout is not at the requested commit.'
}

if ($ReleaseTag -notmatch '^v([0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.-]+)?)$') {
    throw 'ReleaseTag must use vMAJOR.MINOR.PATCH format, for example v0.3.0.'
}
$version = $Matches[1]

$python = Join-Path $Builder 'python.exe'
$rattlerCandidates = @(
    (Join-Path $Builder 'Scripts\rattler-build.exe'),
    (Join-Path $Builder 'Scripts\rattler-build.cmd'),
    (Join-Path $Builder 'Scripts\rattler-build.bat'),
    (Join-Path $Builder 'Scripts\rattler-build'),
    (Join-Path $Builder 'bin\rattler-build.exe'),
    (Join-Path $Builder 'bin\rattler-build'),
    (Join-Path $Builder 'Library\bin\rattler-build.exe'),
    (Join-Path $Builder 'rattler-build.exe')
)
$rattler = $rattlerCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
$rattlerCandidatesDescription = $rattlerCandidates -join ', '
if (-not $rattler) {
    $matchingFiles = @(Get-ChildItem -LiteralPath $Builder -Recurse -File -Filter 'rattler*' -ErrorAction SilentlyContinue | ForEach-Object FullName)
    $matchingFilesDescription = if ($matchingFiles) { $matchingFiles -join ', ' } else { '<none>' }
    $distribution = & $python -c "import importlib.metadata as m; print(m.version('rattler-build'))" 2>&1
    throw "Missing rattler-build executable. Checked: $rattlerCandidatesDescription. Found rattler files: $matchingFilesDescription. Installed rattler-build version: $distribution"
}
$constructor = Join-Path $Builder 'Scripts\constructor.exe'
$standalone = Join-Path $Builder 'standalone_conda\conda.exe'
foreach ($tool in @($python, $constructor, $standalone)) {
    if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) { throw "Missing build tool: $tool" }
}
$pnpm = (Get-Command pnpm.cmd -ErrorAction Stop).Source
$null = Get-Command node.exe -ErrorAction Stop
if (-not $SkipUpload -and -not $CondaOnly) {
    foreach ($name in @('OSS_ACCESS_KEY_ID', 'OSS_ACCESS_KEY_SECRET', 'OSS_ENDPOINT', 'OSS_PREFIX')) {
        if (-not [Environment]::GetEnvironmentVariable($name)) { throw "Missing CI variable: $name" }
    }
    if (-not $env:OSS_PREFIX.StartsWith('oss://')) { throw 'OSS_PREFIX must start with oss://' }
    if (-not (Test-Path -LiteralPath $Uploader -PathType Leaf)) { throw "OSS uploader is missing: $Uploader" }
    foreach ($script in @($VersionPreparer, $OssPrecheck, $Notifier, $OssPublicUrl)) {
        if (-not (Test-Path -LiteralPath $script -PathType Leaf)) { throw "Release helper is missing: $script" }
    }
}

function Import-VsBuildEnvironment {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) { throw "Visual Studio locator not found: $vswhere" }
    $installationPath = (& $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath).Trim()
    $devCmd = Join-Path $installationPath 'Common7\Tools\VsDevCmd.bat'
    if (-not $installationPath -or -not (Test-Path -LiteralPath $devCmd -PathType Leaf)) { throw 'Visual Studio C++ Build Tools with MSVC x64 are required.' }
    & cmd.exe /d /s /c "`"$devCmd`" -arch=x64 -host_arch=x64 >nul && set" | ForEach-Object {
        if ($_ -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process') }
    }
    $null = Get-Command cl.exe -ErrorAction Stop
}

$drive = New-Object IO.DriveInfo ([IO.Path]::GetPathRoot($BuildRoot))
if (-not $drive.IsReady) { throw 'Output drive is not ready.' }
$metadata = [ordered]@{
    source_repo = $Repo
    source_ref = $Ref
    source_commit = $commit
    package_version = $version
    platform = 'win-64'
    build_root = $BuildRoot
    free_gib = [math]::Round($drive.AvailableFreeSpace / 1GB, 1)
    installer = (Join-Path $BuildRoot "installer\Uni-Lab-OS-$version-win-64.exe")
    conda_only = [bool]$CondaOnly
}
$metadata | ConvertTo-Json | Write-Output
if ($CheckOnly) {
    Import-VsBuildEnvironment
    Write-Output 'Preflight complete. No packages built; dependency solving and installation are not tested.'
    return
}
if (Test-Path -LiteralPath $BuildRoot) { throw "BuildRoot already exists: $BuildRoot" }
if ($drive.AvailableFreeSpace -lt 30GB) { Write-Warning 'Less than 30 GiB is available. A full build may exhaust this drive.' }

$source = $Repo
$channel = Join-Path $BuildRoot 'c'
$logDir = Join-Path $BuildRoot 'logs'
$installerDir = Join-Path $BuildRoot 'installer'
New-Item -ItemType Directory -Path $ArtifactDir -Force | Out-Null
foreach ($directory in @($BuildRoot, $channel, $logDir, $installerDir, "$BuildRoot\tmp", "$BuildRoot\pkgs")) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}
$metadata | ConvertTo-Json | Set-Content -LiteralPath "$BuildRoot\source-metadata.json" -Encoding UTF8

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONHOME = ''
$env:PYTHONPATH = ''
$env:TEMP = "$BuildRoot\tmp"
$env:TMP = "$BuildRoot\tmp"
$env:CONDA_PKGS_DIRS = "$BuildRoot\pkgs"
$env:PIP_CACHE_DIR = "$BuildRoot\pip-cache"
$env:pnpm_config_cache_dir = "$BuildRoot\pnpm-cache"
$env:PATH = "$Builder;$Builder\Scripts;$Builder\Library\bin;$Builder\bin;$env:PATH"
Import-VsBuildEnvironment

& $python $VersionPreparer --repo $Repo --tag $ReleaseTag
if ($LASTEXITCODE -ne 0) { throw 'Unable to prepare the release version.' }
if (-not $SkipUpload -and -not $CondaOnly) {
    & $python -m pip install --no-cache-dir oss2
    if ($LASTEXITCODE -ne 0) { throw 'Unable to install oss2 for OSS precheck.' }
    $ossReleasePrefix = "$(($env:OSS_PREFIX).TrimEnd('/'))/installer/$version/win-64"
    & $python $OssPrecheck --prefix $ossReleasePrefix
    if ($LASTEXITCODE -ne 0) { throw 'Release version already exists in OSS.' }
}

function Run-Step([string]$Name, [string]$Executable, [string[]]$Arguments) {
    $logPath = Join-Path $logDir "$Name.log"
    Write-Host "[$Name] $logPath"
    $Name | Set-Content -LiteralPath "$BuildRoot\build-status.txt"
    $ErrorActionPreference = 'Continue'
    & $Executable @Arguments *> $logPath
    $stepExit = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($stepExit -ne 0) {
        Copy-Item -LiteralPath $logPath -Destination $ArtifactDir -Force
        if ($Name -eq 'windows-process-lifecycle') {
            Write-Host "[$Name] failed output:"
            Get-Content -LiteralPath $logPath | Write-Host
        }
        "FAILED $Name exit=$stepExit" | Set-Content -LiteralPath "$BuildRoot\build-status.txt"
        throw "Step $Name failed (exit $stepExit). Raw log retained: $logPath"
    }
}

Push-Location $source
try {
    Push-Location (Join-Path $source 'frontend')
    try {
        if (-not (Test-Path -LiteralPath 'pnpm-lock.yaml' -PathType Leaf)) {
            if (-not (Test-Path -LiteralPath 'package-lock.json' -PathType Leaf)) {
                throw 'Neither pnpm-lock.yaml nor package-lock.json is available for reproducible frontend installation.'
            }
            Run-Step 'frontend-lock-import' $pnpm @('import')
        }
        Run-Step 'frontend-install' $pnpm @('install', '--frozen-lockfile')
        Run-Step 'frontend-build' $pnpm @('run', 'build')
    } finally { Pop-Location }
    if (-not (Test-Path 'unilabos\app\web\static\console\index.html')) { throw 'Embedded console output missing.' }

    # 直接执行 tests\integration 下的文件时，Python 会优先将 tests\integration
    # 作为导入根。显式设置 PYTHONPATH，使本次 checkout 中的 unilabos 可见。
    $env:UNILABOS_CI_SOURCE = $source
    $env:PYTHONPATH = $source
    Run-Step 'windows-process-lifecycle' $python @('tests\integration\test_managed_runtime_supervisor_windows.py')

    $recipes = @(
        @{ Name='msgs'; Recipe='recipes/msgs/recipe.yaml'; Channels=@('robostack','robostack-staging','conda-forge') },
        @{ Name='environment'; Recipe='.conda/environment/recipe.yaml'; Channels=@('uni-lab','robostack-staging','conda-forge',$channel) },
        @{ Name='rfc8785'; Recipe='.conda/vendor/rfc8785/recipe.yaml'; Channels=@('conda-forge') },
        @{ Name='opcua'; Recipe='.conda/vendor/opcua/recipe.yaml'; Channels=@('conda-forge') },
        @{ Name='msgcenterpy'; Recipe='.conda/vendor/msgcenterpy/recipe.yaml'; Channels=@('conda-forge',$channel) },
        @{ Name='pylabrobot'; Recipe='.conda/vendor/pylabrobot/recipe.yaml'; Channels=@('conda-forge',$channel) },
        @{ Name='base'; Recipe='.conda/base/recipe.yaml'; Channels=@('uni-lab','robostack-staging','conda-forge',$channel) }
    )
    foreach ($item in $recipes) {
        $buildArguments = @('build','-r',$item.Recipe,'--target-platform','win-64','--output-dir',$channel)
        foreach ($packageChannel in $item.Channels) { $buildArguments += @('-c',$packageChannel) }
        Run-Step "package-$($item.Name)" $rattler $buildArguments
    }
    $runtime = @(Get-ChildItem -LiteralPath (Join-Path $channel 'win-64') -Filter "unilabos-$version-*.conda")
    if ($runtime.Count -ne 1) { throw "Expected exactly one local Runtime package; found $($runtime.Count)." }
    $packageBuild = $runtime[0].BaseName.Substring("unilabos-$version-".Length)

    if (-not $CondaOnly) {
        $env:UNILABOS_INSTALLER_VERSION = $version
        $env:UNILABOS_INSTALLER_PACKAGE = 'unilabos'
        $env:UNILABOS_INSTALLER_BUILD = $packageBuild
        $env:UNILABOS_INSTALLER_CHANNEL = ([Uri]$channel).AbsoluteUri
        $constructorArguments = @('.conda/constructor','--platform','win-64','--conda-exe',$standalone,'--cache-dir',"$BuildRoot\constructor-cache")
        Run-Step 'constructor-solve' $constructor ($constructorArguments + @('--dry-run'))
        Run-Step 'constructor-build' $constructor ($constructorArguments + @('--output-dir',$installerDir))
        if (-not (Test-Path -LiteralPath $metadata.installer)) { throw 'Installer output missing.' }
        $hash = (Get-FileHash -LiteralPath $metadata.installer -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash *$([IO.Path]::GetFileName($metadata.installer))" | Set-Content -LiteralPath "$($metadata.installer).sha256" -NoNewline -Encoding ascii
    }
    $metadata['package_build'] = $packageBuild
    $metadata['completed_at'] = (Get-Date).ToString('o')
    $metadata | ConvertTo-Json | Set-Content -LiteralPath "$BuildRoot\source-metadata.json" -Encoding UTF8
    'COMPLETE' | Set-Content -LiteralPath "$BuildRoot\build-status.txt"

    # 安装器可能超过 GitLab artifact 的大小上限；它只经 OSS 分发。
    # 此处仅保留校验和和构建元数据。
    Copy-Item -LiteralPath "$($metadata.installer).sha256" -Destination $ArtifactDir -Force
    Copy-Item -LiteralPath "$BuildRoot\source-metadata.json", "$BuildRoot\build-status.txt" -Destination $ArtifactDir -Force
    if ($SkipUpload -or $CondaOnly) { Write-Host "Build complete: $BuildRoot"; return }

    $env:OSS_UPLOAD_PREFIX = "$(($env:OSS_PREFIX).TrimEnd('/'))/installer/$version/win-64"
    $env:OSS_UPLOAD_FILES = (@($metadata.installer, "$($metadata.installer).sha256", "$BuildRoot\source-metadata.json") -join ';')
    & $python $Uploader
    if ($LASTEXITCODE -ne 0) { throw 'OSS upload failed.' }
    $installerUrl = (& $python $OssPublicUrl --prefix $env:OSS_UPLOAD_PREFIX --object-name ([IO.Path]::GetFileName($metadata.installer))).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Unable to generate the OSS download URL.' }
    Write-Host "Installer download URL: $installerUrl"
    & $python $Notifier --system 'Windows x64' --version $version --oss-url $installerUrl
    if ($LASTEXITCODE -ne 0) { throw 'Feishu release notification failed.' }
    Write-Host "Build and upload complete: $BuildRoot"
} finally {
    Pop-Location
}
