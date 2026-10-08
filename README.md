# UFO — a Windows agent that can actually drive your laptop

Give it a goal in plain English and it will look at the screen, work out what
to click, do it, check whether it worked, and tell you what happened.

```powershell
ufo run "open Notepad, type the date, and save it as notes.txt in Documents"
ufo run "summarise the newest file in Downloads into a Notepad window" --dry-run
ufo schedule add "0 9 * * 1-5" --task "clear my inbox"
ufo doctor
```

It is a *whole-laptop* agent: it can see every screen, control the mouse and
keyboard, run shell commands, read and write files, manage processes,
services, the registry, power and volume, and browse the network. It can also
run unattended in the background on a schedule.

## The part that matters: it is fail-closed

An agent that can do all of the above is only useful if it cannot be talked
into doing something you did not ask for. Six independent brakes:

| Brake | What it stops |
| --- | --- |
| **Allowlist** | Anything not explicitly permitted is refused. Empty by default. |
| **Denylist** | Security windows (Credential Manager, Windows Security…) are never touchable, even if allowlisted. |
| **Risk classifier** | Every action is scored low → medium → high → critical. Critical (formatting a disk, writing a password, exfiltrating data) is blocked outright. |
| **Confirmation** | Medium-and-up actions pause and ask, by voice and in the console, unless you are in dry-run. |
| **Budgets** | Hard caps on steps, wall-clock, LLM calls and USD per run. |
| **Loop detector** | The same action repeated stops the run instead of spinning. |

Plus, at any moment:

- **Ctrl+Alt+Q** trips a global emergency stop that nothing can override.
- **Secrets never reach disk or the model.** Anything shaped like a key, token,
  card number or password is redacted before it is logged, traced, stored in
  memory, or sent to a provider.
- **Every step is recorded** to `~/.ufo/runs/<run-id>/` — the prompts, the
  decisions, the actions, the screenshots, and the reason each action was
  allowed or refused.

The default state permits nothing. You widen it deliberately:

```powershell
ufo config allow Notepad
ufo config allow "^Microsoft Outlook"
ufo config set safety.dry_run=true
```

## Install

Requires Windows 10/11 and Python 3.11+.

```powershell
git clone <this repo> ufo && cd ufo
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
ollama pull gemma3:4b
```

`gemma3:4b` is a good default: it reads screenshots, which the agent needs, and
it runs on a laptop CPU. Any vision model works — `qwen2.5vl:7b`, `llava:7b`
and so on. With more than one installed you can pin one:

```powershell
python main.py config set llm.model=qwen2.5vl:7b
```

With none pinned, UFO asks Ollama what it has and picks a vision model itself.

## Run it

```powershell
python main.py
```

That is the whole app. It prints a URL, you open it on your phone on the same
Wi-Fi, hold the button and speak. Every instruction is read back and confirmed
in the terminal before anything happens, and `Ctrl+Alt+Q` stops it from
anywhere.

Before it starts it checks the three things that would make it useless — the
model, the transcription engine, and the allowlist — and if one is not ready it
says which and gives the exact command that fixes it:

```
Cannot start yet. 2 things to fix:

  - phone.transcription is 'local' but faster-whisper is not installed
      install it with:  pip install faster-whisper   (or ...)

  - the allowlist is empty, so no window could be acted on
      add one with:  ufo config allow Notepad
```

All problems are listed at once, so you do not fix one, rerun, and hit the next.

This build talks to **Ollama on this machine and nothing else** — no cloud key is
used or needed, and no other provider is accepted while `llm.offline` is on.

`ufo doctor` checks every subsystem, not just the ones that block startup.

To use a different model for one run:

```powershell
ufo run "what's on my screen?" --model qwen2.5vl:7b
```

### Voice

Voice defaults to being transcribed on the laptop, which needs one extra package:

```powershell
.\.venv\Scripts\python.exe -m pip install faster-whisper
```

No cloud provider is contacted, and `ufo doctor` reports `key not needed` for
the model.

A screenshot of your screen, a window title and a spoken instruction are the
most sensitive things this tool handles, so local is the mode to prefer. The
one remaining exception is the phone page in `transcription: "browser"` mode:
Chrome and Safari hand the microphone recording to their own cloud recogniser.
`local` avoids that — the phone records WAV and the laptop transcribes it.

### How slow is slow

Expect **about a minute per agent step** on a CPU-only laptop — measured at
60–90s per call on 16 cores with `gemma3:4b`. A vision model has to read a full
screenshot on the processor, and there is no GPU in the loop. This is a property
of the hardware, not of the code.

It is worth knowing before you judge it, because it looks like a hang: the
terminal is silent while the model thinks. Watch `ufo logs` in a second window
to see steps progressing, or expect a long pause before the first action.

Three things make it faster:

