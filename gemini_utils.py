import json
import logging
import os
import base64
import signal
import threading
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from PIL import Image

from google import genai
from google.genai import types

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Gemini client (lazy initialization)
client = None


def _normalise_meta_text(value):
    text = " ".join(str(value or "").split())
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    return text.strip()


def _complete_meta_sentences(text):
    return [match.group(0).strip() for match in re.finditer(r"[^.!?]+[.!?]", text or "")]


def _trim_meta_description(value, max_chars=160):
    text = _normalise_meta_text(value)
    if len(text) <= max_chars:
        return text
    complete = []
    for sentence in _complete_meta_sentences(text):
        candidate = " ".join(complete + [sentence]).strip()
        if len(candidate) > max_chars:
            break
        complete.append(sentence)
    if complete:
        return " ".join(complete)
    # A single sentence longer than the limit: never cut mid-word. Trim at the
    # last word boundary; if somehow there is none, keep the whole sentence
    # rather than producing a mangled fragment.
    cutoff = text.rfind(" ", 0, max_chars)
    if cutoff <= 0:
        return text
    return text[:cutoff].rstrip(" .,;:") + "."


def _word_safe_truncate(value, max_chars, suffix_chars=" ,;:|-–"):
    """Trim text to at most ``max_chars`` on a word boundary — never mid-word.

    If there is no space within the limit (e.g. a single very long token) the
    full text is returned unchanged, because a slightly-too-long but complete
    value always beats an arbitrarily clipped one.
    """
    text = _normalise_meta_text(value)
    if len(text) <= max_chars:
        return text
    cutoff = text.rfind(" ", 0, max_chars)
    if cutoff <= 0:
        return text
    return text[:cutoff].rstrip(suffix_chars)


def _normalise_palette_text(value):
    text = " ".join(str(value or "").replace(";", ",").split()).strip(" ,.")
    if not text:
        return ""
    text = re.sub(r"\s*&\s*", ", ", text)
    text = re.sub(r"\s+and\s+", ", ", text, flags=re.I)
    parts = []
    seen = set()
    for part in re.split(r",|/", text):
        clean = " ".join(part.split()).strip(" .")
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        parts.append(clean[:1].upper() + clean[1:])
    return ", ".join(parts[:4])


def _is_generic_color(value):
    return str(value or "").strip().casefold() in {
        "", "multi-color", "multicolor", "multi color", "mixed", "various"
    }


def _normalise_display_suggestion(value, metadata):
    text = " ".join(str(value or "").split()).strip(" ,.")
    title = str((metadata or {}).get("title") or "this wall art").split("|")[0].strip() or "this wall art"
    if not text:
        return ""
    elif "," in text and "." not in text and not text.casefold().startswith(("use ", "style ", "hang ", "place ", "display ")):
        parts = [part.strip() for part in text.split(",") if part.strip()]
        if parts:
            def format_placement(part):
                clean = part.lower().strip()
                if clean.startswith(("above ", "over ")):
                    clean = re.sub(r"\babove sofa\b", "above a sofa", clean)
                    clean = re.sub(r"\bover sofa\b", "over a sofa", clean)
                    return clean
                if "gallery wall" in clean:
                    return "on a gallery wall"
                if "reading nook" in clean:
                    return "in a reading nook"
                if "desk" in clean:
                    return "near a desk"
                if clean in {"feature print", "feature wall", "focal point", "statement print", "statement piece"}:
                    return f"as a {clean}"
                return clean

            placement_parts = [format_placement(part) for part in parts[:3]]
            placements = ", ".join(placement_parts)
            if len(placement_parts) > 1:
                placements = ", ".join(placement_parts[:-1]) + f", or {placement_parts[-1]}"
            text = f"Display {title} {placements}."
    if len(text) > 240:
        cutoff = text.rfind(" ", 0, 239)
        if cutoff < 160:
            cutoff = 239
        text = text[:cutoff]
    return text.rstrip(" ,.;") + "."


def _split_meta_terms(value):
    if isinstance(value, list):
        raw = []
        for item in value:
            raw.extend(_split_meta_terms(item))
        return raw
    return [
        " ".join(part.split()).strip(" .")
        for part in re.split(r",|/|\||\band\b", str(value or ""), flags=re.I)
        if " ".join(part.split()).strip(" .")
    ]


def _merge_meta_terms(*values, limit=4):
    merged = []
    seen = set()
    for value in values:
        for part in _split_meta_terms(value):
            key = part.casefold()
            if key and key not in seen:
                seen.add(key)
                merged.append(part[:1].upper() + part[1:])
            if len(merged) >= limit:
                return ", ".join(merged)
    return ", ".join(merged)


def _strip_html_tags(value):
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return " ".join(text.split())


def _description_specific_terms(metadata):
    metadata = metadata or {}
    mf = metadata.get("metafields") if isinstance(metadata.get("metafields"), dict) else {}
    terms = []
    for value in (
        metadata.get("title"),
        metadata.get("custom_label_0"),
        metadata.get("custom_label_2"),
        metadata.get("custom_label_3"),
        mf.get("subject"),
        mf.get("theme"),
        mf.get("art_style"),
        mf.get("palette"),
        mf.get("composition"),
    ):
        for part in _split_meta_terms(value):
            for word in re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", part):
                clean = word.casefold().strip("'-")
                if clean not in {
                    "wall", "art", "poster", "print", "decor", "home", "room",
                    "style", "piece", "unique", "beautiful", "perfect", "ideal",
                    "charming", "lovely", "space", "your", "this", "that",
                }:
                    terms.append(clean)
    return set(terms)


