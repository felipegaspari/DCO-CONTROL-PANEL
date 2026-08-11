# DCO bench controller

Control the DCO board from Linux over one USB cable, with no Input board and no Screen
attached.

Parameters go out over the board's USB serial port. **Notes do not go through this tool** —
the DCO already enumerates as a USB MIDI device (`DCO3-MONO` / `DCO4-REBORN`), so play it
from a MIDI keyboard, VMPK, or anything else that can reach an ALSA MIDI port.

---

## 0. Synth models

One codebase drives both synths; the copies in `DCO3-MONOSYNTH/DCO/tools/dco_control`
and `DCO4-REBORN/DCO/tools/dco_control` are identical. Everything model-specific lives
in [`models.py`](models.py):

| | dco3 | dco4 |
|---|---|---|
| USB product | `DCO3-MONO` | `DCO4-REBORN` |
| Sub-osc tab (params 90-99) | yes | no |
| Hidden params | — | OSC3 group (2-osc voices) |
| Oscillator naming | OSC1/OSC2 | OSC A/OSC B |
| Cal tables (amp-comp / PW / offsets) | 3 osc, 3 PW ch | 8 osc, 4 PW ch |
| Mainboard profiler panel | no | yes (STM32 opcodes 40-42) |
| Preset bank file | `presets/bank_dco3.json` | `presets/bank_dco4.json` |

The model is **auto-detected from the USB product string** of the connected board;
override with `--model dco3|dco4` or `DCO_CONTROL_MODEL=dco4`. With no board attached
and no override, the tool defaults to dco3.

Patch and bank `.json` files use one shared parameter numbering and load on either
model (missing params fall back to defaults). Calibration files are strictly
per-model (`dco3-cal` / `dco4-cal`) because the table sizes differ.

A pre-existing `presets/bank.json` (from before the tool was model-aware) is adopted
by the first model that runs without its own bank file.

DCO4-REBORN topology note: the DCO's `Serial2` peer is the **STM32 Mainboard**, not the
Input board. USB `'a'`/`'b'`/`'d'` blocks update DCO locals **and** are mirrored to the
Mainboard's analog VCA/VCF CVs when Serial2 is up; `'c'` (EnvDCO) stays on the DCO. The
Diagnostics tab gains a **Mainboard profiler** panel (opcodes 40–42 forwarded over
Serial2, dump returns as `'t'` chunks into the Board output pane; needs Mainboard
`RUNNING_AVERAGE` — see `DCO/docs/BENCHMARKING.md` §12 in DCO4-REBORN).

---

## 1. Firmware requirement

The board must be built with `ENABLE_USB_CONTROL`, which is on by default in
[`DCO.ino`](../../DCO.ino). USB CDC uses the same slim inner frames and handler LUT as
Serial2 (`serial_input_protocol.h`). The Input board firmware still sends the older BE
format until it is updated; this tool is the live control path.

On-wire framing defaults to **RAW** (inner bytes as-is). To A/B **COBS** (`COBS(inner)+0x00`),
uncomment `#define SERIAL_FRAMING_COBS` in [`DCO.ino`](../../DCO.ino) and start this tool
with `--cobs` or `DCO_SERIAL_COBS=1`. Mismatch (firmware RAW + tool `--cobs`, or the reverse)
looks like ignored controls; the tool logs `[link] framing=...` on connect.

The sketch also needs the TinyUSB stack, which it already required for USB MIDI. From the
DCO sketch folder (the parent of `tools/`):

```bash
arduino-cli compile \
  --fqbn rp2040:rp2040:rpipico2:usbstack=tinyusb,flash=4194304_524288 \
  --libraries _build_libs .
```

`usbstack=tinyusb` is required (USB MIDI + CDC). `flash=…_524288` allocates a 512 KB
LittleFS partition for MCU presets and calibration files — without it, board-side save /
dump fails at runtime. See [`docs/PRESET_STORE.md`](../../docs/PRESET_STORE.md) and
[`docs/BUILD_FLAGS.md`](../../docs/BUILD_FLAGS.md).

Without `usbstack=tinyusb` the build fails with
`#error TinyUSB is not selected, please select it in "Tools->Menu->USB Stack"`.

Leave `ENABLE_USB_CONTROL` commented out for production. While it is on, any stray
`a`-`d`, `p`, `q`, `B` or `C` byte typed into a serial terminal is read as a frame header.