- **A smaller vision model.** `qwen2.5vl:3b` or `moondream` is roughly half the
  size of `gemma3:4b`. Quality drops on dense screens; speed roughly doubles.
- **Fewer steps.** `automation.max_steps` is the cap, but a precise instruction
  finishes sooner than a vague one — "click Save" beats "finish my form".
- **Deterministic capabilities skip the model entirely.** `ufo do volume 30`
  and the catalog capabilities run without any LLM call, because nothing needs
  a screenshot read to decide.

A GPU changes this by an order of magnitude, and nothing else in the code needs
to change.

### There is no `.env`

This repo has no `.env` and no `.env.example`, and you do not need either. The
only model is Ollama on this machine, and Ollama has no key — so there is
nothing to put in one. `ufo doctor` reports `key not needed`.

If you do keep credentials of your own, they belong in the local vault, which
writes an owner-readable file rather than one in the source tree:

```powershell
ufo secrets set my-token
ufo secrets list
```

A real environment variable always wins over anything, so you can also set one
for a single run without editing any file:

```powershell
$env:MY_TOKEN="other-value"; ufo doctor   # this run only
```

## Everyday use

```powershell
ufo run "..."                      # one task, in the foreground
ufo run "..." --dry-run           # plan only, no clicking
ufo run "..." --allow Notepad     # widen the allowlist for this run only
ufo run "..." --exe notepad.exe --title "Notepad"   # launch and attach
ufo run "..." --mode app          # control-level UIA instead of pixels
ufo run "..." --mode plan         # show the plan, then run it
ufo run "..." --max-steps 60 --max-cost 0.50

ufo windows                       # what is open, and what is allowed
ufo see "Notepad"                 # annotated screenshot + control list
ufo screenshot out.png            # plain capture

ufo queue add "..."               # queue for later
ufo queue list
ufo queue start                   # drain the queue in this process
ufo daemon start                  # background worker + scheduler
ufo daemon status

ufo schedule add "in 20 minutes" --task "..."
ufo schedule add "0 9 * * 1-5" --task "clear my inbox"     # cron
ufo schedule add "every 30 minutes" --task "check the build" # repeat
ufo schedule list

ufo memory remember editor vscode
ufo memory show
ufo secrets list

ufo serve                         # local dashboard on 127.0.0.1:8765
ufo logs                          # recent runs
ufo replay <run-id>               # step-by-step trace
```

Add `--input` to read the task from stdin instead of passing it as an
argument, which avoids quoting entirely:

```powershell
"rename every .txt in Downloads" | ufo run --input
```

## How a task runs

1. **Observe.** Enumerate windows and controls through UI Automation, capture
   every monitor, annotate the controls with numbers, and OCR the text.
2. **Decide.** The model sees the goal, the observation, what worked last
   time, and the hard rules. It returns one JSON action.
3. **Check.** The guard scores the action, verifies the target window is
   allowlisted, confirms if required, and aborts on a budget breach or the
   emergency stop. Only then does anything happen.
4. **Verify.** Compare the screen before and after. If nothing changed, say so
   rather than pretending.
5. **Repeat** until the model finishes, fails, runs out of steps, trips the
   loop detector, or you press the hotkey.

`--mode app` uses the accessibility tree instead of the screen, which is more
reliable for native Windows apps. The default `computer` mode uses pixels plus
the annotated control list, which also works with apps that expose nothing
useful (Electron, games, PDF viewers, terminals).

## Configuration

```powershell
ufo config show                    # everything, as JSON
ufo config init                    # write ~/.ufo/config.json
ufo config set safety.dry_run=true
```

Precedence is **defaults < config file < `UFO_<SECTION>_<FIELD>` env vars <
CLI flags**. See `config.example.json` for every key. Unknown keys are a hard
error, so a typo never silently does nothing.

The keys that matter most:

| Key | Default | Meaning |
| --- | --- | --- |
| `safety.allowlist` | `[]` | What may be touched. Empty permits nothing. |
| `safety.dry_run` | `false` | Plan everything, execute nothing. |
| `safety.confirm_risk_at_or_above` | `2` | Ask before medium-risk actions. |
| `safety.block_at_or_above` | `4` | Block critical actions. |
| `safety.max_cost_usd_per_run` | `1.0` | Spend cap. |
| `automation.max_steps` | `30` | Steps per task. |
| `llm.fallbacks` | `[]` | Other models to retry on. |
| `llm.offline` | `false` | No network at all; actions only. |

## Background and scheduled work

```powershell
ufo daemon start
```

The daemon runs one task at a time, fires due schedules, and writes state to
`~/.ufo/state.json`. Cron, absolute and recurring schedules all survive a
restart. Natural language works everywhere a time does: `in 20 minutes`,
`tomorrow 08:00`, `weekdays 09:00`, `every 2 hours`, `0 9 * * 1-5`.

