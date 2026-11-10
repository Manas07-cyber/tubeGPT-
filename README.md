# tubeGPT

**Explore YouTube video transcripts with summaries, searchable notes, and timestamp links where timing data exists.**

tubeGPT is an independently developed, skills-based ChatGPT project that helps users work with transcript text from YouTube videos. It uses available transcript content as its source and links answers to relevant video timestamps where possible.

> **Development note:** The project idea was inspired by AskAVideo. tubeGPT was independently implemented with Claude AI used as a development assistant. tubeGPT is not affiliated with, endorsed by, or operated by AskAVideo, YouTube, Google, Anthropic, or OpenAI.

> **Status:** Public documentation and project setup are in progress. Platform listing and availability are not yet confirmed. Compatibility depends on the host platform, the video, and whether captions can be accessed.

## What it does

- Retrieves available captions for a supplied public YouTube video when supported by the execution environment.
- Works with transcript text supplied by the user, including SRT, VTT, and plain-text formats.
- Summarizes and explains transcript content.
- Finds topics, terms, and relevant sections in the transcript.
- Produces timestamp links when the transcript includes usable timing information. Plain pasted text without timestamps cannot provide real video timestamp references, and not every answer will include one.
- Says when transcript content is unavailable rather than inventing what a video contains.

## What it does not promise

- It does not guarantee captions for every video, language, region, or account.
- It does not verify that captions are complete or accurate.
- It is not affiliated with or endorsed by AskAVideo, YouTube, Google, Anthropic, or OpenAI.
- It is not intended to download or republish complete copyrighted videos or transcripts.

## How it works

The project uses transcript text available to it. Its helper script can request public caption data using `youtube-transcript-api` and video metadata through YouTube's oEmbed endpoint. User-provided transcript files can also be parsed. Temporary transcript data may be written to the execution environment's temporary directory; cleanup and retention depend on that environment.

The current project code does not include a developer-operated backend, user account database, analytics service, or advertising tracker. The host platform may separately process prompts, files, and outputs under its own terms and privacy policy.

## Getting started

1. Open tubeGPT in the supported ChatGPT environment once it is made available.
2. Provide a YouTube video URL, or provide transcript text/a supported transcript file.
3. Ask for a summary, explanation, topic search, notes, or timestamped answer.
4. Check important claims against the original video.

For developers, see the project files and the skill documentation in this repository. Dependencies and platform requirements may change; consult the source before running scripts.

### Developer setup

Requirements: Python 3.9 or newer. Run these commands from the repository root (the folder that contains `requirements.txt`). Use a standard virtual environment; no administrator rights are needed, and `sudo pip` or `--break-system-packages` should never be used.

Windows PowerShell:

```powershell
python -m venv .venv
.venv\\Scripts\\Activate.ps1
python -m pip install -r requirements.txt
```

Linux / macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Leave the environment with `deactivate`; delete the `.venv` folder to remove it. If PowerShell blocks `Activate.ps1`, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or use `.venv\\Scripts\\activate.bat` in Command Prompt.

Example commands (from the repository root):

```bash
python skills/youtube-conversation/scripts/transcript.py fetch "https://youtu.be/VIDEO_ID"
python skills/youtube-conversation/scripts/transcript.py show VIDEO_ID --stats
python skills/youtube-conversation/scripts/transcript.py search VIDEO_ID "some term"
python skills/youtube-conversation/scripts/transcript.py link VIDEO_ID 12:34
python skills/youtube-conversation/scripts/transcript.py parse captions.srt --video "https://youtu.be/VIDEO_ID"
```

Only `fetch` needs internet access. The skill's own instructions run the same script from `skills/youtube-conversation/`, where the dependency file is `../../requirements.txt`.

### Common environment problems

- `python` or `python3` not found: install Python 3.9 or newer from python.org (or your system package manager) and reopen the terminal. On Windows the launcher `py -3 -m venv .venv` also works.
- `Activate.ps1 cannot be loaded` (PowerShell): see the execution-policy note above.
- `No such file or directory: requirements.txt`: you are not in the repository root. `cd` into the folder that contains `requirements.txt` first.
- `externally-managed-environment`: this means you are installing outside a virtual environment. Activate `.venv` and run `python -m pip install -r requirements.txt` again; do not override the protection.
- Packages installed in the wrong place: check that your prompt shows `(.venv)` and that `python -m pip --version` points inside the `.venv` folder.

### Run the tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

The tests use mock data and temporary folders only. They make no network requests and need no credentials. Transcript files written by `fetch`/`parse` go to your system temp folder under `youtube-conversation/` (or the path given with `--out`) and are not deleted automatically.

## Documentation

- [About tubeGPT](about.md)
- [Privacy Policy](privacy.md)
- [Terms of Use](terms.md)
- [Support](support.md)

## Responsible use

Respect copyright, platform terms, privacy, and access controls. Only provide transcript material you are allowed to use. Do not use the project to bypass restrictions or expose private information. Summaries and timestamps are aids for navigating source material, not substitutes for checking it.

## Project details

- **Project name:** tubeGPT
- **Project maintainer:** Manas D (preferred project label; public publisher identity remains subject to platform verification)
- **Development assistance:** Claude AI
- **Concept inspiration:** AskAVideo
- **Distribution intention:** Free; worldwide, subject to platform availability and applicable laws
- **Affiliation:** Independent project; not affiliated with AskAVideo, YouTube, Google, Anthropic, or OpenAI

## License

tubeGPT is licensed under the [MIT License](LICENSE).

Copyright (c) 2026 Manas D.

The MIT License applies to the original project materials covered by this repository's copyright notice. Third-party dependencies, tools, and assets remain subject to their respective licences and notices.

## Contact and support

For general questions and bug reports, use [GitHub Issues](https://github.com/Manas07-cyber/tubeGPT-/issues). Do not post passwords, private transcripts, personal data, or security-sensitive information in public issues.
