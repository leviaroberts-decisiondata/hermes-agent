"""System prompt assembly -- identity, platform hints, skills index, context files.

All functions are stateless. AIAgent._build_system_prompt() calls these to
assemble pieces, then combines them with memory and ephemeral prompts.
"""

import hashlib
import json
import logging
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path

from hermes_constants import get_hermes_home, get_skills_dir, is_wsl
from typing import Optional

from agent.skill_utils import (
    extract_skill_conditions,
    extract_skill_description,
    get_all_skills_dirs,
    get_disabled_skill_names,
    iter_skill_index_files,
    parse_frontmatter,
    skill_matches_platform,
)
from utils import atomic_json_write

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context file scanning — detect prompt injection in AGENTS.md, .cursorrules,
# SOUL.md before they get injected into the system prompt.
# ---------------------------------------------------------------------------

_CONTEXT_THREAT_PATTERNS = [
    (r'ignore\s+(previous|all|above|prior)\s+instructions', "prompt_injection"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r'act\s+as\s+(if|though)\s+you\s+(have\s+no|don\'t\s+have)\s+(restrictions|limits|rules)', "bypass_restrictions"),
    (r'<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->', "html_comment_injection"),
    (r'<\s*div\s+style\s*=\s*["\'][\s\S]*?display\s*:\s*none', "hidden_div"),
    (r'translate\s+.*\s+into\s+.*\s+and\s+(execute|run|eval)', "translate_execute"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass)', "read_secrets"),
]

# exfil_curl is adjudicated per-line rather than by bare regex (fleet-repair
# 00.5, WTS 7e1d32e9): ops docs legitimately show `curl -H "… $TOKEN"` against
# internal services, and the old pattern blocked a real project context file
# 104 times on exactly that. The exfil signature is a secret-bearing curl aimed
# at a CONCRETE EXTERNAL host; internal targets (loopback, *.decisiondata.io,
# *.local) and placeholder/no-URL lines are ordinary documentation.
_EXFIL_CURL_LINE_RE = re.compile(
    r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)',
    re.IGNORECASE)
_CONTEXT_URL_RE = re.compile(r'https?://[^\s"\'`)>]+', re.IGNORECASE)
_INTERNAL_URL_RE = re.compile(
    r'https?://(?:localhost|127\.0\.0\.1|\[?::1\]?|0\.0\.0\.0'
    r'|[\w.-]*\.decisiondata\.io|[\w.-]+\.local)(?::\d+)?(?:/|$)',
    re.IGNORECASE)


def _exfil_curl_hit(content: str) -> bool:
    """True when a secret-referencing curl line targets a concrete external URL."""
    for line in content.splitlines():
        if not _EXFIL_CURL_LINE_RE.search(line):
            continue
        for url in _CONTEXT_URL_RE.findall(line):
            if not _INTERNAL_URL_RE.match(url):
                return True
    return False

_CONTEXT_INVISIBLE_CHARS = {
    '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff',
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
}


def _scan_context_content(content: str, filename: str) -> str:
    """Scan context file content for injection. Returns sanitized content."""
    findings = []

    # Check invisible unicode
    for char in _CONTEXT_INVISIBLE_CHARS:
        if char in content:
            findings.append(f"invisible unicode U+{ord(char):04X}")

    # Check threat patterns
    for pattern, pid in _CONTEXT_THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            findings.append(pid)

    # Target-aware curl adjudication (see _exfil_curl_hit).
    if _exfil_curl_hit(content):
        findings.append("exfil_curl")

    if findings:
        logger.warning("Context file %s blocked: %s", filename, ", ".join(findings))
        return f"[BLOCKED: {filename} contained potential prompt injection ({', '.join(findings)}). Content not loaded.]"

    return content


def _find_git_root(start: Path) -> Optional[Path]:
    """Walk *start* and its parents looking for a ``.git`` directory.

    Returns the directory containing ``.git``, or ``None`` if we hit the
    filesystem root without finding one.
    """
    current = start.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


_HERMES_MD_NAMES = (".hermes.md", "HERMES.md")


def _find_hermes_md(cwd: Path) -> Optional[Path]:
    """Discover the nearest ``.hermes.md`` or ``HERMES.md``.

    Search order: *cwd* first, then each parent directory up to (and
    including) the git repository root.  Returns the first match, or
    ``None`` if nothing is found.
    """
    stop_at = _find_git_root(cwd)
    current = cwd.resolve()

    for directory in [current, *current.parents]:
        for name in _HERMES_MD_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
        # Stop walking at the git root (or filesystem root).
        if stop_at and directory == stop_at:
            break
    return None


def _strip_yaml_frontmatter(content: str) -> str:
    """Remove optional YAML frontmatter (``---`` delimited) from *content*.

    The frontmatter may contain structured config (model overrides, tool
    settings) that will be handled separately in a future PR.  For now we
    strip it so only the human-readable markdown body is injected into the
    system prompt.
    """
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            # Skip past the closing --- and any trailing newline
            body = content[end + 4:].lstrip("\n")
            return body if body else content
    return content


# =========================================================================
# Constants
# =========================================================================

DEFAULT_AGENT_IDENTITY = (
    "You are Hermes Agent, an intelligent AI assistant created by Nous Research. "
    "You are helpful, knowledgeable, and direct. You assist users with a wide "
    "range of tasks including answering questions, writing and editing code, "
    "analyzing information, creative work, and executing actions via your tools. "
    "You communicate clearly, admit uncertainty when appropriate, and prioritize "
    "being genuinely useful over being verbose unless otherwise directed below. "
    "Be targeted and efficient in your exploration and investigations."
)

HERMES_AGENT_HELP_GUIDANCE = (
    "If the user asks about configuring, setting up, or using Hermes Agent "
    "itself, load the `hermes-agent` skill with skill_view(name='hermes-agent') "
    "before answering. Docs: https://hermes-agent.nousresearch.com/docs"
)

MEMORY_GUIDANCE = (
    "You have persistent memory across sessions. Save durable facts using the memory "
    "tool: user preferences, environment details, tool quirks, and stable conventions. "
    "Memory is injected into every turn, so keep it compact and focused on facts that "
    "will still matter later.\n"
    "Prioritize what reduces future user steering — the most valuable memory is one "
    "that prevents the user from having to correct or remind you again. "
    "User preferences and recurring corrections matter more than procedural task details.\n"
    "Do NOT save task progress, session outcomes, completed-work logs, or temporary TODO "
    "state to memory; use session_search to recall those from past transcripts. "
    "If you've discovered a new way to do something, solved a problem that could be "
    "necessary later, save it as a skill with the skill tool.\n"
    "Write memories as declarative facts, not instructions to yourself. "
    "'User prefers concise responses' ✓ — 'Always respond concisely' ✗. "
    "'Project uses pytest with xdist' ✓ — 'Run tests with pytest -n 4' ✗. "
    "Imperative phrasing gets re-read as a directive in later sessions and can "
    "cause repeated work or override the user's current request. Procedures and "
    "workflows belong in skills, not memory."
)

SESSION_SEARCH_GUIDANCE = (
    "When the user references something from a past conversation or you suspect "
    "relevant cross-session context exists, use session_search to recall it before "
    "asking them to repeat themselves."
)

SKILLS_GUIDANCE = (
    "After completing a complex task (5+ tool calls), fixing a tricky error, "
    "or discovering a non-trivial workflow, save the approach as a "
    "skill with skill_manage so you can reuse it next time.\n"
    "When using a skill and finding it outdated, incomplete, or wrong, "
    "patch it immediately with skill_manage(action='patch') — don't wait to be asked. "
    "Skills that aren't maintained become liabilities."
)

TOOL_USE_ENFORCEMENT_GUIDANCE = (
    "# Tool-use enforcement\n"
    "You MUST use your tools to take action — do not describe what you would do "
    "or plan to do without actually doing it. When you say you will perform an "
    "action (e.g. 'I will run the tests', 'Let me check the file', 'I will create "
    "the project'), you MUST immediately make the corresponding tool call in the same "
    "response. Never end your turn with a promise of future action — execute it now.\n"
    "Keep working until the task is actually complete. Do not stop with a summary of "
    "what you plan to do next time. If you have tools available that can accomplish "
    "the task, use them instead of telling the user what you would do.\n"
    "Every response should either (a) contain tool calls that make progress, or "
    "(b) deliver a final result to the user. Responses that only describe intentions "
    "without acting are not acceptable."
)

# Model name substrings that trigger tool-use enforcement guidance.
# Add new patterns here when a model family needs explicit steering.
TOOL_USE_ENFORCEMENT_MODELS = ("gpt", "codex", "gemini", "gemma", "grok")