Scheduled tasks run under exactly the same allowlist and risk rules as
interactive ones. A schedule cannot do anything the console cannot.

## The dashboard

`ufo serve` binds to `127.0.0.1` and serves a single-page control panel: live
screenshot, window list, queue, schedules, memory, and buttons to submit a
task, cancel, and trip the emergency stop. A bearer token is generated on
first start and required for every mutating call.

## Voice

```json
"voice": { "enabled": true, "tts": true, "stt": false }
```

With `enabled`, the agent narrates what it is about to do and asks for
confirmation out loud. `stt` additionally needs a speech backend
(`pip install SpeechRecognition pyaudio`) and a `stt_command` to invoke.

### Instructions from your phone

This is what `python main.py` starts, so you do not normally need a command
here. To start it explicitly:

```powershell
ufo listen
```

This starts a small page on your local network. Open the URL it prints in your
phone's browser and **hold the button** to speak. Both devices must be on the
same Wi-Fi.

By default the phone records the audio and this laptop transcribes it with
faster-whisper, so nothing is sent anywhere. If you would rather the phone's own
browser did the recognition — no install, but the audio goes to the browser
vendor — set `phone.transcription` to `browser`.

By default every instruction is read back to you and confirmed before anything
runs. `--auto` skips the confirmation and asks you to type `yes` at startup to
acknowledge that; with `--auto` a misheard word can act immediately, so leave
it off unless you are deliberately testing.

`phone.transcription` chooses who turns speech into text:

| mode | who transcribes | audio leaves the LAN | needs |
| --- | --- | --- | --- |
| `local` (default) | the laptop, via faster-whisper | no | `pip install faster-whisper` |
| `browser` | the phone, via Web Speech API | yes — to the browser vendor | nothing |

`local` is the one to use when the machine must stay self-contained: the phone
records raw WAV, posts it to the laptop, and faster-whisper transcribes it on
the CPU. It costs a few seconds per utterance and needs no key. It is also the
default, so nothing has to be chosen for the audio to stay on your network.
Only `browser` hands the recording to a third party.

There is no Gemini mode: this build has no code path that sends audio to Google.

> Browsers only expose the microphone on a secure origin. `localhost` counts as
> secure, but a phone reaching `http://192.168.x.x:8766` does not. On many
> phones the page will still work; if the mic is blocked, either open it through
> an HTTPS tunnel or set up a self-signed certificate for your machine's LAN
> address. This is a browser rule, not a limit in UFO.

### Persona

A persona changes how the agent *sounds*. It never changes what the agent is
allowed to do — the action vocabulary, the safety rules and the allowlist are
identical with any persona setting.

```json
"voice": {
  "enabled": true,
  "persona": "jarvis",
  "address": "sir",
  "cadence": "dry"
}
```

`persona` is `none`, `jarvis`, `butler` or `terse`. `address` is any form of
address you want (`sir`, `ma'am`, or empty to never address you). `cadence` is
`dry` or `warm`. The persona also colours the model's own `thought` field, so
the reasoning it shows you reads in the same voice.

Read-backs are passed through byte for byte apart from the first letter. If you
say `rename every .txt to .md`, that is exactly what is read back and exactly
what runs — a persona cannot rewrite a command you are about to approve.

## State on disk

Everything lives in `~/.ufo` (override with `--home`):

```
~/.ufo/config.json       your configuration
~/.ufo/queue.json        pending tasks
~/.ufo/schedules.json    schedules
~/.ufo/memory.json       facts and past episodes
~/.ufo/secrets.json      local credential vault (owner-readable)
~/.ufo/runs/<id>/        per-run prompts, decisions, screenshots, trace
```

`ufo memory forget <key>` deletes a fact. `ufo queue clear` empties the queue.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

231 tests, no network, no API key, no desktop automation. The safety layer,
config layering, scheduling, decision validation, the phone/voice layer and the
filesystem/network guards are all covered, including the refusal paths — the
tests care most about what the agent is *not* allowed to do.

## Troubleshooting

**`Allowlist: empty`** — by design. `ufo config allow Notepad`.

**Model returns invalid JSON** — handled: fences, prose, single quotes,
Python literals, trailing commas and truncated output are all repaired, and
the model is asked again. `ufo logs` shows what came back.

**It clicks the wrong thing** — use `--mode app` for native apps, and
`ufo see` to see the numbered control list the model is reading.

**UIA finds no controls** — the app is canvas-drawn. Use `computer` mode and
rely on the annotated screenshot; install an OCR engine if you need to read
text that is not a real control.

**System actions fail with access denied** — run the terminal elevated.

**Press Ctrl+Alt+Q** — always works, and stops everything immediately.
