<#
.SYNOPSIS
  Convert printed sheet music to MusicXML with Audiveris.  Input can be a PDF, one image,
  or a FOLDER of page images (taken in name order: 2.png before 10.png, case ignored).

.DESCRIPTION
  1. Loads each page (PDFs are rendered at 300 dpi); straightens it if tilted; enlarges low-resolution
     scans/screenshots so the staves are the size Audiveris works best with.
  2. Erases red pencil marks, fingering numbers and pedal lines (Audiveris misreads them as
     tuplets and volta brackets).
  3. Runs Audiveris with English OCR so title/tempo/text are kept, treating all pages as one score.
  4. Writes the MusicXML plus report.txt with the checks worth doing before you proofread.

.EXAMPLE
  .\convert.ps1 .\someone.pdf
  .\convert.ps1 .\love_theme                  # folder of page pictures
  .\convert.ps1 .\score.pdf -Pages 1,2 -OutDir D:\scores\out
  .\convert.ps1 .\photo.png -KeepDigits       # leave fingering numbers alone
#>
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$InputFile,
    [string]$OutDir,
    [int]$Dpi = 300,
    [string]$Pages,          # page numbers to use (for a folder: positions in name order), e.g. 1,3
    [switch]$KeepDigits,
    [switch]$KeepPedal,
    [switch]$KeepRed,        # keep red ink (by default red pencil annotations are removed)
    # repair of enlarged low-resolution scans: auto = 'gentle' for enlarged pages, 'none' otherwise.
    #   gentle: thin staff lines to 3 px and reopen note holes   erode: aggressive, thins ALL ink (more whole notes, damaged lines)
    [ValidateSet('auto', 'none', 'gentle', 'erode')][string]$Enhance = 'auto',
    [switch]$KeepEndings,    # keep volta brackets that have no number text (normally removed as misreads)
    [switch]$SeparatePages   # by default a multi-page input gives ONE merged MusicXML; this keeps one file per page
)

$here      = Split-Path -Parent $MyInvocation.MyCommand.Path
$tools     = Join-Path $here 'omr_tools.py'
$audiveris = Join-Path $env:LOCALAPPDATA 'Audiveris\Audiveris\Audiveris.exe'
$tessdata  = Join-Path $env:LOCALAPPDATA 'Audiveris\tessdata'

if (-not (Test-Path $InputFile)) { throw "Input not found: $InputFile" }
$InputFile = (Resolve-Path $InputFile).Path
if (-not (Test-Path $audiveris)) { throw "Audiveris not found at $audiveris" }
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw "python not found on PATH" }

if (Test-Path -LiteralPath $InputFile -PathType Container) { $name = (Get-Item -LiteralPath $InputFile).Name }
else { $name = [IO.Path]::GetFileNameWithoutExtension($InputFile) }
if (-not $OutDir) { $OutDir = Join-Path (Split-Path -Parent $InputFile) ($name + '_out') }
New-Item -ItemType Directory -Force $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path
$work = Join-Path $OutDir 'work'
New-Item -ItemType Directory -Force $work | Out-Null

$chk = & python $tools check 2>&1 | Out-String
if ($chk.Trim() -ne 'ok') { throw "Python helper failed. Needs: pip install --user pymupdf numpy opencv-python-headless`n$chk" }

# --- 1+2: render and clean -------------------------------------------------
$prepArgs = @($tools, 'prep', '--input', $InputFile, '--outdir', $work, '--dpi', $Dpi)
if ($Pages)      { $prepArgs += @('--pages', $Pages) }
if ($KeepDigits) { $prepArgs += '--keep-digits' }
if ($KeepPedal)  { $prepArgs += '--keep-pedal' }
if ($KeepRed)    { $prepArgs += '--keep-red' }
$prepArgs += @('--enhance', $Enhance)
Write-Host "Cleaning pages..."
$prepOut = & python @prepArgs
$prepOut | Where-Object { $_ -notlike 'PAGE|*' } | ForEach-Object { Write-Host $_ }
$pngs = @($prepOut | Where-Object { $_ -like 'PAGE|*' } | ForEach-Object { $_.Substring(5) })
if ($pngs.Count -eq 0) { throw "No pages were prepared." }

# --- 3: Audiveris -------------------------------------------------------------
$oldTess = $env:TESSDATA_PREFIX
if (Test-Path (Join-Path $tessdata 'eng.traineddata')) { $env:TESSDATA_PREFIX = $tessdata }
else { Write-Warning "No eng.traineddata in $tessdata - title/tempo text will be missing." }

$mxls = @()
# one job = one thing handed to Audiveris: either every page bundled into a single PDF, or one page image
$jobs = @()
if ($pngs.Count -gt 1 -and -not $SeparatePages) {
    $bundle = Join-Path $work ($name + '_combined.pdf')
    & python $tools pdf --out $bundle --dpi $Dpi --pngs @pngs | Write-Host
    $jobs += ,@($bundle, ($name + '_combined'))
} else {
    foreach ($png in $pngs) { $jobs += ,@($png, [IO.Path]::GetFileNameWithoutExtension($png)) }
}
try {
    foreach ($job in $jobs) {
        $jobFile = $job[0]; $jobName = $job[1]
        Write-Host ("Running Audiveris on {0} ..." -f $jobName)
        $log = & $audiveris -batch -transcribe -export -output $OutDir $jobFile 2>&1 | Out-String
        # a book with several movements is written as name.mvt1.mxl, name.mvt2.mxl ...
        $found = @(Get-ChildItem -Path $OutDir -Filter ($jobName + '*.mxl') |
            Sort-Object @{ Expression = { $m = [regex]::Match($_.Name, 'mvt(\d+)'); if ($m.Success) { [int]$m.Groups[1].Value } else { 0 } } } |
            ForEach-Object { $_.FullName })
        if ($found.Count -gt 0) { $mxls += $found } else { Write-Warning "No MusicXML produced for $jobName`n$log" }
        if ($log -match 'NullPointerException') { Write-Host "  (Audiveris logged a NullPointerException on the tempo mark; the file was still written.)" }
    }
}
finally {
    $env:TESSDATA_PREFIX = $oldTess
}

# --- 3b: one file out ---------------------------------------------------------
# Audiveris writes one file per "movement"; a page that looks like the start of a piece starts a new one.
# The final file is always written by the merge step (also for a single file): it joins movements and
# drops volta brackets that carry no number text (misread slurs / pedal lines).
if ($mxls.Count -ge 1 -and -not $SeparatePages) {
    $merged = Join-Path $OutDir ($name + '.musicxml')
    $mergeArgs = @($tools, 'merge', '--out', $merged, '--inputs') + $mxls
    if ($KeepEndings) { $mergeArgs += '--keep-endings' }
    & python @mergeArgs | Write-Host
    if (Test-Path $merged) { $mxls = @($merged) }
}

# --- 4: report ----------------------------------------------------------------
if ($mxls.Count -gt 0) {
    $report = Join-Path $OutDir 'report.txt'
    $text = & python $tools report --mxl @mxls | Out-String
    Set-Content -Path $report -Value $text -Encoding UTF8
    Write-Host $text
    Write-Host "MusicXML : $($mxls -join ', ')"
    Write-Host "Report   : $report"
    Write-Host "Overlays : $work (red = erased fingering, blue = erased pedal line)"
}
