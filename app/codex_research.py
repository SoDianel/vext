"""The research prompts and answer schemas for investigating plugins and vendors.

They are the same for every AI agent (Codex, Claude Code, Antigravity, Gemini CLI: see
app/ai_agents.py, which runs the chosen agent's command line). Every result is a proposal. Nothing is applied until the owner
confirms it, the same propose-then-apply model as every other action in this app.

The full-research prompt is user-editable (AI tab). The output shape is NOT prompt-dependent:
the agent is given a JSON Schema (as a CLI flag, or written into the prompt for agents without
one) and every answer is checked against it (ai_agents.conform), so the response has the same
structure whatever the prompt says. Only the research instructions change.
"""

import json

from app import ai_agents
from app.ai_agents import (  # noqa: F401  (re-exported for callers)
    AgentConfig, ResearchCancelled, ResearchError, begin_batch, cancel_running, npm_available,
)
from app.categories import CATEGORIES

DEFAULT_MODEL = ai_agents.AGENTS["codex"].default_model
MAX_SECONDARY_VENDORS = 5

REQUIRED_PLACEHOLDERS = ("{name}", "{category_list}")
TOKENS = ("{name}", "{format}", "{vendor}", "{store_url}", "{host_path}", "{category_list}")

_PLUGIN_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor": {"type": ["string", "null"]},
        "secondary_vendors": {"type": "array", "items": {"type": "string"}},
        "vendor_url": {"type": ["string", "null"]},
        "vendor_status": {"type": ["string", "null"], "enum": ["active", "deprecated", "down", None]},
        "vendor_status_note": {"type": ["string", "null"]},
        "plugin_type": {"type": ["string", "null"], "enum": ["effect", "synth", None]},
        "store_url": {"type": ["string", "null"]},
        "kvr_url": {"type": ["string", "null"]},
        "equipboard_url": {"type": ["string", "null"]},
        "categories": {"type": "array", "items": {"type": "string"}},
        "description_es": {"type": ["string", "null"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "sources": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": ["string", "null"]},
    },
    "required": [
        "vendor", "secondary_vendors", "vendor_url", "vendor_status", "vendor_status_note",
        "plugin_type", "store_url", "categories", "description_es", "confidence", "sources", "notes",
    ],
    "additionalProperties": False,
}

_VENDOR_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": ["string", "null"]},
        "kvr_url": {"type": ["string", "null"]},
        "equipboard_url": {"type": ["string", "null"]},
        "status": {"type": ["string", "null"], "enum": ["active", "deprecated", "down", None]},
        "status_note": {"type": ["string", "null"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "sources": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": ["string", "null"]},
    },
    "required": ["url", "kvr_url", "equipboard_url", "status", "status_note", "confidence", "sources", "notes"],
    "additionalProperties": False,
}

_CATEGORY_LIST_TEXT = "\n".join(f"  {slug}: {desc}" for slug, _name, _sv, desc in CATEGORIES)

DEFAULT_PROMPT = """You are researching one audio plugin for a personal plugin catalog. Use web search and browsing to find authoritative information. The vendor's own product page and its KVR Audio (kvraudio.com) listing are the best sources. Do not guess or fabricate anything. If you cannot find reliable information for a field, return null for it.

Plugin to research:
- Name: "{name}"
- Format: {format}
- Known vendor (may be blank or wrong): {vendor}
- Known store link (may be blank): {store_url}
- File path on disk (context only, not proof of vendor): {host_path}

Find and return:
- vendor: the company that CURRENTLY owns or sells this product. If the product changed hands, use the current owner.
- secondary_vendors: other companies involved in this product's history or distribution: previous developers, companies that acquired or licensed it, co-developers. Most relevant first, at most 5. Empty list if there are none.
- vendor_url: the main vendor's official website (not a reseller or marketplace page)
- vendor_status: "active" if the vendor is clearly still operating, "deprecated" if this product line seems discontinued, "down" if the vendor's site appears dead, null if you can't tell
- vendor_status_note: one short sentence about the status, only if it isn't simply "active and normal"
- plugin_type: "effect" or "synth" (an effect processes audio; a synth generates sound), null if genuinely unclear
- store_url: the page where this plugin itself is sold or described (not the vendor's home page), null if none
- categories: zero or more slugs chosen from EXACTLY this list (use the slug, not the description). Do not invent slugs:
{category_list}
- description_es: a short (1 to 2 sentence) description IN SPANISH of what the plugin does, for a hover tooltip. Neutral, factual, no marketing language.
- confidence: "high", "medium" or "low", your overall confidence in this research
- sources: the URLs you actually consulted
- notes: anything worth flagging for human review, such as a sale, a rename, or conflicting sources. null if nothing"""

