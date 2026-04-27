# Talk-to-Fly

Natural Language UAV Mission Controling with Large Language Models

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


## Bridge server

Use this for Mission Planner or other external clients.

```bash
poetry run python -m talk_to_fly.bridge -s -k
```

Default bridge address: `http://127.0.0.1:8765`


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
