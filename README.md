# Audiosync

Sample-locked audio between the machines on your LAN. One machine hosts a
Snapcast stream captured from PipeWire/PulseAudio; the others join it as
Snapcast clients. The desktop window is a remote control for the systemd user
units and the latency controller. No cloud service or hosted backend is
involved.

## What it does

- Discovers Audiosync peers with mDNS and Snapcast servers with Snapcast's own
  mDNS record; manual IPs cover networks where multicast does not cross.
- Hosts a 48 kHz, 16-bit, stereo stream through Snapcast.
- Uses an adaptive buffer by default: Auto starts at 250 ms and converges
  toward the codec/link floor. Auto has a 1000 ms upper bound so it can recover
  from network dips. Deep pins 60 s; Manual accepts 40–5000 ms.
- Optionally measures the speakers with physical microphones and applies a
  per-client Snapcast output-latency offset. The acoustic monitor also detects
  missing/irregular probes as skipping and raises the shared Auto buffer.
- Preloads a local media file to every selected device, verifies its SHA-256,
  prerolls it, and starts all players at one shared wall-clock instant.
- Optionally reuses a folder already synced from an SMB/share library instead
  of transferring the same file again.
- Provides the matching VLC/Kodi playback offset for the current buffer.

## Requirements

### Runtime

- Fedora 44 or another recent Linux distribution with systemd user services.
- Python **3.11 or newer** (the development environment currently uses 3.14).
- PipeWire or PulseAudio, plus `pactl` and either `parec` (usually
  `pulseaudio-utils`) or `pw-cat` (PipeWire).
- For acoustic sync: one physical microphone near each speaker, `parec` or
  `pw-record` on every measuring device, and `paplay` or `pw-play` on the host.
  Remote microphone measurement also needs keyless SSH and Python 3 on that
  device. NTP-synchronized clocks are required.
- `snapserver` and `snapclient`.
- Python packages `PySide6` and `zeroconf` (declared in `pyproject.toml`).
- For preload playback on each device: Python GObject/GStreamer (`python3-gobject`
  and the GStreamer `playbin`/`pulsesink` plugins), `ssh`, `scp`, and
  optionally `ffprobe` for video detection. Folder/library sync additionally
  needs `rsync`.
- For the optional SMB media mount: `cifs-utils` (`mount.cifs`).

Snapcast is not in Fedora's main repositories on the tested release. The
`install.sh` dependency check prints the COPR repository and package command
used by this project; the script does not run `dnf` or install packages itself.

### System files

The user installer writes only under `$HOME`:

- `~/.config/audiosync/` — config, generated snapserver/capture/client env.
- `~/.config/systemd/user/` — three user units.
- `~/.config/pipewire/pipewire.conf.d/10-audiosync-sink.conf`.
- `~/.local/bin/audiosync-gui` and `audiosync-snapclient`.
- `~/.local/share/applications/audiosync.desktop`.

The optional SMB mount is separate system state, documented below. The app's
own `config.py` does not create or modify `/etc`.

## Install

### Distro wrapper

From the repository:

```sh
./install.sh
```

This checks the runtime tools, detects the hardware output and recorder,
installs the user units/launchers/desktop file, and writes the detected
hardware settings to the user config. It does not use `sudo` for the user
installation; SMB removal on uninstall uses `sudo` only after confirmation.
Launch **Audiosync** from the application menu or run:

```sh
audiosync-gui
```

The wrapper is idempotent; re-running it refreshes the generated units and
launchers.

### Python package / console entry point

For a development install or a system where you manage Python packages
yourself:

```sh
python3 -m pip install --user -e .
audiosync --version
audiosync
```

The package declares `PySide6` and `zeroconf` as runtime dependencies. It does
not install the external system tools listed above. The console entry point and
`python3 -m audiosync` expose the same CLI:

```text
audiosync [--gui] [--list] [--host] [--join HOST] [--stop] [--status]
          [--buffer MS] [--playback synced|direct|off] [--version]
```

`--smoke` is a hidden headless startup check for manual verification; it is
not a user-facing feature.

## SMB media mount

The library feature works with any mounted folder, but a CIFS/SMB share needs
a system-level fstab entry and credentials file. The supported replacement for
the old one-off desktop script is:

```sh
sudo scripts/setup-smb-mount.sh \
  --server 192.0.2.10 \
  --share storage \
  --owner "$USER"
```

Use `--help` for all options. The default artifacts are:

- `/etc/fstab` — a marked CIFS entry for `/mnt/storage`
- `/etc/samba/credentials-storage` — root-owned, mode `0600`
- `/mnt/storage` — the mountpoint

