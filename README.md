# PiEEG Clicker

A hands-free presentation clicker for the PiEEG, the 8-channel ADS1299 EEG shield for the Raspberry Pi. Blink twice to go to the next slide and three times to go back; an optional jaw clench can trigger a third action. Everything is detected on the Pi from two forehead electrodes. The Pi sends the key presses over Wi-Fi to a small receiver on the presenting laptop, which presses PgDn and PgUp by default, like commercial clickers.

```
electrodes -> PiEEG + Raspberry Pi (battery) -- Wi-Fi (UDP) --> receiver on the laptop -> PgDn / PgUp -> slides
```

Read [Safety](#safety) before you put electrodes on.

## Which signal? Blinks and the alternatives

Short answer: blinks are the right starting point, and a jaw clench is the only serious alternative. Both are built in.

**Blinks.** A blink is the largest, most reliable signal at the forehead (Fp1/Fp2): roughly 100-400 µV, against about 10-50 µV for the EEG itself. Detecting it takes only two electrodes plus the REF and BIAS ear clips.

**A single blink cannot be the trigger.** People blink spontaneously about 15-20 times a minute, and more while talking. So the clicker looks for deliberate patterns: a double blink means next, a triple blink means previous. Calibration measures your natural blinks and your deliberate (firmer) ones and puts the threshold between them.

**Jaw clench.** Muscle activity (EMG, 20-100 Hz) is even bigger and more distinct than a blink, invisible to the audience, and you keep eye contact. The catch: talking and chewing also produce jaw EMG, so a clench must be held for 0.4 s and calibration sets the threshold above your speaking level. It is an optional gesture: map it to `next` or `blank` (see [Configuration](#configuration)).

**Not better for this job:**

- Eye movements: you constantly look around and down at your notes. The blink detector even rejects look-up steps on purpose.
- Alpha waves: they need your eyes closed for 1-2 s.
- SSVEP and P300: they need flickering or flashing stimuli.
- Motor imagery: slow, needs training, error-prone.

| Signal | Size at the electrodes | Accidental triggers while presenting | Visible to audience | Verdict |
|---|---|---|---|---|
| Single blink | 100-400 µV | Constant (15-20 a minute) | Yes | Unusable alone |
| Double / triple blink | 100-400 µV per blink | Rare once calibrated | Barely | Default |
| Jaw clench (EMG) | Large, broadband 20-100 Hz | Talking and chewing; handled by the 0.4 s hold and calibration | No | Best alternative, optional |
| Eye movements | Comparable to blinks | Constant (notes, audience) | Yes | Rejected on purpose |
| Alpha waves | 10-50 µV | Low, but eyes must be closed | Yes | Impractical |
| SSVEP / P300 | A few µV | Low | Needs a flashing display | Impractical |
| Motor imagery | A few µV | Error-prone | No | Too slow, needs training |

## Safety

> [!CAUTION]
> The PiEEG is not a medical device and has no patient isolation circuitry. Power the Pi and PiEEG **only from a 5 V battery or power bank.** PiEEG's documentation: "The device MUST not be connected to any kind of mains power, via USB or otherwise."

That includes a wall charger, a laptop's USB port, and an HDMI cable to a mains-powered monitor or projector while the electrodes are on. PiEEG's README says to use "only a monitor that is powered by the Raspberry Pi".

This is why the default output is Wi-Fi (UDP) to the laptop: there is no cable between the laptop and the Pi, so the laptop can stay plugged in. The `uinput` output, where the Pi shows the slides itself, is only safe with a display powered by the Pi or its battery. Do the software setup with the electrodes off. PiEEG's documentation includes a liability notice: use it at your own risk.

## Hardware setup

You need a PiEEG-8 on a Raspberry Pi 4 or 5, a 5 V power bank, two forehead electrodes and two ear clips, and a Wi-Fi network that the Pi and the laptop share (a phone hotspot works). Seat the shield on the Pi's 40-pin header, then connect the electrodes following PiEEG's standard montage:

| PiEEG input | Electrode | Used for |
|---|---|---|
| CH1 | Fp1: forehead, just above the left eyebrow | Blinks, and jaw clench by default |
| CH2 | Fp2: forehead, just above the right eyebrow | Blinks, and jaw clench by default |
| REF | Ear clip, left earlobe | Reference |
| BIAS | Ear clip, right earlobe | Noise cancellation |
| CH3-CH8 | Optional | For example the temples, for a stronger clench signal |

PiEEG's docs only say REF on one ear and BIAS on the other; which side is which is a convention here.

Blinks show up as positive peaks with this montage. If yours are reversed, `calibrate` detects that and sets `blink.polarity` for you. For a stronger jaw-clench signal, add electrodes over the temples (the temporalis muscle) and list their input numbers in `clench.channels`; by default the clench detector reuses inputs 1 and 2.

## Software setup on the Pi

Use Raspberry Pi OS Trixie (Debian 13, Python 3.13, the current release) or Bookworm (Python 3.11). Both work.

```bash
sudo raspi-config nonint do_spi 0     # enable SPI, then reboot
sudo apt install python3-numpy python3-scipy python3-spidev python3-libgpiod git
git clone https://github.com/AbrahamPadua/PIEEG-clicker.git
cd PIEEG-clicker
```

Everything runs from the checkout with `python3 -m pieeg_clicker ...`; nothing needs installing with pip.

- **gpiod.** `python3-libgpiod` is the libgpiod 2.x API on Trixie and the 1.x API on Bookworm. The driver supports both, and also the old PyPI `gpiod` 1.5.x that PiEEG's quick-start installs (`pip install gpiod==1.5.4`), so the apt route above is the recommended one. PyPI `gpiod` 2.x is not needed and has no 32-bit ARM wheel, so it would have to be compiled.
- **DRDY line.** The driver finds the ADS1299 data-ready line (GPIO26) by its name on both the Pi 4 and the Pi 5. On the Pi 5 the header's GPIO chip number has changed between kernel releases (gpiochip4 on early kernels, gpiochip0 later), which is why it does not rely on a chip number. Set `hardware.gpiochip` (for example `"/dev/gpiochip0"`) only if that lookup fails.
- **Permissions.** The default Pi user is already in the `spi`, `gpio` and `input` groups, so the clicker runs without `sudo`.
- **Optional `pieeg-clicker` command.** Inside a virtual environment that can see the apt packages: `python3 -m venv --system-site-packages .venv`, then `. .venv/bin/activate` and `pip install -e .`.
- **Optional: Pi-local `uinput` output** (the Pi types the keys itself; see [Safety](#safety) first):

```bash
sudo apt install python3-evdev
sudo cp extras/99-pieeg-uinput.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo modprobe uinput        # at every boot: echo uinput | sudo tee /etc/modules-load.d/uinput.conf
```

Being in the `input` group matters as well as the udev rule: python-evdev also opens the new `/dev/input/eventN` device, not just `/dev/uinput`. Then run with `--output uinput`.

## Laptop setup

Copy `receiver/pieeg_receiver.py` to the laptop (Windows, macOS or Linux, Python 3.9 or newer). It is a single file. PowerPoint, Keynote, Google Slides, LibreOffice Impress and PDF viewers all accept the PgDn/PgUp keys it presses.

```bash
pip install pynput
python pieeg_receiver.py --token SOME-SECRET        # python3 on macOS and Linux
```

It prints the IP address to use on the Pi, then one line per received click:

```
PiEEG receiver listening on UDP 0.0.0.0:5005 (pynput backend)
Start the clicker on the Pi with --host 192.168.1.23
Firewall: allow inbound UDP port 5005.
Press Ctrl+C to stop.
19:33:18 192.168.1.50    next      -> page_down
```

- **macOS:** give your terminal Accessibility access (System Settings > Privacy & Security > Accessibility). Without it, key presses fail silently.
- **Windows:** allow the firewall prompt on private networks. Windows cannot inject keys into an app running as Administrator unless the receiver also runs as Administrator.
- **Linux, X11:** pynput works (it needs an X server). `pip install pynput` compiles python-evdev, which needs gcc and python3-dev; `sudo apt install python3-pynput` avoids that.
- **Linux, Wayland:** pynput cannot reach native Wayland apps. Use `--backend uinput`, which needs python-evdev and access to `/dev/uinput` (see `extras/99-pieeg-uinput.rules`; you must also be in the `input` group). With the default `--backend auto`, the receiver tries uinput first on Wayland and falls back to pynput with a warning.

Options: `--token TEXT` (only accept messages with this shared secret; set one, or anyone on the network can press keys), `--port N` (default 5005), `--bind ADDR` (default `0.0.0.0`, which also receives broadcasts), `--backend auto|pynput|uinput`, `--dry-run` (print what would be pressed, press nothing; handy for testing the network path) and `--map ACTION=KEY` (press KEY for ACTION whatever the Pi sends, for example `--map next=right`; repeatable).

## Usage

Put the electrodes on, switch the Pi on (from its battery), and work through three steps. Steps 1 and 2 are needed the first time, and again after the electrodes move a lot.

### 1. Check the electrodes: `monitor`

```bash
python3 -m pieeg_clicker monitor
```

`monitor` sends no keys. Every 2 s it prints the RMS of each channel (1-40 Hz). With good contact and a still face, expect roughly tens of µV or less; very large values, or `RAIL` (the input is saturated), mean poor contact, so re-seat the electrode or ear clip. It also shows live blink and jaw (EMG) levels as bars, with the detection threshold marked `|`, and each blink or gesture it recognises. Blink firmly and watch the blink bar cross the `|`. Simulated example (on a terminal the bar line updates in place):

```
signal RMS 1-40 Hz (µV): ch1  14.1  ch2   7.8  ch3   8.6  ch4   9.4  ch5   7.1  ch6   8.7  ch7   7.7  ch8   8.6
blink   102/  80 µV [##########|#--------]   EMG   3.1/25.0 µV [#---------|---------]
    6.36s  blink 304 µV, 388 ms
    7.20s  double blink -> NEXT (page_down)
```

### 2. Calibrate: `calibrate`

```bash
python3 -m pieeg_clicker calibrate
```

About 1.5 minutes, guided by prompts on screen: 15 s of relaxed natural blinking, 15 s of talking as if presenting, 6 prompted firm blinks, then 4 prompted jaw clenches (`--skip-clench` leaves that part out). It writes the thresholds, and the blink polarity if your electrodes are reversed, to the config file (default `~/.config/pieeg-clicker/config.json`, or the file given with `-c`). If it finds fewer than 3 of the 6 prompted blinks it stops without saving. Simulated example:

```
Deliberate blinks: median 245 µV (smallest 204), found 6/6.
Natural blinks: median 74 µV, 0/5 above the new threshold.
Blink threshold set to 147 µV.
Jaw clench: 38 µV vs talking 9 µV. Threshold set to 19 µV.
```

### 3. Run: `run`

```bash
python3 -m pieeg_clicker run --host <laptop-ip> --token SOME-SECRET
```

Start the receiver on the laptop first, and give the clicker about 3 s to settle after it starts. Stop it with Ctrl+C. You can store the address and token in the config file (`output.host`, `output.token`) so that `run` needs no flags. Add `-v` to see every blink and why a candidate was rejected.

### Gestures

| Gesture | Default action | Key | How |
|---|---|---|---|
| Double blink | `next` | PgDn | Two firm blinks, each within 0.7 s of the previous one |
| Triple blink | `previous` | PgUp | Three firm blinks, each within 0.7 s of the previous one |
| Jaw clench | none | none | Clench and hold for about 0.4 s; map it to `next`, `previous` or `blank` |

A single blink never does anything. Because a triple blink is also mapped, the clicker has to see whether a third blink follows, so a double blink is confirmed 0.7 s after the second blink starts, roughly half a second after you finish it. A triple blink fires immediately on the third blink. If you set `mapping.triple_blink` to `null`, the double blink fires at once. After every action there is a 1 s cooldown during which gestures are ignored.

## Configuration

Settings live in a JSON file: `~/.config/pieeg-clicker/config.json` (or under `$XDG_CONFIG_HOME` if set), or whatever you pass with `-c`. `python3 -m pieeg_clicker init-config` writes all defaults to it (`--force` overwrites an existing file); `config.example.json` is the same content. The file is merged over the defaults, so keep only the keys you change. A missing file means defaults, unknown keys are ignored with a warning, and invalid values stop the program with an error that names the field. `calibrate` rewrites the whole file with the new thresholds.

```json
{
  "output": { "host": "192.168.1.23", "token": "SOME-SECRET" },
  "mapping": { "clench": "blank" }
}
```

| Field | Default | Meaning |
|---|---|---|
| `mapping` | `double_blink: "next"`, `triple_blink: "previous"`, `clench: null` | Gesture to action. Actions: `next`, `previous`, `blank`; `null` turns a gesture off |
| `keys` | `next: "page_down"`, `previous: "page_up"`, `blank: "b"` | Key sent for each action (`b` blanks the screen in most presentation software) |
| `output.mode` | `"udp"` | `udp` sends to the laptop receiver, `uinput` types on the Pi itself, `console` only prints (testing) |
| `output.host` | `"255.255.255.255"` | Laptop IP address. The broadcast default needs no setup, but the laptop's own IP is more reliable |
| `output.port` | `5005` | UDP port; must match the receiver's `--port` |
| `output.token` | `""` | Shared secret; must match the receiver's `--token` |
| `blink.channels` | `[1, 2]` | PiEEG inputs (1-8) wired to Fp1/Fp2 |
| `blink.threshold_uv` | `null` | Set by `calibrate`. Until then 80 µV is used; it never drops below 6 times the measured noise. Raise it for fewer false blinks |
| `blink.polarity` | `1` | `1` if blinks are positive peaks, `-1` if reversed; set by `calibrate` |
| `clench.channels` | `[1, 2]` | Inputs used for the jaw EMG, for example temple electrodes |
| `clench.threshold_uv` | `null` | Set by `calibrate`. Until then 25 µV is used; it never drops below 5 times the resting EMG level |
| `clench.notch_hz` | `[50.0, 60.0]` | Mains frequencies (and harmonics) removed from the EMG; the default covers 50 and 60 Hz |
| `gestures.max_gap_s` | `0.7` | Longest pause between the blinks of one pattern |
| `gestures.cooldown_s` | `1.0` | Dead time after every action |
| `hardware.spi_speed_hz` | `1000000` | SPI clock; lower it (for example 600000) if you see glitched frames |
| `hardware.drdy_gpio` | `26` | BCM number of the ADS1299 DRDY line (header pin 37) |
| `hardware.gpiochip` | `null` | GPIO chip holding that line; `null` finds it by the name `GPIO26`. Set it (for example `"/dev/gpiochip0"`) only if the lookup fails |

Key names: letters a-z, digits 0-9, `f1`-`f12`, `space`, `enter`, `escape`, `tab`, `backspace`, `left`, `right`, `up`, `down`, `home`, `end`, `page_up`, `page_down`, `period`, `comma` (aliases `esc`, `pgup`, `pgdn`). The remaining fields (filter corners, duration limits, `min_gap_s`, sample rate, gain) are listed in `config.example.json`; the defaults are sensible.

## Try it without hardware

`run`, `monitor` and `calibrate` all accept `--simulate`, which replaces the PiEEG with synthetic EEG: natural blinks and glances down at notes, plus a scripted double blink, triple blink and jaw clench every 6 s in turn. During `calibrate --simulate` the fake user also talks, blinks and clenches when prompted.

```bash
python3 -m pieeg_clicker run --simulate --output console
```

```
19:26:44 INFO    Gestures: double_blink -> next, triple_blink -> previous. Output: console
19:26:52 INFO    double blink -> NEXT (page_down)
19:26:52 INFO    ACTION next -> key page_down
19:26:58 INFO    triple blink -> PREVIOUS (page_up)
19:26:58 INFO    ACTION previous -> key page_up
```

The clench is silent because it is unmapped by default (`-v` shows it). To test the Wi-Fi path without electrodes, run `python3 -m pieeg_clicker run --simulate --host <laptop-ip> --token SOME-SECRET` on the Pi with the receiver running on the laptop.

Tests: `python3 -m pytest` from the repository root (needs pytest: `sudo apt install python3-pytest` or `pip install pytest`). Two quick synthetic end-to-end checks: the demo gestures give the right actions while natural blinking, glances and talking give none, and calibration separates natural from deliberate blinks and detects reversed polarity.

## How it works

```
PiEEG: ADS1299 at 250 SPS, read over SPI (DRDY falling edge = new sample)
  |
  +--> blink path:  mean of blink.channels -> 10 Hz low-pass -> peak/valley detector
  |                 -> pattern grouping (double / triple blink)
  |
  +--> clench path: 20-100 Hz band-pass + 50/60 Hz notches -> RMS envelope
  |                 -> must stay above the threshold for 0.4 s
  v
gesture -> action (next / previous / blank) -> UDP JSON message, sent twice
  |
  |  Wi-Fi
  v
laptop receiver: check token, drop the duplicate -> key press
```

- **Blink detector.** A blink must rise above the threshold and fall back by half within 0.6 s. A gaze shift is a step that never falls back, so looking up from your notes is ignored. Candidates shorter than 50 ms or larger than 2000 µV (an electrode pop) are rejected too, and so are blinks during a clench when the clench gesture is mapped (`clench.gate_blinks`).
- **Pattern grouping.** Blinks up to 0.7 s apart form one pattern (blinks closer than 0.2 s count as one). A blink smaller than half of the biggest one in the pattern is ignored, so a small spontaneous blink next to two deliberate ones does not turn a double blink into a triple.
- **Clench detector.** The RMS of the 20-100 Hz band, with mains notched out, must stay above the threshold for 0.4 s.
- **Thresholds.** A calibrated threshold is a floor. When the signal gets noisier, the detectors raise it to 6 times the measured noise (blink) or 5 times the resting EMG level (clench).
- **Driver.** Frames without the ADS1299 status header are dropped, and a jump of more than 2500 µV between samples is treated as an SPI glitch.
- **Message.** Each action is one JSON datagram, sent twice as cheap insurance against Wi-Fi packet loss. The receiver drops the copy using `id` and `seq` and ignores messages with the wrong token. The token travels unencrypted, so it guards against accidents and pranks, not attackers.

```json
{"app": "pieeg-clicker", "v": 1, "id": "3fa91c2e", "seq": 7, "action": "next", "key": "page_down", "token": "SOME-SECRET"}
```

- **Units.** Values are in µV using the ADS1299 datasheet scale (0.536 µV per LSB at gain 1). PiEEG's example scripts use half that value, so numbers are not directly comparable. Calibration makes the difference irrelevant.

## Autostart on boot

`extras/pieeg-clicker.service` is a systemd unit that starts the clicker at boot, once the network is up, and restarts it 3 s after it exits with an error (for example if the PiEEG is not answering yet).

1. In the file, replace `YOUR_USER` with your user name (in `User=`, `WorkingDirectory=` and `ExecStart=`), and change `/home/YOUR_USER/PIEEG-clicker` if you cloned elsewhere.
2. The unit runs `python3 -m pieeg_clicker run -c /home/YOUR_USER/.config/pieeg-clicker/config.json` with no other flags, so put `output.host` and `output.token` in that config file. Run `calibrate` as the same user first, which also creates the file.
3. Install and start it:

```bash
sudo cp extras/pieeg-clicker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pieeg-clicker
journalctl -u pieeg-clicker -f        # follow the log
```

## Troubleshooting

| Symptom | What to do |
|---|---|
| `No answer from the ADS1299 (ID register = 0x00/0xFF)` | SPI is not enabled (`sudo raspi-config nonint do_spi 0`, then reboot), the board is not fully seated on the 40-pin header, or the battery is off. |
| `No data from the PiEEG for 1 s` | The DRDY line is not toggling. Check the board's power, `hardware.drdy_gpio` (26) and, if the automatic lookup failed, `hardware.gpiochip`. |
| A message about lost, malformed or glitched frames when you stop the program | SPI glitches (a few are harmless). Add `core_freq_fixed=1` to `/boot/firmware/config.txt` and reboot (on a Pi 4 the SPI clock follows the core clock), or lower `hardware.spi_speed_hz`, for example to 600000, the rate PiEEG's own scripts use. |
| Clicks you did not intend | Recalibrate and blink more firmly on purpose, which widens the gap to your natural blinks. Raise `blink.threshold_uv`. Or set `mapping.triple_blink` to `null`, so a double blink fires at once and can never turn into a "previous". `-v` shows every detected blink. |
| Missed clicks | Recalibrate, check contact with `monitor`, and blink a bit faster (both blinks within 0.7 s) and more firmly. |
| Calibration: `Only N/6 prompted blinks were found` | The forehead electrodes are not touching the skin, or `blink.channels` does not match your wiring. Check with `monitor` and retry. |
| Calibration: a jaw clench "barely exceeds talking" | Clench harder, add temple electrodes (`clench.channels`), or leave `clench` unmapped. |
| The receiver gets nothing | Pi and laptop must be on the same network, the laptop's firewall must allow inbound UDP 5005, and the token must match (the receiver prints "wrong or missing token"). Venue Wi-Fi may isolate clients from each other: use a phone hotspot. Put the laptop's IP in `--host` instead of relying on broadcast. To test the path without electrodes, use `run --simulate` (see [Try it without hardware](#try-it-without-hardware)). |
| The receiver prints the key but the slides do not move | The slideshow window must have focus. Also see the platform notes above: macOS Accessibility permission, a slideshow running as Administrator on Windows, `--backend uinput` on Wayland. |
| `cannot create the uinput virtual keyboard` (Pi) or `cannot create the uinput device` (receiver) | Load the module (`sudo modprobe uinput`), install `extras/99-pieeg-uinput.rules` and join the `input` group, then log out and in again. |
| Wi-Fi and EEG noise | PiEEG's docs recommend disabling Wi-Fi for clean EEG. Blink and clench signals are large enough that it does not matter here. |

## Project layout

```
pieeg_clicker/                the package
  __main__.py                 python3 -m pieeg_clicker entry point
  cli.py                      commands: run, monitor, calibrate, init-config
  config.py                   settings, defaults, JSON load/save, validation
  pieeg.py                    ADS1299 driver (spidev + libgpiod)
  dsp.py                      streaming filters and robust noise statistics
  detectors.py                blink and clench detectors, blink patterns, gesture engine
  calibrate.py                guided calibration
  outputs.py                  console, UDP and uinput outputs
  simulate.py                 synthetic EEG for --simulate and the tests
receiver/pieeg_receiver.py    standalone laptop receiver (UDP to key presses)
extras/pieeg-clicker.service  systemd unit for autostart
extras/99-pieeg-uinput.rules  udev rule for the uinput output
config.example.json           the default configuration
tests/test_detection.py       synthetic end-to-end tests
pyproject.toml                packaging (optional): numpy, scipy
```

## Notes

- **Why not BrainFlow?** BrainFlow has a PiEEG board (`PIEEG_BOARD`), but it only works when BrainFlow is compiled from source on the Pi with the periphery option (`--build-periphery`). This project talks to the ADS1299 directly instead, with the same register setup as PiEEG's official scripts, so the Pi only needs apt packages.
- PiEEG project and documentation: <https://github.com/pieeg-club/PiEEG>
- TI ADS1299 datasheet (SBAS499): <https://www.ti.com/product/ADS1299>
