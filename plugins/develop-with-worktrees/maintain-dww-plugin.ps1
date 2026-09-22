# DWW 稳定本地市场的唯一维护入口。该文件随插件安装，Hook 只接受同一
# 插件根下带有固定参数的 Install 调用。首次市场迁移仍只能由人工在系统
# PowerShell 中执行；普通更新不反复切换市场来源。
[CmdletBinding()]
param(
    [ValidateSet('Check', 'Install', 'RecoveryInstall')]
    [string]$Mode = 'Check',
    [string]$SourceRepo,
    [string]$SourceCommit,
    [string]$CodexPath,
    [string]$MarketplaceRoot = (Join-Path ([Environment]::GetFolderPath('UserProfile')) 'plugins\dww-stable-local'),
    [switch]$MigrateMarketplace
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$PluginName = 'develop-with-worktrees'
$MarketplaceName = 'dww-stable-local'
# 正式入口不依赖源码仓库顶层脚本。辅助工具是 Codex 安装随附的系统技能，
# 由当前环境的 CODEX_HOME（或默认用户级 Codex 目录）确定，调用者不能用
# 参数替换它。
$CodexHome = $env:CODEX_HOME
if ([string]::IsNullOrWhiteSpace($CodexHome)) {
    $CodexHome = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex'
}
$PluginCreatorRoot = Join-Path $CodexHome 'skills\.system\plugin-creator'

function Fail([string]$Message) {
    throw "DWW_LOCAL_PUBLISH_ERROR: DWW 本地发布未继续：$Message"
}

function Normalize-Path([string]$Value) {
    ([IO.Path]::GetFullPath($Value)).TrimEnd([char[]]@('\', '/')).ToLowerInvariant()
}

function Require-Path([string]$PathText, [string]$Label) {
    if (-not (Test-Path -LiteralPath $PathText)) { Fail "缺少$Label：$PathText" }
}

function Invoke-Codex([string[]]$Arguments) {
    $lines = @(& $CodexPath @Arguments 2>&1)
    [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output = ($lines -join "`n")
    }
}

function Read-Json([string]$Text, [string]$Label) {
    try { return $Text | ConvertFrom-Json } catch { Fail "$Label 返回的 JSON 无法读取。" }
}

function Get-Marketplace {
    $result = Invoke-Codex @('plugin', 'marketplace', 'list', '--json')
    if ($result.ExitCode -ne 0) { Fail "codex plugin marketplace list 失败：$($result.Output)" }
    $payload = Read-Json $result.Output 'marketplace list'
    $matches = @($payload.marketplaces | Where-Object { $_.name -eq $MarketplaceName })
    if ($matches.Count -gt 1) { Fail '同名本地市场不唯一。' }
    if ($matches.Count -eq 1) { return $matches[0] }
    return $null
}

function Assert-Expected-Marketplace($Entry) {
    if ($null -eq $Entry) { Fail '未登记预期本地市场。' }
    if ((Normalize-Path ([string]$Entry.root)) -ne (Normalize-Path $MarketplaceRoot)) {
        Fail "市场来源不匹配：$($Entry.root)"
    }
}

function Test-Local-DwwMarket([string]$Root) {
    $manifest = Join-Path $Root '.agents\plugins\marketplace.json'
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { return $false }
    try {
        $payload = Get-Content -LiteralPath $manifest -Raw -Encoding utf8 | ConvertFrom-Json
        return $payload.name -eq $MarketplaceName -and @($payload.plugins | Where-Object { $_.name -eq $PluginName }).Count -eq 1
    } catch { return $false }
}

function Test-OrdinaryDirectory([string]$PathText) {
    try {
        $item = Get-Item -LiteralPath $PathText -Force -ErrorAction Stop
        return $item.PSIsContainer -and -not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
    } catch { return $false }
}

function Get-ExistingItem([string]$PathText) {
    try { return Get-Item -LiteralPath $PathText -Force -ErrorAction Stop } catch { return $null }
}

function Test-InitialMarketplaceLayout([string]$Root) {
    if (-not (Test-OrdinaryDirectory $Root)) { return $false }
    $entries = @(Get-ChildItem -LiteralPath $Root -Force)
    if ($entries.Count -eq 0) { return $true }
    return $entries.Count -eq 1 -and $entries[0].Name -eq 'releases' -and (Test-OrdinaryDirectory $entries[0].FullName)
}

function Assert-CheckLayout([string]$Root) {
    $rootItem = Get-ExistingItem $Root
    if ($null -eq $rootItem) {
        Write-Output 'Check: 布局可接受，稳定市场根尚不存在；未执行 CLI 或写入。'
        return
    }
    if (-not (Test-OrdinaryDirectory $Root)) { Fail 'Check 发现市场根不是普通非链接目录。' }
    $marketplacePath = Join-Path $Root '.agents\plugins\marketplace.json'
    if (-not (Test-Path -LiteralPath $marketplacePath -PathType Leaf)) {
        if (Test-InitialMarketplaceLayout $Root) {
            Write-Output 'Check: 布局可接受，仅有保留的 legacy releases 存档；未执行 CLI 或写入。'
            return
        }
        Fail 'Check 发现未初始化市场根含有未知、链接或混合内容。'
    }
    if (-not (Test-Local-DwwMarket $Root)) { Fail 'Check 发现 marketplace.json 不是可读的指定 DWW 市场。' }
    $pluginRoot = Join-Path $Root "plugins\$PluginName"
    $pluginManifest = Join-Path $pluginRoot '.codex-plugin\plugin.json'
    $receiptPath = Join-Path $Root 'active-release.json'
    $hasPlugin = Test-Path -LiteralPath $pluginRoot -PathType Container
    $hasReceipt = Test-Path -LiteralPath $receiptPath -PathType Leaf
    if (-not $hasPlugin) { Fail 'Check 发现指定插件目录缺失。' }
    Require-Path $pluginManifest '插件清单'
    try {
        $manifest = Get-Content -LiteralPath $pluginManifest -Raw -Encoding utf8 | ConvertFrom-Json
    } catch { Fail 'Check 无法读取插件清单。' }
    if ($manifest.name -ne $PluginName -or [string]::IsNullOrWhiteSpace([string]$manifest.version)) {
        Fail 'Check 发现插件清单身份不一致。'
    }
    if (-not $hasReceipt) {
        Write-Output 'Check: 布局可接受，当前是可读的初始插件 scaffold，尚无 active 回执；未执行 CLI 或写入。'
        return
    }
    try {
        $receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding utf8 | ConvertFrom-Json
    } catch { Fail 'Check 无法读取 active-release.json。' }
    foreach ($field in @('release_id', 'source_commit', 'source_tree', 'package_version')) {
        if ([string]::IsNullOrWhiteSpace([string]$receipt.$field)) { Fail "Check 回执缺少 $field。" }
    }
    if ([string]$receipt.package_version -ne [string]$manifest.version) {
        Fail 'Check 发现 active 回执与插件清单版本不一致。'
    }
    Write-Output 'Check: 布局可接受，市场清单、插件目录和 active 回执可读且一致；未执行 CLI 或写入。'
}

function Find-PluginIdentity($Value, [string]$Version) {
    if ($null -eq $Value) { return $false }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        foreach ($item in $Value) { if (Find-PluginIdentity $item $Version) { return $true } }
        return $false
    }
    if ($Value -is [psobject]) {
        $names = @($Value.PSObject.Properties.Name)
        if ($names -contains 'name' -and $names -contains 'version' -and $Value.name -eq $PluginName -and $Value.version -eq $Version) {
            return $true
        }
        foreach ($property in $Value.PSObject.Properties) {
            if (Find-PluginIdentity $property.Value $Version) { return $true }
        }
    }
    return $false
}

function Get-InstalledPlugin([string]$Version) {
    $result = Invoke-Codex @('plugin', 'list', '--marketplace', $MarketplaceName, '--json')
    if ($result.ExitCode -ne 0) { Fail "codex plugin list 失败：$($result.Output)" }
    Find-PluginIdentity (Read-Json $result.Output 'plugin list') $Version
}

function Invoke-Required([string]$Name, [string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { Fail "$Name 失败，退出码：$LASTEXITCODE" }
}

function Write-Receipt([string]$Path, $Receipt) {
    $Receipt | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $Path -Encoding utf8
}

if ($Mode -eq 'Check') {
    Assert-CheckLayout $MarketplaceRoot
    exit 0
}

foreach ($required in @($SourceRepo, $SourceCommit, $CodexPath)) {
    if ([string]::IsNullOrWhiteSpace($required)) { Fail 'Install 需要 SourceRepo、SourceCommit 和 CodexPath。' }
}
Require-Path $SourceRepo '源码仓库'
Require-Path $CodexPath '正式 Codex CLI'
if ($SourceCommit -notmatch '^[0-9a-fA-F]{40}$') { Fail 'SourceCommit 必须是完整 40 位提交。' }
$cliVersion = Invoke-Codex @('--version')
if ($cliVersion.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($cliVersion.Output)) { Fail '正式 Codex CLI 无法返回版本。' }

$git = (Get-Command git -ErrorAction Stop).Source
$uv = (Get-Command uv -ErrorAction Stop).Source
$tar = (Get-Command tar -ErrorAction Stop).Source
$create = Join-Path $PluginCreatorRoot 'scripts\create_basic_plugin.py'
$cachebuster = Join-Path $PluginCreatorRoot 'scripts\update_plugin_cachebuster.py'
$validate = Join-Path $PluginCreatorRoot 'scripts\validate_plugin.py'
foreach ($helper in @($create, $cachebuster, $validate)) { Require-Path $helper '官方插件辅助脚本' }

$resolved = (& $git -C $SourceRepo rev-parse "$SourceCommit^{commit}").Trim()
if ($LASTEXITCODE -ne 0 -or $resolved -ne $SourceCommit.ToLowerInvariant()) { Fail '指定源码提交不可解析。' }
$main = (& $git -C $SourceRepo rev-parse main).Trim()
if ($LASTEXITCODE -ne 0) { Fail '无法核验 main。' }
$recoveryEvidence = $null
if ($Mode -eq 'RecoveryInstall') {
    if ($MigrateMarketplace) { Fail '恢复安装不得迁移市场。' }
    # 使用随本入口安装的验证器，不执行尚未获核验的源码，也不要求先合入 main。
    $runner = Join-Path $PSScriptRoot 'skills\develop-with-worktrees\scripts\dww.py'
    $checked = @(& $uv run --script $runner --repo $SourceRepo --json batch recovery-source --commit $resolved)
    if ($LASTEXITCODE -ne 0) { Fail '恢复来源未通过精确 Full、日志、基线与候选核验。' }
    $checkedPayload = Read-Json ($checked -join "`n") 'recovery-source'
    if (-not $checkedPayload.ok -or $checkedPayload.result.source_commit -ne $resolved -or $checkedPayload.result.purpose -ne 'recovery-install-only') {
        Fail '恢复来源核验结果不匹配。'
    }
    $recoveryEvidence = $checkedPayload.result
} elseif ($main -ne $resolved) { Fail '只允许从已合入的 main 精确提交发布。' }
$tree = (& $git -C $SourceRepo rev-parse "$resolved`:plugins/$PluginName").Trim()
if ($LASTEXITCODE -ne 0) { Fail '提交中缺少目标插件树。' }

$activePlugin = Join-Path $MarketplaceRoot "plugins\$PluginName"
$activeReceipt = Join-Path $MarketplaceRoot 'active-release.json'
$previous = Join-Path $MarketplaceRoot '.previous-release'
$releaseId = "$resolved-$tree"
$stage = Join-Path $MarketplaceRoot ".stage-$releaseId"
$stagePlugin = Join-Path $stage "plugins\$PluginName"
$scaffoldCreated = $false

$marketRootItem = Get-ExistingItem $MarketplaceRoot
if ($null -ne $marketRootItem) {
    if (-not (Test-OrdinaryDirectory $MarketplaceRoot)) {
        Fail '稳定市场根不是普通非链接目录。'
    }
    $unresolvedStages = @(
        Get-ChildItem -LiteralPath $MarketplaceRoot -Force -Directory |
            Where-Object { $_.Name -like '.stage-*' }
    )
    if ($unresolvedStages.Count -ne 0) {
        Fail '发现未决的 .stage-* 恢复现场；拒绝覆盖，请先按回执核查。'
    }
} else { New-Item -ItemType Directory -Path $MarketplaceRoot | Out-Null }
$marketplacePath = Join-Path $MarketplaceRoot '.agents\plugins\marketplace.json'
if (-not (Test-Path -LiteralPath $marketplacePath -PathType Leaf)) {
    if (-not (Test-InitialMarketplaceLayout $MarketplaceRoot)) {
        Fail '稳定市场根已存在但只有空目录或单一普通 releases 存档才可初始化。'
    }
    Invoke-Required 'create_basic_plugin.py' $uv @('run', '--script', $create, $PluginName, '--path', (Join-Path $MarketplaceRoot 'plugins'), '--marketplace-path', $marketplacePath, '--marketplace-name', $MarketplaceName, '--with-marketplace')
    $scaffoldCreated = $true
}
if (-not (Test-Local-DwwMarket $MarketplaceRoot)) { Fail '稳定市场清单不是指定的本地 DWW 来源。' }

$activeMatches = $false
if (Test-Path -LiteralPath $activeReceipt -PathType Leaf) {
    try {
        $receipt = Get-Content -LiteralPath $activeReceipt -Raw -Encoding utf8 | ConvertFrom-Json
        $activeMatches = $receipt.source_commit -eq $resolved -and $receipt.source_tree -eq $tree -and (Test-Path -LiteralPath $activePlugin -PathType Container)
    } catch { Fail '现有 active-release.json 无法读取，保留现场。' }
}
$resumingInstall = Test-Path -LiteralPath $previous
if ($resumingInstall -and -not $activeMatches) {
    Fail '发现 .previous-release，但 active-release 与本次精确源码不匹配；拒绝覆盖，请先按回执核查。'
}

if (-not $activeMatches) {
    New-Item -ItemType Directory -Path (Join-Path $stage 'plugins') -Force | Out-Null
    Invoke-Required 'create_basic_plugin.py' $uv @('run', '--script', $create, $PluginName, '--path', (Join-Path $stage 'plugins'))
    $archive = Join-Path $stage 'source.tar'
    Invoke-Required 'git archive' $git @('-C', $SourceRepo, 'archive', '--format=tar', "--output=$archive", $resolved, '--', "plugins/$PluginName")
    Invoke-Required 'tar extract' $tar @('-xf', $archive, '-C', $stage)
    Invoke-Required 'update_plugin_cachebuster.py' $uv @('run', '--script', $cachebuster, $stagePlugin)
    Invoke-Required 'validate_plugin.py' $uv @('run', '--with', 'pyyaml', '--script', $validate, $stagePlugin)
    $manifest = Get-Content -LiteralPath (Join-Path $stagePlugin '.codex-plugin\plugin.json') -Raw -Encoding utf8 | ConvertFrom-Json
    if ($manifest.name -ne $PluginName -or $manifest.version -notlike '*+codex.*') { Fail '发布包身份或 cachebuster 不符合预期。' }

    if (Test-Path -LiteralPath $activePlugin) {
        if (-not (Test-Path -LiteralPath $activeReceipt -PathType Leaf)) {
            if (-not $scaffoldCreated) { Fail '现有活跃插件没有回执，保留现场。' }
            # 只删除本次刚由官方 scaffold 生成的空目录，不触碰已有发布内容。
            Remove-Item -LiteralPath $activePlugin -Recurse -Force
        } else {
            $old = Get-Content -LiteralPath $activeReceipt -Raw -Encoding utf8 | ConvertFrom-Json
            if ([string]::IsNullOrWhiteSpace([string]$old.release_id)) { Fail '现有活跃插件回执没有发布身份。' }
            New-Item -ItemType Directory -Path (Join-Path $previous 'plugins') -Force | Out-Null
            Move-Item -LiteralPath $activePlugin -Destination (Join-Path $previous "plugins\$PluginName")
            Move-Item -LiteralPath $activeReceipt -Destination (Join-Path $previous 'active-release.json')
        }
    }
    try {
        Move-Item -LiteralPath $stagePlugin -Destination $activePlugin
        $newReceipt = [ordered]@{ release_id = $releaseId; source_commit = $resolved; source_tree = $tree; package_version = [string]$manifest.version; packaged_at_utc = (Get-Date).ToUniversalTime().ToString('o'); recovery_source = $recoveryEvidence }
        Write-Receipt $activeReceipt $newReceipt
        Remove-Item -LiteralPath $stage -Recurse -Force
    } catch {
        Fail "活跃插件切换未完成；保留 .previous-release、暂存和回执以便核查：$($_.Exception.Message)"
    }
}

$current = Get-Marketplace
if ($null -eq $current) {
    $added = Invoke-Codex @('plugin', 'marketplace', 'add', $MarketplaceRoot, '--json')
    if ($added.ExitCode -ne 0 -and $null -eq (Get-Marketplace)) { Fail "市场登记失败：$($added.Output)" }
    Assert-Expected-Marketplace (Get-Marketplace)
} elseif ((Normalize-Path ([string]$current.root)) -ne (Normalize-Path $MarketplaceRoot)) {
    if ($resumingInstall) { Fail '恢复安装时市场来源不匹配；保留 .previous-release，拒绝切换来源。' }
    if (-not $MigrateMarketplace) { Fail '同名市场来自其他位置；使用一次 -MigrateMarketplace 后再继续。' }
    $previousRoot = [string]$current.root
    if (-not (Test-Local-DwwMarket $previousRoot)) { Fail '原同名市场不是可核实的本地 DWW 来源。' }
    $removed = Invoke-Codex @('plugin', 'marketplace', 'remove', $MarketplaceName, '--json')
    if ($removed.ExitCode -ne 0 -and $null -ne (Get-Marketplace)) { Fail "旧市场移除失败：$($removed.Output)" }
    $added = Invoke-Codex @('plugin', 'marketplace', 'add', $MarketplaceRoot, '--json')
    $actual = Get-Marketplace
    if ($added.ExitCode -ne 0 -and ($null -eq $actual -or (Normalize-Path ([string]$actual.root)) -ne (Normalize-Path $MarketplaceRoot))) {
        $restore = Invoke-Codex @('plugin', 'marketplace', 'add', $previousRoot, '--json')
        if ($restore.ExitCode -ne 0) { Fail '新市场失败且旧市场恢复失败；请保留现场并提供输出。' }
        Fail "新市场登记失败，已恢复已核实旧来源：$($added.Output)"
    }
    Assert-Expected-Marketplace (Get-Marketplace)
} else { Assert-Expected-Marketplace $current }

$receipt = Get-Content -LiteralPath $activeReceipt -Raw -Encoding utf8 | ConvertFrom-Json
if (-not (Get-InstalledPlugin ([string]$receipt.package_version))) {
    $installed = Invoke-Codex @('plugin', 'add', "$PluginName@$MarketplaceName", '--json')
    if ($installed.ExitCode -ne 0) {
        if (-not (Get-InstalledPlugin ([string]$receipt.package_version))) {
            Write-Receipt (Join-Path $MarketplaceRoot 'install-verification.json') ([ordered]@{ release_id = $receipt.release_id; status = 'plugin-install-failed'; output = $installed.Output })
            Fail "插件安装失败；市场和已准备发布包保留，可直接重试：$($installed.Output)"
        }
    }
    if (-not (Get-InstalledPlugin ([string]$receipt.package_version))) { Fail 'CLI 未能回读已安装的精确插件身份。' }
}
Write-Receipt (Join-Path $MarketplaceRoot 'install-verification.json') ([ordered]@{ release_id = $receipt.release_id; status = 'installed-by-cli'; codex_version = $cliVersion.Output.Trim(); marketplace_root = $MarketplaceRoot; package_version = $receipt.package_version; host_runtime_verified = $false })
if ($Mode -eq 'RecoveryInstall') {
    Write-Output "DWW 恢复版已安装：$($receipt.package_version)。来源已通过 Full，但尚未交付 main；保留上一发行。完成恢复和正式合入后，以 Install 收尾并验证宿主。"
} else {
    if (Test-Path -LiteralPath $previous) { Remove-Item -LiteralPath $previous -Recurse -Force }
    Write-Output "DWW 本地插件已由正式 CLI 核验：$($receipt.package_version)。请在新 Codex 会话验证实际加载。"
}