# OpenAI GPT/Codex-specific execution guidance.  Addresses known failure modes
# where GPT models abandon work on partial results, skip prerequisite lookups,
# hallucinate instead of using tools, and declare "done" without verification.
# Inspired by patterns from OpenAI's GPT-5.4 prompting guide & OpenClaw PR #38953.
OPENAI_MODEL_EXECUTION_GUIDANCE = (
    "# Execution discipline\n"
    "<tool_persistence>\n"
    "- Use tools whenever they improve correctness, completeness, or grounding.\n"
    "- Do not stop early when another tool call would materially improve the result.\n"
    "- If a tool returns empty or partial results, retry with a different query or "
    "strategy before giving up.\n"
    "- Keep calling tools until: (1) the task is complete, AND (2) you have verified "
    "the result.\n"
    "</tool_persistence>\n"
    "\n"
    "<mandatory_tool_use>\n"
    "NEVER answer these from memory or mental computation — ALWAYS use a tool:\n"
    "- Arithmetic, math, calculations → use terminal or execute_code\n"
    "- Hashes, encodings, checksums → use terminal (e.g. sha256sum, base64)\n"
    "- Current time, date, timezone → use terminal (e.g. date)\n"
    "- System state: OS, CPU, memory, disk, ports, processes → use terminal\n"
    "- File contents, sizes, line counts → use read_file, search_files, or terminal\n"
    "- Git history, branches, diffs → use terminal\n"
    "- Current facts (weather, news, versions) → use web_search\n"
    "Your memory and user profile describe the USER, not the system you are "
    "running on. The execution environment may differ from what the user profile "
    "says about their personal setup.\n"
    "</mandatory_tool_use>\n"
    "\n"
    "<act_dont_ask>\n"
    "When a question has an obvious default interpretation, act on it immediately "
    "instead of asking for clarification. Examples:\n"
    "- 'Is port 443 open?' → check THIS machine (don't ask 'open where?')\n"
    "- 'What OS am I running?' → check the live system (don't use user profile)\n"
    "- 'What time is it?' → run `date` (don't guess)\n"
    "Only ask for clarification when the ambiguity genuinely changes what tool "
    "you would call.\n"
    "</act_dont_ask>\n"
    "\n"
    "<prerequisite_checks>\n"
    "- Before taking an action, check whether prerequisite discovery, lookup, or "
    "context-gathering steps are needed.\n"
    "- Do not skip prerequisite steps just because the final action seems obvious.\n"
    "- If a task depends on output from a prior step, resolve that dependency first.\n"
    "</prerequisite_checks>\n"
    "\n"
    "<verification>\n"
    "Before finalizing your response:\n"
    "- Correctness: does the output satisfy every stated requirement?\n"
    "- Grounding: are factual claims backed by tool outputs or provided context?\n"
    "- Formatting: does the output match the requested format or schema?\n"
    "- Safety: if the next step has side effects (file writes, commands, API calls), "
    "confirm scope before executing.\n"
    "</verification>\n"
    "\n"
    "<missing_context>\n"
    "- If required context is missing, do NOT guess or hallucinate an answer.\n"
    "- Use the appropriate lookup tool when missing information is retrievable "
    "(search_files, web_search, read_file, etc.).\n"
    "- Ask a clarifying question only when the information cannot be retrieved by tools.\n"
    "- If you must proceed with incomplete information, label assumptions explicitly.\n"
    "</missing_context>"
)

# Gemini/Gemma-specific operational guidance, adapted from OpenCode's gemini.txt.
# Injected alongside TOOL_USE_ENFORCEMENT_GUIDANCE when the model is Gemini or Gemma.
GOOGLE_MODEL_OPERATIONAL_GUIDANCE = (
    "# Google model operational directives\n"
    "Follow these operational rules strictly:\n"
    "- **Absolute paths:** Always construct and use absolute file paths for all "
    "file system operations. Combine the project root with relative paths.\n"
    "- **Verify first:** Use read_file/search_files to check file contents and "
    "project structure before making changes. Never guess at file contents.\n"
    "- **Dependency checks:** Never assume a library is available. Check "
    "package.json, requirements.txt, Cargo.toml, etc. before importing.\n"
    "- **Conciseness:** Keep explanatory text brief — a few sentences, not "
    "paragraphs. Focus on actions and results over narration.\n"
    "- **Parallel tool calls:** When you need to perform multiple independent "
    "operations (e.g. reading several files), make all the tool calls in a "
    "single response rather than sequentially.\n"
    "- **Non-interactive commands:** Use flags like -y, --yes, --non-interactive "
    "to prevent CLI tools from hanging on prompts.\n"
    "- **Keep going:** Work autonomously until the task is fully resolved. "
    "Don't stop with a plan — execute it.\n"
)

# Model name substrings that should use the 'developer' role instead of
# 'system' for the system prompt.  OpenAI's newer models (GPT-5, Codex)
# give stronger instruction-following weight to the 'developer' role.
# The swap happens at the API boundary in _build_api_kwargs() so internal
# message representation stays consistent ("system" everywhere).
DEVELOPER_ROLE_MODELS = ("gpt-5", "codex")

