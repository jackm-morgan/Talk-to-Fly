# Talk-to-Fly

Talk-to-Fly lets you control a MAVLink-connected UAV using natural-language commands.

## Requirements

- Python 3.11
- OpenAI API key
- MAVLink connection to ArduPilot SITL or a real vehicle

## Install

### With Poetry

```bash
poetry install
```

### Without Poetry

```bash
python -m venv .venv
source .venv/bin/activate
pip install openai python-dotenv dronekit pymavlink numpy pyyaml tiktoken
export PYTHONPATH=src
```

Optional voice dependencies:

```bash
pip install faster-whisper sounddevice pynput
```

## Configure

Create a `.env` file in the project root:

```env
OPENAI_API_KEY=your_api_key_here
```

## Run

### Interactive runtime

```bash
poetry run python -m talk_to_fly -s -k
```

or:

```bash
PYTHONPATH=src python -m talk_to_fly -s -k
```

Default simulation connection: `udp:127.0.0.1:14551`

Example tasks:

```text
Take off to 10 metres, fly forward 10 metres, then land.
Fly in a 10 metre square.
Take off to 5 metres and orbit with a radius of 4 metres.
```

## Bridge server

Use this for Mission Planner or other external clients.

```bash
poetry run python -m talk_to_fly.bridge -s -k
```

Default bridge address: `http://127.0.0.1:8765`

Useful endpoints:

- `GET /ping`
- `GET /status`
- `POST /task`
- `POST /clarification`
- `POST /approve`
- `POST /cancel`
- `POST /abort`

Example:

```bash
curl -X POST http://127.0.0.1:8765/task -d "Task=Take off to 10 metres and fly forward 5 metres"
```

## Evaluation

Run an evaluation suite:

```bash
poetry run python -m talk_to_fly.eval run \
  --suite path/to/suite.yaml \
  --simulation \
  --runs 5 \
  --shuffle \
  --llm live
```

Common options:

- `--suite <path>`
- `--simulation`
- `--connect <MAVLink string>`
- `--runs N`
- `--shuffle`
- `--llm live|record|replay`
- `--cache <path>`
- `--ablation full|stateless|no_replanning|open_loop|one_shot`

## Voice mode

```bash
poetry run python -m talk_to_fly -s --voice
```

## Common flags

- `-s, --simulation`
- `-k, --confirm`
- `-v, --verbose`
- `--connect <MAVLink string>`
- `--max-replans N`
- `--plain-ui`
- `--architecture agentic|one_shot`
- `--voice`

## Interactive commands

```text
:help
:status
:pos
:mission
:plan
:history
:repeat
:settings
:dashboard
:ui
:clear
:land / :l
:rtl / :r
quit / exit
```

## Troubleshooting

### Missing API key

```env
OPENAI_API_KEY=...
```

### Cannot connect to vehicle

Check that:

- SITL or the autopilot is running
- the MAVLink port matches `--connect`
- simulation mode is using the expected port

### Voice mode not working

Install the optional voice dependencies. On Arch Linux, also install:

```bash
sudo pacman -S portaudio
```