# Extra focus added per investigation target. Plain-language instructions, no schema detail.
FOCUS = {
    "links": (
        "Focus: three links for this plugin.\n"
        "- store_url: the OFFICIAL, current page where this plugin is sold or described: its product page on the "
        "vendor's own website or the vendor's own store. Never a reseller or marketplace (Plugin Boutique, Splice, "
        "Sweetwater, Thomann, Reverb, Amazon and the like), never the vendor's home page, never a KVR or Equipboard "
        "page. If the product is discontinued, use the vendor's page only if it still exists, otherwise null.\n"
        "- kvr_url: this plugin's product page in the KVR Audio database (kvraudio.com/product/...). The product "
        "page itself, not a search result, a vendor page or a news item.\n"
        "- equipboard_url: this plugin's item page in the Equipboard database (equipboard.com/items/...). The item "
        "page itself, not a search result or a brand page.\n"
        "Return null for a link only when you looked and could not confirm that page exists, because null is "
        "recorded as \"this site has no page for it\". If you could not check a site at all (blocked, unreachable), "
        "say so in notes. Do not build a URL from a pattern."
    ),
    "categories": (
        "Focus: categories. Choose every slug from the list that fits what this plugin does. Use notes to say which "
        "choices you're least sure about."
    ),
    "vendor": (
        "Focus: the vendor. This plugin has no vendor in the catalog. Find the company that makes it and return its "
        "exact, commonly used name in the vendor field, plus its website in vendor_url. Return null for vendor if you "
        "can't find reliable evidence. Name the sources you used."
    ),
    "confirm_vendor": (
        "Focus: confirm the vendor. The catalog guessed the vendor shown above from its own heuristics, so treat it as "
        "unverified. Check who actually makes this plugin. Return the correct vendor name in the vendor field (the same "
        "name if the guess is right), with its website in vendor_url. Say in notes if the guess was wrong or if the "
        "plugin changed hands. Return null for vendor only if you can't verify either way."
    ),
    "types": (
        "Focus: plugin type. Decide whether this is an effect or a synth from how the product describes itself. "
        "Give the reason in notes."
    ),
}

VENDOR_PROMPT = """You are researching one audio software company for a personal plugin catalog. Use web search and browsing. The company's own website and its KVR Audio (kvraudio.com) profile are the best sources. Do not guess. If you can't find reliable information for a field, return null for it. The KVR and Equipboard pages matter most when the company is gone or its site is dead, so check them even then. Return null for a link only when you looked and none exists, because null is recorded as "this site has no page for it". If you could not check a site at all, say so in notes.

Company:
- Name: "{name}"
- Known website (may be blank or wrong): {url}
- Known status (may be blank): {status}

Find and return:
- url: the company's official website (never a reseller or marketplace such as Plugin Boutique)
- kvr_url: the company's page in the KVR Audio database (kvraudio.com/developer/...), null if there is none
- equipboard_url: the company's brand page on Equipboard (equipboard.com/brands/...), null if there is none
- status: "active" if the company is clearly still operating, "deprecated" if its products are discontinued or unmaintained, "down" if the site appears dead, null if unclear
- status_note: one short sentence about the status, only if it isn't simply "active and normal"
- confidence: "high", "medium" or "low"
- sources: the URLs you actually consulted
- notes: anything to flag for review (a sale, a rename, conflicting sources), or null"""

# Always appended after the editable instructions. Not shown in the editor.
_FIXED_FOOTER = "Return only the structured result that matches the provided schema. Do not modify any files. This is research only."