## 2. Install

`tkinter` ships with Python. Only `pyserial` is extra:

```bash
sudo pacman -S python-pyserial      # Arch / CachyOS
# or, in a virtualenv:
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## 3. Run

```bash
python3 app.py                      # auto-detects the board (port + model)
python3 app.py --port /dev/ttyACM0  # or name the port explicitly
python3 app.py --model dco4         # force the synth model (see section 0)
python3 app.py --theme light        # dark is the default
python3 app.py --cobs               # match firmware SERIAL_FRAMING_COBS
DCO_SERIAL_COBS=1 python3 app.py    # same via env
```

Pick the port and press **Connect**. The tool **automatically pushes the sound patch**
with **Send all** (same set as presets: no Calibration, Diagnostics, or bench/debug).
Loading / Prev / Next / Init while connected also push that patch only. Use **Send all**
anytime to resync the patch after a board reset.

When connected, patch sliders/combos/checks send live on change (coalesced ~20 ms).
**Calibration**, **Diagnostics**, and **bench** controls are not stored and are sent
**only on manual use** (move a cal control, click a diag/bench button, drag PIO pulse).

### Preset bank (local)

A classic **256-slot** program bar sits under the connection toolbar:

`[ < ]  [ 000 ]  [ name ]  [ > ]  [ Load ]  [ Save ]  [ Save as… ]  [ Init ]  [ Browse… ]  [ File ]`

| Control | Behaviour |
|---------|-----------|
| **Prev / Next** / program number | Select slot **0–255** and load it immediately (empty slots load Init defaults) |
| **Load** | Reload the current slot from disk, discarding unsaved edits |
| **Save** | Store the current UI into the current slot (keeps the name) |
| **Save as…** | Prompt for slot (0–255) and name, then save there and switch to that slot |
| **Init** | Set patch controls to `params.py` defaults (slot number unchanged); mark dirty until Save |
| **Browse…** | Open the preset browser (local slot ops + board sync) |
| **File** | Export/import a single patch or the whole bank as tagged JSON |

A leading `*` beside the name means the UI differs from the last loaded or saved snapshot.
The working bank lives in [`presets/bank.json`](presets/bank.json). Slot 0 ships as **Init**;
the rest are empty until you save into them. Calibration, Diagnostics, and bench controls
are not stored and are not part of Connect / preset / **Send all** — only the sound patch is.

### Preset browser & MCU sync

**Browse…** opens a window with all 256 slots (local name + last-fetched board name).

**Local bank:** Load, Save into, Rename, Copy/Move to, Delete, Export/Import one slot as
`dco3-patch` JSON.

**Board (MCU)** — needs Connect; transfers queue one at a time and report in the log
(`[mcu]…`):

| Action | What it does |
|--------|----------------|
| **Refresh board list** | `PARAM_PRESET_DUMP` −1 → `[pdir]` names |
| **Send slot → board** | Encode local slot → 598-byte record → `'B'`/`'C'` bulk into that MCU slot |
| **Fetch slot ← board** | Dump that MCU slot → decode into the local bank |
| **Recall slot on board** | `PARAM_PRESET_LOAD` (live synth; panel UI unchanged until Fetch / Send all) |
| **Push / Pull all** | Walk occupied slots either way |
| **Save board live state → slot** | `'q'` name + `PARAM_PRESET_SAVE` (board snapshots *its* live state) |

Firmware details and text protocol: [`docs/PRESET_STORE.md`](../../docs/PRESET_STORE.md).

### Patch / bank / cal files

| Format tag | File | Role |
|------------|------|------|
| `dco3-patch` | `*.json` | One preset (name + params + blocks) |
| `dco3-bank` | `*.json` | Full 256-slot bank |
| `dco3-cal` | `*.json` | Amp-comp + PW + manual-offset tables (numbers, not raw hex) |

Codecs live in [`fileformats.py`](fileformats.py). Importing a bank overwrites
`presets/bank.json` after confirmation.

### If the port will not open

- **Permissions.** You need to be in the group that owns `/dev/ttyACM*` — `uucp` on Arch
  and CachyOS, `dialout` on Debian and Ubuntu. Check with `ls -l /dev/ttyACM0`, then
  `sudo usermod -aG uucp $USER` and log out and back in.
- **ModemManager.** It probes new ACM devices on connect and can hold the port for a few
  seconds or grab it outright. If it is the culprit, `systemctl stop ModemManager`, or
  add a udev rule with `ID_MM_DEVICE_IGNORE="1"` for the board's USB IDs.
- **No `/dev/ttyACM*` at all.** The board is not running a TinyUSB build, or it is in
  BOOTSEL mode. Re-flash with the `usbstack=tinyusb` FQBN above.

## 4. Layout

Parameter tabs from the tables in [`params.py`](params.py), plus Diagnostics for
bench-only buttons:

| Tab | Contents |
|-----|----------|
| Oscillators | Pitch/Sync left, Voice & drift right; levels and wave matrix full-width at the bottom |
| Envelopes | Three vertical ADSR time columns side by side; curve params as named combos; routing and rest below |
| Filter | Cutoff, resonance, envelope and LFO amounts, keytrack, distortion Drive/Mix |
| PWM | Pulse width, LFO2 and envelope to PW |
| LFOs | Waveforms, speeds, and the LFO routing depths |
| Calibration | Autotune, manual cal, PIO pulse (debug 160), fake-cal seed, **Calibration backup** (dump/load LittleFS tables ↔ `dco3-cal` file) |
| Character | Master Character amount (ParamId 221) plus diagnostic noise jitters via debug 160 (`0xC8` / `0xCA` / `0xCB`); see [`docs/CHARACTER.md`](../../docs/CHARACTER.md) |
| Diagnostics | PIO topology / period probes and hot-path profiler buttons |

**Calibration backup** (connected board): **Dump board → file…** pulls all five tables
(`[dump]` text) into a `dco3-cal` JSON; **Load file → board…** bulk-pushes present tables
and the firmware reloads them live. Partial files only overwrite tables they contain.

**Manual calibration offsets** (params 151–153, 156): the board only ever persists these
to flash when you press **Store manual cal offsets** — moving the stage slider or leaving
manual mode does not save. To avoid the offset slider showing a stale/wrong value, enabling
**Manual calibration mode** recalls the real stored offsets from the board (reusing the same
dump machinery as Calibration backup), and switching the stage slider shows that oscillator's
correct cached value. A status line under the buttons flags any oscillator with unsaved
edits, and pressing **Run autotune** while edits are unsaved asks first, since autotune
reloads the filesystem when it finishes and would otherwise silently discard them.

Anything the board prints — topology report, profiler tables, `[dump]`/`[pdir]`/`[preset]`/
`[bulk]` lines, `DCO_DEBUG_REPORT`, autotune progress — lands in the **Board output** pane.
Drag the sash up for long dumps. Tool-authored lines are prefixed `[link]`, `[send]`,
`[ui]` or `[mcu]` and tinted.

### Appearance

Dark by default. The **Light** / **Dark** button in the toolbar switches in place, and
`--theme light` starts that way; the choice is not remembered between runs, so use a shell
alias if you always want light.

The colours are painted onto ttk's built-in `clam` theme, which means **no extra package is
needed**. If [`sv-ttk`](https://pypi.org/project/sv-ttk/) happens to be installed it is used
instead for a more modern look and real toggle switches on the boolean parameters:

```bash
pip install sv-ttk        # entirely optional, detected automatically
```

Everything lives in [`theme.py`](theme.py). Note that ttk styling only reaches ttk widgets,
so that module also recolours the plain-Tk parts by hand — each tab's scroll canvas, the log
pane, the tooltips and the combobox dropdown — reading the colours back out of the live
style so the same code works under either backend.

## 5. Testing the PIO sync work

The **Oscillators** tab (Sync section) is the reason this tool exists. `PARAM_SOFT_SYNC` (36)
(0 = hard, 1..3 = soft-sync thresholds) and `PARAM_SUBOSC_DIVIDE` (37) have no Input-board UI
at all, so the panel cannot reach them; this is the only way to exercise them. See
[`DCO/docs/PIO_OSCILLATORS.md`](../../docs/PIO_OSCILLATORS.md) for what they do.

On the **Diagnostics** tab, the three PIO buttons call the bench helpers documented in
section 12 of that file, which nothing else in the firmware invokes:

- **PIO topology report** verifies every oscillator reset pin still reads back as PIO0.
  Expect the summary line `reset pin ownership: OK (all PIO0)`. Anything else means a pin
  has been stolen and hard sync cannot work, whatever it sounds like. This also doubles as
  an end-to-end link test: press it, see text come back.
- **Period probe** parks OSC1 at a fixed divider and prints the predicted period, to
  compare against a frequency counter on the reset pin. Run both probes and feed the two
  readings to `pio_solve_period_model()` to confirm the weight and overhead constants.
  **Only works with no note playing** — `voice_task_main()` pushes a fresh divider every frame
  for a held note, which immediately overwrites what the probe set.
- **Dump RAM (heap/stack)** (`PARAM_DEBUG_COMMAND` 13) prints heap total/used/free and
  remaining stack on both cores. Needs `ENABLE_MEM_DIAG` (default on). **Mem diag polls
  off/on** (14/15) stop the per-loop poll so period-only profiler dumps match older
  benches without a rebuild; dump 13 is ignored while off. Comment out `ENABLE_MEM_DIAG`
  for a hard zero-cost match. See [`DCO/docs/MEMORY.md`](../../docs/MEMORY.md).

On the same tab, the **Hot-path profiler** buttons drive `PARAM_DEBUG_COMMAND` values
10 / 11 / 12. They only do anything when the firmware is built with `RUNNING_AVERAGE`; see
[`DCO/docs/BENCHMARKING.md`](../../docs/BENCHMARKING.md).

- **Dump profiler once** asks both cores for a snapshot; core 0 prints the budget table
  into the Board output pane a moment later.
- **Reset profiler** clears every accumulator.
- **Toggle ~1 Hz dump** turns the automatic report on or off; the board immediately prints
  `bench periodic on` or `off`.

Two controls on the Oscillators tab (Sync section) are easy to mistake for each other, because they are different
parameters doing different jobs:

- **Hard sync topology** (31) routes one oscillator's sideset onto another's reset pin, so
  the slave is forced to the master's period for the whole note.
- **Osc sync / phase align OSC2** (17) decides what happens at *note-on*. **Off** leaves the
  oscillators running through the note, so their phase relationship is whatever it happens to
  be. **Sync at note-on** stops OSC1 and OSC2 and restarts them together on one cycle. The
  degree entries do the same restart and delay OSC2's first flyback (one-shot X countdown to
  `loop_final`; later cycles keep a normal pulse). Needs EXACT_Y retrig (default). Note that
  free running also skips the exact-period rewrite, so it tunes very slightly differently —
  see section 8 of [`PIO_OSCILLATORS.md`](../../docs/PIO_OSCILLATORS.md).

For the hard-sync listening check: set **Hard sync topology** to 1 or 2, leave **Soft sync**
off, hold a note, and sweep the slave's detune. A timbral formant sweep means sync is working.
If the pitch simply tracks with no change in character, the slave is being cloned rather
than synced.

To hear the note-on sync on its own, leave the topology at 0, set **Osc sync / phase align
OSC2** to **Sync at note-on**, and retrigger the same note repeatedly: the attack transient
becomes identical every time, where at **Off** it varies from note to note.

## 6. The same controls over MIDI CC

This tool talks to the board over USB serial, which needs `ENABLE_USB_CONTROL` and a cable
to this machine. For everything else — a DAW, a tablet, a hardware controller — the same
control surface is also mapped onto 7-bit MIDI CC, listed in
[`DCO/docs/MIDI_CC_MAP.md`](../../docs/MIDI_CC_MAP.md).

`gen_midi_map.py` generates that chart, the firmware's `midi_cc_map.h` and a ready-made
[Open Stage Control](https://openstagecontrol.ammd.net/) session from the same `params.py`
tables the GUI is built from, so the four cannot drift apart:

```bash
python3 gen_midi_map.py            # rewrite the three generated files
python3 gen_midi_map.py --check    # fail if they are out of date
```

To use the panel, install `open-stage-control` (AUR, a `.deb`, or the release zip) and
point it at the board's MIDI port:

```bash
open-stage-control --theme nord \
                   --midi "dco3:DCO3-MONO,DCO3-MONO" \
                   --load ../panels/dco3_panel.json
