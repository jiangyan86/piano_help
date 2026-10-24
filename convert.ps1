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
    [switch]$NoHighlight,    # do not colour the notes that deserve a second look (blue / red / orange)
    [switch]$KeepLayout,     # keep the page size and page/system breaks of the input pictures (normally a portrait page)
    [switch]$KeepEndings,    # keep volta brackets that have no number text (normally removed as misreads)
    [switch]$NoRepair,       # do not rebuild triplets that Audiveris read as plain notes (see omr_repair.py)
    [switch]$NoOttava,       # leave 8va/8vb lines to Audiveris (it only sees a stub of them; see omr_ottava.py)
    # Audiveris is sensitive to the exact pixel scale: a bar it gets wrong at one scale is often right at another.
    # -Readings 3 reads the score 3 times (the normal scale plus two slightly different ones, run in parallel) and keeps,
    # bar by bar, the reading whose rhythm adds up and that the others agree with most (see omr_vote.py).
    [ValidateRange(1, 5)][int]$Readings = 1,
    [double]$Spacing = 19,   # staff-line spacing (px) this reading is scaled to; the extra readings use other values
    [switch]$NoProofread,    # (used internally by the extra readings)
    [switch]$SeparatePages   # by default a multi-page input gives ONE merged MusicXML; this keeps one file per page
)

$here      = Split-Path -Parent $MyInvocation.MyCommand.Path
$tools     = Join-Path $here 'omr_tools.py'
$audiveris = Join-Path $env:LOCALAPPDATA 'Audiveris\Audiveris\Audiveris.exe'
$tessdata  = Join-Path $env:LOCALAPPDATA 'Audiveris\tessdata'

if (-not (Test-Path $InputFile)) { throw "Input not found: $InputFile" }
$InputFile = (Resolve-Path $InputFile).Path
if (-not (Test-Path $audiveris)) { throw "Audiveris not found at $audiveris" }

