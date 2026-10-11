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
| Triplets Audiveris could not see | a group of three eighths becomes a separate voice of plain notes, so the bar does not add up | rebuilt as real triplets, but only when the arithmetic proves it (the groups must tile the bar exactly); each rebuilt bar carries a small "auto: triplets rebuilt" text (`-NoRepair` turns it off) |

| 8va / 8vb lines | Audiveris reads only a short stub of a long dashed line, so only the first notes under it are raised an octave and the rest sound an octave low (this breaks the key checker) | the dashed line, its sign and its end hook are found in the picture and erased; afterwards the notes under each line (located with the measure time slots from the project file) are raised or lowered and one clean octave-shift mark is written around them. A line only counts if an "8" sits next to it, so hyphen runs like "rit. - - -" are never mistaken for it (`-NoOttava` turns it off). Stray octave marks Audiveris writes for the "8" in a measure number such as "28" are dropped |
| Clef changes (e.g. a small bass clef before a barline) | the export puts an end-of-bar change one bar early and drops some, so a bar is in the wrong clef and its rests (exported with an explicit position) float above or below the staff | the clef signs and their positions are read from the project file and every staff's clef changes are rewritten to match (end-of-bar changes apply from the next bar; changes inside a bar are placed before the following note); `--no-clef-fix` turns it off |
| Key signature changes, especially to C major | C major has no sign, so Audiveris records no change and keeps the old key (an E that should be natural stays E-flat); the "sharps" in a bar can also be a printed accidental mistaken for a key sign | the key follows the system openings in the project file (a clef with no key = C major); a change between two openings happens at the first double barline between them; key signs elsewhere are only trusted right after a double barline, otherwise they are stray accidentals and ignored; notes whose accidental came only from the old key are re-pitched with the new one (`--no-key-fix` turns it off) |
| A part split in two | thin strokes at the left edge of a staff are read as part-name text ("i", "l"), so one part can come out as two (the export has more parts than any system has) | the extra part is merged back into the part it belongs to, but only when the two never both have notes in the same bar |

### Several readings (`-Readings`, default 3)

Audiveris is sensitive to the exact pixel scale: the same bar can be right at one scale and wrong at another (a beamed pair with a far-away head, for instance), and the two staves of one bar fail independently. `convert.ps1` therefore reads the score `-Readings` times (the normal scale plus slightly different `-Spacing` values, run in parallel) and `omr_vote.py` picks, for every staff of every bar, the reading whose rhythm adds up and that the others agree with most. Your first reading keeps a staff unless it is wrong and another reading is right, or the other valid readings outvote it; if no reading is valid nothing is replaced. Cost: about twice the runtime. `-Readings 1` gives the old single-reading behaviour. The unvoted result is kept as `<name>.single.musicxml`.

### Proofreading list

Rhythm is the cheapest way to find conversion errors: a bar whose voices do not add up to the time signature is almost always wrong.
After each run, open `<output>\proofread\index.html` in a browser. It lists every such bar with

- the page, the system and the position in the system,
- what is wrong in plain words ("voice 1 holds 4.5 beats in a 4-beat bar", "nothing is written here", "the fullest voice holds 3 of 4 beats"),
- a picture of the bar with the staff in question outlined, and a second picture showing where it sits in its system,
- filters by kind (overfull, empty staff, short, auto-repaired).

A wrong pitch inside a bar that still adds up cannot be found this way, so proofreading the whole score is still needed.
`report.txt` has the same findings as text. Bar numbers are the converted file's numbers; the numbers printed in the score can differ (a pickup bar shifts them by one).

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
