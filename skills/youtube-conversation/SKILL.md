---
name: youtube-conversation
description: Chat with a YouTube video using its real transcript, with timestamp links to the exact moment whenever the transcript has timing data. Use whenever the user shares a YouTube link (youtube.com, youtu.be, Shorts, live, embed) or refers to "this video" and wants a summary, key points, notes, a study guide, an answer about what was said, a quote, or the moment where something is discussed, even if they never say "transcript". Also use when the user pastes a YouTube transcript or attaches an .srt/.vtt caption file. Never answer questions about a video from its title, search results, or guesses.
---

# YouTube Conversation

Answer questions about a YouTube video from what is actually said in it, and, when the transcript has timing data, let the user check claims with timestamp links. The value here is trust, so three rules shape everything below:

1. **Read the real transcript.** Work from the video's own captions, not from a summary site, a search result, the title, or what you already know about the topic.
2. **Make claims checkable.** When the transcript has timing data, each point comes with a timestamp link that lands on the exact second. Plain pasted text without timestamps cannot give real timestamp links, so say that once and do not invent times.
3. **No fallback.** If the transcript cannot be read, say so plainly. A confident answer built on a guess is worse than no answer, because the user cannot tell the difference.

All commands below use the helper `scripts/transcript.py`. It sits in the `scripts` folder right next to this SKILL.md file, so there is no need to search the file system for it. Run the commands from this skill's folder (the folder that contains this SKILL.md), or give the full path to the script, and use `python3` if `python` is not found.

## Workflow

### 1. Identify the video

Find the YouTube URL (or 11-character ID) in the user's message. If there is none, ask for the link. If they give several videos, handle them one at a time, since each needs its own transcript.

### 2. Get the transcript

**Run `fetch` first**, as soon as you have the video link. Do not web-search or browse for the video, its transcript or a summary before this command has run and you have read its exit code. If you cannot run commands at all, say so and go to the paste option below.

```bash
python scripts/transcript.py fetch "<url>"
```

On success it prints the video details and saves the transcript, so later questions about the same video need no new fetch. Check the exit code:

| Exit | Meaning | What to do |
|---|---|---|
| 0 | Transcript saved | Go to step 3 |
| 6 | Dependency missing | Create and activate a virtual environment at the plugin root, then, from this skill folder, run `python -m pip install -r ../../requirements.txt` (the one dependency list lives at the plugin root; see README.md), and rerun. Never use `sudo pip` or `--break-system-packages` |
| 3 | Cannot reach YouTube from this environment (no internet access, or the sandbox blocks it) | Say in one short line that the script could not reach YouTube from here, then use one of the other ways below |
| 5 | YouTube refused the request (cloud or VPN address, rate limit) | Say in one short line that YouTube refused the request, then use one of the other ways below. Running from the user's own computer usually avoids this |
| 4 | The video has no readable captions (disabled, private, age-restricted, or none in that language) | Tell the user. If the error lists other languages, rerun with `--lang <code>`. Otherwise stop: do not answer from other sources |
| 2 | No video ID found | Ask for a proper YouTube link |

The default language order is English first, then whatever exists. Pass `--lang hi,en` (or any codes) to change it. The output says which language was used and whether the captions are auto-generated.

**Other ways to get the real transcript** (when exit code is 3 or 5):

- **Browser tool.** If you can drive a browser (for example the ChatGPT Chrome extension or a Codex browser), open the video page, expand the description, choose **Show transcript**, and copy the text of the transcript panel into a file. This reads the transcript through the user's own browser, so it works where the script cannot reach YouTube.
- **Paste.** Ask the user to open the video on YouTube's website, expand the description, choose **Show transcript**, copy the text of the transcript panel, and paste it in the chat. They can also attach a `.srt`, `.vtt` or `.txt` caption file.
- **A connected tool** that returns the video's actual caption text with timestamps is also fine. Anything that only describes the video (a summary, the page description, comments) is not a transcript.

Then import the text:

```bash
python scripts/transcript.py parse <file> --video "<url>"
```

- If the user attached a file, parse it from the path where the attachment was saved. If the text came inline, write it to a file first.
- `--video` lets timestamps become links. Without it you still get timestamps, only no links.
- If the text has no timestamps, tell the user once that you can answer from the content but cannot point to a time.

### 3. Read it

```bash
python scripts/transcript.py show <video-id-or-saved-json> --stats
```

This prints the duration, word count, language and caption type. `<id>` is the video ID (or URL) used for `fetch`; for a transcript imported without `--video`, use the `saved:` path that `parse` printed. Then read the text:

- **Short videos** (one window): `show <id>` prints everything as `[mm:ss] text` lines.
- **Long videos**: `show <id>` stops after about 30,000 characters and prints the exact `--from` value to continue. Use `--from` and `--to` to read a specific stretch (`--from 10:00 --to 20:00`).
- **A specific question**: find the relevant places first, then read around them.

  ```bash
  python scripts/transcript.py search <id> "interest rate" "rates" --context 3
  ```

  Several terms count as "any of them", so include synonyms. Read the surrounding passage before you cite it, because a keyword hit is not yet an answer. No hits does not prove the video never covers it: captions can misspell names and terms, so try other wording before concluding anything.

### 4. Answer

- Lead with the answer, then support it.
- When the transcript has timestamps, put a timestamp link after each claim. Generate the links with the helper instead of converting minutes to seconds by hand:

  ```bash
  python scripts/transcript.py link <id> 12:34 1:02:03
  ```

  It prints ready-made markdown such as `[12:34](https://youtu.be/ID?t=754)`. Ask for all the timestamps you need in one call.
- Paraphrase. Quote only when the exact wording matters, and keep quotes short. Do not reproduce long stretches of the transcript. If the user wants the full transcript, point them to YouTube's own transcript panel.
- For summaries, go through the video in order, one short section each, each with its link. Read the whole transcript first rather than summarizing from the opening.
- If the video does not address the question, say so, and mention what it does cover nearby.
- Auto-generated captions can mishear names, numbers and technical terms. Flag this when an answer depends on an exact spelling or figure.
- Reply in the user's language, even when the video is in another one.
- For follow-up questions, reuse the saved transcript with `search` and `show`. Do not fetch again.

## What not to do

- Do not use web search, summary websites, or the video's title or description as a stand-in for the transcript. At most, a page fetch can confirm which video a link is, and it must not supply content.
- Do not fill gaps with background knowledge and present it as what the video says. If outside context would help, add it separately and label it clearly as not from the video.
- Do not invent or round timestamps. Every link must come from the `link` command or a `search` result.