# --- find a working Python ----------------------------------------------------
# On Windows a plain `python` is often a Microsoft Store shortcut that only prints "Python was not found",
# so do not trust PATH alone.  Candidates, in order: $env:PIANO_HELP_PYTHON, real `python` entries on PATH
# (skipping the WindowsApps stub), the `py` launcher, then the usual install folders.  The first one that can
# import everything omr_tools.py needs wins.
function Find-Python {
    $cands = New-Object System.Collections.Generic.List[string]
    if ($env:PIANO_HELP_PYTHON) { $cands.Add($env:PIANO_HELP_PYTHON) }
    foreach ($c in @(Get-Command python -All -ErrorAction SilentlyContinue)) {
        if ($c.Source -and $c.Source -notlike '*\WindowsApps\*') { $cands.Add($c.Source) }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $real = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($real) { $cands.Add(([string]$real).Trim()) }
    }
    foreach ($pat in @("$env:LOCALAPPDATA\Python\pythoncore-*\python.exe",
                       "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
                       "$env:ProgramFiles\Python3*\python.exe")) {
        foreach ($f in @(Get-ChildItem $pat -ErrorAction SilentlyContinue | Sort-Object FullName -Descending)) { $cands.Add($f.FullName) }
    }
    $withoutModules = $null
    foreach ($c in ($cands | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $c)) { continue }
        $r = (& $c $tools check 2>&1 | Out-String).Trim()
        if ($r -eq 'ok') { return @{ Path = $c; Missing = $null } }
        if (-not $withoutModules) { $withoutModules = $c }
    }
    return @{ Path = $null; Missing = $withoutModules }
}
$found = Find-Python
if (-not $found.Path) {
    if ($found.Missing) {
        throw ("Found Python at {0} but it lacks a required package. Install them with:`n    `"{0}`" -m pip install --user -r requirements.txt" -f $found.Missing)
    }
    throw ("No working Python found. Install Python 3 from https://www.python.org/downloads/ (tick 'Add to PATH'), then run:`n" +
           "    python -m pip install --user -r requirements.txt`n" +
           "or point PIANO_HELP_PYTHON at an interpreter that already has the packages.")
}
$python = $found.Path
Write-Host "Using Python: $python"

if (Test-Path -LiteralPath $InputFile -PathType Container) { $name = (Get-Item -LiteralPath $InputFile).Name }
else { $name = [IO.Path]::GetFileNameWithoutExtension($InputFile) }
if (-not $OutDir) { $OutDir = Join-Path (Split-Path -Parent $InputFile) ($name + '_out') }
New-Item -ItemType Directory -Force $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path
$work = Join-Path $OutDir 'work'
New-Item -ItemType Directory -Force $work | Out-Null


# --- 1+2: render and clean -------------------------------------------------
$prepArgs = @($tools, 'prep', '--input', $InputFile, '--outdir', $work, '--dpi', $Dpi)
if ($Pages)      { $prepArgs += @('--pages', $Pages) }
if ($KeepDigits) { $prepArgs += '--keep-digits' }
if ($KeepPedal)  { $prepArgs += '--keep-pedal' }
if ($KeepRed)    { $prepArgs += '--keep-red' }
$prepArgs += @('--enhance', $Enhance)
if ($NoOttava)   { $prepArgs += '--keep-ottava' }
if ($Spacing -ne 19) { $prepArgs += @('--spacing', $Spacing) }
Write-Host "Cleaning pages..."
$prepOut = & $python @prepArgs
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
    & $python $tools pdf --out $bundle --dpi $Dpi --pngs @pngs | Write-Host
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
        $findMxl = {
            @(Get-ChildItem -Path $OutDir -Filter ($jobName + '*.mxl') |
                Sort-Object @{ Expression = { $m = [regex]::Match($_.Name, 'mvt(\d+)'); if ($m.Success) { [int]$m.Groups[1].Value } else { 0 } } } |
                ForEach-Object { $_.FullName })
        }
        $found = & $findMxl
        # Audiveris can fail on ONE page at ONE scale (a NullPointerException deep in its tie/slur linking) and then
        # writes nothing for the whole book, although the same page reads fine at another scale.  Prepare the page(s)
        # that failed at another scale and run the book again.
        if ($found.Count -eq 0 -and $bundle -and $jobFile -eq $bundle -and $log -match 'Error processing stub') {
            foreach ($sp in @(17, 21.5, 15.5)) {
                $failed = @([regex]::Matches($log, '\[[^\]]*#(\d+)\][^\r\n]*Error processing stub') |
                    ForEach-Object { [int]$_.Groups[1].Value } | Sort-Object -Unique)
                if ($failed.Count -eq 0) { break }
                foreach ($k in $failed) {
                    $pageNo = [regex]::Match($pngs[$k - 1], '_p(\d+)\.png$').Groups[1].Value
                    Write-Host ("  Audiveris failed on page {0}; reading it again at staff spacing {1} ..." -f $pageNo, $sp)
                    $rpArgs = @($tools, 'reprep', '--input', $InputFile, '--page', [int]$pageNo, '--stub', $k,
                                '--spacing', $sp, '--work', $work, '--dpi', $Dpi, '--enhance', $Enhance)
                    if ($NoOttava) { $rpArgs += '--keep-ottava' }
                    & $python @rpArgs 2>&1 | Out-Null
                }
                & $python $tools pdf --out $bundle --dpi $Dpi --pngs @pngs | Out-Null
                Remove-Item -LiteralPath (Join-Path $OutDir ($jobName + '.omr')) -Force -ErrorAction SilentlyContinue
                $log = & $audiveris -batch -transcribe -export -output $OutDir $jobFile 2>&1 | Out-String
                $found = & $findMxl
                if ($found.Count -gt 0) { Write-Host ("  ... fine at spacing {0}." -f $sp); break }
            }
        }
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
$repairFiles = @()          # one repair log per final MusicXML file (for the proofreading list)
if ($mxls.Count -ge 1 -and -not $SeparatePages) {
    $movements = @($mxls)
    $pieceFiles = @()
    $pieceRepairs = @()
    # 8va/8vb lines taken out of the pictures: apply them to the notes (needs the project file's measure positions)
    $ottavaArgs = @()
    $omrForMerge = Join-Path $OutDir (($jobs[0][1]) + '.omr')
    $ottavaJson  = Join-Path $work 'ottava.json'
    if (-not $NoOttava -and $jobs.Count -eq 1 -and (Test-Path $omrForMerge) -and (Test-Path $ottavaJson)) {
        $ottavaArgs = @('--ottava', $ottavaJson, '--omr', $omrForMerge, '--pages', $work)
    }
    elseif ($jobs.Count -eq 1 -and (Test-Path $omrForMerge)) {
        $ottavaArgs = @('--omr', $omrForMerge, '--pages', $work)      # ties/slurs over page breaks only
    }
    if ($movements.Count -gt 1) {
        # A PDF often holds several pieces (the end of one exercise, the next exercise, example lines ...).
        # Say what each one is, and also write each as its own cleaned file, so a piece is never "lost" just
        # because the pieces could not be joined into one score.
        $omrFile = Join-Path $OutDir (($jobs[0][1]) + '.omr')
        & $python $tools pieces --omr $omrFile --mxl @movements 2>&1 | Out-String | Write-Host
        for ($i = 0; $i -lt $movements.Count; $i++) {
            $pf = Join-Path $OutDir ("{0}_piece{1}.musicxml" -f $name, ($i + 1))
            $pr = Join-Path $OutDir ("{0}_piece{1}.repairs.json" -f $name, ($i + 1))
            $pa = @($tools, 'merge', '--out', $pf, '--repairs-out', $pr, '--inputs', $movements[$i]) + $ottavaArgs
            if ($ottavaArgs.Count -gt 0 -and $i -gt 0) { $pa += '--preceding'; $pa += @($movements[0..($i - 1)]) }
            if ($KeepEndings) { $pa += '--keep-endings' }
            if ($KeepLayout)  { $pa += '--keep-layout' }
            if ($NoHighlight) { $pa += '--no-highlight' }
            if ($NoRepair)    { $pa += '--no-repair' }
            & $python @pa 2>&1 | Out-Null
            if (Test-Path $pf) { $pieceFiles += $pf; $pieceRepairs += $pr }
        }
    }
    $merged = Join-Path $OutDir ($name + '.musicxml')
    $mergedRepairs = Join-Path $OutDir ($name + '.repairs.json')
    $mergeArgs = @($tools, 'merge', '--out', $merged, '--repairs-out', $mergedRepairs) + $ottavaArgs + @('--inputs') + $movements
    if ($KeepEndings) { $mergeArgs += '--keep-endings' }
    if ($KeepLayout)  { $mergeArgs += '--keep-layout' }
    if ($NoHighlight) { $mergeArgs += '--no-highlight' }
    if ($NoRepair)    { $mergeArgs += '--no-repair' }
    if (Test-Path $merged) { Remove-Item -LiteralPath $merged -Force }      # never leave a stale file from an earlier run
    $mergeOut = (& $python @mergeArgs 2>&1 | Out-String).Trim()
    if (Test-Path $merged) {
        Write-Host $mergeOut
        $mxls = @($merged)
        $repairFiles = @($mergedRepairs)
    }
    elseif ($pieceFiles.Count -gt 0) {
        # Not an error: the pieces simply have different staff layouts (for example a piano exercise followed by
        # single-staff example lines), so they are different pieces and were not joined.
        Write-Host ("`nNo combined file: the {0} pieces have different staff layouts, so they are separate pieces, not one score." -f $movements.Count)
        Write-Host "Each piece was written as its own file:"
        $pieceFiles | ForEach-Object { Write-Host "  $_" }
        $mxls = $pieceFiles
        $repairFiles = $pieceRepairs
    }
    else {
        Write-Warning ("NO MUSICXML COULD BE WRITTEN from the {0} pieces Audiveris found: {1}" -f $movements.Count, $mergeOut)
    }
}

# --- 3c: extra readings, then a bar-by-bar vote -------------------------------
# The same page read at a slightly different pixel scale often comes out differently; a bar that is wrong in one
# reading is frequently right in another.  Each extra reading is a complete run of this script at another staff
# spacing (all in parallel); then for every bar the reading whose rhythm adds up and that the others agree with
# most is kept.  The first (normal) reading wins ties, so a bar is only replaced when another is clearly better.
if ($Readings -gt 1 -and $mxls.Count -eq 1 -and $merged -and (Test-Path $merged) -and -not $SeparatePages) {
    $spacings = @(17, 15.5, 21.5, 23.5)       # smaller scales first: they read faster
    $inv = [Globalization.CultureInfo]::InvariantCulture
    $procs = @(); $extraXml = @(); $extraRep = @()
    for ($k = 0; $k -lt ($Readings - 1); $k++) {
        $sp  = $spacings[$k]
        $sub = Join-Path $OutDir ('reading_sp' + $sp.ToString($inv).Replace('.', '_'))
        $a = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath), ('"{0}"' -f $InputFile),
               '-OutDir', ('"{0}"' -f $sub), '-Spacing', $sp.ToString($inv), '-NoProofread', '-Dpi', $Dpi, '-Enhance', $Enhance)
        if ($Pages)       { $a += @('-Pages', $Pages) }
        if ($KeepDigits)  { $a += '-KeepDigits' }
        if ($KeepPedal)   { $a += '-KeepPedal' }
        if ($KeepRed)     { $a += '-KeepRed' }
        if ($KeepEndings) { $a += '-KeepEndings' }
        if ($NoRepair)    { $a += '-NoRepair' }
        if ($NoOttava)    { $a += '-NoOttava' }
        New-Item -ItemType Directory -Force $sub | Out-Null
        $log = Join-Path $sub 'run.log'
        $procs += Start-Process -FilePath 'powershell.exe' -ArgumentList ($a -join ' ') -PassThru -WindowStyle Hidden `
                  -RedirectStandardOutput $log -RedirectStandardError ($log + '.err')
        $extraXml += (Join-Path $sub ($name + '.musicxml'))
        $extraRep += (Join-Path $sub ($name + '.repairs.json'))
    }
    Write-Host ("Reading the score {0} more time(s) at other scales, in parallel (staff spacing {1}) ..." -f ($Readings - 1), (($spacings[0..($Readings - 2)]) -join ', '))
    $procs | Wait-Process
    $okXml = @(); $okRep = @()
    for ($k = 0; $k -lt $extraXml.Count; $k++) {
        if (Test-Path $extraXml[$k]) { $okXml += $extraXml[$k]; $okRep += $extraRep[$k] }
        else { Write-Warning ("extra reading {0} produced no combined MusicXML; see {1}" -f ($k + 1), (Join-Path (Split-Path $extraXml[$k]) 'run.log')) }
    }
    if ($okXml.Count -gt 0) {
        $single = Join-Path $OutDir ($name + '.single.musicxml')
        Copy-Item -LiteralPath $merged -Destination $single -Force
        Copy-Item -LiteralPath $mergedRepairs -Destination ($mergedRepairs + '.single') -Force -ErrorAction SilentlyContinue
        $voteArgs = @($tools, 'vote', '--out', $merged, '--inputs', $single) + $okXml + @('--repairs-in', ($mergedRepairs + '.single')) + $okRep + @('--repairs-out', $mergedRepairs)
        Write-Host ((& $python @voteArgs 2>&1 | Out-String).Trim())
        Write-Host "(the normal single reading is kept as $single)"
    }
}

# --- 4: proofreading list -----------------------------------------------------
# Every bar whose rhythm does not add up, with its page, system and a picture of that spot (index.html).
$proofIndex = $null
if ($mxls.Count -gt 0 -and $jobs.Count -eq 1 -and -not $SeparatePages -and -not $NoProofread) {
    $omrForProof = Join-Path $OutDir (($jobs[0][1]) + '.omr')
    if (Test-Path $omrForProof) {
        $pfArgs = @($tools, 'proofread', '--omr', $omrForProof, '--work', $work, '--mxl') + $mxls
        $existing = @($repairFiles | Where-Object { $_ -and (Test-Path $_) })
        if ($existing.Count -eq $mxls.Count) { $pfArgs += '--repairs'; $pfArgs += $existing }
        $pfArgs += @('--out', (Join-Path $OutDir 'proofread'), '--title', "$name : bars to proofread")
        $pfOut = (& $python @pfArgs 2>&1 | Out-String)
        foreach ($line in ($pfOut -split "`r?`n")) {
            if ($line -like 'PROOF|*') { $proofIndex = $line.Substring(6) }
            elseif ($line.Trim()) { Write-Host $line }
        }
    }
}

# --- 5: report ----------------------------------------------------------------
if ($mxls.Count -gt 0) {
    $report = Join-Path $OutDir 'report.txt'
    $text = & $python $tools report --mxl @mxls | Out-String
    if ($proofIndex) { $text = $text.TrimEnd() + "`r`nProofreading list (open in a browser): $proofIndex`r`n" }
    Set-Content -Path $report -Value $text -Encoding UTF8
    Write-Host $text
    Write-Host "MusicXML : $($mxls -join ', ')"
    Write-Host "Report   : $report"
    if ($proofIndex) { Write-Host "Proofread: $proofIndex" }
    Write-Host "Overlays : $work (red = erased fingering, blue = erased pedal line)"
}

# --- 5b: pad short bars (last, on the finished file: the lists above look for short bars) --------------------
if ($mxls.Count -gt 0 -and -not $NoProofread) {
    $padArgs = @($tools, 'pad', '--inputs') + $mxls
    if ($NoHighlight) { $padArgs += '--no-highlight' }
    & $python @padArgs 2>&1 | Out-String | Write-Host
    if (-not $NoHighlight) { Write-Host 'Colours in MuseScore: blue = written or rebuilt by the script (never checked); red = a notehead Audiveris read with low confidence; orange = a staff in a bar whose rhythm does not add up.' }
}
