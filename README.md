# ClipIn AI — phone + cloud

Personal build. The phone app (`docs/`, hosted on GitHub Pages) sends a video link to GitHub Actions
(`.github/workflows/clip.yml`), which makes the clips with the same engine as the laptop app (`core/`)
and publishes them to the `clips` branch. The phone shows live progress from the workflow steps and plays,
shares and saves the finished clips. AI keys live only in this repository's secrets.

Set up / update from the laptop app: **Settings → Phone → Set up / update phone**.

| Secret | Use |
|---|---|
| `GEMINI_API_KEY` | picks the moments, writes titles and hashtags |
| `GROQ_API_KEY` | fast speech-to-text (without it the cloud uses local Whisper, slower) |
| `YT_COOKIES` | optional: cookies.txt text, only if YouTube blocks GitHub's servers |