```

The `dco3` name in that string is what the session's widgets target, so keep it. `--theme`
is optional; `nord` and `orange` are the two built in.

One tab per group, one knob per controller, grouped under the block they belong to. The
number under each knob is the value the DCO will actually hold — `0..4095` for cutoff, the
exp-mapped `0..25000` for envelope times — not the controller number, computed by the same
formula as the chart. Double-tapping a knob returns it to its `params.py` default, which is
also where every knob starts.

As with this tool, the board boots with its own defaults and never reports back, so push
the panel's state once after connecting: **State -> Send All** in the client menu.

Autotune and "store manual cal offsets" are deliberately left off the CC map, since one is
a minute-long takeover of the board and the other writes the filesystem. Those stay here.

## 7. Files

| File | Role |
|------|------|
| [`protocol.py`](protocol.py) | Frame builders (`'a'`–`'d'`, `'p'`, `'q'`, `'B'`, `'C'`), lin-to-exp, port detection |
| [`params.py`](params.py) | The whole control surface as data; edit this to add a control |
| [`presets.py`](presets.py) | Local 256-slot bank load/save, capture/apply of patch parameters |
| [`presets/bank.json`](presets/bank.json) | Working program bank (slot 0 = Init); edit via the UI |
| [`fileformats.py`](fileformats.py) | `dco3-patch` / `dco3-bank` / `dco3-cal` JSON + MCU record/cal codecs |
| [`mcu_link.py`](mcu_link.py) | Queued dump/bulk/save/recall; parses `[dump]`/`[pdir]`/`[preset]`/`[bulk]` |
| [`app.py`](app.py) | tkinter UI, preset browser, MCU sync, calibration backup |
| [`theme.py`](theme.py) | Dark and light palettes, ttk styling, plain-Tk recolouring |
| [`gen_midi_map.py`](gen_midi_map.py) | Emits the CC map, its chart and the panel session from `params.py` |

### Adding a parameter

Add the ID to [`DCO/params_def.h`](../../params_def.h) and an `apply_param_*` entry to
`paramTable[]` in [`DCO/params.ino`](../../params.ino) as usual, then add one `Param`
row to `params.py`. The UI picks it up with no changes to `app.py`. To give it a CC as
well, put a free controller number in the row's `cc=` field and re-run `gen_midi_map.py`;
it refuses to run on a collision or a reserved controller. Persistable patch params also
need a case/range in `preset_param_is_persistable()` (`preset_store.h`) so they shadow into
MCU presets.

## 8. Protocol notes

Frames match [`DCO/serial_input_protocol.h`](../../serial_input_protocol.h) /
[`DCO/serial_frame.h`](../../serial_frame.h): one command byte, then a fixed little-endian
payload (RAW on the wire today; COBS can wrap the same inner frames later). There is no
finish byte. Full preset/cal dump protocol: [`docs/PRESET_STORE.md`](../../docs/PRESET_STORE.md).

- **`'p'`** — `[id:u8][value:i16 LE]` (4 bytes total). Includes PW 210, EnvVCA→VCA 222, and
  preset/cal commands 170–173.
- **`'a'`/`'b'`/`'c'`/`'d'`** — packed 1 ms ADSR / filter blocks (9 bytes total).
- **`'q'`** — 16 ASCII name chars (before board-side `PARAM_PRESET_SAVE`).
- **`'B'` / `'C'`** — bulk restore chunk (36-byte payload) and CRC32 commit (8-byte payload).
- **`'N'` / `'O'` / `'L'`** — Input↔DCO-only preset directory protocol: `'N'` (1 pad
  byte directory request), `'O'` (17-byte `[slot:u8][name:16]` directory entry, 256 per
  request), `'L'` (1-byte `[slot:u8]` load notice). Not used by `dco_control` itself
  (USB host uses `'p'` PARAM_PRESET_DUMP / `[pdir]` text instead); documented here
  because the wire and ParamId numbering are shared. See
  [`docs/PRESET_STORE.md`](../../docs/PRESET_STORE.md).
- **Envelope attack, decay and release are exp-mapped on the wire** (0..25000), while
  sustain is linear (0..4095). `protocol.lin_to_exp()` replicates
  `linearToExponential(v, 50, 25000)` from the Input board so a slider here feels like the
  physical fader. The CC layer and the MCU preset record use the same exp domain for A/D/R.

Board → host answers for dump/sync are **plain text** on CDC (`[dump]`, `[pdir]`, …), not
framed. `mcu_link.py` parses them out of the same RX stream the log pane shows.

Input and Screen use the same slim inner frames for panel traffic (LE, no finish). Flash
all three together. This tool talks to the DCO over USB CDC; `--cobs` must match
`SERIAL_FRAMING_COBS` on the MCU.
