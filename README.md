# tubeGPT

**Explore YouTube video transcripts with summaries, searchable notes, and timestamped answers.**

tubeGPT is an independent, skills-based ChatGPT project designed to help users work with the captions/transcripts of YouTube videos. It uses available transcript text as its source and links answers back to relevant video timestamps where possible.

> **Status:** Documentation and public project setup are in progress. Availability and compatibility depend on the host platform, the video, and whether captions can be accessed.

## What it does

- Retrieves available captions for a supplied public YouTube video when supported by the execution environment.
- Works with transcript text supplied by the user, including SRT, VTT, and plain-text formats.
- Summarizes and explains transcript content.
- Finds topics, terms, and relevant sections in the transcript.
- Produces timestamp links when the transcript includes usable timing information.
- Says when transcript content is unavailable rather than inventing what a video contains.

## What it does not promise

- It does not guarantee captions for every video, language, region, or account.
- It does not verify that captions are complete or accurate.
- It is not affiliated with, endorsed by, or operated by YouTube or Google.
- It is not intended to download or republish complete copyrighted videos or transcripts.

## How it works

The project uses the transcript text available to it. Its helper script can request public caption data using `youtube-transcript-api` and video metadata through YouTube's oEmbed endpoint. User-provided transcript files can also be parsed. Temporary transcript data may be written to the execution environment's temporary directory; cleanup and retention depend on that environment.

The project does not currently include a developer-operated backend, user account database, analytics service, or advertising tracker. The host platform may separately process prompts, files, and outputs under its own terms and privacy policy.

## Getting started

1. Open tubeGPT in the supported ChatGPT environment once it is made available.
2. Provide a YouTube video URL, or provide transcript text/a supported transcript file.
3. Ask for a summary, explanation, topic search, notes, or timestamped answer.
4. Check important claims against the original video.

For developers, see the project files and the skill documentation in this repository. Dependencies and platform requirements may change; consult the source before running scripts.

## Documentation

- [About tubeGPT](about.md)
- [Privacy Policy](privacy.md)
- [Terms of Use](terms.md)
- [Support](support.md)

## Responsible use

Respect copyright, platform terms, privacy, and access controls. Only provide transcript material you are allowed to use. Do not use the project to bypass restrictions or to expose private information. Summaries and timestamps are aids for navigating source material, not substitutes for checking it.

## Project details

- **Project name:** tubeGPT
- **Developer display name:** Manas D (preferred project label; public publisher identity remains subject to platform verification)
- **Distribution intention:** Free; worldwide, subject to platform availability and applicable laws
- **Affiliation:** Independent project; not affiliated with YouTube or Google

## License

No open-source license has been selected for this repository yet. Until a license is added, do not assume that the source is licensed for reuse, redistribution, or modification.

## Contact and support

For general questions and bug reports, use [GitHub Issues](https://github.com/Manas07-cyber/tubeGPT-/issues). Do not post passwords, private transcripts, personal data, or security-sensitive information in public issues.
