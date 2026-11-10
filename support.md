# tubeGPT Support

Thanks for trying tubeGPT. This page explains how to report a problem or ask a project question.

## Project background

The tubeGPT idea was inspired by AskAVideo and the project was independently implemented with Claude AI used as a development assistant. tubeGPT is not affiliated with AskAVideo, YouTube, Google, Anthropic, or OpenAI.

## Report a bug or request a feature

Open an issue in the [tubeGPT GitHub repository](https://github.com/Manas07-cyber/tubeGPT-/issues).

When reporting a problem, include:

- What you were trying to do.
- What happened and what you expected to happen.
- The type of input (for example, a public YouTube URL or an SRT/VTT/TXT transcript).
- The language of the video and whether captions are available, if known.
- Relevant error text, with personal or sensitive information removed.

## Before reporting transcript problems

- Check whether captions are available on the original video.
- Try another video with publicly available captions.
- If you have permission to use the transcript, try a supported transcript file.
- Remember that auto-generated captions may contain errors.
- If you run the helper script yourself, add `-v` before the command (for example `transcript.py -v show VIDEO_ID`) to get diagnostic logs. They never contain transcript text, but still check them for personal information before posting.

## Storage and permission problems

If a command stops with exit code 7, the transcript storage folder could not be used. The message says why (no free space, read-only location, permission denied, a symbolic link, or a folder owned by another user). Free some space, or point `TUBEGPT_STORAGE_DIR` at a folder of your own. Cleanup never deletes anything in a folder you chose with `TUBEGPT_STORAGE_DIR` unless you also set `TUBEGPT_STORAGE_CLEANUP=1`; see the README section "Storage and retention".

## Protect your privacy

GitHub issues in this repository may be public. Never post passwords, access tokens, private video links, confidential transcripts, personal data, or security-sensitive information. Redact usernames and other identifiers from screenshots and logs.

A private support or privacy-request email has not yet been established. Do not include private information in a public issue.

## Response expectations

This is an independently maintained project. A response is not guaranteed, and there is no guaranteed support schedule or service-level agreement.