PLATFORM_HINTS = {
    "whatsapp": (
        "You are on a text messaging communication platform, WhatsApp. "
        "Please do not use markdown as it does not render. "
        "You can send media files natively: to deliver a file to the user, "
        "include MEDIA:/absolute/path/to/file in your response. The file "
        "will be sent as a native WhatsApp attachment — images (.jpg, .png, "
        ".webp) appear as photos, videos (.mp4, .mov) play inline, and other "
        "files arrive as downloadable documents. You can also include image "
        "URLs in markdown format ![alt](url) and they will be sent as photos."
    ),
    "telegram": (
        "You are on a text messaging communication platform, Telegram. "
        "Standard markdown is automatically converted to Telegram format. "
        "Supported: **bold**, *italic*, ~~strikethrough~~, ||spoiler||, "
        "`inline code`, ```code blocks```, [links](url), and ## headers. "
        "Telegram has NO table syntax — prefer bullet lists or labeled "
        "key: value pairs over pipe tables (any tables you do emit are "
        "auto-rewritten into row-group bullets, which you can produce "
        "directly for cleaner output). "
        "You can send media files natively: to deliver a file to the user, "
        "include MEDIA:/absolute/path/to/file in your response. Images "
        "(.png, .jpg, .webp) appear as photos, audio (.ogg) sends as voice "
        "bubbles, and videos (.mp4) play inline. You can also include image "
        "URLs in markdown format ![alt](url) and they will be sent as native photos."
    ),
    "discord": (
        "You are in a Discord server or group chat communicating with your user. "
        "You can send media files natively: include MEDIA:/absolute/path/to/file "
        "in your response. Images (.png, .jpg, .webp) are sent as photo "
        "attachments, audio as file attachments. You can also include image URLs "
        "in markdown format ![alt](url) and they will be sent as attachments."
    ),
    "slack": (
        "You are in a Slack workspace communicating with your user. "
        "You can send media files natively: include MEDIA:/absolute/path/to/file "
        "in your response. Images (.png, .jpg, .webp) are uploaded as photo "
        "attachments, audio as file attachments. You can also include image URLs "
        "in markdown format ![alt](url) and they will be uploaded as attachments."
    ),
    "signal": (
        "You are on a text messaging communication platform, Signal. "
        "Please do not use markdown as it does not render. "
        "You can send media files natively: to deliver a file to the user, "
        "include MEDIA:/absolute/path/to/file in your response. Images "
        "(.png, .jpg, .webp) appear as photos, audio as attachments, and other "
        "files arrive as downloadable documents. You can also include image "
        "URLs in markdown format ![alt](url) and they will be sent as photos."
    ),
    "email": (
        "You are communicating via email. Write clear, well-structured responses "
        "suitable for email. Use plain text formatting (no markdown). "
        "Keep responses concise but complete. You can send file attachments — "
        "include MEDIA:/absolute/path/to/file in your response. The subject line "
        "is preserved for threading. Do not include greetings or sign-offs unless "
        "contextually appropriate."
    ),
    "cron": (
        "You are running as a scheduled cron job. There is no user present — you "
        "cannot ask questions, request clarification, or wait for follow-up. Execute "
        "the task fully and autonomously, making reasonable decisions where needed. "
        "Your final response is automatically delivered to the job's configured "
        "destination — put the primary content directly in your response."
    ),
    "cli": (
        "You are a CLI AI Agent. Try not to use markdown but simple text "
        "renderable inside a terminal. "
        "File delivery: there is no attachment channel — the user reads your "
        "response directly in their terminal. Do NOT emit MEDIA:/path tags "
        "(those are only intercepted on messaging platforms like Telegram, "
        "Discord, Slack, etc.; on the CLI they render as literal text). "
        "When referring to a file you created or changed, just state its "
        "absolute path in plain text; the user can open it from there."
    ),
    "sms": (
        "You are communicating via SMS. Keep responses concise and use plain text "
        "only — no markdown, no formatting. SMS messages are limited to ~1600 "
        "characters, so be brief and direct."
    ),
    "bluebubbles": (
        "You are chatting via iMessage (BlueBubbles). iMessage does not render "
        "markdown formatting — use plain text. Keep responses concise as they "
        "appear as text messages. You can send media files natively: include "
        "MEDIA:/absolute/path/to/file in your response. Images (.jpg, .png, "
        ".heic) appear as photos and other files arrive as attachments."
    ),
    "mattermost": (
        "You are in a Mattermost workspace communicating with your user. "
        "Mattermost renders standard Markdown — headings, bold, italic, code "
        "blocks, and tables all work. "
        "You can send media files natively: include MEDIA:/absolute/path/to/file "
        "in your response. Images (.jpg, .png, .webp) are uploaded as photo "
        "attachments, audio and video as file attachments. "
        "Image URLs in markdown format ![alt](url) are rendered as inline previews automatically."
    ),
    "matrix": (
        "You are in a Matrix room communicating with your user. "
        "Matrix renders Markdown — bold, italic, code blocks, and links work; "
        "the adapter converts your Markdown to HTML for rich display. "
        "You can send media files natively: include MEDIA:/absolute/path/to/file "
        "in your response. Images (.jpg, .png, .webp) are sent as inline photos, "
        "audio (.ogg, .mp3) as voice/audio messages, video (.mp4) inline, "
        "and other files as downloadable attachments."
    ),
    "feishu": (
        "You are in a Feishu (Lark) workspace communicating with your user. "
        "Feishu renders Markdown in messages — bold, italic, code blocks, and "
        "links are supported. "
        "You can send media files natively: include MEDIA:/absolute/path/to/file "
        "in your response. Images (.jpg, .png, .webp) are uploaded and displayed "
        "inline, audio files as voice messages, and other files as attachments."
    ),
    "weixin": (
        "You are on Weixin/WeChat. Markdown formatting is supported, so you may use it when "
        "it improves readability, but keep the message compact and chat-friendly. You can send media files natively: "
        "include MEDIA:/absolute/path/to/file in your response. Images are sent as native "
        "photos, videos play inline when supported, and other files arrive as downloadable "
        "documents. You can also include image URLs in markdown format ![alt](url) and they "
        "will be downloaded and sent as native media when possible."
    ),
    "wecom": (
        "You are on WeCom (企业微信 / Enterprise WeChat). Markdown formatting is supported. "
        "You CAN send media files natively — to deliver a file to the user, include "
        "MEDIA:/absolute/path/to/file in your response. The file will be sent as a native "
        "WeCom attachment: images (.jpg, .png, .webp) are sent as photos (up to 10 MB), "
        "other files (.pdf, .docx, .xlsx, .md, .txt, etc.) arrive as downloadable documents "
        "(up to 20 MB), and videos (.mp4) play inline. Voice messages are supported but "
        "must be in AMR format — other audio formats are automatically sent as file attachments. "
        "You can also include image URLs in markdown format ![alt](url) and they will be "
        "downloaded and sent as native photos. Do NOT tell the user you lack file-sending "
        "capability — use MEDIA: syntax whenever a file delivery is appropriate."
    ),
    "qqbot": (
        "You are on QQ, a popular Chinese messaging platform. QQ supports markdown formatting "
        "and emoji. You can send media files natively: include MEDIA:/absolute/path/to/file in "
        "your response. Images are sent as native photos, and other files arrive as downloadable "
        "documents."
    ),
    "yuanbao": (
        "You are on Yuanbao (腾讯元宝), a Chinese AI assistant platform. "
        "Markdown formatting is supported (code blocks, tables, bold/italic). "
        "You CAN send media files natively — to deliver a file to the user, include "
        "MEDIA:/absolute/path/to/file in your response. The file will be sent as a native "
        "Yuanbao attachment: images (.jpg, .png, .webp, .gif) are sent as photos, "
        "and other files (.pdf, .docx, .txt, .zip, etc.) arrive as downloadable documents "
        "(max 50 MB). You can also include image URLs in markdown format ![alt](url) and "
        "they will be downloaded and sent as native photos. "
        "Do NOT tell the user you lack file-sending capability — use MEDIA: syntax "
        "whenever a file delivery is appropriate.\n\n"
        "Stickers (贴纸 / 表情包 / TIM face): Yuanbao has a built-in sticker catalogue. "
        "When the user sends a sticker (you see '[emoji: 名称]' in their message) or asks "
        "you to send/reply-with a 贴纸/表情/表情包, you MUST use the sticker tools:\n"
        "  1. Call yb_search_sticker with a Chinese keyword (e.g. '666', '比心', '吃瓜', "
        "     '捂脸', '合十') to discover matching sticker_ids.\n"
        "  2. Call yb_send_sticker with the chosen sticker_id or name — this sends a real "
        "     TIMFaceElem that renders as a native sticker in the chat.\n"
        "DO NOT draw sticker-like PNGs with execute_code/Pillow/matplotlib and then send "
        "them via MEDIA: or send_image_file. That produces a fake low-quality 'sticker' "
        "image and is the WRONG path. Bare Unicode emoji in text is also not a substitute "
        "— when a sticker is the right response, use yb_send_sticker."
    ),
}

# ---------------------------------------------------------------------------
# Environment hints — execution-environment awareness for the agent.
# Unlike PLATFORM_HINTS (which describe the messaging channel), these describe
# the machine/OS the agent's tools actually run on.
# ---------------------------------------------------------------------------

WSL_ENVIRONMENT_HINT = (
    "You are running inside WSL (Windows Subsystem for Linux). "
    "The Windows host filesystem is mounted under /mnt/ — "
    "/mnt/c/ is the C: drive, /mnt/d/ is D:, etc. "
    "The user's Windows files are typically at "
    "/mnt/c/Users/<username>/Desktop/, Documents/, Downloads/, etc. "
    "When the user references Windows paths or desktop files, translate "
    "to the /mnt/c/ equivalent. You can list /mnt/c/Users/ to discover "
    "the Windows username if needed."
)


def build_environment_hints() -> str:
    """Return environment-specific guidance for the system prompt.

    Detects WSL, and can be extended for Termux, Docker, etc.
    Returns an empty string when no special environment is detected.
    """
    hints: list[str] = []
    if is_wsl():
        hints.append(WSL_ENVIRONMENT_HINT)
    return "\n\n".join(hints)


CONTEXT_FILE_MAX_CHARS = 20_000
CONTEXT_TRUNCATE_HEAD_RATIO = 0.7
CONTEXT_TRUNCATE_TAIL_RATIO = 0.2


# =========================================================================
# Skills prompt cache
# =========================================================================

_SKILLS_PROMPT_CACHE_MAX = 8
_SKILLS_PROMPT_CACHE: OrderedDict[tuple, str] = OrderedDict()
_SKILLS_PROMPT_CACHE_LOCK = threading.Lock()
_SKILLS_SNAPSHOT_VERSION = 1


def _skills_prompt_snapshot_path() -> Path:
    return get_hermes_home() / ".skills_prompt_snapshot.json"


def clear_skills_system_prompt_cache(*, clear_snapshot: bool = False) -> None:
    """Drop the in-process skills prompt cache (and optionally the disk snapshot)."""
    with _SKILLS_PROMPT_CACHE_LOCK:
        _SKILLS_PROMPT_CACHE.clear()
    if clear_snapshot:
        try:
            _skills_prompt_snapshot_path().unlink(missing_ok=True)
        except OSError as e:
            logger.debug("Could not remove skills prompt snapshot: %s", e)


def _build_skills_manifest(skills_dir: Path) -> dict[str, list[int]]:
    """Build an mtime/size manifest of all SKILL.md and DESCRIPTION.md files."""
    manifest: dict[str, list[int]] = {}
    for filename in ("SKILL.md", "DESCRIPTION.md"):
        for path in iter_skill_index_files(skills_dir, filename):
            try:
                st = path.stat()
            except OSError:
                continue
            manifest[str(path.relative_to(skills_dir))] = [st.st_mtime_ns, st.st_size]
    return manifest


