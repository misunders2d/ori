---
name: youtube-summarizer
description: Multimodal YouTube video summarization tool using Gemini 2.0+ native video understanding.
author: Bezos
created: 2026-04-06
verified: true
tags: [youtube, video, summarization, gemini, multimodal]
files:
  - app/tools/youtube.py
---

# youtube-summarizer

Multimodal YouTube video summarization tool using Gemini 2.0+ native video understanding. Processes both visual and audio content from YouTube videos to answer specific queries.

## Usage

### Prerequisites

1. A Google API key (`GOOGLE_API_KEY`) or Vertex AI credentials.
2. A Gemini model that supports video input (e.g., `gemini-2.0-flash`, `gemini-3-flash-preview`).

### How it works

The tool passes the YouTube URL directly to Gemini via `Part.from_uri(file_uri=url, mime_type="video/mp4")`. Gemini 2.0+ processes the video natively — no download, transcription, or preprocessing required.

### Integration

1. Add `youtube_summary` to CoordinatorAgent's tool list.
2. Ensure `get_model_name("youtube_summarizer")` resolves to a Gemini 2.0+ model. Add to `MODEL_DEFAULTS` in `app/app_utils/models.py`:
   ```python
   "youtube_summarizer": "google/gemini-2.0-flash"
   ```

### Example

User: "Summarize this video: https://youtube.com/watch?v=..."
Agent calls: `youtube_summary(url="https://youtube.com/watch?v=...", query="Provide a detailed summary of this video")`

## Files

- `app/tools/youtube.py`