def validate_prompt(template: str) -> None:
    """Raises ValueError if the edited prompt would break research."""
    if not template or not template.strip():
        raise ValueError("The prompt can't be empty.")
    missing = [p for p in REQUIRED_PLACEHOLDERS if p not in template]
    if missing:
        raise ValueError("The prompt must keep these placeholders: " + ", ".join(missing))


def render_prompt(template: str, name: str, format_: str, vendor: str | None,
                  host_path: str, store_url: str | None, focus: str | None = None) -> str:
    values = {
        "{name}": name,
        "{format}": format_,
        "{vendor}": vendor or "(unknown)",
        "{store_url}": store_url or "(none)",
        "{host_path}": host_path,
        "{category_list}": _CATEGORY_LIST_TEXT,
    }
    text = template
    for token in TOKENS:
        text = text.replace(token, values[token])
    parts = [text.rstrip()]
    if focus:
        parts.append(focus)
    parts.append(_FIXED_FOOTER)
    return "\n\n".join(parts)


_RESELLERS = (
    "pluginboutique.com", "splice.com", "sweetwater.com", "thomann.de", "thomannmusic.com", "reverb.com",
    "amazon.", "ebay.", "gear4music.com", "audioplugin.deals", "pluginfox.com", "plugins4free.com",
    "bestservice.com", "soundsonline.com", "producerspot.com", "musicradar.com",
)


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "").lower()


def _is_web_url(url) -> bool:
    return isinstance(url, str) and url.lower().startswith(("http://", "https://")) and bool(_host(url))


def sanitize_links(result: dict) -> dict:
    """Drops links that can't be right, whatever the AI said: a KVR link must be on kvraudio.com, an
    Equipboard link on equipboard.com, and a store link or website must not be a reseller or a database
    page. A dropped link is mentioned in notes so the owner knows why it is missing."""
    dropped = []
    for field, domain in (("kvr_url", "kvraudio.com"), ("equipboard_url", "equipboard.com")):
        value = result.get(field)
        if value and not (_is_web_url(value) and _host(value).endswith(domain)):
            dropped.append(f"{field} ({value}) isn't on {domain}")
            result[field] = None
    for field in ("store_url", "url", "vendor_url"):
        value = result.get(field)
        if not value:
            continue
        host = _host(value) if _is_web_url(value) else ""
        if not host:
            dropped.append(f"{field} ({value}) isn't a web address")
            result[field] = None
        elif host.endswith(("kvraudio.com", "equipboard.com")):
            dropped.append(f"{field} ({value}) is a database page, not an official one")
            result[field] = None
        elif any(r in host for r in _RESELLERS):
            dropped.append(f"{field} ({value}) is a reseller, not the official page")
            result[field] = None
    if dropped:
        note = "Dropped: " + "; ".join(dropped) + "."
        result["notes"] = ((result.get("notes") or "") + " " + note).strip()
    return result


def _run(cfg: AgentConfig, prompt: str, schema: dict) -> dict:
    """One call to whichever AI agent the owner picked. The prompt and schema are the same for every agent."""
    return sanitize_links(ai_agents.run(cfg, prompt, schema))


_FOCUSED = {
    "links": {"fields": ("store_url", "kvr_url", "equipboard_url", "confidence", "sources", "notes"), "categories": False},
    "categories": {"fields": ("categories", "confidence", "notes"), "categories": True},
    "types": {"fields": ("plugin_type", "confidence", "notes"), "categories": False},
    "vendor": {"fields": ("vendor", "vendor_url", "confidence", "sources", "notes"), "categories": False},
    "confirm_vendor": {"fields": ("vendor", "vendor_url", "confidence", "sources", "notes"), "categories": False},
}

_FOCUSED_PROMPT = """Audio plugin for a personal catalog: "{name}" ({format}).
Known vendor (may be blank or wrong): {vendor}
Known store link (may be blank): {store_url}
File path (context only, not proof of vendor): {host_path}

{focus}
{categories}
Search the web only as much as you need. The vendor's own product page and KVR Audio (kvraudio.com) are the best sources. Never guess: return null (or an empty list) for anything you can't verify. Keep notes to one short sentence."""