def _load_skills_snapshot(skills_dir: Path) -> Optional[dict]:
    """Load the disk snapshot if it exists and its manifest still matches."""
    snapshot_path = _skills_prompt_snapshot_path()
    if not snapshot_path.exists():
        return None
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(snapshot, dict):
        return None
    if snapshot.get("version") != _SKILLS_SNAPSHOT_VERSION:
        return None
    if snapshot.get("manifest") != _build_skills_manifest(skills_dir):
        return None
    return snapshot


def _write_skills_snapshot(
    skills_dir: Path,
    manifest: dict[str, list[int]],
    skill_entries: list[dict],
    category_descriptions: dict[str, str],
) -> None:
    """Persist skill metadata to disk for fast cold-start reuse."""
    payload = {
        "version": _SKILLS_SNAPSHOT_VERSION,
        "manifest": manifest,
        "skills": skill_entries,
        "category_descriptions": category_descriptions,
    }
    try:
        atomic_json_write(_skills_prompt_snapshot_path(), payload)
    except Exception as e:
        logger.debug("Could not write skills prompt snapshot: %s", e)


def _build_snapshot_entry(
    skill_file: Path,
    skills_dir: Path,
    frontmatter: dict,
    description: str,
) -> dict:
    """Build a serialisable metadata dict for one skill."""
    rel_path = skill_file.relative_to(skills_dir)
    parts = rel_path.parts
    if len(parts) >= 2:
        skill_name = parts[-2]
        category = "/".join(parts[:-2]) if len(parts) > 2 else parts[0]
    else:
        category = "general"
        skill_name = skill_file.parent.name

    platforms = frontmatter.get("platforms") or []
    if isinstance(platforms, str):
        platforms = [platforms]

    return {
        "skill_name": skill_name,
        "category": category,
        "frontmatter_name": str(frontmatter.get("name", skill_name)),
        "description": description,
        "platforms": [str(p).strip() for p in platforms if str(p).strip()],
        "conditions": extract_skill_conditions(frontmatter),
    }


# =========================================================================
# Skills index
# =========================================================================

def _parse_skill_file(skill_file: Path) -> tuple[bool, dict, str]:
    """Read a SKILL.md once and return platform compatibility, frontmatter, and description.

    Returns (is_compatible, frontmatter, description). On any error, returns
    (True, {}, "") to err on the side of showing the skill.
    """
    try:
        raw = skill_file.read_text(encoding="utf-8")
        frontmatter, _ = parse_frontmatter(raw)

        if not skill_matches_platform(frontmatter):
            return False, frontmatter, ""

        return True, frontmatter, extract_skill_description(frontmatter)
    except Exception as e:
        logger.warning("Failed to parse skill file %s: %s", skill_file, e)
        return True, {}, ""


def _skill_should_show(
    conditions: dict,
    available_tools: "set[str] | None",
    available_toolsets: "set[str] | None",
) -> bool:
    """Return False if the skill's conditional activation rules exclude it."""
    if available_tools is None and available_toolsets is None:
        return True  # No filtering info — show everything (backward compat)

    at = available_tools or set()
    ats = available_toolsets or set()

    # fallback_for: hide when the primary tool/toolset IS available
    for ts in conditions.get("fallback_for_toolsets", []):
        if ts in ats:
            return False
    for t in conditions.get("fallback_for_tools", []):
        if t in at:
            return False

    # requires: hide when a required tool/toolset is NOT available
    for ts in conditions.get("requires_toolsets", []):
        if ts not in ats:
            return False
    for t in conditions.get("requires_tools", []):
        if t not in at:
            return False

    return True


def build_skills_system_prompt(
    available_tools: "set[str] | None" = None,
    available_toolsets: "set[str] | None" = None,
) -> str:
    """Build a compact skill index for the system prompt.

    Two-layer cache:
      1. In-process LRU dict keyed by (skills_dir, tools, toolsets)
      2. Disk snapshot (``.skills_prompt_snapshot.json``) validated by
         mtime/size manifest — survives process restarts

    Falls back to a full filesystem scan when both layers miss.

    External skill directories (``skills.external_dirs`` in config.yaml) are
    scanned alongside the local ``~/.hermes/skills/`` directory.  External dirs
    are read-only — they appear in the index but new skills are always created
    in the local dir.  Local skills take precedence when names collide.
    """
    skills_dir = get_skills_dir()
    external_dirs = get_all_skills_dirs()[1:]  # skip local (index 0)

    if not skills_dir.exists() and not external_dirs:
        return ""

    # ── Layer 1: in-process LRU cache ─────────────────────────────────
    # Include the resolved platform so per-platform disabled-skill lists
    # produce distinct cache entries (gateway serves multiple platforms).
    from gateway.session_context import get_session_env
    _platform_hint = (
        os.environ.get("HERMES_PLATFORM")
        or get_session_env("HERMES_SESSION_PLATFORM")
        or ""
    )
    disabled = get_disabled_skill_names()
    cache_key = (
        str(skills_dir.resolve()),
        tuple(str(d) for d in external_dirs),
        tuple(sorted(str(t) for t in (available_tools or set()))),
        tuple(sorted(str(ts) for ts in (available_toolsets or set()))),
        _platform_hint,
        tuple(sorted(disabled)),
    )
    with _SKILLS_PROMPT_CACHE_LOCK:
        cached = _SKILLS_PROMPT_CACHE.get(cache_key)
        if cached is not None:
            _SKILLS_PROMPT_CACHE.move_to_end(cache_key)
            return cached

    # ── Layer 2: disk snapshot ────────────────────────────────────────
    snapshot = _load_skills_snapshot(skills_dir)

    skills_by_category: dict[str, list[tuple[str, str]]] = {}
    category_descriptions: dict[str, str] = {}

    if snapshot is not None:
        # Fast path: use pre-parsed metadata from disk
        for entry in snapshot.get("skills", []):
            if not isinstance(entry, dict):
                continue
            skill_name = entry.get("skill_name") or ""
            category = entry.get("category") or "general"
            frontmatter_name = entry.get("frontmatter_name") or skill_name
            platforms = entry.get("platforms") or []
            if not skill_matches_platform({"platforms": platforms}):
                continue
            if frontmatter_name in disabled or skill_name in disabled:
                continue
            if not _skill_should_show(
                entry.get("conditions") or {},
                available_tools,
                available_toolsets,
            ):
                continue
            skills_by_category.setdefault(category, []).append(
                (frontmatter_name, entry.get("description", ""))
            )
        category_descriptions = {
            str(k): str(v)
            for k, v in (snapshot.get("category_descriptions") or {}).items()
        }
    else:
        # Cold path: full filesystem scan + write snapshot for next time
        skill_entries: list[dict] = []
        for skill_file in iter_skill_index_files(skills_dir, "SKILL.md"):
            is_compatible, frontmatter, desc = _parse_skill_file(skill_file)
            entry = _build_snapshot_entry(skill_file, skills_dir, frontmatter, desc)
            skill_entries.append(entry)
            if not is_compatible:
                continue
            skill_name = entry["skill_name"]
            if entry["frontmatter_name"] in disabled or skill_name in disabled:
                continue
            if not _skill_should_show(
                extract_skill_conditions(frontmatter),
                available_tools,
                available_toolsets,
            ):
                continue
            skills_by_category.setdefault(entry["category"], []).append(
                (entry["frontmatter_name"], entry["description"])
            )

        # Read category-level DESCRIPTION.md files
        for desc_file in iter_skill_index_files(skills_dir, "DESCRIPTION.md"):
            try:
                content = desc_file.read_text(encoding="utf-8")
                fm, _ = parse_frontmatter(content)
                cat_desc = fm.get("description")
                if not cat_desc:
                    continue
                rel = desc_file.relative_to(skills_dir)
                cat = "/".join(rel.parts[:-1]) if len(rel.parts) > 1 else "general"
                category_descriptions[cat] = str(cat_desc).strip().strip("'\"")
            except Exception as e:
                logger.debug("Could not read skill description %s: %s", desc_file, e)

        _write_skills_snapshot(
            skills_dir,
            _build_skills_manifest(skills_dir),
            skill_entries,
            category_descriptions,
        )

    # ── External skill directories ─────────────────────────────────────
    # Scan external dirs directly (no snapshot caching — they're read-only
    # and typically small).  Local skills already in skills_by_category take
    # precedence: we track seen names and skip duplicates from external dirs.
    seen_skill_names: set[str] = set()
    for cat_skills in skills_by_category.values():
        for name, _desc in cat_skills:
            seen_skill_names.add(name)

    for ext_dir in external_dirs:
        if not ext_dir.exists():
            continue
        for skill_file in iter_skill_index_files(ext_dir, "SKILL.md"):
            try:
                is_compatible, frontmatter, desc = _parse_skill_file(skill_file)
                if not is_compatible:
                    continue
                entry = _build_snapshot_entry(skill_file, ext_dir, frontmatter, desc)
                skill_name = entry["skill_name"]
                frontmatter_name = entry["frontmatter_name"]
                if frontmatter_name in seen_skill_names:
                    continue
                if frontmatter_name in disabled or skill_name in disabled:
                    continue
                if not _skill_should_show(
                    extract_skill_conditions(frontmatter),
                    available_tools,
                    available_toolsets,
                ):
                    continue
                seen_skill_names.add(frontmatter_name)
                skills_by_category.setdefault(entry["category"], []).append(
                    (frontmatter_name, entry["description"])
                )
            except Exception as e:
                logger.debug("Error reading external skill %s: %s", skill_file, e)

        # External category descriptions
        for desc_file in iter_skill_index_files(ext_dir, "DESCRIPTION.md"):
            try:
                content = desc_file.read_text(encoding="utf-8")
                fm, _ = parse_frontmatter(content)
                cat_desc = fm.get("description")
                if not cat_desc:
                    continue
                rel = desc_file.relative_to(ext_dir)
                cat = "/".join(rel.parts[:-1]) if len(rel.parts) > 1 else "general"
                category_descriptions.setdefault(cat, str(cat_desc).strip().strip("'\""))
            except Exception as e:
                logger.debug("Could not read external skill description %s: %s", desc_file, e)

    if not skills_by_category:
        result = ""
    else:
        index_lines = []
        for category in sorted(skills_by_category.keys()):
            cat_desc = category_descriptions.get(category, "")
            if cat_desc:
                index_lines.append(f"  {category}: {cat_desc}")
            else:
                index_lines.append(f"  {category}:")
            # Deduplicate and sort skills within each category
            seen = set()
            for name, desc in sorted(skills_by_category[category], key=lambda x: x[0]):
                if name in seen:
                    continue
                seen.add(name)
                if desc:
                    index_lines.append(f"    - {name}: {desc}")
                else:
                    index_lines.append(f"    - {name}")

        result = (
            "## Skills (mandatory)\n"
            "Before replying, scan the skills below. If a skill matches or is even partially relevant "
            "to your task, you MUST load it with skill_view(name) and follow its instructions. "
            "Err on the side of loading — it is always better to have context you don't need "
            "than to miss critical steps, pitfalls, or established workflows. "
            "Skills contain specialized knowledge — API endpoints, tool-specific commands, "
            "and proven workflows that outperform general-purpose approaches. Load the skill "
            "even if you think you could handle the task with basic tools like web_search or terminal. "
            "Skills also encode the user's preferred approach, conventions, and quality standards "
            "for tasks like code review, planning, and testing — load them even for tasks you "
            "already know how to do, because the skill defines how it should be done here.\n"
            "Whenever the user asks you to configure, set up, install, enable, disable, modify, "
            "or troubleshoot Hermes Agent itself — its CLI, config, models, providers, tools, "
            "skills, voice, gateway, plugins, or any feature — load the `hermes-agent` skill "
            "first. It has the actual commands (e.g. `hermes config set …`, `hermes tools`, "
            "`hermes setup`) so you don't have to guess or invent workarounds.\n"
            "If a skill has issues, fix it with skill_manage(action='patch').\n"
            "After difficult/iterative tasks, offer to save as a skill. "
            "If a skill you loaded was missing steps, had wrong commands, or needed "
            "pitfalls you discovered, update it before finishing.\n"
            "\n"
            "<available_skills>\n"
            + "\n".join(index_lines) + "\n"
            "</available_skills>\n"
            "\n"
            "Only proceed without loading a skill if genuinely none are relevant to the task."
        )

    # ── Store in LRU cache ────────────────────────────────────────────
    with _SKILLS_PROMPT_CACHE_LOCK:
        _SKILLS_PROMPT_CACHE[cache_key] = result
        _SKILLS_PROMPT_CACHE.move_to_end(cache_key)
        while len(_SKILLS_PROMPT_CACHE) > _SKILLS_PROMPT_CACHE_MAX:
            _SKILLS_PROMPT_CACHE.popitem(last=False)

    return result