def _is_filler_description_sentence(sentence, metadata=None):
    text = _strip_html_tags(sentence)
    lowered = text.casefold().strip()
    if not lowered:
        return True
    filler_patterns = (
        r"^elevate your (decor|space|home)\b",
        r"^add (a )?(touch|dash|sense) of\b",
        r"^bring (a )?(touch|sense) of\b",
        r"^perfect for\b",
        r"^it'?s (a|an) (ideal|perfect|excellent|great|wonderful) (choice|addition|piece)\b",
        r"^it'?s (a|an) (wonderful|great|excellent|perfect) way\b",
        r"\binfuse your (space|home|decor)\b",
        r"^this (charming|beautiful|stunning|captivating|unique) (art )?(piece|print)\b",
        r"^make(s)? (a|an) (ideal|perfect|excellent|great|wonderful) (choice|gift|addition)\b",
    )
    if not any(re.search(pattern, lowered) for pattern in filler_patterns):
        return False
    specific_terms = _description_specific_terms(metadata)
    words = {word.casefold().strip("'-") for word in re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", lowered)}
    return len(words & specific_terms) < 2


def _clean_product_description(value, metadata=None):
    raw = str(value or "").strip()
    if not raw:
        return raw
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", raw, flags=re.I | re.S)
    had_paragraphs = bool(paragraphs)
    if not paragraphs:
        paragraphs = [raw]
    cleaned_paragraphs = []
    for index, paragraph in enumerate(paragraphs):
        text = _strip_html_tags(paragraph)
        if not text:
            continue
        sentences = _complete_meta_sentences(text)
        if not sentences:
            sentences = [text.rstrip(" .") + "."]
        if index == len(paragraphs) - 1 and len(sentences) == 1 and cleaned_paragraphs and _is_filler_description_sentence(sentences[0], metadata):
            continue
        while len(sentences) > 1 and _is_filler_description_sentence(sentences[-1], metadata):
            sentences.pop()
        cleaned = " ".join(sentence.strip() for sentence in sentences if sentence.strip()).strip()
        if cleaned:
            cleaned_paragraphs.append(cleaned)
    if not cleaned_paragraphs:
        return raw
    if had_paragraphs:
        return "".join(f"<p>{paragraph}</p>" for paragraph in cleaned_paragraphs)
    return " ".join(cleaned_paragraphs)


def _extract_required_description_checks(prompt_text):
    """Return profile-declared description checks without knowing product facts.

    Editable profiles can add one or more lines in this form:
      - REQUIRED DESCRIPTION FACT: phrase one || acceptable alternative
    Each line is one required fact; any ``||`` alternative satisfies it.
    """
    checks = []
    for raw_line in str(prompt_text or "").splitlines():
        match = re.match(
            r"^\s*-?\s*REQUIRED\s+DESCRIPTION\s+FACT\s*:\s*(.+?)\s*$",
            raw_line,
            flags=re.I,
        )
        if not match:
            continue
        alternatives = [
            _normalise_meta_text(value).casefold()
            for value in match.group(1).split("||")
            if _normalise_meta_text(value)
        ]
        if alternatives:
            checks.append(alternatives)
    return checks


def _missing_required_description_facts(description, prompt_text):
    """Return required profile facts that are absent from the description."""
    haystack = _normalise_meta_text(_strip_html_tags(description)).casefold()
    missing = []
    for alternatives in _extract_required_description_checks(prompt_text):
        if not any(alternative in haystack for alternative in alternatives):
            missing.append(alternatives)
    return missing


def _final_required_description_checklist(prompt_text):
    """Repeat editable required facts at the end of the model prompt for salience."""
    facts = []
    for raw_line in str(prompt_text or "").splitlines():
        match = re.match(
            r"^\s*-?\s*REQUIRED\s+DESCRIPTION\s+FACT\s*:\s*(.+?)\s*$",
            raw_line,
            flags=re.I,
        )
        if match:
            facts.append(match.group(1).strip())
    if not facts:
        return ""
    lines = [
        "FINAL DESCRIPTION FACT CHECKLIST (mandatory before returning JSON):",
        "The initial description must contain at least one exact alternative from every numbered line below. Alternatives are separated by ||.",
        "Silently compare the finished description with all lines and rewrite it before responding if any line is missing.",
    ]
    lines.extend(f"{index}. {fact}" for index, fact in enumerate(facts, start=1))
    return "\n".join(lines)


def profile_requires_description_facts(prompt_text):
    """Return whether the editable profile declares mandatory description facts."""
    return bool(_extract_required_description_checks(prompt_text))


def _clean_short_description(value, full_description=""):
    """Return a complete 50-80 word plain-text summary without truncation."""
    text = _normalise_meta_text(_strip_html_tags(value))
    if not text:
        text = _normalise_meta_text(_strip_html_tags(full_description))
    sentences = _complete_meta_sentences(text)
    selected = []
    for sentence in sentences:
        candidate = " ".join(selected + [sentence]).strip()
        if len(candidate.split()) > 80:
            break
        selected.append(sentence)
        if len(candidate.split()) >= 50:
            break
    if selected:
        return " ".join(selected)
    words = text.split()
    if len(words) <= 80:
        return text
    cutoff = " ".join(words[:80]).rstrip(" ,;:")
    return cutoff.rstrip(".") + "."


def _default_product_faq(metadata=None):
    """Never invent product FAQs when a profile or model did not supply them."""
    return []


def _normalise_product_faq(value, metadata=None, limit=6):
    """Normalize AI/review FAQ data to a safe list of question/answer objects."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return _default_product_faq(metadata)
    if not isinstance(value, list):
        return _default_product_faq(metadata)
    result = []
    for item in value[:limit]:
        if not isinstance(item, dict):
            continue
        question = _normalise_meta_text(item.get("question") or "").rstrip("?")
        answer = _normalise_meta_text(item.get("answer") or "")
        if not question or not answer:
            continue
        result.append({"question": question + "?", "answer": answer})
    return result or _default_product_faq(metadata)


def _profile_requires_faq(prompt_text, faq_kind=None):
    """Let an editable profile require either FAQ group without product-specific code."""
    kind = {
        "product_specific": r"PRODUCT[-\s]+SPECIFIC",
        "generic": r"(?:GENERIC|STORE[-\s]+WIDE)",
    }.get(faq_kind, r"(?:PRODUCT|PRODUCT[-\s]+SPECIFIC|GENERIC|STORE[-\s]+WIDE)")
    return bool(re.search(
        rf"^\s*-?\s*REQUIRE\s+{kind}\s+FAQ\s*:\s*(yes|true|1)\s*$",
        str(prompt_text or ""),
        flags=re.I | re.M,
    ))


def _combine_product_faqs(product_specific, generic):
    """Combine FAQ groups in display order and remove repeated questions."""
    combined = []
    seen = set()
    for item in list(product_specific or []) + list(generic or []):
        key = re.sub(r"[^a-z0-9]+", "", item.get("question", "").casefold())
        if not key or key in seen:
            continue
        seen.add(key)
        combined.append(item)
    return combined[:10]


def _normalise_alt_text(value, fallback="", max_chars=125):
    """Return useful plain alt text without exceeding the configured limit."""
    text = _normalise_meta_text(_strip_html_tags(value or fallback))
    if len(text) <= max_chars:
        return text
    return _word_safe_truncate(text, max_chars, suffix_chars=" ,;:-")


def _remove_unsupported_sensitive_themes(metadata):
    """Remove religious/spiritual classification when no explicit evidence exists."""
    if not isinstance(metadata, dict):
        return metadata
    mf = metadata.get("metafields") if isinstance(metadata.get("metafields"), dict) else {}
    evidence = " ".join(str(value or "") for value in (
        metadata.get("title"), metadata.get("description"), mf.get("subject")
    )).casefold()
    explicit_spiritual_evidence = {
        "prayer", "worship", "deity",
        "angel", "cross", "crucifix", "church", "mosque", "temple",
        "buddha", "hindu", "christian", "islamic", "jewish", "saint",
    }
    if not any(term in evidence for term in explicit_spiritual_evidence):
        for key in ("theme",):
            values = _split_meta_terms(mf.get(key))
            values = [value for value in values if value.casefold() not in {"spiritual", "spirituality"}]
            if values:
                mf[key] = ", ".join(values)
            else:
                mf.pop(key, None)
        label = str(metadata.get("custom_label_3") or "")
        label_values = [value for value in _split_meta_terms(label) if value.casefold() not in {"spiritual", "spirituality"}]
        metadata["custom_label_3"] = ", ".join(label_values)
        picks = metadata.get("category_attribute_picks")
        if isinstance(picks, dict) and isinstance(picks.get("Theme"), list):
            picks["Theme"] = [value for value in picks["Theme"] if str(value).casefold() not in {"spiritual", "spirituality"}]
    return metadata


def _normalise_metafields(metadata):
    if not isinstance(metadata, dict):
        return metadata
    mf = metadata.get("metafields")
    if not isinstance(mf, dict):
        return metadata
    palette = _normalise_palette_text(mf.get("palette") or metadata.get("custom_label_2") or mf.get("color"))
    if palette:
        mf["palette"] = palette
        color_text = str(mf.get("color") or "").strip()
        if _is_generic_color(color_text) or len(palette.split(",")) > len(color_text.split(",")):
            mf["color"] = palette
        current_label = str(metadata.get("custom_label_2") or "").strip()
        if not current_label or "&" in current_label or current_label.casefold() in {
            "green", "blue", "red", "black", "white", "multi-color", "multicolor"
        }:
            metadata["custom_label_2"] = palette
    if mf.get("theme"):
        metadata["custom_label_3"] = mf["theme"]
    mf["display_suggestion"] = _normalise_display_suggestion(mf.get("display_suggestion"), metadata)
    return metadata


def _prune_unsupported_merchandising_inferences(metadata, prompt_text=""):
    """Remove merchandising guesses not corroborated by product evidence."""
    if not isinstance(metadata, dict):
        return metadata
    metafields = metadata.get("metafields") if isinstance(metadata.get("metafields"), dict) else {}
    evidence = " ".join(str(value or "") for value in (
        metadata.get("title"),
        metadata.get("alt_text"),
        metafields.get("subject"),
        metafields.get("composition"),
        metafields.get("theme"),
        metafields.get("palette"),
    )).casefold()
    evidence_tokens = {
        token for token in re.findall(r"[a-z0-9]+", evidence)
        if len(token) >= 4
    }
    stop_tokens = {
        "room", "decor", "product", "style", "piece", "display", "home",
        "year", "round", "ideal", "perfect", "lover", "lovers", "people",
        "customer", "customers", "enthusiast", "enthusiasts",
    }

    def supported(value):
        tokens = {
            token for token in re.findall(r"[a-z0-9]+", str(value or "").casefold())
            if len(token) >= 4 and token not in stop_tokens
        }
        return bool(tokens) and any(
            token in evidence_tokens
            or any(token[:4] == evidence_token[:4] for evidence_token in evidence_tokens)
            for token in tokens
        )

    # Room, mood, occasion and display_suggestion are populated universally.
    # They describe how a piece feels and where it is used rather than literal
    # pixels, so a strict lexical evidence match is the wrong gate for them: the
    # hardwired prompt constrains them to neutral, evidence-based values and the
    # render app profile can refine them per store. Audience stays strictly
    # gated because it describes people, so a profile must opt in via
    # "ALLOW MERCHANDISING FIELD: audience".
    default_inference_fields = {"room", "mood", "occasion", "display_suggestion"}
    profile_inference_fields = {
        match.group(1).casefold()
        for match in re.finditer(
            r"^\s*-?\s*ALLOW\s+MERCHANDISING\s+FIELD\s*:\s*"
            r"(room|mood|audience|occasion|display_suggestion)\s*$",
            str(prompt_text or ""),
            flags=re.I | re.M,
        )
    }
    allowed_inference_fields = default_inference_fields | profile_inference_fields

    for key in ("room", "mood", "occasion"):
        if key not in allowed_inference_fields:
            metafields.pop(key, None)
            continue
        values = _split_meta_terms(metafields.get(key))
        if values:
            metafields[key] = ", ".join(values)
        else:
            metafields.pop(key, None)

    if "audience" not in allowed_inference_fields:
        metafields.pop("audience", None)
    else:
        audience_values = _split_meta_terms(metafields.get("audience"))
        kept_audience = [value for value in audience_values if supported(value)]
        if kept_audience:
            metafields["audience"] = ", ".join(kept_audience)
        else:
            metafields.pop("audience", None)

    # Season is a closed vocabulary, so it can be filled without guessing: a
    # specific season is kept only when the artwork actually evidences it, and
    # everything else is factually "Year-round" rather than left blank.
    seasonal_vocabulary = {
        "spring": "Spring", "summer": "Summer", "autumn": "Autumn",
        "fall": "Autumn", "winter": "Winter", "christmas": "Christmas",
        "halloween": "Halloween", "easter": "Easter",
        "thanksgiving": "Thanksgiving", "valentine's day": "Valentine's Day",
        "valentines day": "Valentine's Day", "year-round": "Year-round",
        "year round": "Year-round", "all year": "Year-round",
    }
    supported_seasons = []
    for value in _split_meta_terms(metafields.get("season")):
        canonical = seasonal_vocabulary.get(str(value).strip().casefold())
        if not canonical or canonical in supported_seasons:
            continue
        if canonical == "Year-round" or supported(canonical):
            supported_seasons.append(canonical)
    metafields["season"] = ", ".join(
        value for value in supported_seasons if value != "Year-round"
    ) or "Year-round"

    if "display_suggestion" not in allowed_inference_fields or not str(metafields.get("display_suggestion") or "").strip():
        metafields.pop("display_suggestion", None)

    metadata["custom_label_1"] = metafields.get("room", "")
    # Google custom label 4 is the season/audience/occasion slot. Audience stays
    # opt-in (it describes people), so fall back to the occasion and then the
    # season - both already evidence-checked - instead of shipping it empty.
    metadata["custom_label_4"] = (
        metafields.get("audience")
        or metafields.get("occasion")
        or metafields.get("season")
        or ""
    )
    return metadata


def _meta_description_is_clipped(text):
    lowered = _normalise_meta_text(text).casefold()
    if not lowered:
        return True
    bad_endings = (
        " for a calm.",
        " for a serene.",
        " for a modern.",
        " for a coastal.",
        " for a relaxing.",
        " with a.",
        " for the.",
        " for an.",
        " those seeking.",
        " customers seeking.",
        " shoppers seeking.",
        " people seeking.",
        " anyone seeking.",
        " adding vintage.",
        " adding modern.",
        " adding classic.",
        " adding timeless.",
        " adding elegant.",
        " adding floral.",
        " adding botanical.",
        " high-quality.",
        " museum-quality.",
        " premium-quality.",
        " gallery-quality.",
        " archival-quality.",
    )
    clipped_adjectives = (
        "vintage", "modern", "classic", "timeless", "elegant", "floral",
        "botanical", "coastal", "serene", "calm", "warm", "inviting",
        "sophisticated", "peaceful", "relaxing", "decorative", "stylish",
        "spicy", "vibrant", "bold", "subtle", "fresh", "unique", "artistic",
        "rustic", "minimalist", "colorful", "colourful", "neutral", "organic",
    )
    adjective_group = "|".join(re.escape(word) for word in clipped_adjectives)
    clipped_patterns = (
        r"\b(adding|bringing|creating|offering|seeking)\s+("
        + adjective_group
        + r")\.$",
        r"\b(adding|bringing|creating|offering)\s+(a|an|the)?\s*("
        + adjective_group
        + r")\.$",
        r"\b(adding|bringing|creating|offering)\s+(a|an|the)?\s*(?:[a-z-]+,\s*)+("
        + adjective_group
        + r")\.$",
        r"\b(perfect|ideal|great|suited|designed)\s+for\b.*\bor\s+(adding|bringing|creating|offering)\s+(a|an|the)?\s*(?:[a-z-]+,\s*)*("
        + adjective_group
        + r")\.$",
        r"\b(perfect|ideal|great|suited|designed)\s+for\s+(adding|bringing|creating|those|people|customers|shoppers)\b",
        r"\b(for|with|and|or|to|in|of|a|an|the)\.$",
        r"(^|[.!?]\s+)(high|museum|premium|gallery|archival)-quality\.?$",
        r"\b(seeking|wanting|needing)\s+(serene|calm|warm|modern|stylish|rustic|botanical|coastal)?\s*home\.$",
        r",\s*(" + adjective_group + r")\.$",
    )
    return lowered.endswith(bad_endings) or any(
        re.search(pattern, lowered) for pattern in clipped_patterns
    )


def _meta_description_is_template_slop(text):
    lowered = _normalise_meta_text(text).casefold()
    bad_patterns = (
        r"\bas an unframed paper poster\b",
        r"\ba detailed wall art print for\b",
        r"\bfor animals decor\b",
        r"\bfor nature decor\b",
        r"\bfor home decor\.$",
        r"\bfeaturing raccoon dog\b",
    )
    return any(re.search(pattern, lowered) for pattern in bad_patterns)


def _rewrite_meta_description_with_ai(metadata, previous_value=""):
    if os.environ.get("GEMINI_ENABLE_SEO_REPAIR", "false").lower() not in {"1", "true", "yes"}:
        return ""
    if not os.environ.get("GEMINI_API_KEY"):
        return ""
    metadata = metadata or {}
    metafields = metadata.get("metafields") if isinstance(metadata.get("metafields"), dict) else {}
    payload = {
        "title": metadata.get("title") or "",
        "seo_title": metadata.get("seo_title") or "",
        "previous_meta_description": previous_value or "",
        "description": _strip_html_tags(metadata.get("description") or "")[:900],
        "subject": metafields.get("subject") or metadata.get("custom_label_3") or "",
        "theme": metafields.get("theme") or "",
        "art_style": metafields.get("art_style") or metadata.get("custom_label_0") or "",
        "mood": metafields.get("mood") or "",
        "room": metafields.get("room") or metadata.get("custom_label_1") or "",
        "palette": metafields.get("palette") or metafields.get("color") or metadata.get("custom_label_2") or "",
    }
    prompt = (
        "You are writing factual Shopify catalogue SEO text for one product.\n"
        "Return JSON only: {\"meta_description\":\"...\"}\n"
        "Rules:\n"
        "- Write a fresh meta description from the product data; do not copy the previous value if it is blank, clipped, templated, or awkward.\n"
        "- 120-155 characters. Never over 160 characters.\n"
        "- Must be one or two complete natural sentences with a proper ending.\n"
        "- Use plain factual catalog language, not promotional copywriting.\n"
        "- Start with the product type or primary search phrase, then include only relevant concrete details such as subject, function, material, objects or text, colour, construction, composition, and style.\n"
        "- Use product-specific nouns. A good sentence should only fit this exact item.\n"
        "- Do not use subjective praise, emotional sales language, vague benefits, CTAs, or claims about how the buyer will feel.\n"
        "- Do not reuse a fixed sentence template across products or force category terms into unnatural phrases.\n"
        "- Do not end with a fragment, adjective, category label, or incomplete purpose phrase.\n"
        "- Before returning, silently verify: valid JSON, field is non-empty, 120-155 characters, complete sentence, factual, image-specific, no dangling ending, no template wording.\n"
        "- If your draft fails any check, rewrite it before returning JSON.\n"
        f"INPUT JSON:\n{json.dumps(payload, ensure_ascii=False)}"
    )
    try:
        gemini_client = get_client()

        def make_api_call():
            return gemini_client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=[prompt],
                config=_gemini_config("gemini-3.1-flash-lite", types.GenerateContentConfig(
                    response_mime_type="application/json",
                    max_output_tokens=512,
                    thinking_config=types.ThinkingConfig(thinking_budget=128),
                    temperature=0.35,
                )),
            )

        timeout_s = int(os.environ.get("GEMINI_SEO_REPAIR_TIMEOUT_S", "10"))
        response = _run_with_hard_timeout(make_api_call, timeout_s)
        if not response or not response.text:
            return ""
        data = json.loads(response.text)
        candidate = _normalise_meta_text((data or {}).get("meta_description") or "")
        if (
            135 <= len(candidate) <= 160
            and not _meta_description_is_clipped(candidate)
            and not _meta_description_is_template_slop(candidate)
        ):
            return candidate
    except FutureTimeoutError:
        logger.warning("Gemini SEO repair timed out")
    except Exception as exc:
        logger.warning("Gemini SEO repair failed: %s", exc)
    return ""


def _fit_complete_meta_sentences(text, limit=160):
    """Longest run of leading complete sentences that fits the limit.

    Stops at the first clipped sentence and never exceeds ``limit``, so the
    result is always made of whole sentences — never a mid-word fragment.
    """
    fitted = []
    for sentence in _complete_meta_sentences(_normalise_meta_text(text)):
        if _meta_description_is_clipped(sentence):
            break
        candidate = " ".join(fitted + [sentence]).strip()
        if len(candidate) > limit:
            break
        fitted.append(sentence)
    return " ".join(fitted).strip()


def _clean_meta_description(value, metadata=None):
    metadata = metadata or {}
    raw = _normalise_meta_text(value)

    # 1. The model's meta description, keeping only its complete sentences.
    candidate = _fit_complete_meta_sentences(raw)
    candidate_ok = bool(candidate) and not _meta_description_is_template_slop(candidate)
    if candidate_ok and len(candidate) >= 135:
        return candidate

    # 2. The whole raw value if it is already a clean, ideal-length fit.
    if 135 <= len(raw) <= 160 and not _meta_description_is_clipped(raw) and not _meta_description_is_template_slop(raw):
        return raw

    # 3. Ask the model to rewrite to the ideal length.
    repaired = _rewrite_meta_description_with_ai(metadata, raw)
    if repaired:
        return repaired

    # 4. The complete-sentence candidate, even if shorter than the 135 ideal —
    #    a complete short description always beats a clipped long one.
    if candidate_ok:
        return candidate

    # 5. Never return empty and never clip mid-word: synthesise a complete,
    #    word-safe description from the product's own copy. The title always
    #    exists, so this guarantees a non-empty result.
    mf = metadata.get("metafields") if isinstance(metadata.get("metafields"), dict) else {}
    source_texts = [
        _normalise_meta_text(_strip_html_tags(source))
        for source in (
            metadata.get("short_description"),
            metadata.get("description"),
            metadata.get("title"),
            mf.get("subject"),
        )
    ]
    source_texts = [text for text in source_texts if text]
    # Prefer a complete fitted sentence from any source over a trimmed fragment.
    for source_text in source_texts:
        fitted = _fit_complete_meta_sentences(source_text)
        if fitted:
            return fitted
    # Last resort: word-safe trim of the first available copy (never mid-word).
    for source_text in source_texts:
        return _trim_meta_description(source_text, 160)
    return ""


def _clean_product_title(value, max_chars=70):
    """Keep a generated title natural and within a useful catalogue length."""
    title = _normalise_meta_text(value)
    # Standardise the display separator to a pipe so the product title matches
    # the SEO title. Only a spaced dash acting as a separator is converted;
    # hyphenated words like "Mid-Century" or "Black-and-White" are left intact.
    title = re.sub(r"\s+[–—-]\s+", " | ", title).strip()
    if len(title) <= max_chars:
        return title
    parts = [part.strip() for part in re.split(r"\s*[|]\s*", title) if part.strip()]
    if len(parts) > 1:
        selected = parts[:2]
        candidate = " | ".join(selected)
        while len(candidate) > max_chars and len(selected) > 1:
            selected.pop()
            candidate = " | ".join(selected)
        if candidate and len(candidate) <= max_chars:
            return candidate
    return _word_safe_truncate(title, max_chars)


def _canonical_tag_case(value):
    text = " ".join(str(value or "").split())
    if text.isupper() or text.islower():
        text = text.title()
    for source, replacement in (
        ("Seo", "SEO"), ("Ai", "AI"), ("Uk", "UK"),
        ("Usa", "USA"), ("Eu", "EU"), ("Diy", "DIY"),
    ):
        text = re.sub(rf"\b{source}\b", replacement, text)
    return text


def _normalise_tags(tags):
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.replace("\n", ",").split(",")]
    if not isinstance(tags, list):
        return []
    cleaned = []
    seen = set()
    for tag in tags:
        text = _canonical_tag_case(str(tag or "").replace("#", "")).strip(" ,;")
        if not text or not _is_safe_product_tag(text):
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return cleaned[:24]


def _add_tag(tags, seen, value):
    text = _canonical_tag_case(str(value or "").replace("#", "")).strip(" ,;")
    if not text or not _is_safe_product_tag(text):
        return
    key = text.casefold()
    if key in seen:
        return
    seen.add(key)
    tags.append(text)


def _is_safe_product_tag(text):
    """Reject taxonomy paths, broken comma fragments, dimensions, and junk tags."""
    text = str(text or "").strip()
    if not text:
        return False
    lowered = text.casefold()
    if ">" in text or lowered.startswith("&"):
        return False
    if len(text) > 48:
        return False
    dimension_terms = {
        "inch", "inches", "wide", "high", "tall", "cm", "mm", "size", "sizes",
        "x", "24", "36", "24x36", "24 x 36",
    }
    words = {part.strip(" ,;:-").casefold() for part in text.split()}
    if words and words <= dimension_terms:
        return False
    return True


def _expand_tags(metadata):
    tags = _normalise_tags(metadata.get("suggested_tags", []))
    seen = {tag.casefold() for tag in tags}
    metafields = metadata.get("metafields") if isinstance(metadata.get("metafields"), dict) else {}

    for key in (
        "custom_label_0",
        "custom_label_1",
        "custom_label_2",
        "custom_label_3",
        "custom_label_4",
    ):
        _add_tag(tags, seen, metadata.get(key))

    for key in (
        "color",
        "theme",
        "frame_style",
        "art_movement",
        "art_style",
        "artwork_authenticity",
        "material",
        "orientation",
        "subject",
        "room",
        "mood",
        "audience",
        "occasion",
        "season",
    ):
        value = metafields.get(key)
        if isinstance(value, str):
            for part in value.replace("/", ",").replace(";", ",").split(","):
                _add_tag(tags, seen, part)
        else:
            _add_tag(tags, seen, value)

    title_words = str(metadata.get("title", "")).replace("|", " ").split()
    if any(word.casefold() in {"poster", "print", "art", "wall"} for word in title_words):
        for fallback in (
            "Wall Art",
            "Poster",
            "Art Print",
            "Unframed Print",
            "Home Decor",
            "Gallery Wall",
        ):
            _add_tag(tags, seen, fallback)

    return tags[:24]


def _filename_terms(value):
    base = os.path.splitext(os.path.basename(str(value or "")))[0].lower()
    base = re.sub(r"[0-9a-f]{8}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{12}", " ", base)
    base = re.sub(r"\b[0-9a-f]{12,}\b", " ", base)
    for marker in ("imagineinfinity123_", "__analysis", "--ar_", "--sref_", "--sre_"):
        base = base.replace(marker, " ")
    for ch in "_-0123456789":
        base = base.replace(ch, " ")
    stop = {
        "png", "jpg", "jpeg", "webp", "lot", "empty", "background", "image",
        "frame", "mockup", "analysis", "on", "of", "and", "with", "the", "a",
        "an", "for", "in", "to", "inch", "inches", "wide", "high", "tall",
        "cm", "mm", "size", "sizes", "x", "printable", "upload", "local",
        "worker", "copy", "final", "main"
    }
    return [part for part in base.split() if len(part) > 2 and part not in stop]


def _title_from_terms(terms):
    useful = terms[:6] or ["wall", "art"]
    text = " ".join(word.capitalize() for word in useful)
    suffix = "Wall Art Poster"
    if "poster" in text.lower() or "wall art" in text.lower():
        return text
    return f"{text} | {suffix}"


def _basic_color_from_image(image_path, terms):
    named = {
        "red": "Red", "blue": "Blue", "green": "Green", "yellow": "Yellow",
        "black": "Black", "white": "White", "beige": "Beige", "cream": "Beige",
        "brown": "Brown", "orange": "Orange", "pink": "Pink", "purple": "Purple",
        "gold": "Gold", "grey": "Gray", "gray": "Gray"
    }
    for term in terms:
        if term in named:
            return named[term]
    try:
        with Image.open(image_path) as img:
            img = img.convert("RGB").resize((1, 1))
            r, g, b = img.getpixel((0, 0))
        if max(r, g, b) - min(r, g, b) < 30:
            return "Gray" if r < 180 else "White"
        if r >= g and r >= b:
            return "Red" if r > 150 else "Brown"
        if g >= r and g >= b:
            return "Green"
        return "Blue"
    except Exception:
        return "Multi-color"


def _orientation_from_image(image_path):
    try:
        with Image.open(image_path) as img:
            width, height = img.size
        if width > height * 1.08:
            return "Landscape"
        if height > width * 1.08:
            return "Portrait"
        return "Square"
    except Exception:
        return "Portrait"


# build_fallback_product_metadata was removed deliberately. A listing must never
# be published from guessed, image-derived placeholder copy: it puts poor-quality
# products live without anyone noticing. If the AI cannot produce complete
# metadata, the job fails loudly and processing stops.


def _run_with_hard_timeout(fn, timeout_s):
    """Run a blocking SDK call without hanging on executor shutdown after timeout."""
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout_s)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def get_client():
    """Get or initialize the Gemini client"""
    global client
    if client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            error_msg = "GEMINI_API_KEY environment variable is not set. Please set it to use Gemini features."
            logger.error(f"❌ {error_msg}")
            logger.error("❌ This error occurs when the API key is not available in the current process/thread context.")
            logger.error("❌ Make sure to start the server using run_server.py which sets the environment variable.")
            raise ValueError(error_msg)
        try:
            # CRITICAL: give the underlying httpx call a real socket-level timeout.
            # Without this the google-genai client has NO timeout, so if Gemini
            # stalls mid-response (logs die at receive_response_headers) the call
            # hangs forever. On the single-worker free tier that wedges the whole
            # instance until Render kills it ("connection refused"). HttpOptions
            # timeout is in MILLISECONDS.
            client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=45_000),
            )
            logger.info("✅ Gemini client initialized successfully (http timeout=45s)")
        except Exception as e:
            logger.error(f"❌ Failed to initialize Gemini client: {str(e)}")
            logger.error(f"❌ Error type: {type(e).__name__}")
            raise
    return client


_GEMINI_VERSION_RE = re.compile(r"gemini-(\d+)\.(\d+)")


def _gemini_version(model):
    match = _GEMINI_VERSION_RE.search(str(model or "").lower())
    return (int(match.group(1)), int(match.group(2))) if match else None


def _thinking_level_for_budget(budget):
    if budget <= 128:
        return "minimal"
    if budget <= 512:
        return "low"
    if budget <= 4096:
        return "medium"
    return "high"


def _gemini_config(model, config):
    """Drop settings that newer Gemini models reject (Google notice, Oct 2026).

    temperature/top_p/top_k have had no effect since Gemini 3.6 and upcoming
    models will reject them, so they are left out from 3.6 on. thinking_budget
    is still accepted by every released 3.x model, so it is only swapped for
    thinking_level on models newer than 3.6. Older models get the config as is.
    """
    version = _gemini_version(model)
    if version is None:
        return config
    update = {}
    if version >= (3, 6):
        update.update(temperature=None, top_p=None, top_k=None)
    thinking = config.thinking_config
    if version > (3, 6) and thinking is not None and thinking.thinking_budget is not None:
        budget = thinking.thinking_budget
        update["thinking_config"] = (
            types.ThinkingConfig(thinking_level=_thinking_level_for_budget(budget))
            if budget >= 0 else None
        )
    return config.model_copy(update=update) if update else config


def curate_listing_discovery_with_ai(metadata, candidate_products=None, max_related=4, max_complementary=4):
    """Use Gemini to choose search boosts and discovery recommendations from real candidates.

    Returns None when unavailable/failing so callers can use their strict fallback.
    """
    if os.environ.get("GEMINI_ENABLE_DISCOVERY_CURATION", "false").lower() not in {"1", "true", "yes"}:
        return None
    if not os.environ.get("GEMINI_API_KEY"):
        return None

    metadata = metadata or {}
    metafields = metadata.get("metafields") if isinstance(metadata.get("metafields"), dict) else {}

    def clean_list(value, limit=12):
        if isinstance(value, list):
            raw = value
        else:
            raw = re.split(r"[,|;/]+", str(value or ""))
        cleaned = []
        seen = set()
        for item in raw:
            text = " ".join(str(item or "").split()).strip(" ,.;:")
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                cleaned.append(text)
            if len(cleaned) >= limit:
                break
        return cleaned

    candidates = []
    for product in (candidate_products or [])[:20]:
        product_id = str(product.get("id") or "").strip()
        title = str(product.get("title") or "").strip()
        if not product_id or not title:
            continue
        collections = product.get("collections") or {}
        if isinstance(collections, dict):
            collection_names = [
                str(node.get("title") or "").strip()
                for node in (collections.get("nodes") or [])
                if str(node.get("title") or "").strip()
            ]
        else:
            collection_names = clean_list(collections)
        candidates.append({
            "id": product_id,
            "title": title,
            "handle": str(product.get("handle") or "").strip(),
            "product_type": str(product.get("productType") or "").strip(),
            "tags": clean_list(product.get("tags"), limit=10),
            "collections": collection_names[:8],
        })

    prompt_payload = {
        "current_product": {
            "title": metadata.get("title") or "",
            "seo_title": metadata.get("seo_title") or "",
            "description": _strip_html_tags(metadata.get("description") or "")[:900],
            "subject": metafields.get("subject") or metadata.get("custom_label_3") or "",
            "theme": metafields.get("theme") or "",
            "art_style": metafields.get("art_style") or metadata.get("custom_label_0") or "",
            "mood": metafields.get("mood") or "",
            "room": metafields.get("room") or metadata.get("custom_label_1") or "",
            "palette": metafields.get("palette") or metafields.get("color") or metadata.get("custom_label_2") or "",
        },
        "candidate_products": candidates,
        "limits": {
            "related": max_related,
            "complementary": max_complementary,
            "search_boosts": 8,
        },
    }

    prompt = (
        "You are an expert Shopify merchandiser and search specialist.\n"
        "Return JSON only with keys: search_boosts, related_product_ids, complementary_product_ids.\n\n"
        "SEARCH BOOST RULES:\n"
        "- Generate 6-8 likely shopper search phrases for the current product.\n"
        "- Each phrase must be 2-5 words and specific to the product's visible subject, attributes, function, style, or supported use case.\n"
        "- Do not use broad product categories or generic promotional terms by themselves.\n"
        "- Prefer phrases combining two or more distinguishing, supported attributes.\n\n"
        "RELATED PRODUCT RULES:\n"
        "- Choose only candidate IDs that are genuinely close to the current product.\n"
        "- Related means strong overlap in specific subject, function, attributes, or style, not merely a broad category, colour, audience, or product type.\n"
        "- Reject broad-only and false word matches.\n"
        "- It is better to return an empty list than a weak match.\n\n"
        "COMPLEMENTARY PRODUCT RULES:\n"
        "- Choose products that would merchandise naturally alongside this item because of a specific functional, subject, attribute, style, or theme relationship.\n"
        "- Do not fill slots. Empty is acceptable.\n\n"
        f"INPUT JSON:\n{json.dumps(prompt_payload, ensure_ascii=False)}"
    )

    try:
        gemini_client = get_client()

        def make_api_call():
            return gemini_client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=[prompt],
                config=_gemini_config("gemini-3.1-flash-lite", types.GenerateContentConfig(
                    response_mime_type="application/json",
                    max_output_tokens=2048,
                    thinking_config=types.ThinkingConfig(thinking_budget=256),
                    temperature=0.2,
                )),
            )

        timeout_s = int(os.environ.get("GEMINI_DISCOVERY_TIMEOUT_S", "12"))
        response = _run_with_hard_timeout(make_api_call, timeout_s)
        if not response or not response.text:
            return None
        data = json.loads(response.text)
        if not isinstance(data, dict):
            return None
        allowed_ids = {candidate["id"] for candidate in candidates}
        related = [
            product_id for product_id in clean_list(data.get("related_product_ids"), limit=max_related)
            if product_id in allowed_ids
        ][:max_related]
        complementary = [
            product_id for product_id in clean_list(data.get("complementary_product_ids"), limit=max_complementary)
            if product_id in allowed_ids and product_id not in related
        ][:max_complementary]
        boosts = clean_list(data.get("search_boosts"), limit=8)
        return {
            "search_boosts": boosts,
            "related_products": related,
            "complementary_products": complementary,
        }
    except FutureTimeoutError:
        logger.warning("Gemini discovery curation timed out")
    except Exception as exc:
        logger.warning("Gemini discovery curation failed: %s", exc)
    return None

def get_default_prompt():
    """Return the default base prompt text (without collections or custom instructions appended)."""
    return DEFAULT_BASE_PROMPT


def get_prompt_sections():
    """Return the prompt broken into labelled sections (OrderedDict).
    
    Each key is a section identifier (used as accordion panel id) and each
    value is a dict with:
      - 'title'          : display label
      - 'content'        : editable text shown in the accordion textarea
      - 'locked'         : (optional, bool) if True the entire section is hidden
                           from the UI and always injected server-side.
      - 'locked_content' : (optional, str) structural prompt text that is always
                           prepended before 'content' when building the final
                           prompt.  The user never sees or edits this part.
    
    The flat DEFAULT_BASE_PROMPT can be rebuilt by joining the effective content
    of every section (locked_content + content, or just content).
    """
    from collections import OrderedDict
    return OrderedDict([
        ("intro", {
            "title": "System Role & General Guidance",
            "hint": "Sets who the AI is (e.g. SEO copywriter) and any overall rules. Changes here affect tone, focus, and how the AI interprets the rest of the prompt.",
            "locked_content": (
                "MANDATORY CATALOG QUALITY RULES (these apply after and override any conflicting custom prompt):\n"
                "- Ground the listing in the supplied product image and known product settings. Never invent manufacturing, material, provenance, cultural, historical, performance, or shipping facts.\n"
                "- Before writing, inspect the complete image and identify at least 6 concrete differentiating details, including visible subjects, objects, actions, typography or text, colors, composition, background, and visual technique where present. Use the strongest details naturally across the listing fields.\n"
                "- Treat words or claims visible on the product or in its image as depicted content, not independently verified facts. Attribute them clearly when needed. Do not translate unfamiliar text unless confident it is legible and the translation is reliable.\n"
                "- Persuasive language is allowed, but concrete product-specific information must dominate. Remove generic praise or lifestyle wording that could describe many unrelated products.\n"
                "- Do not open with stock sales phrases such as 'immerse yourself', 'discover', 'elevate your space', or 'bring this artwork to life'. Do not promise that a product will inspire, captivate, spark conversation, become a focal point, or deliver exceptional/premium/museum quality. State what is visible and what is factually supplied.\n"
                "- Do not infer a religion, culture, nationality, historical provenance, protected characteristic, target audience, occasion, or room merely from one decorative cue. Only return these when the image contains clear supporting evidence; otherwise use a neutral, product-led value.\n"
                "- Treat product specifications, included or excluded items, materials, dimensions, production methods, packaging, care, and fulfilment details as profile-supplied facts. Use them only when explicitly supplied, and never transfer them to another product type.\n"
                "- The meta_description must be a complete, natural 140-155 character summary, never over 160 characters, using specific visible details rather than filler.\n"
                "- The product description must contain 3 HTML paragraphs and 120-180 words. Each paragraph must add new information; do not pad or repeat the title.\n"
                "- Before drafting the description, collect every editable '- REQUIRED DESCRIPTION FACT:' line. Include at least one stated alternative from every line naturally in the initial description. Treat this as a mandatory checklist, not optional guidance.\n"
                "- Before returning JSON, compare the completed description against that checklist word-for-word. If any required fact is absent, rewrite the description before responding. Never rely on a later repair step.\n"
                "- alt_text must be plain, image-specific text no longer than 125 characters. suggested_tags must contain 16-24 distinct, relevant tags.\n"
            ),
            "content": "You are an expert e-commerce catalog specialist. Analyze this product image and generate factual, image-specific, SEO-optimized product metadata in JSON format. Prefer concrete visual detail over promotional language."
        }),
        ("product_title", {
            "title": "Product Title",
            "hint": "Controls how the main product title is written: length, keyword placement, and structure. Influences what appears on your store and in search.",
            "content": (
                "PRODUCT TITLE REQUIREMENTS:\n"
                "- 50-70 characters optimal (Google displays ~60, but platforms vary)\n"
                "- Write a natural product name; separators are optional and must not make the title read like a keyword list\n"
                "- Front-load highest-volume search terms (e.g., \"Wall Art\", \"Silver Ring\")\n"
                "- Include specific attributes: material, color, style, size (where visible)\n"
                "- Use the pipe character \"|\" as the separator (not hyphens or dashes), and no more than two of them\n"
                "- Include one broad + one specific term (e.g., \"Flower\" + \"Pansy\")\n"
                "- Avoid: keyword stuffing, multiple product type words, filler words (beautiful, perfect, amazing)\n"
                "- Example: \"Purple Pansy Wall Art | Botanical Flower Print | Modern Home Decor\""
            )
        }),
        ("meta_description", {
            "title": "Meta Description",
            "hint": "Guides the short summary under your product link in Google search. Affects character length, keyword use, and call-to-action.",
            "content": (
                "META DESCRIPTION REQUIREMENTS:\n"
                "- 140-155 characters. Never exceed 160 characters.\n"
                "- Include primary keyword in first 50 characters\n"
                "- Answer using concrete visible/product details: What is it and what exact subject/details are visible? Include audience or room only when genuinely supported.\n"
                "- Use image-specific nouns from the artwork: subject, objects, text, palette, composition, and style. Do not rely on generic praise.\n"
                "- End with a complete factual use/room phrase that reads naturally, not a fragment.\n"
                "- Use plain catalog language. Avoid subjective praise, emotional sales language, CTAs, vague benefits, and sentences that could fit hundreds of unrelated posters.\n"
                "- Do NOT use templated wording such as \"[Title] as an unframed paper poster\" or \"A detailed wall art print for [Theme] decor\".\n"
                "- Do NOT write awkward category phrases like \"Animals decor\", \"Nature decor\", or \"Home decor\" as the ending.\n"
                "- Use the actual visible subject and product context, e.g. tanuki with sake bottle and kanji, not broad category labels.\n"
                "- Must be a complete natural sentence. Never end with a dangling phrase such as \"perfect for adding vintage\", \"those seeking\", \"with a\", or \"for a\".\n"
                "- Before returning JSON, silently check the meta_description is non-empty, 140-155 characters, under 160, complete, factual, image-specific, and not templated. If it fails, rewrite it.\n"
                "- Example style: \"Purple pansy wall art with detailed petals, green leaves, and soft botanical color. Floral print for bedrooms, gallery walls, and nature-led interiors.\""
            )
        }),
        ("product_description", {
            "title": "Product Description",
            "hint": "Defines style, length, and structure of the main product description (paragraphs, tone, keywords). Influences what customers read on the product page.",
            "content": (
                "PRODUCT DESCRIPTION REQUIREMENTS:\n"
                "- STRICT LENGTH: 3 substantial but concise paragraphs, 120-180 words. Do NOT exceed 190 words.\n"
                "- Write in flowing, natural prose paragraphs ONLY. Do NOT use bullet points, asterisks, dashes, numbered lists, or any list formatting whatsoever. Every sentence should be part of a paragraph.\n"
                "- Each paragraph should be wrapped in HTML <p> tags (e.g. <p>First paragraph here.</p><p>Second paragraph here.</p>).\n"
                "- Structure: paragraph 1 identifies the product and its strongest visible features; paragraph 2 adds distinct design, material, functional, palette, typography, composition, or construction details; paragraph 3 gives only known specifications and configured buying details. Mention uses, audiences, moods, rooms, or occasions only when supported, not as padding.\n"
                "- Primary keyword: 2-3 times naturally woven in\n"
                "- Include concrete noun phrases that help long-tail SEO, selecting only attributes relevant to this product such as subject, material, colour, style, function, intended use, dimensions, and product format.\n"
                "- Before writing, identify 6-10 visible details from the image (subject, pose/action, expression, text, objects, background, palette, composition, style). Use those details in the prose instead of generic praise.\n"
                "- Every paragraph must contain at least two product-specific details drawn from the image or supplied settings. If a sentence could fit hundreds of unrelated products, rewrite it with evidence specific to this product.\n"
                "- Include relevant profile-supplied specifications naturally in paragraph 3. Do not invent dimensions, materials, included items, production details, or variant values; use exact configured data only.\n"
                "- To make a supplied fact mandatory, add an editable line in the form '- REQUIRED DESCRIPTION FACT: exact phrase || acceptable alternative'. Every such line must be represented naturally in the full description.\n"
                "- Write in plain catalog prose: factual, specific, and readable. Do not write like an advert, salesperson, lifestyle blog, or inspirational caption.\n"
                "- Avoid subjective praise, emotional promises, buyer-flattery, CTAs, vague benefits, filler closing sentences, excessive adjectives, generic claims, and any form of bullet points or lists.\n"
                "- After drafting, silently test every sentence: if it still works for a different product after removing the title, rewrite it with visible or supplied details specific to this item.\n"
                "- Do not end with a generic decor sentence. The final sentence should mention the exact product format plus a visible subject/style detail.\n"
                "- REMEMBER: make the listing detailed and useful, but not padded."
            )
        }),
        ("short_description", {
            "title": "Short Description",
            "hint": "Controls the concise product-specific summary shown beside the product images, above the price.",
            "locked_content": (
                "SHORT DESCRIPTION OUTPUT (required): Return short_description as a complete 50-80 word plain-text product summary based on visible evidence and known product facts.\n"
            ),
            "content": (
                "SHORT DESCRIPTION REQUIREMENTS:\n"
                "- Return a separate short_description containing 50-80 words in plain text.\n"
                "- Summarize the strongest visible subject, typography, palette, composition, and product format details.\n"
                "- Use complete sentences and do not repeat the title verbatim.\n"
                "- This is the above-the-fold buying summary; prioritize concrete differentiating information over generic praise."
            )
        }),
        ("product_specific_faq", {
            "title": "Product-specific FAQs",
            "hint": "Controls questions unique to each product. The AI answers them from visible product evidence, not store-wide production facts.",
            "locked_content": (
                "PRODUCT-SPECIFIC FAQ OUTPUT (required): Return product_specific_faq as a JSON array of 3-4 objects, each containing non-empty question and answer strings. Base every answer on visible product evidence or supplied product data.\n"
            ),
            "content": (
                "PRODUCT-SPECIFIC FAQ REQUIREMENTS:\n"
                "- Return 3-4 useful questions whose answers distinguish this exact product.\n"
                "- Select only relevant topics supported by the image, such as the depicted subject, visible colours, visual style or technique, composition or orientation, and clearly legible wording or places.\n"
                "- Do not repeat store-wide questions about materials, framing, production, packaging, delivery, returns, care, or variants.\n"
                "- Do not invent symbolism, provenance, cultural meaning, audience, room, occasion, or facts that cannot be verified from the image or supplied data."
            )
        }),
        ("faq", {
            "title": "Store-wide FAQ Questions",
            "hint": "Set recurring customer questions and verified store facts here. The AI writes concise answers, and this content is saved with the profile.",
            "locked_content": (
                "STORE-WIDE FAQ OUTPUT (required): Return generic_faq as a JSON array of 4-6 objects, each containing non-empty question and answer strings. Use only facts explicitly supplied in the editable prompt or product settings.\n"
            ),
            "content": (
                "STORE-WIDE FAQ REQUIREMENTS:\n"
                "- Return 4-6 recurring purchase questions selected from the questions configured below.\n"
                "- Answer each question only from verified facts supplied in this editable section or product settings; never infer specifications from the image.\n"
                "- Keep answers concise and factual. Do not add qualitative claims, benefits, guarantees, or unsupported details.\n"
                "- Do not promise dispatch times, delivery dates, border dimensions, archival life, waterproofing, care, or returns terms unless explicitly supplied in these instructions."
            )
        }),
        ("alt_text", {
            "title": "Alt Text",
            "hint": "Sets rules for image alt text: length and how to describe the image for accessibility and image SEO.",
            "content": (
                "ALT TEXT (for image SEO):\n"
                "- 125 characters max\n"
                "- Describe what's actually in the image\n"
                "- Prefer the most distinguishing visible subjects, actions/poses, typography, composition, and background details; do not use the product title as the alt text\n"
                "- Include primary keyword naturally\n"
                "- Example: \"Purple pansy flower botanical illustration art print on white background\""
            )
        }),
        ("metafields", {
            "title": "Metafield Guidance",
            "hint": "Guides values for Shopify metafields (color, theme, frame style, condition, etc.). Affects filtering and product attributes in your store.",
            "locked_content": (
                "METAFIELDS (for Shopify product attributes):\n"
                "Include a \"metafields\" object with these exact keys (short string values). Infer from the image where possible:\n"
                "- \"color\": dominant or notable color(s)\n"
                "- \"theme\": 2-5 specific subject/style themes supported by visible evidence, e.g. Botanical, Japanese Culture, Psychedelic, Desert, Retro. Do not infer a culture/religion/region from one generic cue such as calligraphy, arches, ornament, vintage, or traditional; only use those labels when multiple visible cues clearly support them.\n"
                "- \"frame_style\": framing status\n"
                "- \"condition\": item condition\n"
                "- \"decoration_material\": material used for decor/finish\n"
                "- \"artwork_frame_material\": frame material if framed\n"
                "- \"art_movement\": likely art movement if visually inferable, otherwise a broad safe value such as Contemporary\n"
                "- \"art_style\": 1-4 visual style terms such as Illustrative, Photographic, Minimalist, Abstract, Vintage, Botanical, Ornate, Retrofuturist. Do not use generic fallback labels unless the image clearly supports them.\n"
                "- \"artwork_authenticity\": use Reproduction/Print for poster or print products unless clearly original\n"
                "- \"material\": product/display material such as Paper, Canvas, Digital print, Wood, Metal, or Mixed materials\n"
                "- \"orientation\": Portrait, Landscape, Square, or Panoramic\n"
                "- \"subject\": 1-4 concise visible subjects, e.g. Beach scene, Botanical flowers, Abstract shapes\n"
                "- \"room\": 2-4 best-fit rooms/uses when plausible, e.g. Living room, Bedroom, Nursery, Office, Kitchen\n"
                "- \"mood\": 2-4 visual moods, e.g. Calm, Playful, Dramatic, Serene, Elegant\n"
                "- \"palette\": concise color palette, e.g. Beige, blue, red\n"
                "- \"audience\": evidence-supported buyer interest, e.g. Coastal decor lovers, Nature lovers; otherwise use a neutral product interest and never infer religion, identity, or demographics\n"
                "- \"occasion\": gift/use occasion only when supported by the design or supplied product context; otherwise use Year-round or leave it neutral\n"
                "- \"season\": seasonal relevance if visible, e.g. Summer, Autumn, Christmas, Year-round\n"
                "- \"composition\": visible composition/layout, e.g. Centered portrait, Beach umbrellas, Typography layout\n"
                "- \"display_suggestion\": one complete natural sentence about where/how to display the print. Do not output a bare comma list. Do not end with style labels or color labels. Example: \"Display this print on a gallery wall, above a console table, or in a reading nook.\""
            ),
            "content": (
                "Guidance for metafield values (edit examples/preferences here):\n"
                "- color: e.g. \"Multi-color\", \"Blue\", \"Earth tones\"\n"
                "- theme: e.g. \"Botanical, Japanese Culture, Retro\" or \"Psychedelic, 70s, Mushroom, Typography\". Prefer multiple precise themes over one generic value. Do not map words like classic, beautiful, elegant, stylish, calligraphy, arch, traditional, or vintage to unrelated cultural/religious themes.\n"
                "- frame_style: e.g. \"Framed\", \"Unframed\", \"Black frame\", \"Natural wood\"\n"
                "- condition: e.g. \"New\", \"Like New\"\n"
                "- decoration_material: e.g. \"Paper\", \"Canvas\", \"Wood\", \"Metal\", \"Mixed Materials\"\n"
                "- artwork_frame_material: e.g. \"Wood\", \"Metal\", \"Unframed\", \"Black metal\"\n"
                "- art_movement: e.g. \"Contemporary\", \"Modern\", \"Impressionism\", \"Art Nouveau\", \"Minimalism\". Do not overclaim a historic movement unless visually supported.\n"
                "- art_style: e.g. \"Illustrative, Ornate\" or \"Illustrative, Vintage, Botanical\" or \"Illustrative, Retrofuturist\". Prefer multiple specific visual styles when visible, but never invent a movement/style just to fill space.\n"
                "- artwork_authenticity: usually \"Reproduction\" or \"Print\" for wall art/posters\n"
                "- material: e.g. \"Paper\", \"Fine art paper\", \"Canvas\", \"Digital print\"\n"
                "- orientation: \"Portrait\", \"Landscape\", \"Square\", or \"Panoramic\"\n"
                "- Fill subject, palette, composition, material and other observable fields specifically. Also populate room, mood, occasion and display_suggestion with neutral, evidence-based values (best-fit rooms, visual moods, a plausible gift/use occasion or 'Year-round', and one natural display sentence). Never infer religion, identity, or demographics from a design.\n"
                "- audience is left empty by default because it describes people. To enable it, add an editable line '- ALLOW MERCHANDISING FIELD: audience'; its values must still be supported by product evidence. The same directive also works for room, mood, occasion or display_suggestion, but those are already populated by default.\n"
                "- For poster/wall-art print products, default material to \"Paper\", frame_style to \"Unframed\", artwork_authenticity to \"Reproduction\", condition to \"New\", and artwork_frame_material to \"Unframed\" unless the image/settings clearly prove otherwise."
            )
        }),
        ("seo_title", {
            "title": "SEO Title",
            "hint": "Controls the SEO title (browser tab / search result title). Can match or vary from the product title; influences how your product appears in search.",
            "content": (
                "SEO TITLE REQUIREMENTS:\n"
                "- 50-70 characters (Google displays ~60)\n"
                "- Can match the product title or be a shorter/varied version optimized for search\n"
                "- Front-load the primary keyword\n"
                "- Example: \"Purple Pansy Wall Art | Botanical Flower Print\""
            )
        }),
        ("google_shopping", {
            "title": "Google Shopping Guidance",
            "hint": "Guides Google Merchant Center fields: category, gender, age group, condition, and custom labels. Affects how your products appear in Google Shopping and Ads.",
            "locked_content": (
                "GOOGLE SHOPPING FIELDS:\n"
                "Generate these fields for Google Shopping / Merchant Center integration:\n"
                "- \"google_product_category\": The most specific matching Google product taxonomy category.\n"
                "- \"gender\": Target gender (\"Unisex\", \"Male\", or \"Female\").\n"
                "- \"age_group\": Target age group (\"Adult\" or \"Kids\").\n"
                "- \"condition\": Product condition (\"New\", \"Refurbished\", or \"Used\").\n"
                "- \"custom_product\": Boolean. true only when the item genuinely has no assigned GTIN/MPN; false when standard identifiers exist. Never invent a GTIN, barcode, MPN, brand, or manufacturer.\n"
                "- \"custom_label_0\": A short style tag.\n"
                "- \"custom_label_1\": A short room/use tag.\n"
                "- \"custom_label_2\": A short color/palette tag.\n"
                "- \"custom_label_3\": A short subject/theme tag.\n"
                "- \"custom_label_4\": A short evidence-supported audience/occasion tag, or a neutral product-format tag when no specific audience/occasion is supported.\n"
                "The product creator supplies deterministic commerce data outside the AI response: vendor as brand, stable product and variant IDs, Size as the variant option, unique SKUs, configured prices, availability, packaged weights, currency, product URL, and consistent variant grouping. Do not guess or replace those values. Your job is to return every Google Shopping field listed above accurately so it is written to Shopify when the product is created."
            ),
            "content": (
                "Guidance for Google Shopping values (edit examples/preferences here):\n"
                "- google_product_category: e.g. \"Home & Garden > Decor > Artwork > Posters, Prints & Visual Artwork\". Pick the most specific matching Google taxonomy category.\n"
                "- gender: For wall art, home decor, and most general products use \"Unisex\". Only use \"Male\" or \"Female\" if clearly gender-specific.\n"
                "- age_group: Use \"Adult\" for most products. Use \"Kids\" only for children's items (e.g. nursery art).\n"
                "- condition: Use \"New\" for print-on-demand, new stock, or any non-secondhand items.\n"
                "- custom_product: Set to true for unique/custom items with no standard barcode.\n"
                "- Identifier strategy: if custom_product is true, do not fabricate identifiers. If it is false, identifiers must come from supplied catalog data rather than image inference. Vendor/brand is supplied by the product settings.\n"
                "- custom_label_0 (visual style/ad group): e.g. \"Minimalist\", \"Vintage\", \"Abstract\", \"Photographic\", \"Illustrative\"\n"
                "- custom_label_1 (primary room/use): e.g. \"Kitchen Decor\", \"Bedroom Art\", \"Office Wall\", \"Living Room\", \"Gift\"\n"
                "- custom_label_2 (dominant palette): e.g. \"Pink\", \"Earth Tones\", \"Monochrome\", \"Blue & White\"\n"
                "- custom_label_3 (specific subject/theme): e.g. \"Botanical\", \"Animals\", \"Landscape\", \"Typography\", \"Food & Drink\"\n"
                "- custom_label_4 (season/audience/occasion): e.g. \"Gift for Her\", \"Housewarming\", \"Summer Decor\", \"Nursery\", \"Coastal Home\"\n"
                "- For color/palette fields, preserve the main visible colors as specifically as the platform allows. If the artwork is pink, teal, orange, and white, do not simplify it to blue/green unless those are the only allowed options in a provided taxonomy list.\n"
                "- For Shopify category attribute picks, choose the closest exact allowed values to the visible image. Prefer specific visible colors and themes over generic values such as multicolor, art, or modern when better allowed values exist.\n"
                "- Make custom labels concise but commercially useful for Google Ads segmentation. Avoid unsupported audience, religious, cultural, demographic, room, and gift assumptions. Use a specific image-grounded label where possible and a neutral product-format label otherwise.\n"
                "- suggested_tags should contain 16-24 focused tags selected from relevant observable attributes such as subject, function, style, colour, material, construction and product format. Avoid duplicates, speculative audiences/rooms/moods, and tags that do not match the product."
            )
        }),
        ("output_format", {
            "title": "Output Format",
            "locked": True,
            "content": (
                "OUTPUT FORMAT:\n"
                "Return valid JSON with these exact fields. The \"description\" value MUST be an HTML string using <p> tags to separate paragraphs. Use 3 paragraphs, 120-180 words total. Do NOT include bullet points, asterisks, dashes, or list markup of any kind in the description. The \"meta_description\" MUST be a non-empty complete sentence, 140-155 characters, and never more than 160 characters. Do not return blank, null, placeholder text, or a clipped sentence for meta_description.\n"
                "{\n"
                "  \"title\": \"...\",\n"
                "  \"seo_title\": \"...\",\n"
                "  \"meta_description\": \"...\",\n"
                "  \"short_description\": \"50-80 word plain-text summary\",\n"
                "  \"description\": \"<p>First paragraph.</p><p>Second paragraph.</p><p>Third paragraph.</p>\",\n"
                "  \"product_specific_faq\": [{\"question\": \"... ?\", \"answer\": \"...\"}],\n"
                "  \"generic_faq\": [{\"question\": \"... ?\", \"answer\": \"...\"}],\n"
                "  \"alt_text\": \"...\",\n"
                "  \"suggested_tags\": [\"keyword1\", \"keyword2\", ...],\n"
                "  \"google_product_category\": \"...\",\n"
                "  \"gender\": \"Unisex\",\n"
                "  \"age_group\": \"Adult\",\n"
                "  \"condition\": \"New\",\n"
                "  \"custom_product\": true,\n"
                "  \"custom_label_0\": \"...\",\n"
                "  \"custom_label_1\": \"...\",\n"
                "  \"custom_label_2\": \"...\",\n"
                "  \"custom_label_3\": \"...\",\n"
                "  \"custom_label_4\": \"...\",\n"
                "  \"metafields\": {\n"
                "    \"color\": \"...\",\n"
                "    \"theme\": \"...\",\n"
                "    \"frame_style\": \"...\",\n"
                "    \"condition\": \"...\",\n"
                "    \"decoration_material\": \"...\",\n"
                "    \"artwork_frame_material\": \"...\",\n"
                "    \"art_movement\": \"...\",\n"
                "    \"art_style\": \"...\",\n"
                "    \"artwork_authenticity\": \"...\",\n"
                "    \"material\": \"...\",\n"
                "    \"orientation\": \"...\",\n"
                "    \"subject\": \"...\",\n"
                "    \"room\": \"...\",\n"
                "    \"mood\": \"...\",\n"
                "    \"palette\": \"...\",\n"
                "    \"audience\": \"...\",\n"
                "    \"occasion\": \"...\",\n"
                "    \"season\": \"...\",\n"
                "    \"composition\": \"...\",\n"
                "    \"display_suggestion\": \"...\"\n"
                "  },\n"
                "  \"category_attribute_picks\": {\n"
                "    \"Color\": [\"...\"],\n"
                "    \"Material\": [\"Paper\"],\n"
                "    \"Art movement\": [\"...\"],\n"
                "    \"Art style\": [\"...\"],\n"
                "    \"Artwork authenticity\": [\"Reproduction\"],\n"
                "    \"Frame style\": [\"Unframed\"],\n"
                "    \"Orientation\": [\"...\"],\n"
                "    \"Theme\": [\"...\"]\n"
                "  }\n"
                "}"
            )
        }),
    ])


def _section_effective_content(sec):
    """Return the full prompt text for a single section.
    
    If the section has a 'locked_content' prefix it is prepended to 'content'.
    """
    locked = sec.get("locked_content", "")
    content = sec.get("content", "")
    if locked:
        return locked + "\n" + content
    return content


def _join_prompt_sections(sections):
    """Join prompt sections dict back into a single flat prompt string."""
    return "\n\n".join(_section_effective_content(s) for s in sections.values())


def get_locked_prompt_text():
    """Return the combined prompt text of all fully-locked and locked_content portions.
    
    This is used to ensure locked portions are always present in the final
    prompt even when the user supplies a full prompt override.
    """
    parts = []
    for sec in get_prompt_sections().values():
        if sec.get("locked"):
            # Entirely locked section — include its full content
            parts.append(_section_effective_content(sec))
        elif sec.get("locked_content"):
            # Mixed section — include only the locked prefix
            parts.append(sec["locked_content"])
    return "\n\n".join(parts)


# Build the flat DEFAULT_BASE_PROMPT from the authoritative sections dict
DEFAULT_BASE_PROMPT = _join_prompt_sections(get_prompt_sections())


def _format_category_attribute_prompt(category_attribute_options):
    """Format Shopify category attributes for the single metadata AI call."""
    if not isinstance(category_attribute_options, dict) or not category_attribute_options:
        return ""

    lines = [
        "SHOPIFY CATEGORY ATTRIBUTE VALUES:",
        "Add a \"category_attribute_picks\" object to the JSON response.",
        "For each attribute, choose 0-3 values that fit the product image.",
        "Only use exact values from the allowed lists below. Do not invent values.",
        "Choose the closest allowed values to the visible image, prioritising specific subject/style/color evidence over broad labels.",
        "For Color, pick the closest visible colors available in the list; use Multicolor only when the image is genuinely mixed or when no closer allowed colors exist.",
        "For Theme and Art style, avoid generic values like Art, Modern, or Contemporary when the image supports more specific allowed values.",
        "For Art movement, always return one allowed value: pick the specific movement when the image supports one, otherwise pick the closest broad allowed value such as Contemporary.",
        "If no allowed value fits an attribute honestly, return an empty list for that attribute.",
    ]
    for attr_name, options in category_attribute_options.items():
        if not options:
            continue
        options_text = ", ".join(str(option) for option in options)
        lines.append(f"- {attr_name}: {options_text}")
    lines.append(
        "Example: \"category_attribute_picks\": {\"Color\": [\"Blue\"], \"Material\": [\"Paper\"]}"
    )
    return "\n".join(lines)


def create_gemini_prompt(custom_instructions="", available_collections=None, full_prompt_override=None, category_attribute_options=None):
    """Create the product metadata generation prompt for Gemini.
    
    If full_prompt_override is provided (non-empty string), it replaces the
    editable parts of the prompt.  Locked / structural sections (output format,
    metafield keys, Google Shopping field names) are **always** appended so
    they can never be accidentally removed by the user.
    """

    if full_prompt_override and full_prompt_override.strip():
        # User sent a full prompt from the accordion — it already contains the
        # editable content.  Append the locked structural portions that the UI
        # does not expose so they are always present in the final prompt.
        base_prompt = full_prompt_override.strip()
        locked_text = get_locked_prompt_text()
        if locked_text:
            base_prompt += "\n\n" + locked_text
        # Profiles saved before new editable prompt sections existed contain a
        # complete flat prompt. Backfill only missing sections so new listing
        # fields work immediately without replacing the user's custom prompt.
        sections = get_prompt_sections()
        for section_id, marker in (
            ("short_description", "SHORT DESCRIPTION REQUIREMENTS:"),
            ("product_specific_faq", "PRODUCT-SPECIFIC FAQ REQUIREMENTS:"),
            ("faq", "STORE-WIDE FAQ REQUIREMENTS:"),
        ):
            if marker not in base_prompt:
                section_content = sections.get(section_id, {}).get("content", "").strip()
                if section_content:
                    base_prompt += "\n\n" + section_content
    else:
        base_prompt = DEFAULT_BASE_PROMPT

    if custom_instructions.strip():
        base_prompt += f"\n\nCUSTOM INSTRUCTIONS FROM USER:\n{custom_instructions.strip()}\nPlease incorporate these specific requirements into your analysis and copywriting."

    if available_collections and len(available_collections) > 0:
        collections_list = ", ".join(available_collections)
        base_prompt += f'\n\nAVAILABLE COLLECTIONS: Choose the most appropriate collection(s) from this list: {collections_list}. Add a "collections" array to your JSON with the exact collection name(s) you choose (e.g. "collections": ["Collection Name"]).'
    else:
        base_prompt += '\n\nCOLLECTIONS: No collections are available from the store. You may omit the "collections" field or leave it as an empty array.'

    category_attribute_prompt = _format_category_attribute_prompt(category_attribute_options)
    if category_attribute_prompt:
        base_prompt += "\n\n" + category_attribute_prompt

    required_fact_checklist = _final_required_description_checklist(base_prompt)
    if required_fact_checklist:
        base_prompt += "\n\n" + required_fact_checklist

    return base_prompt

def analyze_product_category_visual_cues(image_path):
    """
    Perform detailed visual analysis to determine accurate product category
    This function focuses specifically on category accuracy
    """
    if os.environ.get("GEMINI_ENABLE_CATEGORY_PREPASS", "false").lower() not in {"1", "true", "yes"}:
        logger.info("Skipping enhanced category pre-pass to keep one AI image analysis per listing")
        return None, 'low'

    try:
        logger.info(f"🔍 Performing enhanced visual category analysis: {image_path}")
        
        # Check API key availability before proceeding
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            logger.warning("⚠️ GEMINI_API_KEY not available for category analysis, skipping enhanced categorization")
            return None, 'low'
        
        with open(image_path, "rb") as image_file:
            image_bytes = image_file.read()
        
        category_prompt = """You are a Shopify product categorization expert. Analyze the complete image and determine the most specific supported category in Shopify's Standard Product Taxonomy.

Identify the actual item being sold, its function, visible construction and supported material cues. Distinguish the product from props, packaging, models, room settings and mockup surroundings. Do not infer a material, manufacturing method or included accessory that the image does not establish. Prefer a specific taxonomy leaf only when the evidence supports it; otherwise choose the accurate broader parent category.

Return only JSON in this form:
{
    "category": "Exact Shopify taxonomy path",
    "confidence": "high|medium|low",
    "reasoning": "Brief explanation of the decisive visible product cues"
}"""

        try:
            logger.info(f"⏱️  Category analysis API call timeout set to 60 seconds")
            
            # Use ThreadPoolExecutor for proper timeout handling
            client = get_client()
            
            def make_api_call():
                return client.models.generate_content(
                    model="gemini-3.1-flash-lite",
                    contents=[
                        types.Part.from_bytes(
                            data=image_bytes,
                            mime_type="image/jpeg",
                        ),
                        category_prompt
                    ],
                    config=_gemini_config("gemini-3.1-flash-lite", types.GenerateContentConfig(
                        response_mime_type="application/json",
                        # Bound thinking + output so Flash-Lite cannot overrun the worker.
                        # 8192 is ample for the JSON; thinking_budget caps slow reasoning
                        # while preserving classification quality.
                        max_output_tokens=8192,
                        thinking_config=types.ThinkingConfig(thinking_budget=1024),
                        temperature=0.1  # Lower temperature for more consistent categorization
                    ))
                )
            
            try:
                response = _run_with_hard_timeout(make_api_call, 60)
            except FutureTimeoutError:
                logger.warning("⚠️ Category analysis API call timed out after 60 seconds")
                return None, 'low'
            except Exception as e:
                logger.error(f"❌ Category analysis API call failed: {str(e)}")
                logger.error(f"❌ Error type: {type(e).__name__}")
                return None, 'low'
            
            if response and response.text:
                category_data = json.loads(response.text)
                logger.info(f"✅ Enhanced category analysis: {category_data.get('category')} (confidence: {category_data.get('confidence')})")
                logger.info(f"📝 Reasoning: {category_data.get('reasoning')}")
                return category_data.get('category'), category_data.get('confidence', 'medium')
        except ValueError as ve:
            # API key missing - already logged in get_client()
            logger.warning("⚠️ Skipping enhanced category analysis due to missing API key")
            return None, 'low'
        except Exception as api_error:
            logger.error(f"❌ Category analysis API call failed: {str(api_error)}")
            logger.error(f"❌ Error type: {type(api_error).__name__}")
            return None, 'low'
        
    except Exception as e:
        logger.error(f"Enhanced category analysis failed: {str(e)}")
        logger.error(f"Error type: {type(e).__name__}")
        return None, 'low'
    
    return None, 'low'

def generate_product_metadata(image_path, custom_prompt="", available_collections=None, category_attribute_options=None,
                              model_name=None):
    logger.warning(f"🔍 GEMINI: Starting metadata generation for: {image_path}")
    """Generate product metadata using Gemini 2.5 Flash vision analysis with enhanced category detection"""
    try:
        # CRITICAL: Validate API key availability upfront
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            error_msg = "GEMINI_API_KEY environment variable is not set. Cannot generate metadata."
            logger.error(f"❌ {error_msg}")
            logger.error("❌ Please ensure the server was started using run_server.py which sets the API key.")
            logger.error("❌ Or set GEMINI_API_KEY as an environment variable before starting the server.")
            return None
        
        logger.info(f"Analyzing image with Gemini 2.5 Flash: {image_path}")
        logger.info(f"📋 Image file size: {os.path.getsize(image_path) / (1024*1024):.2f} MB")
        logger.info(f"📋 Image exists: {os.path.exists(image_path)}")
        
        # The main metadata prompt already asks Gemini for the Shopify category.
        # Keep this optional because it is a second vision API call before the
        # listing can progress, which is too fragile on the free Render instance.
        if os.environ.get("ENABLE_ENHANCED_CATEGORY_ANALYSIS", "").lower() in {"1", "true", "yes"}:
            logger.info(f"🔍 Step 1: Starting enhanced category analysis...")
            try:
                enhanced_category, confidence = analyze_product_category_visual_cues(image_path)
                logger.info(f"🎯 Enhanced category detection: {enhanced_category} (confidence: {confidence})")
            except Exception as cat_error:
                logger.warning(f"⚠️ Category analysis failed, continuing without it: {str(cat_error)}")
                enhanced_category, confidence = None, 'low'
        else:
            enhanced_category, confidence = None, 'low'
        
        # Read and encode image
        with open(image_path, "rb") as image_file:
            image_bytes = image_file.read()
        
        # Create prompt: if the user sent a full prompt (contains recognisable
        # section headers from the accordion), treat it as a full override;
        # otherwise treat as custom instructions appended to the default.
        if custom_prompt and 'PRODUCT TITLE REQUIREMENTS:' in custom_prompt and 'META DESCRIPTION REQUIREMENTS:' in custom_prompt:
            prompt = create_gemini_prompt(
                "",
                available_collections,
                full_prompt_override=custom_prompt,
                category_attribute_options=category_attribute_options,
            )
        else:
            prompt = create_gemini_prompt(
                custom_prompt,
                available_collections,
                category_attribute_options=category_attribute_options,
            )
        
        # If we have a high-confidence enhanced category, inject it into the prompt
        if enhanced_category and confidence in ['high', 'medium']:
            prompt += f"\n\nIMPORTANT: Based on detailed visual analysis, the most accurate category for this product is: '{enhanced_category}'. Use this exact category unless you strongly disagree based on visual evidence."
        
        # One explicit multimodal call. Text-only repairs and discovery curation
        # are disabled by default so a listing does not silently fan out into
        # several billable requests.
        try:
            logger.warning(f"🚀 CALLING GEMINI API with prompt length: {len(prompt)} chars")
            logger.info(f"⏱️  API call timeout set to 120 seconds")
            logger.info(f"📋 Image bytes size: {len(image_bytes) / (1024*1024):.2f} MB")
            
            # Get client - this will raise ValueError if API key is missing, but we already checked above
            logger.info(f"🔑 Getting Gemini client...")
            client = get_client()
            logger.info(f"✅ Gemini client obtained successfully")
            
            selected_model = str(model_name or os.environ.get('GEMINI_LISTING_MODEL') or 'gemini-3.1-flash-lite').strip()
            allowed_models = {'gemini-3.1-flash-lite', 'gemini-3.5-flash'}
            if selected_model not in allowed_models:
                selected_model = 'gemini-3.1-flash-lite'

            # Use ThreadPoolExecutor for proper timeout handling
            def make_api_call():
                return client.models.generate_content(
                    model=selected_model,
                    contents=[
                        types.Part.from_bytes(
                            data=image_bytes,
                            mime_type="image/jpeg",
                        ),
                        prompt
                    ],
                    config=_gemini_config(selected_model, types.GenerateContentConfig(
                        response_mime_type="application/json",
                        # Bound thinking+output so the analysis step stays short
                        # enough for Render free tier status polling.
                        max_output_tokens=int(os.environ.get("GEMINI_MAIN_OUTPUT_TOKENS", "4096")),
                        thinking_config=types.ThinkingConfig(
                            thinking_budget=int(os.environ.get("GEMINI_MAIN_THINKING_BUDGET", "512"))
                        ),
                        temperature=0.7
                    ))
                )
            
            try:
                main_timeout_s = int(os.environ.get("GEMINI_MAIN_TIMEOUT_S", "35"))
                response = _run_with_hard_timeout(make_api_call, main_timeout_s)
            except FutureTimeoutError:
                logger.error(f"❌ GEMINI API CALL TIMED OUT after {main_timeout_s} seconds")
                logger.error("❌ The API call is taking too long. This may indicate:")
                logger.error("   - Network connectivity issues")
                logger.error("   - API service problems")
                logger.error("   - Image file too large")
                return None
            except Exception as e:
                logger.error(f"❌ GEMINI API CALL FAILED: {str(e)}")
                logger.error(f"❌ ERROR TYPE: {type(e).__name__}")
                logger.error(f"❌ ERROR DETAILS: {repr(e)}")
                import traceback
                logger.error(f"❌ TRACEBACK: {traceback.format_exc()}")
                return None
            
            if not response:
                logger.error("❌ GEMINI API returned None response")
                return None
            
            logger.warning(f"✅ GEMINI API RESPONDED: {len(response.text) if response.text else 0} chars")
        except ValueError as ve:
            # API key missing - should not happen due to upfront check, but handle gracefully
            logger.error(f"❌ GEMINI API KEY ERROR: {str(ve)}")
            logger.error("❌ This should not happen - API key was checked upfront. Possible thread context issue.")
            return None
        except Exception as api_error:
            logger.error(f"❌ GEMINI API CALL FAILED: {str(api_error)}")
            logger.error(f"❌ ERROR TYPE: {type(api_error).__name__}")
            logger.error(f"❌ ERROR DETAILS: {repr(api_error)}")
            import traceback
            logger.error(f"❌ TRACEBACK: {traceback.format_exc()}")
            return None
        
        if not response.text:
            logger.error("❌ EMPTY RESPONSE FROM GEMINI")
            logger.error(f"❌ RESPONSE OBJECT: {response}")
            return None
        
        logger.info(f"Gemini response received: {len(response.text)} chars")
        logger.info(f"🔍 RAW AI RESPONSE: {response.text[:1000]}...")
        
        # Parse JSON response
        try:
            # Clean up the response text - sometimes it gets truncated
            response_text = response.text.strip()
            
            # Try to repair truncated JSON
            if not response_text.endswith('}'):
                logger.warning("⚠️  JSON appears truncated, attempting repair...")
                
                # More aggressive JSON repair - look for last complete field
                try:
                    # Try to find the last complete JSON field by looking for patterns
                    text_parts = response_text.split('\n')
                    
                    # Build JSON by keeping complete fields
                    json_parts = ['{']
                    
                    current_field = None
                    for line in text_parts[1:]:  # Skip first { line
                        stripped = line.strip()
                        
                        # Skip empty lines
                        if not stripped:
                            continue
                            
                        # Look for complete field patterns
                        if '"' in stripped and ':' in stripped and (stripped.endswith(',') or stripped.endswith('"')):
                            json_parts.append('  ' + stripped)
                        elif stripped.startswith('"') and ('":' in stripped or '": [' in stripped or '": {' in stripped):
                            # This looks like a field start
                            json_parts.append('  ' + stripped)
                        elif stripped in ['],', '}', ']', '},']:
                            json_parts.append('  ' + stripped)
                    
                    # Remove trailing comma and close JSON
                    if len(json_parts) > 1:
                        last_line = json_parts[-1].strip()
                        if last_line.endswith(','):
                            json_parts[-1] = '  ' + last_line[:-1]
                        json_parts.append('}')
                        
                        response_text = '\n'.join(json_parts)
                        logger.warning(f"🔧 Repaired JSON: {response_text[:200]}...")
                    else:
                        logger.error("Could not repair JSON - falling back to defaults")
                        response_text = None
                        
                except Exception as repair_error:
                    logger.error(f"JSON repair failed: {repair_error}")
                    response_text = None
                    
                # No fallback metadata: an unparseable AI response fails the job.
                if not response_text:
                    logger.error("AI response could not be parsed or repaired; refusing to publish a placeholder listing")
                    return None
            
            metadata = json.loads(response_text)
            usage = getattr(response, 'usage_metadata', None)
            metadata['_ai_usage'] = {
                'model': selected_model,
                'prompt_tokens': int(getattr(usage, 'prompt_token_count', 0) or 0),
                'output_tokens': int(getattr(usage, 'candidates_token_count', 0) or 0),
                'thinking_tokens': int(getattr(usage, 'thoughts_token_count', 0) or 0),
                'total_tokens': int(getattr(usage, 'total_token_count', 0) or 0),
                'calls': 1,
            }
            # Use safe logging without emojis to avoid Unicode encoding errors on Windows
            try:
                logger.info(f"DEBUG: Parsed metadata: {metadata}")
            except UnicodeEncodeError:
                logger.info("DEBUG: Parsed metadata successfully (logging details skipped due to encoding)")

            # Normalize new prompt output: suggested_tags -> tags
            if 'tags' not in metadata or not isinstance(metadata['tags'], list):
                metadata['tags'] = metadata.get('suggested_tags') if isinstance(metadata.get('suggested_tags'), list) else ['poster', 'wall art', 'print', 'decor', 'artwork']

            # CRITICAL DEBUGGING: Track category at source
            ai_category = metadata.get('category', 'No category')
            try:
                logger.warning(f"GEMINI: Raw AI category response: '{ai_category}'")
            except UnicodeEncodeError:
                logger.warning(f"GEMINI: Raw AI category response: {ai_category}")

            # Validate required fields
            required_fields = ['title', 'description']
            for field in required_fields:
                if field not in metadata:
                    logger.error(f"Missing required field: {field}")
                    return None

            metadata['title'] = _clean_product_title(metadata.get('title', ''))
            if not metadata['title']:
                logger.error("Generated product title was empty after normalization")
                return None

            # Set defaults for missing optional fields
            if 'alt_text' not in metadata:
                metadata['alt_text'] = metadata['title']

            # Derive SEO handle/filename from title if not provided (new prompt does not request these)
            _title = metadata.get('title', '')
            _slug = _title.lower().replace('|', ' ').replace('  ', ' ').strip().replace(' ', '-')
            for c in '"\'!?,:;':
                _slug = _slug.replace(c, '')
            if 'seo_url_handle' not in metadata or not (metadata.get('seo_url_handle') or '').strip():
                metadata['seo_url_handle'] = _slug or 'product'
            if 'seo_filename' not in metadata or not (metadata.get('seo_filename') or '').strip():
                metadata['seo_filename'] = _slug or 'product'
            if 'product_type' not in metadata or not (metadata.get('product_type') or '').strip():
                metadata['product_type'] = 'Poster'

            # Normalize SEO title: default to product title (word-safe) if AI didn't return one
            if not (metadata.get('seo_title') or '').strip():
                metadata['seo_title'] = _word_safe_truncate(_title, 70) if _title else ''

            # Normalize Google Shopping fields with sensible defaults
            metadata['google_product_category'] = '500044'
            if not (metadata.get('gender') or '').strip():
                metadata['gender'] = 'Unisex'
            if not (metadata.get('age_group') or '').strip():
                metadata['age_group'] = 'Adult'
            if not (metadata.get('condition') or '').strip():
                metadata['condition'] = 'New'
            # custom_product can be bool or string; normalize to string for CSV
            cp = metadata.get('custom_product')
            if cp is None:
                metadata['custom_product'] = 'TRUE'
            elif isinstance(cp, bool):
                metadata['custom_product'] = 'TRUE' if cp else 'FALSE'
            else:
                metadata['custom_product'] = str(cp).strip().upper() if str(cp).strip() else 'TRUE'
            # Custom labels: default to empty string if missing
            for i in range(5):
                key = f'custom_label_{i}'
                if not (metadata.get(key) or '').strip():
                    metadata[key] = ''
            
            if 'category' not in metadata:
                # Use enhanced category if available, otherwise use fallback
                if enhanced_category:
                    metadata['category'] = enhanced_category
                    try:
                        logger.info(f"Using enhanced category analysis: {enhanced_category}")
                    except UnicodeEncodeError:
                        logger.info("Using enhanced category analysis")
                else:
                    metadata['category'] = 'Home & Garden > Decor > Artwork > Posters, Prints, & Visual Artwork'
                    try:
                        logger.info("Using default category fallback")
                    except UnicodeEncodeError:
                        pass
            else:
                try:
                    logger.info(f"AI RETURNED CATEGORY: '{metadata['category']}'")
                except UnicodeEncodeError:
                    logger.info(f"AI RETURNED CATEGORY: {metadata['category']}")
                
            # Double-check the final category value
            final_category = metadata.get('category', 'NO CATEGORY')
            try:
                logger.info(f"FINAL CATEGORY before mapping: '{final_category}'")
            except UnicodeEncodeError:
                logger.info(f"FINAL CATEGORY before mapping: {final_category}")
            
            if 'collections' not in metadata or not isinstance(metadata['collections'], list):
                metadata['collections'] = []

            if 'category_attribute_picks' not in metadata or not isinstance(metadata.get('category_attribute_picks'), dict):
                metadata['category_attribute_picks'] = {}

            metadata['meta_description'] = _clean_meta_description(metadata.get('meta_description', ''), metadata)
            palette_label = str(metadata.get('custom_label_2') or '').strip()
            
            # Check for metafields and provide defaults if AI didn't generate them
            if 'metafields' not in metadata or not isinstance(metadata['metafields'], dict):
                logger.warning("⚠️ AI did not generate metafields, using universal defaults")
                # Create basic metafields including category metafields (Condition, Decoration material, Artwork frame material)
                metadata['metafields'] = {
                    "material": "Paper",
                    "style": "Contemporary",
                    "color": palette_label.replace("&", ",") if palette_label else "Multi-color",
                    "theme": "General",
                    "frame_style": "Unframed",
                    "condition": "New",
                    "decoration_material": "Paper",
                    "artwork_frame_material": "Unframed",
                    "art_movement": "Contemporary",
                    "art_style": "Illustrative",
                    "artwork_authenticity": "Reproduction",
                    "subject": metadata.get('custom_label_3') or "Wall art",
                    "room": metadata.get('custom_label_1') or "",
                    "mood": "",
                    "palette": metadata.get('custom_label_2') or "Multi-color",
                    "audience": "",
                    "occasion": "",
                    "season": "Year-round",
                    "composition": "Wall art composition",
                    "display_suggestion": ""
                }
            else:
                logger.info(f"✅ AI-generated metafields found: {metadata['metafields']}")
                mf = metadata['metafields']
                # Ensure product metafields exist
                if not mf.get('theme') or not str(mf.get('theme', '')).strip():
                    mf['theme'] = 'General'
                if not mf.get('frame_style') or not str(mf.get('frame_style', '')).strip():
                    mf['frame_style'] = 'Unframed'
                if not mf.get('color') or not str(mf.get('color', '')).strip():
                    mf['color'] = 'Multi-color'
                if palette_label and str(mf.get('color', '')).strip().casefold() in {'multi-color', 'multicolor', 'multi color'}:
                    mf['color'] = palette_label.replace("&", ",")
                # Ensure category metafields exist so they are sent to Shopify
                if not mf.get('condition') or not str(mf.get('condition', '')).strip():
                    mf['condition'] = 'New'
                if not mf.get('decoration_material') or not str(mf.get('decoration_material', '')).strip():
                    mf['decoration_material'] = 'Mixed Materials'
                if not mf.get('artwork_frame_material') or not str(mf.get('artwork_frame_material', '')).strip():
                    mf['artwork_frame_material'] = 'Unframed'
                if not mf.get('material') or not str(mf.get('material', '')).strip():
                    mf['material'] = 'Paper'
                if not mf.get('art_movement') or not str(mf.get('art_movement', '')).strip():
                    mf['art_movement'] = 'Contemporary'
                if not mf.get('art_style') or not str(mf.get('art_style', '')).strip():
                    mf['art_style'] = 'Illustrative'
                if not mf.get('artwork_authenticity') or not str(mf.get('artwork_authenticity', '')).strip():
                    mf['artwork_authenticity'] = 'Reproduction'
                if not mf.get('subject') or not str(mf.get('subject', '')).strip():
                    mf['subject'] = metadata.get('custom_label_3') or metadata.get('title', '').split('|')[0].strip() or 'Wall art'
                # Do not inject uniform filler for the interpretive merchandising
                # fields; leave them to the model's evidence-based values so the
                # catalogue is not curve-fit to one generic room/mood/occasion.
                if not mf.get('room') or not str(mf.get('room', '')).strip():
                    mf['room'] = metadata.get('custom_label_1') or ''
                if not mf.get('mood') or not str(mf.get('mood', '')).strip():
                    mf['mood'] = ''
                if not mf.get('palette') or not str(mf.get('palette', '')).strip():
                    mf['palette'] = mf.get('color') or metadata.get('custom_label_2') or 'Multi-color'
                if not mf.get('audience') or not str(mf.get('audience', '')).strip():
                    mf['audience'] = metadata.get('custom_label_4') or ''
                if not mf.get('occasion') or not str(mf.get('occasion', '')).strip():
                    mf['occasion'] = ''
                if not mf.get('season') or not str(mf.get('season', '')).strip():
                    mf['season'] = 'Year-round'
                if not mf.get('composition') or not str(mf.get('composition', '')).strip():
                    mf['composition'] = metadata.get('title', '').split('|')[0].strip() or 'Wall art composition'
                if not mf.get('display_suggestion') or not str(mf.get('display_suggestion', '')).strip():
                    mf['display_suggestion'] = ''

            metadata = _remove_unsupported_sensitive_themes(metadata)
            metadata = _prune_unsupported_merchandising_inferences(metadata, custom_prompt)
            raw_tags = metadata.get('suggested_tags') if isinstance(metadata.get('suggested_tags'), list) else []
            metadata['suggested_tags'] = [
                tag for tag in raw_tags
                if str(tag).casefold().strip() not in {'spiritual', 'spirituality'}
            ]
            metadata['suggested_tags'] = _expand_tags(metadata)
            metadata['tags'] = metadata['suggested_tags']
            metadata['description'] = _clean_product_description(metadata.get('description', ''), metadata)
            missing_description_facts = _missing_required_description_facts(
                metadata['description'], custom_prompt
            )
            if missing_description_facts:
                readable_missing = [" || ".join(options) for options in missing_description_facts]
                logger.error(
                    "Generated description omitted required profile facts: %s",
                    readable_missing,
                )
                return None
            metadata['short_description'] = _clean_short_description(
                metadata.get('short_description', ''), metadata['description']
            )
            product_specific_faq = _normalise_product_faq(
                metadata.get('product_specific_faq'), metadata, limit=4
            )
            generic_faq = _normalise_product_faq(
                metadata.get('generic_faq', metadata.get('faq')), metadata, limit=6
            )
            if _profile_requires_faq(custom_prompt, 'product_specific') and len(product_specific_faq) < 3:
                logger.error("Generated metadata omitted product-specific FAQs required by the active profile")
                return None
            if _profile_requires_faq(custom_prompt, 'generic') and len(generic_faq) < 4:
                logger.error("Generated metadata omitted store-wide FAQs required by the active profile")
                return None
            metadata['product_specific_faq'] = product_specific_faq
            metadata['generic_faq'] = generic_faq
            metadata['faq'] = _combine_product_faqs(product_specific_faq, generic_faq)
            metadata['alt_text'] = _normalise_alt_text(metadata.get('alt_text'), metadata.get('title'))
            
            try:
                logger.info("Successfully generated metadata from Gemini 2.5 Flash")
            except UnicodeEncodeError:
                logger.info("Successfully generated metadata from Gemini 2.5 Flash")
            
            return _normalise_metafields(metadata)
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response from Gemini: {str(e)}")
            try:
                logger.error(f"Raw content: {response.text[:500]}")
            except UnicodeEncodeError:
                logger.error("Raw content: [Unable to log due to encoding]")
            return None
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error in Gemini API call: {error_msg}")
        logger.error(f"Error type: {type(e).__name__}")
        # If it's a Unicode error, the API call actually succeeded - don't fail
        if 'charmap' in error_msg or 'UnicodeEncodeError' in str(type(e)):
            logger.warning("Unicode encoding error in logging - but API call succeeded, returning metadata")
            # Try to return the metadata if we have it
            try:
                if 'metadata' in locals() and metadata:
                    return metadata
            except:
                pass
        return None

def pick_category_attribute_values(image_path, attributes_with_options):
    """
    Ask Gemini to pick 1-3 attribute values per category attribute based on the product image.

    Args:
        image_path: Path to the product image file
        attributes_with_options: Dict of {attribute_name: [option1, option2, ...]}
            e.g. {"Color": ["Black", "Blue", "Red"], "Material": ["Canvas", "Paper"]}

    Returns:
        Dict of {attribute_name: [picked_value1, ...]} or None on failure
        e.g. {"Color": ["Blue", "White"], "Material": ["Paper"]}
    """
    try:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            logger.warning("GEMINI_API_KEY not available, skipping category attribute picking")
            return None

        if not attributes_with_options:
            return None

        logger.info(f"Asking Gemini to pick category attribute values from image: {image_path}")

        with open(image_path, "rb") as image_file:
            image_bytes = image_file.read()

        # Build the attributes list for the prompt
        attr_lines = []
        for attr_name, options in attributes_with_options.items():
            options_str = ", ".join(options[:50])  # Limit options shown to avoid token overflow
            attr_lines.append(f"- {attr_name}: {options_str}")
        attributes_text = "\n".join(attr_lines)

        prompt = f"""Analyze this product image. For each attribute below, pick 1-3 values that best describe the product.
Only pick values from the provided options. If no option fits well, use an empty list for that attribute.
Return valid JSON mapping each attribute name to a list of picked values.

Example response: {{"Color": ["Blue", "White"], "Material": ["Paper"]}}

Attributes:
{attributes_text}"""

        client = get_client()

        def make_api_call():
            return client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=[
                    types.Part.from_bytes(
                        data=image_bytes,
                        mime_type="image/jpeg",
                    ),
                    prompt
                ],
                config=_gemini_config("gemini-3.1-flash-lite", types.GenerateContentConfig(
                    response_mime_type="application/json",
                    # Small JSON only; category enrichment should not block
                    # product creation if Gemini is slow.
                    max_output_tokens=int(os.environ.get("GEMINI_CATEGORY_PICK_OUTPUT_TOKENS", "1024")),
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=int(os.environ.get("GEMINI_CATEGORY_PICK_THINKING_BUDGET", "128"))
                    ),
                    temperature=0.1
                ))
            )

        try:
            pick_timeout_s = int(os.environ.get("GEMINI_CATEGORY_PICK_TIMEOUT_S", "35"))
            response = _run_with_hard_timeout(make_api_call, pick_timeout_s)
        except FutureTimeoutError:
            logger.warning(f"Gemini category attribute picking timed out after {pick_timeout_s} seconds")
            return None
        except Exception as e:
            logger.error(f"Gemini category attribute picking failed: {e}")
            return None

        if not response or not response.text:
            logger.warning("Empty response from Gemini for category attributes")
            return None

        try:
            picks = json.loads(response.text)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Gemini category attribute response: {e}")
            return None

        # Validate: ensure all picked values are from the allowed options
        validated = {}
        for attr_name, picked in picks.items():
            if attr_name not in attributes_with_options:
                continue
            allowed = set(attributes_with_options[attr_name])
            if not isinstance(picked, list):
                picked = [picked]
            valid_picks = [v for v in picked if v in allowed]
            if valid_picks:
                validated[attr_name] = valid_picks

        logger.info(f"Gemini picked category attributes: {validated}")
        return validated if validated else None

    except Exception as e:
        logger.error(f"Error picking category attribute values: {e}")
        return None


def test_gemini_connection():
    """Test Gemini API connection"""
    try:
        # Simple test without image
        response = get_client().models.generate_content(
            model="gemini-3.1-flash-lite",
            contents="Hello, can you respond with 'Gemini connection successful'?"
        )
        return response.text if response.text else None
    except Exception as e:
        logger.error(f"Gemini connection test failed: {e}")
        return None


def generate_collection_metadata(collection, custom_prompt="", model_name="gemini-3.1-flash-lite",
                                 sample_products=None):
    """Write improved copy for one Shopify collection page.

    Text only - there is no image to read, so the model is given the collection
    title, its current copy and a sample of the products inside it. Returns a
    dict with description_html, seo_title and seo_description, or raises. There
    is deliberately no fallback: a collection is left alone rather than filled
    with generic text.
    """
    if not os.environ.get("GEMINI_API_KEY"):
        raise ValueError("GEMINI_API_KEY is not set. Cannot generate collection copy.")

    collection = collection or {}
    payload = {
        "collection_title": collection.get("title") or "",
        "handle": collection.get("handle") or "",
        "product_count": collection.get("product_count") or 0,
        "current_description": _strip_html_tags(collection.get("description_html")
                                                or collection.get("description") or "")[:1200],
        "current_seo_title": collection.get("seo_title") or "",
        "current_seo_description": collection.get("seo_description") or "",
        "example_products": [str(title) for title in (sample_products or [])][:12],
    }
    prompt = (
        "You are writing factual Shopify collection page copy for an art print store.\n"
        "Return JSON only: {\"description_html\":\"...\",\"seo_title\":\"...\",\"seo_description\":\"...\"}\n"
        "Rules:\n"
        "- Write about what is actually in this collection, using the collection title and the example products.\n"
        "- description_html: 2 to 3 short paragraphs wrapped in <p> tags. Plain factual language a shopper can use.\n"
        "- Describe what the prints show, who the collection suits, and where the prints work in a home.\n"
        "- Never invent product names, materials, prices, sizes, delivery times, guarantees or awards.\n"
        "- seo_title: at most 60 characters, leads with the collection subject, no site name.\n"
        "- seo_description: 120 to 155 characters, one or two complete sentences, no call to action.\n"
        "- No emojis, no exclamation marks, no hype words such as stunning, breathtaking or must-have.\n"
        "- Before returning, silently check: valid JSON, all three fields filled, lengths within the limits.\n"
        + (("STORE RULES TO FOLLOW:\n" + str(custom_prompt).strip()[:4000] + "\n") if custom_prompt else "")
        + f"INPUT JSON:\n{json.dumps(payload, ensure_ascii=False)}"
    )

    gemini_client = get_client()
    response = gemini_client.models.generate_content(
        model=model_name or "gemini-3.1-flash-lite",
        contents=[prompt],
        config=_gemini_config(model_name or "gemini-3.1-flash-lite", types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=1400,
            thinking_config=types.ThinkingConfig(thinking_budget=256),
            temperature=0.4,
        )),
    )
    if not response or not response.text:
        raise ValueError("The AI returned nothing for this collection.")
    data = json.loads(response.text) or {}
    description_html = str(data.get("description_html") or "").strip()
    seo_title = _normalise_meta_text(data.get("seo_title") or "")
    seo_description = _normalise_meta_text(data.get("seo_description") or "")
    if not description_html or not seo_title or not seo_description:
        raise ValueError("The AI reply was missing collection description or SEO text.")
    usage = getattr(response, "usage_metadata", None)
    return {
        "description_html": description_html,
        "seo_title": _word_safe_truncate(seo_title, 60),
        "seo_description": _trim_meta_description(seo_description, max_chars=160),
        "ai_usage": {
            "calls": 1,
            "prompt_tokens": int(getattr(usage, "prompt_token_count", 0) or 0),
            "output_tokens": int(getattr(usage, "candidates_token_count", 0) or 0),
            "thinking_tokens": int(getattr(usage, "thoughts_token_count", 0) or 0),
            "total_tokens": int(getattr(usage, "total_token_count", 0) or 0),
        },
    }


TAG_CASE_STYLES = {
    "sentence": "Sentence case: capitalise the first word only, but keep real names, places and "
                "brands capitalised wherever they appear (van gogh -> Van Gogh, "
                "birds of prey -> Birds of prey, JAPANESE ART -> Japanese art).",
    "title": "Title Case: capitalise every word except short joining words such as of, and, the, "
             "in, on, a, an - unless one of those is the first word.",
    "lower": "all lowercase, with no exceptions, including names.",
}


def generate_tag_cleanup_plan(tags, case_style="sentence", merge_mode="synonyms",
                              custom_prompt="", model_name="gemini-3.1-flash-lite"):
    """Propose a tidy-up of a store's product tags.

    `tags` is a list of {"tag": str, "count": int}. Returns a dict with
    `renames` (a list of {"from", "to", "reason"}) and `ai_usage`. Only tags
    whose final spelling differs from the current one come back.

    There is deliberately no fallback. If the model returns nothing usable this
    raises, because a half-finished tag plan applied to a live store is worse
    than no plan at all.
    """
    if not os.environ.get("GEMINI_API_KEY"):
        raise ValueError("GEMINI_API_KEY is not set. Cannot plan a tag cleanup.")

    entries = []
    for item in tags or []:
        name = str((item or {}).get("tag") or "").strip()
        if name:
            entries.append({"tag": name, "used_on": int((item or {}).get("count") or 0)})
    if not entries:
        raise ValueError("No tags were supplied to plan.")

    case_rule = TAG_CASE_STYLES.get(case_style) or TAG_CASE_STYLES["sentence"]
    if merge_mode == "exact":
        merge_rule = (
            "- Merge only tags that are the same words. Ignore capitals, spaces, hyphens and "
            "plural endings when deciding. Do NOT merge tags that use different words, even if "
            "they mean the same thing."
        )
    elif merge_mode == "full":
        merge_rule = (
            "- Merge duplicates, and also merge different wordings that mean the same thing.\n"
            "- You may retire a tag that is noise (a typo used once, a leftover internal code) by "
            "mapping it to the tag it should have been."
        )
    else:
        merge_rule = (
            "- Merge duplicates (same words, different capitals, spaces, hyphens or plurals).\n"
            "- Also merge clearly different wordings of the same thing: \"a black cat\" and "
            "\"cat black\" both become \"Black cat\"; \"vangogh\" and \"van gough\" become \"Van Gogh\".\n"
            "- Do NOT invent a new tag vocabulary and do NOT merge two tags that a shopper would "
            "read as different things (\"cat\" and \"kitten\" stay apart; \"black cat\" and \"cat\" stay apart)."
        )

    prompt = (
        "You are tidying the product tags of a wall-art store. Return JSON only.\n"
        "Format: {\"renames\":[{\"from\":\"exact existing tag\",\"to\":\"new tag\",\"reason\":\"short reason\"}]}\n"
        "Rules:\n"
        f"- Capitalisation: {case_rule}\n"
        f"{merge_rule}\n"
        "- Include an entry for EVERY tag whose final spelling differs from its current spelling, "
        "including changes that are only capitals.\n"
        "- Leave a tag out entirely if it is already correct.\n"
        "- \"from\" must be copied character for character from the input list. Never invent a tag "
        "that is not in the list.\n"
        "- Several tags may point at the same \"to\" value; that is how a merge is expressed.\n"
        "- Keep the meaning. Never change what a tag is about, only how it is written.\n"
        "- Before returning, silently check: valid JSON, every \"from\" appears in the input, "
        "no \"to\" is empty.\n"
        + (("STORE RULES TO FOLLOW:\n" + str(custom_prompt).strip()[:2000] + "\n") if custom_prompt else "")
        + f"TAGS:\n{json.dumps(entries, ensure_ascii=False)}"
    )

    gemini_client = get_client()
    response = gemini_client.models.generate_content(
        model=model_name or "gemini-3.1-flash-lite",
        contents=[prompt],
        config=_gemini_config(model_name or "gemini-3.1-flash-lite", types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=8192,
            thinking_config=types.ThinkingConfig(thinking_budget=512),
            temperature=0.1,
        )),
    )
    if not response or not response.text:
        raise ValueError("The AI returned nothing for this batch of tags.")
    data = json.loads(response.text) or {}
    raw_renames = data.get("renames")
    if raw_renames is None:
        raise ValueError("The AI reply had no renames list.")
    if not isinstance(raw_renames, list):
        raise ValueError("The AI reply had a renames value that was not a list.")

    known = {entry["tag"] for entry in entries}
    renames = []
    for item in raw_renames:
        if not isinstance(item, dict):
            continue
        source = str(item.get("from") or "").strip()
        target = str(item.get("to") or "").strip()
        # A rename of a tag that does not exist would silently do nothing, and a
        # blank target would wipe the tag, so both are refused outright.
        if not source or not target or source not in known or source == target:
            continue
        renames.append({
            "from": source,
            "to": target,
            "reason": str(item.get("reason") or "").strip()[:120],
        })

    usage = getattr(response, "usage_metadata", None)
    return {
        "renames": renames,
        "ai_usage": {
            "calls": 1,
            "prompt_tokens": int(getattr(usage, "prompt_token_count", 0) or 0),
            "output_tokens": int(getattr(usage, "candidates_token_count", 0) or 0),
            "thinking_tokens": int(getattr(usage, "thoughts_token_count", 0) or 0),
            "total_tokens": int(getattr(usage, "total_token_count", 0) or 0),
        },
    }


def generate_collection_groups(tags, minimum_products=8, custom_prompt="",
                               existing_collections=None, model_name="gemini-3.1-flash-lite"):
    """Group loose tags into collections worth giving a page to.

    `tags` is a list of {"tag": str, "count": int}. Returns a list of proposed
    collections, each with a title, the tags that feed it, an estimated product
    count, description_html, seo_title and seo_description.

    Raises rather than falling back. A guessed collection page is worse than no
    page, because it goes live on the storefront.
    """
    if not os.environ.get("GEMINI_API_KEY"):
        raise ValueError("GEMINI_API_KEY is not set. Cannot plan collections.")

    entries = []
    for item in tags or []:
        name = str((item or {}).get("tag") or "").strip()
        if name:
            entries.append({"tag": name, "products": int((item or {}).get("count") or 0)})
    if not entries:
        raise ValueError("No tags were supplied to group.")

    prompt = (
        "You are planning collection pages for a wall-art print store. Return JSON only.\n"
        "Format: {\"collections\":[{\"title\":\"...\",\"tags\":[\"exact tag\",\"exact tag\"],"
        "\"description_html\":\"<p>...</p><p>...</p>\",\"seo_title\":\"...\","
        "\"seo_description\":\"...\",\"reason\":\"short reason\"}]}\n"
        "Rules:\n"
        "- Group tags that describe the same subject into ONE collection. A shopper browsing "
        "\"Cat art\" expects cat prints whether the tag said cat, cats, black cat or kitten.\n"
        f"- Only propose a collection if the tags feeding it add up to at least {int(minimum_products)} "
        "products. Skip anything thinner; a page with three prints on it is not worth having.\n"
        "- Every tag in \"tags\" must be copied character for character from the input list.\n"
        "- A tag may appear in more than one collection only when it genuinely belongs to both "
        "(a tag like \"blue\" can feed both a colour collection and nothing else). Do not spread a "
        "tag across loosely related collections.\n"
        "- title: what a shopper would call the category, 2 to 4 words, no store name.\n"
        "- description_html: 2 short paragraphs in <p> tags, factual, about what is in the "
        "collection and where the prints suit a home. Never invent prices, sizes, delivery times "
        "or awards.\n"
        "- seo_title: at most 60 characters. seo_description: 120 to 155 characters.\n"
        "- No emojis, no exclamation marks, no hype words such as stunning or must-have.\n"
        "- Before returning, silently check: valid JSON, every tag exists in the input, every "
        "collection has all its text filled in.\n"
        + (("STORE RULES TO FOLLOW:\n" + str(custom_prompt).strip()[:2000] + "\n") if custom_prompt else "")
        + (("COLLECTIONS THAT ALREADY EXIST (reuse the same title if you are covering the same "
            "subject):\n" + json.dumps([str(name) for name in existing_collections][:200], ensure_ascii=False) + "\n")
           if existing_collections else "")
        + f"TAGS:\n{json.dumps(entries, ensure_ascii=False)}"
    )

    gemini_client = get_client()
    response = gemini_client.models.generate_content(
        model=model_name or "gemini-3.1-flash-lite",
        contents=[prompt],
        config=_gemini_config(model_name or "gemini-3.1-flash-lite", types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=16384,
            thinking_config=types.ThinkingConfig(thinking_budget=1024),
            temperature=0.3,
        )),
    )
    if not response or not response.text:
        raise ValueError("The AI returned nothing when grouping tags into collections.")
    data = json.loads(response.text) or {}
    raw = data.get("collections")
    if not isinstance(raw, list):
        raise ValueError("The AI reply had no collections list.")

    known = {entry["tag"]: entry["products"] for entry in entries}
    collections = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        tags_in = [str(tag).strip() for tag in item.get("tags") or [] if str(tag).strip()]
        tags_in = [tag for tag in tags_in if tag in known]
        description_html = str(item.get("description_html") or "").strip()
        seo_title = _normalise_meta_text(item.get("seo_title") or "")
        seo_description = _normalise_meta_text(item.get("seo_description") or "")
        if not title or not tags_in or not description_html or not seo_title or not seo_description:
            continue
        collections.append({
            "title": title,
            "tags": tags_in,
            # An upper bound: a product carrying two of these tags is counted once
            # by Shopify but twice here, so the real page is never bigger.
            "estimated_products": sum(known.get(tag, 0) for tag in tags_in),
            "description_html": description_html,
            "seo_title": _word_safe_truncate(seo_title, 60),
            "seo_description": _trim_meta_description(seo_description, max_chars=160),
            "reason": str(item.get("reason") or "").strip()[:160],
        })
    if not collections:
        raise ValueError("The AI did not return a usable collection out of these tags.")

    usage = getattr(response, "usage_metadata", None)
    return {
        "collections": collections,
        "ai_usage": {
            "calls": 1,
            "prompt_tokens": int(getattr(usage, "prompt_token_count", 0) or 0),
            "output_tokens": int(getattr(usage, "candidates_token_count", 0) or 0),
            "total_tokens": int(getattr(usage, "total_token_count", 0) or 0),
        },
    }


def choose_products_for_collection(collection_title, collection_summary, products,
                                   custom_prompt="", model_name="gemini-3.1-flash-lite"):
    """Decide which existing listings belong in a collection.

    `products` is a list of {"handle","title","summary","colour"}. Returns the
    handles that fit. Text only - it reads the listing wording rather than the
    picture, which is what makes it cheap enough to run over a whole catalogue.
    """
    if not os.environ.get("GEMINI_API_KEY"):
        raise ValueError("GEMINI_API_KEY is not set. Cannot sort products into collections.")
    rows = []
    for product in products or []:
        handle = str((product or {}).get("handle") or "").strip()
        if not handle:
            continue
        rows.append({
            "handle": handle,
            "title": str(product.get("title") or "")[:160],
            "about": str(product.get("summary") or "")[:280],
            "colour": str(product.get("colour") or "")[:60],
        })
    if not rows:
        raise ValueError("No products were supplied to sort.")

    prompt = (
        "You are deciding which art prints belong on one collection page. Return JSON only.\n"
        "Format: {\"handles\":[\"exact-handle\",\"exact-handle\"]}\n"
        "Rules:\n"
        f"- The collection is: {collection_title}. {collection_summary}\n"
        "- Include a print only when it clearly belongs. A shopper opening this page should not "
        "be surprised to see it.\n"
        "- Leave out anything marginal. A smaller honest page beats a padded one.\n"
        "- If the collection is about a colour, judge by the colour of the artwork itself, using "
        "the colour field and the wording, not by a colour mentioned in passing.\n"
        "- A print may belong to several collections; judge this one on its own.\n"
        "- Every handle must be copied character for character from the input.\n"
        + (("STORE RULES TO FOLLOW:\n" + str(custom_prompt).strip()[:1500] + "\n") if custom_prompt else "")
        + f"PRINTS:\n{json.dumps(rows, ensure_ascii=False)}"
    )

    gemini_client = get_client()
    response = gemini_client.models.generate_content(
        model=model_name or "gemini-3.1-flash-lite",
        contents=[prompt],
        config=_gemini_config(model_name or "gemini-3.1-flash-lite", types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=8192,
            thinking_config=types.ThinkingConfig(thinking_budget=256),
            temperature=0.1,
        )),
    )
    if not response or not response.text:
        raise ValueError("The AI returned nothing when sorting products into '%s'." % collection_title)
    data = json.loads(response.text) or {}
    raw = data.get("handles")
    if not isinstance(raw, list):
        raise ValueError("The AI reply had no handles list for '%s'." % collection_title)
    known = {row["handle"] for row in rows}
    chosen = [str(handle).strip() for handle in raw if str(handle).strip() in known]

    usage = getattr(response, "usage_metadata", None)
    return {
        "handles": chosen,
        "ai_usage": {
            "calls": 1,
            "prompt_tokens": int(getattr(usage, "prompt_token_count", 0) or 0),
            "output_tokens": int(getattr(usage, "candidates_token_count", 0) or 0),
            "total_tokens": int(getattr(usage, "total_token_count", 0) or 0),
        },
    }
