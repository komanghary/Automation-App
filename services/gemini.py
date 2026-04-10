"""
Gemini service — AI title/description generator with API key rotation.
Supports text-only and video-based generation using Gemini 2.5 Flash.
"""

import os
import google.generativeai as genai

from config import GEMINI_API_KEYS

# ── Key rotation state ────────────────────────────────────────────────────────
_key_index = 0


def _next_key() -> str:
    """Round-robin rotate through Gemini API keys."""
    global _key_index
    if not GEMINI_API_KEYS:
        return ""
    key = GEMINI_API_KEYS[_key_index % len(GEMINI_API_KEYS)]
    _key_index += 1
    return key


def _call_gemini(prompt, video_path: str = None) -> str:
    """Call Gemini with optional video file. Rotates through all API keys on failure."""
    if not GEMINI_API_KEYS:
        return ""

    last_err = None
    for _ in range(len(GEMINI_API_KEYS)):
        api_key = _next_key()
        key_num = (_key_index - 1) % len(GEMINI_API_KEYS) + 1
        try:
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel("gemini-2.5-flash")

            if video_path and os.path.exists(video_path):
                print(f"[Gemini] Key #{key_num} — Uploading video to Gemini...")
                video_file = genai.upload_file(video_path, mime_type="video/mp4")

                # Wait for processing
                import time
                while video_file.state.name == "PROCESSING":
                    time.sleep(2)
                    video_file = genai.get_file(video_file.name)

                if video_file.state.name == "FAILED":
                    raise RuntimeError("Gemini video processing failed")

                response = model.generate_content([prompt, video_file])

                # Clean up uploaded file
                try:
                    genai.delete_file(video_file.name)
                except Exception:
                    pass
            else:
                response = model.generate_content(prompt)

            result = response.text.strip()
            print(f"[Gemini] Key #{key_num} — Generated ({len(result)} chars)")
            return result

        except Exception as e:
            last_err = e
            print(f"[Gemini] Key #{key_num} gagal: {e} — coba key berikutnya...")

    print(f"[Gemini] Semua {len(GEMINI_API_KEYS)} keys gagal. Error terakhir: {last_err}")
    return ""


# ── Description generator (text-only, for wanderingwithme) ───────────────────

def generate_description(title: str) -> str:
    """Generate YouTube video description using Gemini 2.5 Flash with key rotation."""
    prompt = (
        f'Create a YouTube Shorts description for a video titled: "{title}"\n\n'
        "Requirements:\n"
        "- Write in English\n"
        "- Write exactly 2 sentences that give context about the video content, "
        "make it slightly detailed and engaging so viewers understand what the video is about\n"
        "- After the 2 sentences, add a blank line then exactly 5 relevant hashtags\n"
        "- Hashtags should be highly relevant to the video title and content\n"
        "- Do NOT include the title itself at the start\n"
        "- Do NOT add any call-to-action like subscribe/like/comment\n"
        "- Keep the tone natural and descriptive\n"
    )
    result = _call_gemini(prompt)
    return result or f"{title}\n\n#shorts #travel #viral"


# ── Video-based title + description generator (for Film & Motivational) ──────

def generate_title_and_description(video_path: str, channel_type: str = "film") -> tuple[str, str]:
    """
    Generate YouTube title AND description by analyzing the actual video content.
    Returns (title, description).
    """
    if channel_type == "bola_geming":
        prompt = (
            "Watch this football/soccer video clip carefully.\n\n"
            "Generate a YouTube Shorts title and description for this football video.\n\n"
            "Requirements:\n"
            "- Write everything in English\n"
            "- TITLE: Create a catchy, hype title (max 70 characters). "
            "It should capture the football action — goals, tricks, skills, or highlights. "
            "Use exciting words like 'insane', 'crazy', 'unbelievable', 'legendary', etc.\n"
            "- DESCRIPTION: Write 2-3 sentences describing the football action in the video. "
            "Make it hype and exciting. "
            "After the sentences, add a blank line then these MANDATORY hashtags exactly as written: "
            "#football #Ronaldo #goal #footballedit #trickshot "
            "followed by 3-5 more relevant football hashtags.\n"
            "- Do NOT add any call-to-action\n\n"
            "Format your response EXACTLY like this (no extra text):\n"
            "TITLE: <your title here>\n"
            "DESCRIPTION: <your description here>"
        )
    elif channel_type == "film":
        prompt = (
            "Watch this video clip carefully.\n\n"
            "Generate a YouTube Shorts title and description for this video.\n\n"
            "Requirements:\n"
            "- Write everything in English\n"
            "- TITLE: Create a catchy, engaging title (max 70 characters). "
            "It should capture the essence of the scene/clip. "
            "Use emotional or intriguing wording that makes people want to watch.\n"
            "- DESCRIPTION: Write 2-3 sentences describing what happens in the video, "
            "make it engaging and slightly detailed. "
            "After the sentences, add a blank line then 5-10 relevant hashtags.\n"
            "- Do NOT add any call-to-action\n\n"
            "Format your response EXACTLY like this (no extra text):\n"
            "TITLE: <your title here>\n"
            "DESCRIPTION: <your description here>"
        )
    else:  # motivational
        prompt = (
            "Watch this video clip carefully.\n\n"
            "Generate a YouTube Shorts title and description for this motivational video.\n\n"
            "Requirements:\n"
            "- Write everything in English\n"
            "- TITLE: Create a powerful, inspiring title (max 70 characters). "
            "It should be motivational and thought-provoking. "
            "Use strong emotional words that resonate with viewers seeking motivation.\n"
            "- DESCRIPTION: Write 2-3 motivational sentences related to the video content. "
            "Make it inspiring and impactful. "
            "After the sentences, add a blank line then 5-10 relevant hashtags "
            "including #motivation #mindset #success.\n"
            "- Do NOT add any call-to-action\n\n"
            "Format your response EXACTLY like this (no extra text):\n"
            "TITLE: <your title here>\n"
            "DESCRIPTION: <your description here>"
        )

    result = _call_gemini(prompt, video_path=video_path)

    if not result:
        if channel_type == "bola_geming":
            fallback_title = f"Insane Football Moment #{_key_index}"
            fallback_desc = f"{fallback_title}\n\n#football #Ronaldo #goal #footballedit #trickshot #shorts #viral"
        elif channel_type == "film":
            fallback_title = f"Movie Scene #{_key_index}"
            fallback_desc = f"{fallback_title}\n\n#shorts #viral"
        else:
            fallback_title = f"Stay Motivated #{_key_index}"
            fallback_desc = f"{fallback_title}\n\n#shorts #viral"
        return fallback_title, fallback_desc

    # Parse TITLE: and DESCRIPTION: from response
    title = ""
    description = ""
    lines = result.split("\n")
    desc_lines = []
    in_desc = False

    for line in lines:
        if line.strip().upper().startswith("TITLE:"):
            title = line.split(":", 1)[1].strip()
            in_desc = False
        elif line.strip().upper().startswith("DESCRIPTION:"):
            desc_lines.append(line.split(":", 1)[1].strip())
            in_desc = True
        elif in_desc:
            desc_lines.append(line)

    description = "\n".join(desc_lines).strip()

    if not title:
        title = f"{'Movie Scene' if channel_type == 'film' else 'Stay Motivated'} #{_key_index}"
    if not description:
        description = f"{title}\n\n#shorts #viral"

    print(f"[Gemini] Title: {title}")
    print(f"[Gemini] Description: {description[:100]}...")
    return title, description
