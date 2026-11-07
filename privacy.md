# Privacy Policy

**Effective date: 7 November 2026**

This policy describes the data handling of the current tubeGPT project files inspected for this documentation release. It is not a statement about the separate privacy practices of ChatGPT, YouTube, Google, Anthropic, or other third-party services.

## 1. Who operates tubeGPT

tubeGPT is an independent project. The preferred public project label is **Manas D**; the publisher identity shown in any platform directory remains subject to that platform's verification requirements. The project idea was inspired by AskAVideo and the implementation was developed with Claude AI assistance. AskAVideo and Anthropic do not operate tubeGPT.

## 2. Information that may be processed

Depending on how you use tubeGPT, the project may process:

- A YouTube video URL or video identifier you provide.
- Public video metadata returned by YouTube's oEmbed service, such as a title or channel/author label.
- Caption or transcript text retrieved for a video.
- Transcript text or subtitle files you choose to provide, including supported SRT, VTT, or plain-text files.
- Your questions and the generated summaries, explanations, notes, or timestamp references.

Please do not provide confidential, sensitive, or personal information unless you have a lawful reason and understand the risks of sharing it with the host platform.

## 3. How the current project code handles transcript data

The helper script can request public caption data using the `youtube-transcript-api` library and request video metadata using YouTube's oEmbed endpoint. It can also parse transcript text supplied by the user.

The inspected project code writes transcript-related working data to a temporary directory in the execution environment. The project does not define its own guaranteed deletion schedule. Cleanup and retention depend on the host environment and its configuration.

The inspected project code does not contain a developer-operated backend, developer-owned transcript database, analytics service, advertising tracker, or developer-controlled endpoint for collecting transcript content. This description may change if the software is updated; check the repository's current source and release notes.

## 4. Third-party services and platform processing

- **ChatGPT/OpenAI:** If you use tubeGPT through ChatGPT, prompts, uploaded files, and outputs are handled by the host platform under its applicable terms, privacy policy, and account settings. Review OpenAI's current privacy information at https://openai.com/policies/privacy-policy/.
- **YouTube/Google:** Requests for captions or video metadata are subject to the relevant YouTube/Google services, availability, and policies. Review https://policies.google.com/privacy and YouTube's terms at https://www.youtube.com/t/terms.
- **Execution environment:** Temporary files may be subject to the host environment's storage, logging, and cleanup behavior.
- **Development assistance:** Claude AI was used as a development assistant. This development history does not mean user transcripts are sent to Claude by the released helper script. Any future integration that sends user data to an additional provider must be disclosed before release.

tubeGPT does not control these third-party services and cannot make promises on their behalf.

## 5. Sharing and disclosure

The current project code is not designed to send transcript content to a developer-operated server or database. However, content may be processed by the host platform and by services contacted to retrieve captions or metadata. Do not assume that content submitted to an online platform is private from that platform.

## 6. Your choices and precautions

- Use only video links and transcript files you are permitted to access and process.
- Avoid submitting confidential or sensitive material.
- Review a transcript before sharing it with others.
- Remove personal details from public bug reports.
- Stop using the project if you do not agree with the relevant third-party terms or data practices.

## 7. Children's privacy

tubeGPT is not designed to collect personal information directly from children. Use of the host platform must follow its age requirements and applicable law. Where required, a parent or guardian should review and supervise use.

## 8. Changes to this policy

This policy must be updated if the project adds accounts, analytics, advertising, a developer-operated backend, cloud storage, or other material data processing. The effective date above will be updated when the policy changes.

## 9. Contact

For general project questions, use [GitHub Issues](https://github.com/Manas07-cyber/tubeGPT-/issues). Do not post private or sensitive information in a public issue. A dedicated private privacy-request contact route has not yet been established; this should be arranged before public launch if a private contact channel is required.

---

This document is informational and does not replace legal advice. Have the policy reviewed for the intended distribution platform and applicable jurisdictions before public release.
