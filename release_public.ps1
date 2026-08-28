param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')]
    [string]$Version,
    [switch]$FreshRoot,
    [switch]$Push
)

$ErrorActionPreference = "Stop"
$arguments = @("run", "python", "scripts/publish_public_release.py", "--version", $Version)
if ($FreshRoot) {
    $arguments += "--fresh-root"
}
if ($Push) {
    $arguments += "--push"
}

& uv @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Public release verification failed with exit code $LASTEXITCODE."
}