def focused_schema(target: str) -> dict:
    fields = _FOCUSED[target]["fields"]
    return {
        "type": "object",
        "properties": {k: _PLUGIN_SCHEMA["properties"][k] for k in fields},
        "required": list(fields),
        "additionalProperties": False,
    }


def run_focused(target: str, name: str, format_: str, vendor: str | None, host_path: str,
                store_url: str | None, cfg: AgentConfig) -> dict:
    """Investigates ONE question about a plugin (store link, categories, type or vendor): a short
    prompt and an answer schema holding only that question's fields."""
    spec = _FOCUSED[target]
    prompt = _FOCUSED_PROMPT.format(
        name=name, format=format_, vendor=vendor or "(unknown)", store_url=store_url or "(none)",
        host_path=host_path, focus=FOCUS[target],
        categories=("\nAllowed category slugs (use EXACTLY these, never invent one):\n"
                    + _CATEGORY_LIST_TEXT + "\n") if spec["categories"] else "",
    )
    return _run(cfg, prompt + "\n\n" + _FIXED_FOOTER, focused_schema(target))


def run_research(name: str, format_: str, vendor: str | None, host_path: str,
                 cfg: AgentConfig, template: str | None = None,
                 store_url: str | None = None, focus: str | None = None) -> dict:
    """Researches one plugin. `focus` is an investigation target's extra instructions
    (see FOCUS), or None for a full research pass."""
    prompt = render_prompt(template or DEFAULT_PROMPT, name, format_, vendor, host_path, store_url, focus)
    return _run(cfg, prompt, _PLUGIN_SCHEMA)


# ---------- a picture of the plugin (Actions > Metadata > Missing picture) ----------
# The answer only proposes image ADDRESSES (and pages to read). app/pictures.py downloads and checks
# each one, also reads the store / KVR pages itself, and keeps the good ones as candidates; nothing
# becomes the plugin's picture until the owner picks one and applies it.

MAX_IMAGE_ANSWERS = 6
MAX_IMAGE_PAGES = 5

_IMAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "images": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "image_url": {"type": "string"},
                    "page_url": {"type": ["string", "null"]},
                    "tier": {"type": "string", "enum": ["verified", "possible"]},
                    "kind": {"type": "string", "enum": ["interface", "product", "video", "other"]},
                    "reason": {"type": ["string", "null"]},
                },
                "required": ["image_url", "page_url", "tier", "kind", "reason"],
                "additionalProperties": False,
            },
        },
        "pages": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "sources": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": ["string", "null"]},
    },
    "required": ["images", "pages", "confidence", "sources", "notes"],
    "additionalProperties": False,
}