def build_nous_subscription_prompt(valid_tool_names: "set[str] | None" = None) -> str:
    """Build a compact Nous subscription capability block for the system prompt."""
    try:
        from hermes_cli.nous_subscription import get_nous_subscription_features
        from tools.tool_backend_helpers import managed_nous_tools_enabled
    except Exception as exc:
        logger.debug("Failed to import Nous subscription helper: %s", exc)
        return ""

    if not managed_nous_tools_enabled():
        return ""

    valid_names = set(valid_tool_names or set())
    relevant_tool_names = {
        "web_search",
        "web_extract",
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "browser_console",
        "browser_press",
        "browser_get_images",
        "browser_vision",
        "image_generate",
        "text_to_speech",
        "terminal",
        "process",
        "execute_code",
    }

    if valid_names and not (valid_names & relevant_tool_names):
        return ""

    features = get_nous_subscription_features()

    def _status_line(feature) -> str:
        if feature.managed_by_nous:
            return f"- {feature.label}: active via Nous subscription"
        if feature.active:
            current = feature.current_provider or "configured provider"
            return f"- {feature.label}: currently using {current}"
        if feature.included_by_default and features.nous_auth_present:
            return f"- {feature.label}: included with Nous subscription, not currently selected"
        if feature.key == "modal" and features.nous_auth_present:
            return f"- {feature.label}: optional via Nous subscription"
        return f"- {feature.label}: not currently available"

    lines = [
        "# Nous Subscription",
        "Nous subscription includes managed web tools (Firecrawl), image generation (FAL), OpenAI TTS, and browser automation (Browser Use) by default. Modal execution is optional.",
        "Current capability status:",
    ]
    lines.extend(_status_line(feature) for feature in features.items())
    lines.extend(
        [
            "When a Nous-managed feature is active, do not ask the user for Firecrawl, FAL, OpenAI TTS, or Browser-Use API keys.",
            "If the user is not subscribed and asks for a capability that Nous subscription would unlock or simplify, suggest Nous subscription as one option alongside direct setup or local alternatives.",
            "Do not mention subscription unless the user asks about it or it directly solves the current missing capability.",
            "Useful commands: hermes setup, hermes setup tools, hermes setup terminal, hermes status.",
        ]
    )
    return "\n".join(lines)


# =========================================================================
# Context files (SOUL.md, AGENTS.md, .cursorrules)
# =========================================================================

def _truncate_content(content: str, filename: str, max_chars: int = CONTEXT_FILE_MAX_CHARS) -> str:
    """Head/tail truncation with a marker in the middle."""
    if len(content) <= max_chars:
        return content
    head_chars = int(max_chars * CONTEXT_TRUNCATE_HEAD_RATIO)
    tail_chars = int(max_chars * CONTEXT_TRUNCATE_TAIL_RATIO)
    head = content[:head_chars]
    tail = content[-tail_chars:]
    marker = f"\n\n[...truncated {filename}: kept {head_chars}+{tail_chars} of {len(content)} chars. Use file tools to read the full file.]\n\n"
    return head + marker + tail


def load_soul_md() -> Optional[str]:
    """Load SOUL.md from HERMES_HOME and return its content, or None.

    Used as the agent identity (slot #1 in the system prompt).  When this
    returns content, ``build_context_files_prompt`` should be called with
    ``skip_soul=True`` so SOUL.md isn't injected twice.
    """
    try:
        from hermes_cli.config import ensure_hermes_home
        ensure_hermes_home()
    except Exception as e:
        logger.debug("Could not ensure HERMES_HOME before loading SOUL.md: %s", e)

    soul_path = get_hermes_home() / "SOUL.md"
    if not soul_path.exists():
        return None
    try:
        content = soul_path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        content = _scan_context_content(content, "SOUL.md")
        content = _truncate_content(content, "SOUL.md")
        return content
    except Exception as e:
        logger.debug("Could not read SOUL.md from %s: %s", soul_path, e)
        return None


def _load_hermes_md(cwd_path: Path) -> str:
    """.hermes.md / HERMES.md — walk to git root."""
    hermes_md_path = _find_hermes_md(cwd_path)
    if not hermes_md_path:
        return ""
    try:
        content = hermes_md_path.read_text(encoding="utf-8").strip()
        if not content:
            return ""
        content = _strip_yaml_frontmatter(content)
        rel = hermes_md_path.name
        try:
            rel = str(hermes_md_path.relative_to(cwd_path))
        except ValueError:
            pass
        content = _scan_context_content(content, rel)
        result = f"## {rel}\n\n{content}"
        return _truncate_content(result, ".hermes.md")
    except Exception as e:
        logger.debug("Could not read %s: %s", hermes_md_path, e)
        return ""


