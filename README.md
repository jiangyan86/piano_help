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
| Ties and slurs over a page break | Audiveris only links arcs whose two ends are on the same page; the arc leaving the last bar of a page and the stub entering the next page are both dropped | the arc is looked for in the page picture (a thin curved stroke right of the last chord that runs to the final barline); the chord is then tied to the first chord of the next page (same pitches) or, if the pitches differ, slurred; marked "please verify" in the log |
| An extra note beside a chord note | a smudge such as the bowl of a flat is read as a second notehead one step away from a real note; the rhythm still adds up, so nothing else notices | a head is dropped only if the project file grades it below 0.35, it shares a chord with a head graded 0.55+, and less than 30% of its own box (outside stems, other heads and accidentals) holds ink; the bar gets a small text mark "auto: extra note removed - please verify" |
| A natural / sharp sign read as a note | the sign is taken for a notehead on the same line as the real note (two identical notes in one chord) | same rule as above (a very poorly graded head sitting on the same line or space as a better head of its chord is dropped); the real note then gets a natural sign if the key signature alters it |
| A staccato dot read as an augmentation dot | a dot right above the next note of a beamed group is attached to the note before: it comes out 50% too long and the bar is over; a smudge much bigger than a dot is also taken for one | an augmentation dot must lie right after its head and in line with it, and be dot-sized; otherwise the note loses the dot (and the bar's timing follows) and, if the dot sits over another note, that note gets a staccato |
| Fermatas | Audiveris does not read them | found in the page picture (a crescent with a dot inside, above or below a staff) and written on the notes or rests at that moment of the bar |
| Trill lines | Audiveris does not read them ("tr" and a wavy line) | a long thin stroke above a staff whose centre line goes up and down many times is a trill: the first note under it gets a trill mark and the start of a wavy line, the last one its end |
| Grace notes | Audiveris does not read them; a small notehead becomes a badly graded extra note, and the sharp in front of it ends up on the main note, giving the main note a wrong pitch | small noteheads (about 0.9 x 0.8 staff spaces, with a stem) are found in the page picture; their pitch comes from the staff position relative to the main note, the accidental in front of each is read from the picture, the misread extra notes are removed and slashed grace notes are written before the main note |
| A short 8va line | `(8va)` with just a dash and the end hook, over a single chord, was not recognised as an ottava | a short line is accepted when it ends in the dashed hook |
| One line written as two voices | Audiveris splits a single line of notes into two voices: the second starts after a gap, a rest is missing or doubled, MuseScore shows an extra voice full of rests | when the staff has 2+ voices, its rhythm does not add up, and its chords (as exported, in the order they stand on the page) plus the rests of the project file fill the bar exactly, it is rewritten as one voice; one single rest that Audiveris did not see is put in the one gap that is much wider than the others when the missing length is a plain rest value (the rest is blue; old beams are dropped, MuseScore beams by beat) |
| A bar that is one fast run split between the hands | printed with two beams (one above, one below) and a stem through every note; Audiveris links stems to the wrong beams, doubles chords, leaves notes in no voice and the slot times add up to more than the bar | if the rhythm of a staff does not add up and the project file holds 6+ beamed black heads in it (one per place, no chords) that fill the bar with equal lengths of a plain note value or a tuplet (24 notes in 2/4 = four sextuplets of 32nds), the staff is rebuilt as one voice: pitch from the staff position (clef, 8va line, key, signs - read from the picture when the project file missed them), length from the beams; the beams as printed are not reproduced |
| An 8va line in a coarse picture | the "8" has no two holes, the staff below is not found by the projection, the line sits far above the notes, one dash is missing | a long chain of dashes (8+) with a hook is accepted without the "8", gaps of one dash are bridged, the staff is also looked for by following its lines and lines up to 16 spaces above are taken |
| MuseScore shows a landscape page with one or two huge systems | Audiveris copies the layout of the input pictures into the file: a page the size of the picture (a 1920x1080 screenshot is a landscape page), a staff scale that makes a staff several centimetres high, and a page / system break where each input page ended | the finished file gets a normal portrait A4 page, the usual staff size and no forced breaks, so MuseScore lays the music out itself (`-KeepLayout` keeps the input's layout) |
| Bent or unevenly tilted staves (scans of books, screenshots of videos) | one rotation for the whole page (deskew) cannot straighten a page whose staves run uphill in one system and downhill in the next, or bend in the middle; Audiveris then misreads rhythms and misses notes | every staff is followed along the page (a 5-line comb matched in vertical strips), its centre line is fitted with a smooth curve and the picture is re-sampled so each curve becomes a straight horizontal line, blended between neighbouring staves; pages that are straight already are left alone (`--no-dewarp` turns it off) |
| Audiveris writes nothing for the whole book | it can crash on one page at one scale (a NullPointerException in its tie/slur linking: "Error processing stub") and then exports nothing, although the same page reads fine at another scale | the page that failed is prepared again at staff spacing 17, then 21.5, then 15.5, the book is rebuilt and read again; the log says which page needed another scale |
| Notes of an 8va line shown an octave too high in MuseScore | MuseScore adds up the lengths of the bars to place the ends of an 8va line: any bar that is shorter or longer than its time signature (a note Audiveris left out, an unfinished rhythm) throws the sum off, and every 8va that ends later in the piece ends too early (the last notes under the bracket show an octave too high although the bracket is drawn right) | the finished file is padded: every short bar is filled with a `<forward>` to its full length (nothing is drawn). This is done last, after the proofreading list and the vote, which look for short bars. A bar that holds MORE than its time signature cannot be padded; the log names them. There is one octave-shift mark pair per system, as the line is printed |
| Notes missing from a bar | when a bar's rhythm will not add up Audiveris leaves some chords out of every voice (they stay in the project file); the bar comes out short, or a triplet group is missing so the triplet repair cannot prove its arithmetic | a well-graded chord with a stem that belongs to no voice is put back after the chord on its left in the same staff when that one ends its voice; its length comes from the beams on its stem (an unbeamed one only if it fills the voice exactly), its pitch from the staff position, its sign from the project file; the triplet repair then runs on the completed bar |
| A tuplet that is not on the page | a caption or other text between the staves read as a tuplet number (low grade) | a tuplet whose notes are not all in one voice of one staff, has no end, or does not hold a whole number of groups loses its tuplet marks and gets its normal lengths back |
| An accidental holds for the rest of the bar | a natural sign added to a note did not reach the later notes of the same bar on that line or space | later same-pitch notes of the bar follow the sign |
| Grace notes: the voice after a misread grace note | the misread note pushed every later note of its voice one beat to the right and the last chord fell out of the bar | the misread note is removed with its time closed up when the main note belongs where the misread note began (judged from the chords to its left), otherwise its time is kept as a gap |

### Several readings (`-Readings`, default 1)

Audiveris is sensitive to the exact pixel scale: the same bar can be right at one scale and wrong at another (a beamed pair with a far-away head, for instance), and the two staves of one bar fail independently. `convert.ps1` therefore reads the score `-Readings` times (the normal scale plus slightly different `-Spacing` values, run in parallel) and `omr_vote.py` picks, for every staff of every bar, the reading whose rhythm adds up and that the others agree with most. Your first reading keeps a staff unless it is wrong and another reading is right, or the other valid readings outvote it; if no reading is valid nothing is replaced. Cost: about twice the runtime for 3 readings, so it is off by default (`-Readings 1`); use `-Readings 3` when accuracy matters more than time. The unvoted result is kept as `<name>.single.musicxml`.

### Colours: where to look first

The converted file colours the notes that deserve a second look (MuseScore shows them; `-NoHighlight` turns it off):

- **blue**: written or rebuilt by the script from the picture (a note put back, a run rebuilt, a grace note, a triplet group): never checked against anything;
- **red**: a notehead Audiveris read with low confidence (the project file grades it below 0.6; good ones are 0.7 and up);
- **orange**: every note of a staff in a bar whose rhythm does not add up (too short or too long): something is missing, doubled or of the wrong length, but not necessarily this note.

Blue wins over red, red over orange. The proofreading list below has the same bars with pictures.

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
