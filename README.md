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

The project uses transcript text available to it. Its helper script can request public caption data using `youtube-transcript-api` and video metadata through YouTube's oEmbed endpoint. User-provided transcript files can also be parsed. Saved transcripts are written to a private folder in the execution environment's temporary directory and are removed automatically when unused for 24 hours by default (see [Storage and retention](#storage-and-retention)); the host environment may retain data separately.

The current project code does not include a developer-operated backend, user account database, analytics service, or advertising tracker. The host platform may separately process prompts, files, and outputs under its own terms and privacy policy.

## Getting started

1. Open tubeGPT in the supported ChatGPT environment once it is made available.
2. Provide a YouTube video URL, or provide transcript text/a supported transcript file.
3. Ask for a summary, explanation, topic search, notes, or timestamped answer.
4. Check important claims against the original video.

For developers, see the project files and the skill documentation in this repository. Dependencies and platform requirements may change; consult the source before running scripts.

### Developer setup

Requirements: **Python 3.11 or newer**. CI tests Python 3.11, 3.12, 3.13, 3.14 and 3.15 on Linux, and Python 3.11 and 3.15 on Windows and macOS. Python 3.9 and 3.10 are past their upstream end-of-life (see the [Python release schedule](https://devguide.python.org/versions/)) and are neither supported nor tested; the script uses language features that need 3.10 or newer. If you must run an older Python, that is your own responsibility and receives no upstream security fixes. Run these commands from the repository root (the folder that contains `requirements.txt`). Use a standard virtual environment; no administrator rights are needed, and `sudo pip` or `--break-system-packages` should never be used.

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Linux / macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

For a reproducible install that uses the exact dependency versions CI tests, add the constraints file (`constraints.txt`; it pins `youtube-transcript-api`, its dependencies and the linter, with `requests`/`urllib3` pinned per Python version):

```bash
python -m pip install -r requirements-dev.txt -c constraints.txt
```

Normal installs use `requirements.txt` alone and therefore receive newer compatible releases, including security fixes. The pins are a snapshot that CI installs and tests on Python 3.11 to 3.15 (Linux, Windows and macOS); Dependabot proposes updates weekly and every proposal must pass CI before it is merged. A manual `resolve-pins` job prints the newest compatible versions when you want to refresh them by hand.

Leave the environment with `deactivate`; delete the `.venv` folder to remove it. If PowerShell blocks `Activate.ps1`, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or use `.venv\Scripts\activate.bat` in Command Prompt.

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

- `python` or `python3` not found: install Python 3.11 or newer from python.org (or your system package manager) and reopen the terminal. On Windows the launcher `py -3 -m venv .venv` also works.
- `Activate.ps1 cannot be loaded` (PowerShell): see the execution-policy note above.
- `No such file or directory: requirements.txt`: you are not in the repository root. `cd` into the folder that contains `requirements.txt` first.
- `externally-managed-environment`: this means you are installing outside a virtual environment. Activate `.venv` and run `python -m pip install -r requirements.txt` again; do not override the protection.
- Packages installed in the wrong place: check that your prompt shows `(.venv)` and that `python -m pip --version` points inside the `.venv` folder.

### Run the tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

The tests use mock data and temporary folders only. They make no network requests and need no credentials. One optional test talks to the real YouTube; it is skipped unless you ask for it:

```bash
TUBEGPT_RUN_INTEGRATION=1 python -m pytest -m integration -v -rs     # Linux / macOS
$env:TUBEGPT_RUN_INTEGRATION = "1"; python -m pytest -m integration -v -rs   # PowerShell
```

If YouTube is unreachable or refuses the request, that test is reported as skipped with the reason; ordinary test runs never depend on YouTube.

Contributors also run the linter and the same checks as CI:

```bash
python -m ruff check .
python -m compileall -q skills tests
python -m pip check
python -m pytest -q tests/test_manifests.py tests/test_dependencies.py tests/test_repo_hygiene.py tests/test_docs.py
```

GitHub Actions run on every push to `main` and every pull request, with the workflow token limited to read-only access, checkout credentials not persisted, every third-party Action pinned to a full commit SHA (the comment after each SHA names the release), and a timeout on every job:

- `tests.yml`: a static job (dependency install from `constraints.txt`, `pip check`, compilation, `ruff` with style, bugbear and security rules, and the manifest-schema, dependency, repository-hygiene, documentation and workflow tests), the full suite with coverage on Linux for Python 3.11, 3.12, 3.13, 3.14 and 3.15, and the full suite on Windows and macOS for Python 3.11 and 3.15. Tests that need POSIX permissions, symbolic links or user IDs are marked and skipped on Windows with a stated reason. The remaining jobs run only when started by hand from the Actions tab and never on push or pull request, so ordinary CI never depends on YouTube, GitHub Pages or a vulnerability database being reachable: `live-fetch` (one real transcript fetch), `pages-check` (the public pages and their links) and `resolve-pins`.
- `security.yml`: secret scanning with gitleaks over the whole Git history and the current files (secret values are redacted in the output), `pip-audit` of the pinned runtime and development dependencies and of the freshly resolved runtime dependencies, validation of `plugin.json` against the published Agent Plugins schema, and the same checks weekly so new advisories surface without a code change.
- `.github/dependabot.yml` proposes weekly updates for Python packages and GitHub Actions; it never merges anything.

Not covered by CI: behaviour inside ChatGPT's own sandbox, Python versions other than those listed, and Windows or macOS beyond the jobs above.

## Storage and retention

`fetch` and `parse` save a transcript as JSON so later `show`, `search` and `link` calls do not need to fetch it again.

- **Where:** `youtube-conversation/` inside the system temp folder, or the folder in `TUBEGPT_STORAGE_DIR`. Files are named after the video ID (`<id>.json`) or `pasted-<32 hex characters>.json` (a SHA-256 of the pasted text and its timestamps; older 8-character names are still read and cleaned).
- **Permissions on Linux and macOS (POSIX):** the default folder is created owner-only (mode 700) and its transcript files owner-only (600). A folder or file left behind with looser permissions (for example 755 or 644) is tightened the next time the tool uses it, but only after checking that you own it, and only for the folder itself and for files whose names this tool generates. Other files in the folder are never modified. A permission failure is reported as a clear error (exit code 7).
- **Permissions on Windows:** Windows has no POSIX mode bits, so the tool does not change access rules there. Who can read the files is decided by the permissions the temp folder (or your chosen folder) already has, normally your own user profile. Do not assume the files are private from other administrators or other accounts on a shared machine.
- **Links and other users' files:** a storage folder that is a symbolic link, is not a folder, or belongs to another user is refused. Inside the folder, symbolic links, special files and files owned by someone else are never read, changed or deleted. Reads (`show`, `search`, `link`) go through the same checks as saves.
- **Retention:** a stored transcript is deleted once it has not been used (saved or read) for `TUBEGPT_RETENTION_HOURS` hours (default 24, range 0.1 to 8760).
- **Limits:** when the folder exceeds `TUBEGPT_MAX_STORE_MB` megabytes (default 200) or `TUBEGPT_MAX_FILES` files (default 500), the oldest files are deleted first.
- **When cleanup runs:** automatically before each save to the default folder, and on demand with `python skills/youtube-conversation/scripts/transcript.py cleanup` (`--dry-run` shows what would be removed). There is no background process, so files stay until the next run.
- **Your own folder (`TUBEGPT_STORAGE_DIR`):** nothing in it is ever deleted unless you also set `TUBEGPT_STORAGE_CLEANUP=1`. Even then, cleanup only acts if the folder contains the `.tubegpt-store` marker file, which the tool writes when it creates the folder or finds it empty. The marker is a guard against pointing cleanup at the wrong folder, not proof that the folder is yours: anyone who can write to the folder can create that file. A non-empty folder you point the tool at is never adopted automatically, and its permissions are not changed unless it carries the marker.
- **Safe cleanup:** cleanup only looks at regular files directly inside the folder whose names this tool generates (and its own leftover temporary files); it never recurses and deletes single files only. It never deletes a file used in the last 5 minutes. Each file is re-checked immediately before it is deleted, and one that was replaced, rewritten or read after the scan is kept. Saves, reads and cleanup runs take a lock file (`.tubegpt-lock`) inside the folder so cooperating processes do not interleave; if the lock cannot be taken, the re-check still protects the file. Files written with `--out` are yours and are never deleted.
- **Writes are atomic:** a private temporary file is renamed into place, so a crash or a full disk never leaves a half-written transcript. A full or unwritable disk gives a clear message and exit code 7.
- **Shared computers:** the default folder name is the same for every user. If another user created it first, the tool refuses it (exit code 7); set `TUBEGPT_STORAGE_DIR` to a folder of your own.

## Environment variables

| Variable | Meaning | Default |
|---|---|---|
| `TUBEGPT_STORAGE_DIR` | Folder for saved transcripts | `youtube-conversation/` in the system temp folder |
| `TUBEGPT_STORAGE_CLEANUP` | Set to `1` to allow cleanup inside a folder given by `TUBEGPT_STORAGE_DIR` (the marker file is also required) | unset: nothing is deleted there |
| `TUBEGPT_RETENTION_HOURS` | Hours without use before a transcript is deleted (0.1 to 8760) | 24 |
| `TUBEGPT_MAX_STORE_MB` | Maximum total size of the folder in MB (1 to 10240) | 200 |
| `TUBEGPT_MAX_FILES` | Maximum number of transcripts (1 to 10000) | 500 |
| `TUBEGPT_RUN_INTEGRATION` | Set to `1` to run the optional real-YouTube test (tests only) | unset |

## Input validation and limits

Saved transcripts are fully validated when loaded (types, required fields, finite non-negative timestamps, end after start, start times in order), and every transcript the tool creates is validated by the same rules before it is saved, so a saved file can always be read back. A caption longer than 100,000 characters is split into several segments with the same timestamp instead of being rejected. Imported text must be valid UTF-8 (otherwise nothing is imported and the position of the bad byte is reported); the 50 MB input limit is counted in encoded bytes. A malformed file or number gives a clear error and exit code 2, never a crash. Other limits: input files up to 50 MB, saved transcripts up to 100 MB and 500,000 segments, search terms up to 500 characters (50 per search), 200 times per `link` call, `--max` 1 to 1000, `--context` 0 to 50, `--max-chars` 1 to 1,000,000, and times of at most 10,000,000 seconds. Minutes and seconds must be below 60 in `M:SS` and `H:MM:SS` times.

## Logging and exit codes

Add `-v` before the command (for example `python skills/youtube-conversation/scripts/transcript.py -v show VIDEO_ID`) to print structured debug lines to stderr. Logs never contain transcript text. Exit codes: 0 ok, 1 unexpected error, 2 bad input, 3 cannot reach YouTube, 4 no readable transcript, 5 YouTube refused the request, 6 missing or outdated dependency, 7 storage unavailable or full.

## Known limitations

- Fetching depends on the third-party, unofficial `youtube-transcript-api` package and on YouTube allowing the request; it often fails from cloud, VPN or shared addresses. Pasting a transcript or caption file always works.
- Videos without captions cannot be read. Auto-generated captions can misspell names and terms.
- Plain pasted text without timestamps cannot give real video timestamps, and not every answer will include a timestamp link.
- Rolling auto-caption repeats are detected by timing and line overlap. Unusual files may keep a repeated line, and an unusually timed genuine repeat in a multi-line rolling window may be dropped.
- Cleanup is opportunistic (see above), not a scheduled job. Nothing here can control what the host platform keeps.
- The tool reads and writes JSON transcripts only; it has no subtitle export command.

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

No open-source license has been selected for this repository yet. Until a license is added, do not assume that the source is licensed for reuse, redistribution, or modification.

## Contact and support

For general questions and bug reports, use [GitHub Issues](https://github.com/Manas07-cyber/tubeGPT-/issues). Do not post passwords, private transcripts, personal data, or security-sensitive information in public issues.