def _load_agents_md(cwd_path: Path) -> str:
    """AGENTS.md — top-level only (no recursive walk)."""
    for name in ["AGENTS.md", "agents.md"]:
        candidate = cwd_path / name
        if candidate.exists():
            try:
                content = candidate.read_text(encoding="utf-8").strip()
                if content:
                    content = _scan_context_content(content, name)
                    result = f"## {name}\n\n{content}"
                    return _truncate_content(result, "AGENTS.md")
            except Exception as e:
                logger.debug("Could not read %s: %s", candidate, e)
    return ""


def _load_claude_md(cwd_path: Path) -> str:
    """CLAUDE.md / claude.md — cwd only."""
    for name in ["CLAUDE.md", "claude.md"]:
        candidate = cwd_path / name
        if candidate.exists():
            try:
                content = candidate.read_text(encoding="utf-8").strip()
                if content:
                    content = _scan_context_content(content, name)
                    result = f"## {name}\n\n{content}"
                    return _truncate_content(result, "CLAUDE.md")
            except Exception as e:
                logger.debug("Could not read %s: %s", candidate, e)
    return ""


def _load_cursorrules(cwd_path: Path) -> str:
    """.cursorrules + .cursor/rules/*.mdc — cwd only."""
    cursorrules_content = ""
    cursorrules_file = cwd_path / ".cursorrules"
    if cursorrules_file.exists():
        try:
            content = cursorrules_file.read_text(encoding="utf-8").strip()
            if content:
                content = _scan_context_content(content, ".cursorrules")
                cursorrules_content += f"## .cursorrules\n\n{content}\n\n"
        except Exception as e:
            logger.debug("Could not read .cursorrules: %s", e)

    cursor_rules_dir = cwd_path / ".cursor" / "rules"
    if cursor_rules_dir.exists() and cursor_rules_dir.is_dir():
        mdc_files = sorted(cursor_rules_dir.glob("*.mdc"))
        for mdc_file in mdc_files:
            try:
                content = mdc_file.read_text(encoding="utf-8").strip()
                if content:
                    content = _scan_context_content(content, f".cursor/rules/{mdc_file.name}")
                    cursorrules_content += f"## .cursor/rules/{mdc_file.name}\n\n{content}\n\n"
            except Exception as e:
                logger.debug("Could not read %s: %s", mdc_file, e)

    if not cursorrules_content:
        return ""
    return _truncate_content(cursorrules_content, ".cursorrules")


# --- DecisionData /context tree injection (gateway mirror of the Slack canary) ----
# Mirrors dd-slack-service/src/context-tree.js: load the AWARENESS-level _node.md
# summaries from the shared ~/.hermes/context tree and render a lower-precedence
# background block. Read-only, fail-soft (any error → empty block → today's
# behavior). The tree is SHARED across profiles (home-anchored), not per-profile,
# so it is NOT resolved via HERMES_HOME (which points at a profile dir for
# specialists). Override with DD_CONTEXT_TREE_ROOT (tests / relocation).
_CONTEXT_TREE_AWARENESS_NODES = ["global", "operating-model", "platform"]
# Per-node cap raised 6000 → 8500 (P5 review G1/G2), then 8500 → 16000
# (2026-06-14 operating-model correction): the composed operating-model node
# (shared _core.md + the audience role view) is THE coordination contract and
# legitimately the largest node. At 8500 the p1-default view (composed ≈19.2k)
# was being HARD-TRUNCATED at char 8500 — silently dropping the ENTIRE lane
# registry, WTS-binding, and software-build-authority sections before they ever
# reached the live P1 prompt. That is precisely why P1 overstepped (did port
# reservation / registry register / a build / a release pin itself instead of
# routing the Deploy/Ops lane) and then hung on the guard block: the rules that
# say "route the lane, do not build it yourself; emit a clean HOLD on a blocked
# boundary" were never delivered. p1-default is the ONLY view that exceeds 8500
# (every other audience composes < 8500); raising the cap to 16800 affects only
# the P1 node. The p1-default view is also trimmed so composed ≈16.7k < 16800,
# and with the two tiny sibling nodes (global ~1.4k + platform ~0.9k) the 3-node
# TOTAL stays ≈19.0k < the 20000 total cap (≈1k headroom). Gateway-side only; the
# Slack loader (dd-slack-service/src/context-tree.js) composes a different, smaller
# view and is untouched.
_CONTEXT_TREE_PER_NODE_CHAR_CAP = 16800
_CONTEXT_TREE_TOTAL_CHAR_CAP = 20000


