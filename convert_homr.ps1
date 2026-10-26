<#
.SYNOPSIS
  Convert printed sheet music to MusicXML with homr (a neural-network reader), marking the notes that are probably wrong.
  Input can be a PDF, one image, or a FOLDER of page images (taken in name order: 2.png before 10.png, case ignored).

.DESCRIPTION
  1. First run only: sets up a private Python 3.12 environment in .homr_env (homr needs 3.10-3.12) with homr and
     PyMuPDF, and downloads homr's models.  Needs the internet once; about 0.5 GB.  Nothing outside this folder is changed
     except a `pip install --user uv` into the Python it finds.
  2. Renders the PDF pages, runs homr on every page (about 1-2 minutes per reading on a CPU) and joins the pages into ONE
     MusicXML file.  homr is sensitive to the pixel scale, so each page is read -Readings times (default 4: as rendered,
     1.5x and 0.75x); the reading with the most bars whose rhythm adds up is the base, and each bar of it that does not
     add up is replaced by the same bar of another reading where it does.  A staff left empty in a bar gets a whole-bar rest
     (blue).
  3. Marks what deserves a second look, so a manual proofread starts where the doubt is:
       red     homr was unsure about this note (its own certainty is below -Low)
       orange  every note of a staff in a bar whose rhythm does not add up
     and writes proofread\index.html (every marked bar with a picture of its system) and report.txt.

  Compared with convert.ps1 (Audiveris): no cleaning of fingering numbers, pedal lines, 8va lines etc. and no repairs;
  it ignores fingerings, pedal, 8va and ties/slurs across pages, and reads the whole piece as one part with two staves.

.EXAMPLE
  .\convert_homr.ps1 .\score.pdf
  .\convert_homr.ps1 .\pages_folder
  .\convert_homr.ps1 .\score.pdf -Pages 1,3 -OutDir D:\scores\out -Low 0.8 -Readings 1
  .\convert_homr.ps1 .\score.pdf -NoCompare       # skip the side-by-side page (it needs MuseScore 4)
#>
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$InputFile,
    [string]$OutDir,
    [int]$Dpi = 200,                  # PDF rendering resolution
    [string]$Pages,                   # page numbers to use (for a folder: positions in name order), e.g. 1,3
    [double]$Low = 0.7,               # notes whose certainty is below this are marked red (higher = more marks)
    [ValidateRange(1, 5)][int]$Readings = 4,  # read every page this many times at other scales and take the best bars (1 = faster)
    [double]$OrnamentCertainty = 0.8, # ornaments, arpeggios and breath marks the model was less sure of than this are removed (0 keeps all)
    [switch]$DropOrnaments,            # remove every ornament, arpeggio and breath mark homr read, however sure it was
    [switch]$NoCompare                 # do not build compare\index.html (every row of the original above the same row of the score)
)

$ErrorActionPreference = 'Continue'   # native tools write notices to stderr; failures are caught through $LASTEXITCODE
$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$envDir = Join-Path $here '.homr_env'
$envPy  = Join-Path $envDir 'Scripts\python.exe'
$run    = Join-Path $here 'homr_run.py'

if (-not (Test-Path -LiteralPath $InputFile)) { throw "Input not found: $InputFile" }
$InputFile = (Resolve-Path -LiteralPath $InputFile).Path

function Find-BootstrapPython {
    # a real python (not the Microsoft Store stub) that can run pip
    foreach ($cmd in @('py', 'python')) {
        if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { continue }
        $exe = (& $cmd -c "import sys; print(sys.executable)" 2>$null | Out-String).Trim()
        if ($exe -and (Test-Path -LiteralPath $exe) -and $exe -notlike '*\WindowsApps\*') { return $exe }
    }
    return $null
}

if (-not (Test-Path $envPy)) {
    Write-Host "First run: setting up homr in $envDir (once; downloads Python 3.12, homr and its models, about 0.5 GB)..."
    $boot = Find-BootstrapPython
    if (-not $boot) { throw "No working Python found to bootstrap with. Install Python 3 from https://www.python.org/downloads/ and run again." }
    & $boot -m pip install --user --quiet uv
    if ($LASTEXITCODE) { throw "Could not install uv (pip install --user uv failed)." }
    & $boot -m uv venv --python 3.12 $envDir
    if ($LASTEXITCODE) { throw "Could not create the Python 3.12 environment." }
    & $boot -m uv pip install --python $envPy 'homr==0.7.0' pymupdf
    if ($LASTEXITCODE) { throw "Could not install homr." }
}

$runArgs = @($run, $InputFile, '--dpi', $Dpi, '--low', $Low)
$runArgs += @('--readings', $Readings)
$runArgs += @('--ornament-certainty', $OrnamentCertainty)
if ($DropOrnaments) { $runArgs += '--drop-ornaments' }
if ($OutDir) { $runArgs += @('--outdir', $OutDir) }
if ($Pages)  { $runArgs += @('--pages', $Pages) }

# homr and its libraries print a lot of harmless warnings; keep them out of the way
$env:PYTHONWARNINGS = 'ignore'
$env:PYTHONIOENCODING = 'utf-8'
& $envPy @runArgs 2>&1 | Where-Object {
    $_ -notmatch 'SyntaxWarning|Possible attributes|XSDSimpleType|\[RapidOCR\]|^\s+``|^Inference Time|^Downloaded \d+ of|RemoteException|^Dewarping|^Running TrOmr|^Writing XML'
} | ForEach-Object { Write-Host "$_" }
if ($LASTEXITCODE) { throw "homr_run.py failed (exit code $LASTEXITCODE)." }

# --- side by side: every row of the original above the same row of the score (needs MuseScore)
if (-not $NoCompare -and $LASTEXITCODE -eq 0) {
    if ($OutDir) { $outFolder = $OutDir }
    else {
        $leaf = if (Test-Path -LiteralPath $InputFile -PathType Container) { (Get-Item -LiteralPath $InputFile).Name } else { [IO.Path]::GetFileNameWithoutExtension($InputFile) }
        $outFolder = Join-Path (Split-Path -Parent $InputFile) ($leaf + '_homr')
    }
    & $envPy (Join-Path $here 'homr_compare.py') $outFolder
}