The script is idempotent: it does not duplicate the fstab entry and keeps an
existing credentials file unless `--force` is supplied. Before editing fstab
it writes `/etc/fstab.audiosync.bak`; before replacing credentials with
`--force` it writes a root-only `.audiosync.bak`. Use `--dry-run` to inspect the
plan without writing or mounting. The password is prompted without echo (or
can be read from stdin with `--password-stdin`).

Example server-side share (the server is separate from the client mount):

```ini
[storage]
   path = /mnt/storage
   browseable = yes
   read only = no
   guest ok = no
   valid users = your-user
```

After mounting, choose **Sync & Library → Sync folder…** and select the folder
inside the mount (for example `/mnt/storage/myMusic`). The app records its
stable share-relative identity and preload can reuse the synced copy.

### Uninstalling and the SMB artifacts

`install.sh --uninstall` removes the user installer's files. It also detects
the CIFS fstab entry, Samba credentials, and mountpoint, prints them, and asks
before removing system files. Safe modes:

```sh
./install.sh --uninstall --dry-run     # report only; no files changed
./install.sh --uninstall --no-smb      # remove only the user installation
./install.sh --uninstall --remove-smb  # explicit consent; still reports first
```

Removal of the mountpoint uses `rmdir` only when it is empty and not mounted.
A non-empty mountpoint is reported and left in place; media data is never
recursively deleted. Removing fstab lines is backed up first. The root-only
credentials backup created by `--force` is removed with the credentials. The
system artifacts are never touched without either an interactive confirmation
or the explicit `--remove-smb` flag.

## Sample rate, buffer, and latency

The capture path is fixed at **48,000 Hz, 16-bit, stereo** (`48000:16:2`).
Snapcast delivers that stream to every client. The control plane is local
JSON-RPC over HTTP.

The default **Auto** buffer starts at 250 ms and converges toward the link /
codec floor (typically 100 ms for PCM, higher for compressed codecs). The
controller steps down after clean playback, raises the buffer on underruns or
disconnects, and can learn a higher floor. **Deep** pins 60 s and is useful for
very poor links or when video sync matters more than latency. **Manual** pins
a value from 40 to 5000 ms and is intended for controlled experiments; it is
not adaptive.

Lower buffers reduce the audio delay but are more sensitive to Wi-Fi jitter.
The UI shows the current buffer and underrun count; treat them as diagnostics,
not as an acoustic latency measurement.

### Acoustic sync

**Sync & Library → Acoustic sync** measures the actual speaker path:

1. The host plays a low-level three-pulse probe into `audiosync_sink`. Every
   Synced client reproduces it.
2. Each device records a short window from a selected physical microphone
   (`parec` or `pw-record`); `.monitor` loopback sources are deliberately
   ignored because they measure the digital path, not the speaker.
3. The host matched-filters the recording, checks that all three pulses arrived
   with the expected spacing, and aligns every device to the earliest valid
   acoustic arrival using `Client.SetLatency`. The reference device receives
   an explicit zero latency; later arrivals receive positive values only.
4. Corrections require repeated measurements, use a small deadband, and slew
   in small steps instead of jumping. The values are persisted by device name
   and reapplied when a client rejoins.

**Calibrate now** runs one round. **Monitor during playback** is opt-in and
repeats the audible probe every 10–300 seconds (default 30 s). It is intended
for testing a room, not for silent background operation. If some devices miss
the probe, they are reported as skipping and the shared Auto buffer is raised;
no per-client latency is changed from that partial round. A capture or SSH
failure likewise leaves the existing per-client values untouched. If every
device misses the probe, the app reports a measurement failure instead of
inventing an audio fault.

The microphone is active only during each short capture. Raw audio stays in
memory on the recording device and is never written to disk or sent over the
network; only onset times, confidence and errors are returned. Use
**Microphones…** to choose a built-in or USB input per device.

This measures output-path/acoustic offset and skip evidence. Snapcast already
corrects clock drift by dropping or duplicating samples; the acoustic loop must
not fight that. A device without a physical microphone near its speaker cannot
be measured and should fall back to group-aware Auto plus manual calibration.

## Video offset workflow

The audio path is delayed by the buffer, so the video player must emit audio
earlier by the same amount (equivalently, show video later):

1. Start hosting or join the host and let the buffer settle.
2. Open **Sync & Library**. The short label shows `Audio offset −N ms`.
3. Use **Copy VLC** to copy the exact command, or **Launch mpv** to open mpv
   with `--audio-delay` already applied. The tooltip contains the VLC command
   as well as Kodi and browser guidance.
4. For VLC, paste the copied command and add the media file:
   `vlc --audio-desync=-N FILE`.
5. Browsers have no audio-offset control. For browser video, use the Auto
   preset and accept its smaller buffer.

The copied VLC command always uses the buffer currently reported by the app.

## Preload and library sync

- **Preload & Play** copies a selected file to every reachable device,
  verifies the complete file with SHA-256, and starts playback after a 10 s
  delay. Remote devices need keyless SSH from this machine.
