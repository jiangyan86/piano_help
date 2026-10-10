# piano_help

Two small tools for learning piano from printed sheet music, built around [Audiveris](https://github.com/Audiveris/audiveris)
(open-source optical music recognition) and plain Python. Everything runs on a laptop CPU, no GPU needed.

1. **Sheet music to MusicXML**: turn a PDF, an image, or a folder of page pictures into a MusicXML file you can open in MuseScore.
2. **Wrong-key checker**: listen to you play (microphone or a `.wav`) and report which keys differed from the score.

> Developed and tested on Windows 11. `convert.ps1` is PowerShell; the Python parts are portable.

## 1. Sheet music to MusicXML

```powershell
.\convert.ps1 .\score.pdf
.\convert.ps1 .\pages_folder          # images are taken in name order (2.png before 10.png)
.\convert.ps1 .\score.pdf -Pages 1,3 -OutDir D:\scores\out
```

The output folder gets the MusicXML file, `report.txt` and the cleaned page images under `work/`
(red boxes = erased fingering numbers, blue boxes = erased pedal lines).

What the script does before and after Audiveris, because Audiveris misreads some common printed-piano details:

| Problem on the page | What goes wrong in Audiveris | What this project does |
|---|---|---|
| Fingering numbers | read as tuplet numbers or stray text | detected by size/shape and erased |
| Pedal lines (`Ped. ___∧___`) | read as volta brackets | detected as straight thin lines under the last staff of a system and erased; hairpins, slurs and real voltas are left alone |
| Page tilted by a fraction of a degree | staves not found | straightened when the tilt is 0.2° or more |
| Low-resolution pictures | recognition degrades | enlarged until the staff spacing is about 19 px |
| Red pencil marks | read as notation | removed (`-KeepRed` keeps them) |
| Enlarged blurry scans | staff lines too thick, whole-note holes closed | staff lines thinned to 3 px, note holes reopened (`-Enhance`) |
| Two systems side by side (e.g. a Coda) | read as one system with a hole | cut apart and stacked as separate rows |
| Several pages / movements | separate files | merged into one score; volta marks without a number are dropped (`-KeepEndings`) |

`report.txt` lists measures worth proofreading first: rhythm errors, tuplets, and bars where a whole hand is empty (usually missed whole notes).

### Setup (Windows)
- Python 3.10+ and `pip install -r requirements.txt`. `convert.ps1` finds Python by itself (it skips the Microsoft Store
  `python` shortcut and tries the `py` launcher and the usual install folders); to force one, set `PIANO_HELP_PYTHON` to its path.
- Audiveris 5.11 extracted to `%LOCALAPPDATA%\Audiveris\Audiveris\Audiveris.exe` (the Windows MSI can be unpacked there with
  `msiexec /a <msi> /qn TARGETDIR=...` if you cannot install it system-wide)
- English OCR data: the *standard* `eng.traineddata` (not `tessdata_fast`; Audiveris needs the legacy engine) in
  `%LOCALAPPDATA%\Audiveris\tessdata`

### Known limits
- Accuracy depends on the source. Clean PDFs convert well; low-resolution pictures lose notes, **especially whole notes**.
  Always proofread the result against the score.
- Pedal marks and fingering are removed, not recognised, so they are not in the output.
- Tuned on a few piano scores; check the overlay images on new material.

## 2. Wrong-key checker

```powershell
python piano_check.py score.musicxml --record        # play, press Enter to stop
python piano_check.py score.musicxml --wav take.wav  # analyse a recording
python piano_check.py score.musicxml --wav take.wav --measures 5-12 --no-jumps
```

### Live version

```powershell
python live_check.py                 # then pick a music file in the browser page that opens
python live_check.py score.musicxml  # or start with a file
python live_check.py --list-devices  # choose a microphone with --device N
```

Listens through the microphone, follows you through the score and marks wrong keys on the sheet music, judging each measure
once you have moved on. Mistakes stay on screen until you press **New practice**. Recent files are remembered
(`score_history.json`); the last take is saved as `last_take.wav`. Use `--wav take.wav` to replay a recording instead of the microphone.

### iPad / browser app (no install)

`docs/` is the same checker rewritten in JavaScript. It runs entirely in the browser (Safari on iPad, or any desktop browser):
it listens through the microphone, follows you through the score and marks wrong keys on the sheet music. Opened files and
your last practice are remembered on the device. Serve it on GitHub Pages (Settings -> Pages -> branch `main`, folder `/docs`)
and open the address on the iPad; "Add to Home Screen" makes it launch like an app. Locally: `python -m http.server -d docs`.

`node docs/test/run.mjs score.musicxml take.wav` replays a 22.05 kHz mono WAV through the JavaScript engine; its output matches
the Python tool on the same audio.

`report_html.py` writes a graphical practice report; `make_test_audio.py` synthesises a take with deliberate mistakes to test the checker;
`tutor/index.html` is a browser version that shows the score and flags wrong keys.

## License

The code in this repository is released under the [MIT License](LICENSE).

It drives, but does not include, third-party software that has its own licenses: Audiveris (AGPL-3.0), Tesseract language data
(Apache-2.0), PyMuPDF (AGPL-3.0 or commercial), and OpenSheetMusicDisplay (BSD-3-Clause, loaded from a CDN by `tutor/index.html`).

## Sheet music and copyright

Do not commit scores, page pictures, recordings or conversion output: most printed music is copyrighted.
The `.gitignore` only allows the source files listed in it.