IMAGE_PROMPT = """Audio plugin for a personal catalog: "{name}" ({format}).
Known vendor (may be blank or wrong): {vendor}
Known official product page (may be blank): {store_url}
Known KVR Audio page (may be blank): {kvr_url}
File path (context only, not proof of vendor): {host_path}

The catalog shows a picture of each plugin, normally a screenshot of its window. This plugin has none, so find pictures of it on the web. The owner looks at every candidate and picks one, so return your best candidates even when you aren't certain, labelled honestly. An empty list is only right when nothing on the web plausibly shows this plugin.
{known}
Return up to 6 images, best first:
- image_url: the DIRECT address of the image file (.png, .jpg, .webp), copied from the page's HTML: an <img> src or srcset (take the largest), the og:image or twitter:image meta tag, or a JSON-LD image. Never invent or build an address, with one exception: the thumbnail of an official YouTube video about this plugin (by the vendor or its partner), https://i.ytimg.com/vi/<video id>/maxresdefault.jpg.
- page_url: the page where you found it.
- tier: "verified" when you can tell it shows this plugin's own interface (the page or caption names it, it is the product page's main screenshot). "possible" when it likely shows it but you can't confirm: the header image of an announcement or article about this plugin, a store's product image, a video thumbnail, a bundle picture that includes it.
- kind: "interface" (the plugin's own window), "product" (box shot, marketing composite), "video" (a video thumbnail) or "other".
- reason: a few words. For "possible", why it likely shows the plugin (for example "header of Image-Line's AutoTune Go announcement"); for "verified", what it shows.

Also return pages: up to 5 pages about this plugin that likely carry its picture (its product page, manual or help page, a partner's announcement, a press kit), including pages you couldn't open. VEXT reads each page's own share image (og:image) itself.

Where to look, in this order:
1. The vendor's product page and store page for this plugin.
2. The vendor's manual, help center or support pages for it.
3. Press kits and announcements by the vendor or its partners. For a plugin made with or for a DAW, the DAW maker's news (for example Image-Line's news for FL Studio and FL Cloud plugins).
4. Official videos about it (vendor or partner YouTube channels): use the thumbnail address above.
5. Its KVR Audio product page (kvraudio.com).
6. Retailer product pages (Plugin Boutique, Sweetwater, Thomann, Best Service and the like), for their product IMAGE only, when the vendor's own pages have none. A retailer's image is fine here; a retailer's page is never a link for the catalog.
When a page is blocked (403, a captcha) or doesn't load, don't stop: look for the same picture elsewhere (a search result, the vendor's CDN, a partner's page, an archived copy on web.archive.org).
Never stock photos, watermarked images, logos or icons, or a different product from the same vendor. When a page shows several versions (for example "Pro-Q 3" and "Pro-Q 4"), pick the one matching this plugin's name.
Keep it short: about 10 searches and page reads in all, then answer with what you have.
- confidence: "high", "medium" or "low" that the first picture shows this exact plugin.
- sources: the pages you consulted.
- notes: one short sentence for the reviewer, or null."""


def _known_images_text(known: list | None) -> str:
    if not known:
        return ""
    lines = [f"- {k['image_url']} (from {k.get('page_url') or 'an unknown page'}: {k.get('reason') or ''})"
             for k in known[:8]]
    return ("\nVEXT already read the pages it knows and found these images. Check what they show: return the ones "
            "that show this plugin, with the tier you can justify, and add better ones if you find them.\n"
            + "\n".join(lines) + "\n")


def run_image_search(name: str, format_: str, vendor: str | None, host_path: str,
                     store_url: str | None, cfg: AgentConfig, kvr_url: str | None = None,
                     known: list | None = None, hints: list | None = None) -> dict:
    """Asks the agent for picture addresses and pages for one plugin (see IMAGE_PROMPT). `known`:
    candidates VEXT already found on the store / KVR pages, for the agent to verify and rank.
    `hints`: where this vendor's earlier pictures came from (pictures.vendor_hints)."""
    hint_text = ""
    if hints:
        hint_text = ("\nFor this vendor, the owner's earlier pictures were found at addresses like these "
                     "(look there first, with this plugin's name):\n" + "\n".join(f"- {h}" for h in hints[:4]) + "\n")
    prompt = IMAGE_PROMPT.format(name=name, format=format_, vendor=vendor or "(unknown)",
                                 store_url=store_url or "(none)", kvr_url=kvr_url or "(none)",
                                 host_path=host_path, known=_known_images_text(known) + hint_text)
    result = ai_agents.run(cfg, prompt + "\n\n" + _FIXED_FOOTER, _IMAGE_SCHEMA)
    result["images"] = [i for i in (result.get("images") or []) if _is_web_url(i.get("image_url"))][:MAX_IMAGE_ANSWERS]
    result["pages"] = [p for p in (result.get("pages") or []) if _is_web_url(p)][:MAX_IMAGE_PAGES]
    return result


def run_vendor_research(name: str, url: str | None, status: str | None,
                        cfg: AgentConfig) -> dict:
    """Researches one vendor's website and status. One result per vendor, however many
    plugins it has."""
    prompt = VENDOR_PROMPT.format(name=name, url=url or "(unknown)", status=status or "(unknown)")
    return _run(cfg, prompt.rstrip() + "\n\n" + _FIXED_FOOTER, _VENDOR_SCHEMA)