- **Sync folder…** copies one folder to every device once using `rsync
  --partial`; devices that cannot be reached are reported and skipped rather
  than discarding the copies that succeeded. A synced copy is reused when its
  digest matches.
- **Stop playback** stops Audiosync preload players and restores the
  Snapcast units it took down. Personal VLC/browser audio is left alone.

### Routing safety

Audiosync fails closed when it cannot prove an audio route is safe. A monitor
or virtual source is never accepted as an acoustic microphone, and the SSH
capture helper rechecks the source on the device that records it. `snapclient`
must have an explicit hardware `--soundcard`; an empty or virtual value is
refused rather than falling back to `audiosync_sink`. Direct capture likewise
requires a real hardware sink, and the playback-mode radios are locked while
this device is hosting or joining so a saved setting cannot silently diverge
from the running route. **Preload & Play** is available only from an idle group
and preflights every local/SSH target for an active Snapcast capture path and a
real hardware default sink; its players use `pulsesink` with that sink rather
than `autoaudiosink`. In short, stop the group before changing playback mode or
running preload.

## Tests and lint

Run the commands from the repository root. Install the optional development
tooling first with `python3 -m pip install -e ".[dev]"` (this also installs the
runtime Python dependencies):

```sh
python3 -m unittest discover -s tests -t .
ruff check .
bash -n install.sh scripts/setup-smb-mount.sh
```

The GUI smoke test is headless:

```sh
QT_QPA_PLATFORM=offscreen python3 -m audiosync --smoke
```

The test suite has no network, audio, microphone, or root requirements; SMB and
installer tests use temporary files and fake paths, and acoustic tests use
synthetic PCM.

## Troubleshooting

### No host, no peers, or no audio

- Check the user units:
  `systemctl --user status snapserver.service snapcast-capture.service snapclient.service`
- Read recent logs:
  `journalctl --user -u snapserver.service -n 80 --no-pager`
- Confirm Snapcast's ports are reachable on the LAN: stream `1704`, control
  `1705`, HTTP `1780`.
- `audiosync --status` prints unit state and any visible server clients.
- If mDNS is blocked, use **Add by IP** or `audiosync --join ADDRESS`.
- Check the selected hardware output in the app and in
  `~/.config/audiosync/config.json`.

### “No synced folder” or the SMB folder is missing

- `findmnt -T /mnt/storage` should show the CIFS mount.
- `ls -l /etc/samba/credentials-storage` should show mode `0600` and root
  ownership.
- If the mount is absent, run `sudo mount /mnt/storage` or re-run
  `scripts/setup-smb-mount.sh` with the same server/share. Do not paste
  credentials into a terminal or the chat.
- If the server address or share changed, update the mount rather than editing
  an unrelated fstab line.

### Folder sync or preload says SSH failed

Preload/library transfers use keyless SSH from this machine:

```sh
ssh -o BatchMode=yes DEVICE true
```

`ssh`, `scp`, `rsync`, and (on each device) the Python GStreamer bindings must
be installed. A Tailscale SSH interception or a hostname that only resolves
off-LAN is reported explicitly; use the device's LAN address.

### Acoustic sync says no physical microphone

Only real input sources are accepted; a source ending in `.monitor` or marked
as monitor/virtual by `pactl` is a loopback and is not acoustic. The same check
runs again inside the SSH capture helper on the recording device. Check the
device's default and available inputs:

```sh
pactl list short sources
pactl get-default-source
```

Choose a physical source in **Microphones…**. On a desktop with only line-in,
connect a USB microphone. The app refuses a muted physical microphone rather
than recording silence, so unmute the chosen input before calibrating. Remote
discovery and capture use the same keyless SSH
path as preload; `ssh -o BatchMode=yes DEVICE true` and `python3` on that device
must work. If the clocks are not NTP-synchronized the measured offsets are not
meaningful; run `timedatectl show -p NTPSynchronized` on every device.

The probe is audible by design. Start with a low **probe level**, keep the
monitor off until calibration succeeds, and do not enable it while microphones
would record private conversation.

### Uninstall left system files

That is intentional unless you confirmed removal. Re-run:

```sh
./install.sh --uninstall --dry-run
```

Then remove only what you intend to remove:

```sh
./install.sh --uninstall --remove-smb
```

A mounted or non-empty `/mnt/storage` is never removed automatically.

## Repository layout

```text
audiosync/          Python package and console entry point
tests/              unittest suite (pure helpers; no live audio)
systemd/            user-unit templates
pipewire/           audible sink configuration
scripts/            SMB mount setup
install.sh          distro-level user installer/uninstaller
pyproject.toml      package metadata, dependencies, entry point, ruff config
.github/workflows/  CI gate
```