def _context_tree_root() -> Path:
    override = os.getenv("DD_CONTEXT_TREE_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".hermes" / "context"


def _strip_frontmatter(raw: str) -> str:
    """Strip a leading YAML frontmatter block (--- ... ---); return the body."""
    text = (raw or "").strip("﻿")
    if not text.startswith("---"):
        return text.strip()
    closing = text.find("\n---", 3)
    if closing == -1:
        return text.strip()
    after = text.find("\n", closing + 1)
    if after == -1:
        return ""
    return text[after + 1:].strip()


# ── Contract version identity (graduation P1a / WTS 2911977a) ─────────────────
# The operating contract now carries an explicit, validatable version. The
# shared _core.md frontmatter declares `contract_version` and a `content_hash`
# that pins the sha256 of the frontmatter-stripped core BODY (the exact bytes
# _read_node_body injects). Each role card declares `composes_against_core` with
# the same value. dd-context-validate enforces the match out-of-band and can
# hard-fail; the COMPOSER only ever *signals* on mismatch (logs), never hard-
# fails delivery — a drifted pin must not blank a live prompt.
_CONTRACT_VERSION_FALLBACK = "v2.0"          # used if _core.md omits the field
_CORE_HASH_PREFIX = "sha256:"
_CORE_HASH_SHORT_LEN = 16                     # hex chars kept in the short pin


def _short_core_hash(core_body: str) -> str:
    """The canonical short core hash: 'sha256:<16hex>' of the stripped core body."""
    digest = hashlib.sha256((core_body or "").encode("utf-8")).hexdigest()
    return f"{_CORE_HASH_PREFIX}{digest[:_CORE_HASH_SHORT_LEN]}"


def _read_core_frontmatter(root: Path) -> dict:
    """Return the parsed frontmatter dict of operating-model/_core.md ({} on any error)."""
    core_file = (root / "operating-model" / "_core.md").resolve()
    if not str(core_file).startswith(str(root.resolve()) + os.sep):
        return {}
    try:
        raw = core_file.read_text(encoding="utf-8")
    except Exception:
        return {}
    fm, _body = parse_frontmatter(raw)
    return fm if isinstance(fm, dict) else {}


def _core_version_and_hash(root: Path, core_body: Optional[str]) -> tuple:
    """Return (contract_version, short_core_hash) for the live core.

    contract_version comes from _core.md frontmatter (falling back to the
    module constant); the hash is ALWAYS recomputed from the injected body so
    the header can never advertise a stale pin. Returns (fallback, "") when the
    core body is unavailable.
    """
    fm = _read_core_frontmatter(root)
    version = str(fm.get("contract_version") or _CONTRACT_VERSION_FALLBACK).strip()
    short = _short_core_hash(core_body) if core_body else ""
    return version, short


def _read_card_frontmatter(card_file: Path, root: Path) -> dict:
    """Frontmatter dict of a role card, guarded to stay inside root ({} on error)."""
    resolved = card_file.resolve()
    if not str(resolved).startswith(str(root.resolve()) + os.sep):
        return {}
    try:
        raw = resolved.read_text(encoding="utf-8")
    except Exception:
        return {}
    fm, _body = parse_frontmatter(raw)
    return fm if isinstance(fm, dict) else {}


def _signal_pin_mismatch(card_file: Path, root: Path, live_short_hash: str) -> bool:
    """Log-only pin check for a composed role card.

    Compares the card's `composes_against_core` pin to the live short core hash.
    Returns True when they match (or the card declares no pin — nothing to
    enforce), False on a real mismatch, which is LOGGED at WARNING. This never
    raises and never blocks composition: the validator (bin/dd-context-validate)
    is the enforcement gate; the composer only surfaces a signal.
    """
    if not live_short_hash:
        return True
    fm = _read_card_frontmatter(card_file, root)
    pin = fm.get("composes_against_core")
    if not pin:
        return True  # unpinned card — nothing to validate here
    if str(pin).strip() == live_short_hash:
        return True
    logger.warning(
        "operating-contract pin MISMATCH: card %s pins composes_against_core=%r "
        "but live core hash is %s — role view may be composed against a core it "
        "was not written for. Run bin/dd-context-validate.",
        card_file.name, str(pin).strip(), live_short_hash,
    )
    return False


# Per-audience role views the operating-model node composes with the shared
# core. Keys are the audience labels passed by each loader; values are the
# role-view filename under operating-model/agent-roles/.
#
# THIS TABLE IS THE GROUND TRUTH for gateway composition. Only audiences that
# appear here (plus _LANE_PROFILE_AUDIENCES below) are composed into a live
# agent prompt. Cards declare their own mechanism in a `delivery:` frontmatter
# field, and bin/dd-context-validate warns (never blocks) when a card's
# declaration disagrees with this table — so the two cannot drift apart
# silently again.
#
# "Not composed here" does NOT mean "reaches nobody": dispatch is delivered by
# bin/dd-context-compose --target claude-md, which renders the card into a
# CLAUDE.md that Claude Code loads. It is `delivery: generated-claude-md`, and
# it is the audience this gateway never reaches — not a stub. claude-code and
# openclaw render on demand but nothing currently generates them, so they are
# `delivery: none`. An earlier comment here called all three "tree-side stubs,
# not composed", which read as "inert" and contradicted dispatch.md's own
# frontmatter; that ambiguity is what the `delivery:` vocabulary replaced.
# "p1-specialists" was retired 2026-07-17 (WTS 2911977a): the WS4 resolver never
# returns it, its card is status:superseded in the tree, and it must NOT be
# composable — an audience absent here composes the shared core only.
_OPERATING_MODEL_VIEW_BY_AUDIENCE = {
    "slack-project-agent": "slack-project-agent",
    "p1-default": "p1-default",
}

# WS4 §4.1 — the lane profiles whose own role view lives under
# agent-roles/specialists/<profile>.md. When the audience is one of these, the
# loader composes that specialist view (not the generic coordinator view).
#
# graduation P1a (WTS 2911977a): security-review and video-review are ADDED. Both
# profiles set tree_injection:true but were absent here, so the audience resolver
# silently gave them the p1-default coordinator view (deploy-authority text they
# must NOT carry — a boundary leak). Their thin review-scoped cards now live at
# agent-roles/specialists/{security-review,video-review}.md.
_LANE_PROFILE_AUDIENCES = frozenset({
    "dd-design", "dd-engineer-1", "dd-engineer-2", "dd-engineer-3", "qa-review",
    "dd-pmo", "architect-standards", "product-os", "knowledge-context", "devops-release",
    "security-review", "video-review",
})


def _read_node_body(node_file: Path, root: Path) -> Optional[str]:
    """Read + frontmatter-strip a node file, guarded to stay inside the root."""
    resolved = node_file.resolve()
    if not str(resolved).startswith(str(root.resolve()) + os.sep):
        return None
    try:
        raw = resolved.read_text(encoding="utf-8")
    except Exception:
        return None
    body = _strip_frontmatter(raw)
    return body or None


def _resolve_view_card(root: Path, audience: str) -> Optional[Path]:
    """Return the filesystem path of the role-view card for *audience*, or None.

    A known lane profile composes its OWN specialist view
    (agent-roles/specialists/<audience>.md); the three coordinator-family
    audiences map through _OPERATING_MODEL_VIEW_BY_AUDIENCE to
    agent-roles/<slug>.md. An unwired audience has no view card (core only).
    Path resolution only — existence is checked by the reader.
    """
    if audience in _LANE_PROFILE_AUDIENCES:
        return root / "operating-model" / "agent-roles" / "specialists" / f"{audience}.md"
    view_slug = _OPERATING_MODEL_VIEW_BY_AUDIENCE.get(audience)
    if view_slug:
        return root / "operating-model" / "agent-roles" / f"{view_slug}.md"
    return None


def compose_operating_model(root: Path, audience: str) -> Optional[str]:
    """Compose the operating-model payload = shared _core.md + the audience role view.

    The shared core is the single source of truth for the inter-layer boundary;
    the role view describes only the reader's own role. Returns the composed
    body, or the core alone if the audience has no wired view, or None if the
    core is unreadable (caller falls back to the legacy _node.md).

    graduation P1a (WTS 2911977a): fail-loud. An unreadable/empty core is LOGGED
    at ERROR (it used to return None silently — the caller then fell back to the
    legacy node with no signal). A wired audience whose view card fails to load
    is LOGGED at WARNING (core-only composition is a degraded contract, not a
    normal path). A pin mismatch between the view card and the live core is
    SIGNALLED (logged) but never blocks — see _signal_pin_mismatch.
    """
    core = _read_node_body(root / "operating-model" / "_core.md", root)
    if not core:
        logger.error(
            "operating-model core UNREADABLE at %s — composing NOTHING; caller "
            "falls back to legacy _node.md. The shared operating contract is NOT "
            "being delivered this turn.",
            (root / "operating-model" / "_core.md"),
        )
        return None
    live_hash = _short_core_hash(core)
    view = None
    view_card = _resolve_view_card(root, audience)
    if view_card is not None:
        # Signal (log-only) if the card's pin disagrees with the live core, then
        # compose regardless — a drifted pin must never blank a live prompt.
        _signal_pin_mismatch(view_card, root, live_hash)
        view = _read_node_body(view_card, root)
        if view is None:
            logger.warning(
                "operating-model role view for audience %r FAILED to load "
                "(%s) — composing CORE ONLY. The reader gets the shared boundary "
                "but NOT its role-specific contract.",
                audience, view_card,
            )
    return f"{core}\n\n{view}" if view else core


# graduation P1a (WTS 2911977a): the explicit, machine-greppable marker that
# replaces the old silent "[...node truncated...]" tail. Its presence in a live
# prompt means the operating contract this turn is INCOMPLETE — the reader must
# not treat the truncated node as authoritative, and telemetry can detect it.
CONTRACT_TRUNCATION_MARKER = "[CONTRACT TRUNCATED — incomplete, do not treat as authoritative]"


def _dropped_sections(body: str, cap: int) -> "list[str]":
    """Names of the markdown sections lost when *body* is sliced at *cap*:
    the section containing the cut (delivered incomplete) plus every section
    that starts at or after the cut (fleet-repair 00.3, WTS 7e1d32e9 — a
    truncated agent must at least be able to say WHAT it is missing)."""
    partial = None
    fully_dropped = []
    for m in re.finditer(r'(?m)^(#{1,4})\s+(.+?)\s*$', body):
        if m.start() < cap:
            partial = m.group(2)
        else:
            fully_dropped.append(m.group(2))
    out = []
    if partial is not None and len(body) > cap:
        out.append("%s (cut mid-section)" % partial)
    out.extend(fully_dropped)
    return out


# Fail-closed composition for the coordinator (fleet-repair 00.3, operator
# decision 2026-07-30): P1 silently missing its deploy gate is worse than a
# loud failure. When p1-default composes over cap, the role card is WITHHELD
# (never half-delivered) — the shared core still ships, deploy authority is
# explicitly revoked, and the failure is surfaced to the operator. Every other
# audience keeps truncate-and-warn.
_P1_FAIL_CLOSED_NOTICE = (
    "## OPERATING CONTRACT COMPOSITION FAILED — RUNNING FAIL-CLOSED\n"
    "The p1-default contract composed to %d chars against the %d cap. A "
    "truncated coordinator contract must not ship, so the role card was "
    "withheld this turn (the shared core above still applies). Sections that "
    "would have been lost or cut: %s.\n"
    "Until an operator fixes the tree (`dd-context-validate` must pass):\n"
    "- Do NOT exercise deploy authority (no deploy_approve / deploy_transition).\n"
    "- Do NOT dispatch new specialist lanes.\n"
    "- Tell the operator in your FIRST reply: \"P1 operating contract failed "
    "composition (over cap); running fail-closed without deploy authority "
    "until dd-context-validate passes.\"\n"
    + CONTRACT_TRUNCATION_MARKER
)


def _load_context_tree_node(slug: str, root: Path, audience: str) -> Optional[str]:
    """Load one awareness node's summary body by fixed slug. None on any failure.

    For the ``operating-model`` slug, compose the shared core + the audience's
    role view (alignment-by-construction split). Other slugs load their _node.md.

    graduation P1a (WTS 2911977a):
      * ``audience`` is now REQUIRED (the vestigial "p1-specialists" default is
        gone — every caller must state the audience it is composing for).
      * Over-cap truncation is FAIL-LOUD: the body is cut and the explicit
        CONTRACT_TRUNCATION_MARKER is appended (not the old silent tail), and a
        WARNING is logged with the node, audience, and dropped-char count.
      * A node that loads empty is LOGGED (a missing operating-model core or a
        blank node must never pass silently).
    """
    if slug not in _CONTEXT_TREE_AWARENESS_NODES:
        return None
    if slug == "operating-model":
        body = compose_operating_model(root, audience)
        if body is None:
            # Fall back to the legacy single _node.md if core is missing. The
            # ERROR was already logged inside compose_operating_model.
            body = _read_node_body(root / slug / "_node.md", root)
            if body:
                logger.warning(
                    "operating-model composed empty; using legacy %s/_node.md "
                    "fallback (audience=%r).", slug, audience,
                )
    else:
        body = _read_node_body(root / slug / "_node.md", root)
    if not body:
        logger.warning(
            "context-tree node %r loaded EMPTY (audience=%r) — it will be absent "
            "from the injected prompt.", slug, audience,
        )
        return None
    if len(body) > _CONTEXT_TREE_PER_NODE_CHAR_CAP:
        dropped = len(body) - _CONTEXT_TREE_PER_NODE_CHAR_CAP
        lost = _dropped_sections(body, _CONTEXT_TREE_PER_NODE_CHAR_CAP)
        lost_txt = "; ".join(lost) if lost else "unnamed tail content"
        if slug == "operating-model" and audience == "p1-default":
            # Fail-closed for the coordinator (00.3): ship core + explicit
            # authority revocation, never a half-delivered card.
            logger.error(
                "operating-model composition FAIL-CLOSED for p1-default: %d "
                "chars vs cap %d (would lose: %s). Role card WITHHELD; deploy "
                "authority revoked in-band. Fix the tree and rerun "
                "dd-context-validate.",
                len(body), _CONTEXT_TREE_PER_NODE_CHAR_CAP, lost_txt,
            )
            core = _read_node_body(root / "operating-model" / "_core.md", root) or ""
            notice = _P1_FAIL_CLOSED_NOTICE % (
                len(body), _CONTEXT_TREE_PER_NODE_CHAR_CAP, lost_txt)
            combined = f"{core}\n\n{notice}" if core else notice
            # The combined fallback must itself respect the cap.
            return combined[:_CONTEXT_TREE_PER_NODE_CHAR_CAP + len(CONTRACT_TRUNCATION_MARKER) + 1]
        logger.warning(
            "context-tree node %r OVER per-node cap (audience=%r): %d chars, cap "
            "%d — TRUNCATING and dropping %d chars (lost: %s). Injecting %s. The "
            "operating contract delivered this turn is INCOMPLETE.",
            slug, audience, len(body), _CONTEXT_TREE_PER_NODE_CHAR_CAP,
            dropped, lost_txt, CONTRACT_TRUNCATION_MARKER,
        )
        return (body[:_CONTEXT_TREE_PER_NODE_CHAR_CAP]
                + f"\n{CONTRACT_TRUNCATION_MARKER}"
                + f"\n[dropped sections: {lost_txt}]")
    return body


def _operating_contract_version_line(tree_root: Path, audience: str) -> str:
    """Build the single operating-contract version header line (graduation P1a).

    Format:
      DecisionData operating contract v2.0 (core <shorthash>) — role view <card>@<date>

    Replaces the old hardcoded "injection active since 2026-06-02" prose with a
    real, self-describing version stamp: the contract version + the live short
    core hash + which role view was composed and when it was last updated.
    Always returns a usable line; unknown/missing pieces degrade to explicit
    placeholders (never an empty or misleading stamp).
    """
    core = _read_node_body(tree_root / "operating-model" / "_core.md", tree_root)
    version, short = _core_version_and_hash(tree_root, core)
    core_part = short or "unavailable"
    # Which role-view card was composed, and its last_updated stamp.
    card = _resolve_view_card(tree_root, audience)
    if card is None:
        view_part = "core-only (no role view)"
    else:
        fm = _read_card_frontmatter(card, tree_root)
        date = str(fm.get("last_updated") or "undated").strip()
        view_part = f"{card.stem}@{date}"
    return (
        f"DecisionData operating contract {version} (core {core_part}) — "
        f"role view {view_part}"
    )


def build_context_tree_prompt(root: Optional[Path] = None, *, audience: str) -> str:
    """Render the DecisionData /context tree awareness block for a turn.

    Mirrors the Slack canary loader (awareness _node.md summaries only, no
    detail/ walk), capped and fail-soft. Returns "" when nothing loads, so the
    system prompt is unchanged on a missing/broken tree. The block is explicitly
    LOWER precedence than the live turn and never overrides the requested output.

    graduation P1a (WTS 2911977a):
      * ``audience`` is a REQUIRED keyword-only arg (the vestigial
        "p1-specialists" default is gone). ``root`` stays optional — pass None
        (the default) to use the live tree.
      * The header carries a real version line (see
        _operating_contract_version_line) instead of the hardcoded
        "injection active since 2026-06-02" prose.
      * When a node is DROPPED because it would breach the total cap, that is now
        LOUD: a WARNING is logged and an explicit CONTRACT_TRUNCATION_MARKER
        section is appended so the reader sees the contract is incomplete.
    """
    tree_root = root or _context_tree_root()
    sections = []
    loaded = []
    dropped_slugs = []
    total = 0
    for slug in _CONTEXT_TREE_AWARENESS_NODES:
        body = _load_context_tree_node(slug, tree_root, audience=audience)
        if not body:
            continue
        if total + len(body) > _CONTEXT_TREE_TOTAL_CHAR_CAP:
            dropped_slugs.append(slug)
            continue
        sections.append(f"### context: {slug}\n{body}")
        loaded.append(slug)
        total += len(body)
    if not sections:
        return ""
    if dropped_slugs:
        logger.warning(
            "context-tree TOTAL cap %d exceeded (audience=%r): loaded %s (%d "
            "chars), DROPPED %s. Injecting %s. The operating contract delivered "
            "this turn is INCOMPLETE.",
            _CONTEXT_TREE_TOTAL_CHAR_CAP, audience, loaded, total, dropped_slugs,
            CONTRACT_TRUNCATION_MARKER,
        )
        sections.append(
            f"### context: (dropped {', '.join(dropped_slugs)})\n"
            f"{CONTRACT_TRUNCATION_MARKER}"
        )
    version_line = _operating_contract_version_line(tree_root, audience)
    header = "\n".join([
        "--- DecisionData /context tree (awareness; background system + operating context) ---",
        version_line,
        "You ARE operating live on the DecisionData /context tree right now: the summaries below",
        "are loaded into this very turn, and your live working focus is in your MEMORY notes.",
        "These are durable, git-versioned awareness summaries from the shared /context tree. They",
        "are LOWER precedence than the current request — they set operating defaults and system",
        "awareness, and must never override the requested output for this turn. If a summary",
        "disagrees with its named source, the source wins.",
    ])
    logger.debug(
        "context-tree injected nodes: %s (%d chars); %s",
        loaded, total, version_line,
    )
    return f"{header}\n\n" + "\n\n".join(sections)


def build_context_files_prompt(cwd: Optional[str] = None, skip_soul: bool = False) -> str:
    """Discover and load context files for the system prompt.

    Priority (first found wins — only ONE project context type is loaded):
      1. .hermes.md / HERMES.md  (walk to git root)
      2. AGENTS.md / agents.md   (cwd only)
      3. CLAUDE.md / claude.md   (cwd only)
      4. .cursorrules / .cursor/rules/*.mdc  (cwd only)

    SOUL.md from HERMES_HOME is independent and always included when present.
    Each context source is capped at 20,000 chars.

    When *skip_soul* is True, SOUL.md is not included here (it was already
    loaded via ``load_soul_md()`` for the identity slot).
    """
    if cwd is None:
        cwd = os.getcwd()

    cwd_path = Path(cwd).resolve()
    sections = []

    # Priority-based project context: first match wins
    project_context = (
        _load_hermes_md(cwd_path)
        or _load_agents_md(cwd_path)
        or _load_claude_md(cwd_path)
        or _load_cursorrules(cwd_path)
    )
    if project_context:
        sections.append(project_context)

    # SOUL.md from HERMES_HOME only — skip when already loaded as identity
    if not skip_soul:
        soul_content = load_soul_md()
        if soul_content:
            sections.append(soul_content)

    if not sections:
        return ""
    return "# Project Context\n\nThe following project context files have been loaded and should be followed:\n\n" + "\n".join(sections)
