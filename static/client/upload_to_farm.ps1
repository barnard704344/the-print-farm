# BambuLab Print Farm - OrcaSlicer Post-Processing Upload Script (PowerShell)
#
# Setup in OrcaSlicer:
#   Print Settings → Others → Post-processing Scripts:
#   powershell -ExecutionPolicy Bypass -File "C:\Tools\upload_to_farm.ps1"
#
param(
    [Parameter(Position=0)]
    [string]$GcodePath
)

# ─── Configuration ───────────────────────────────────────────
$FARM_URL = "http://0941-webserver.seatonhs.internal:5000"
$API_KEY  = "bambulab-farm-2026"
# ─────────────────────────────────────────────────────────────

if (-not $GcodePath) {
    Write-Host "[Farm Upload] No file path provided. Is this set as a post-processing script in OrcaSlicer?" -ForegroundColor Yellow
    exit 1
}

# Normalize path (OrcaSlicer may use forward slashes)
$GcodePath = $GcodePath -replace '/', '\'

if (-not (Test-Path -LiteralPath $GcodePath)) {
    Write-Host "[Farm Upload] File not found: $GcodePath" -ForegroundColor Red
    exit 1
}

$FileName = Split-Path $GcodePath -Leaf
Write-Host "[Farm Upload] Uploading $FileName to print farm..." -ForegroundColor Cyan

try {
    $uri = "$FARM_URL/api/jobs/upload"
    $filePath = (Resolve-Path -LiteralPath $GcodePath).Path

    Add-Type -AssemblyName System.Net.Http
    $client = New-Object System.Net.Http.HttpClient
    $client.Timeout = New-TimeSpan -Seconds 120
    $client.DefaultRequestHeaders.Add("X-Api-Key", $API_KEY)

    $content = New-Object System.Net.Http.MultipartFormDataContent
    $fileStream = [System.IO.File]::OpenRead($filePath)
    $streamContent = New-Object System.Net.Http.StreamContent($fileStream)
    $streamContent.Headers.ContentType = New-Object System.Net.Http.Headers.MediaTypeHeaderValue("application/octet-stream")
    $content.Add($streamContent, "file", $FileName)

    $response = $client.PostAsync($uri, $content).Result
    $body = $response.Content.ReadAsStringAsync().Result
    $fileStream.Close()
    $client.Dispose()

    if ($response.IsSuccessStatusCode) {
        $result = $body | ConvertFrom-Json
        Write-Host "[Farm Upload] Success! Job ID: $($result.job_id)" -ForegroundColor Green
    } else {
        Write-Host "[Farm Upload] Server error ($($response.StatusCode)): $body" -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "[Farm Upload] Upload failed: $_" -ForegroundColor Red
    exit 1
}
