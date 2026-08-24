import base64
import json
import os
import re
import time

import requests
from google.genai import types

from gemini_utils import create_gemini_prompt, generate_product_metadata, get_client


MODEL_PRICES_PER_MILLION = {
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
    "qwen3-vl-flash": {"input": 0.05, "output": 0.40},
    "qwen/qwen3-vl-8b-instruct": {"input": 0.117, "output": 0.455},
    "qwen/qwen3-vl-32b-instruct": {"input": 0.104, "output": 0.416},
    "qwen/qwen3-vl-235b-a22b-instruct": {"input": 0.20, "output": 0.88},
    "mistralai/mistral-small-3.2-24b-instruct": {"input": 0.075, "output": 0.20},
    "google/gemma-3-27b-it": {"input": 0.08, "output": 0.16},
}

OPENROUTER_BENCHMARK_MODELS = {
    "openrouter-qwen-8b": "qwen/qwen3-vl-8b-instruct",
    "openrouter-qwen-32b": "qwen/qwen3-vl-32b-instruct",
    "openrouter-qwen-235b": "qwen/qwen3-vl-235b-a22b-instruct",
    "openrouter-mistral-24b": "mistralai/mistral-small-3.2-24b-instruct",
    "openrouter-gemma-27b": "google/gemma-3-27b-it",
}

# Use the production prompt builder with clearly synthetic catalogue facts. The
# benchmark therefore measures the complete listing contract without reading
# or changing any live store/profile data.
BENCHMARK_FACTS = """
BENCHMARK-ONLY PRODUCT FACTS:
- REQUIRED DESCRIPTION FACT: supplied as an unframed paper poster || unframed paper print
- The frame and hanging hardware are not included.
- Multiple size variants are available; do not invent their measurements.
- Keep the print dry and handle it with clean, dry hands.
- Packaging and delivery timing vary by destination; do not make delivery promises.
Use those facts for 4 concise store-wide FAQs covering format, framing, sizes, and care.
"""
BENCHMARK_PROMPT = create_gemini_prompt(BENCHMARK_FACTS, None)


def _extract_json(text):
    value = str(text or "").strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        start, end = value.find("{"), value.rfind("}")
        if start >= 0 and end > start:
            return json.loads(value[start:end + 1])
        raise


def score_listing_metadata(metadata):
    """Cheap deterministic QA score; it does not call an AI model."""
    metadata = metadata or {}
    title = str(metadata.get("title") or "").strip()
    description = re.sub(r"<[^>]+>", " ", str(metadata.get("description") or ""))
    description_words = re.findall(r"\b[\w'-]+\b", description)
    tags = metadata.get("tags") or metadata.get("suggested_tags") or []
    if isinstance(tags, str):
        tags = [item.strip() for item in tags.split(",") if item.strip()]
    metafields = metadata.get("metafields") if isinstance(metadata.get("metafields"), dict) else {}
    score = 0
    reasons = []
    if title and description:
        score += 30
    else:
        reasons.append("missing title or description")
    if 28 <= len(title) <= 70:
        score += 15
    else:
        reasons.append("title length outside 28-70")
    if 80 <= len(description_words) <= 350:
        score += 20
    else:
        reasons.append("description depth outside 80-350 words")
    if len(tags) >= 8:
        score += 10
    else:
        reasons.append("fewer than 8 tags")
    if metadata.get("seo_title") and metadata.get("meta_description"):
        score += 10
    else:
        reasons.append("SEO title or description missing")
    grounded = sum(bool(metafields.get(key) or metadata.get(key)) for key in (
        "subject", "palette", "color", "theme", "art_style", "mood", "orientation"
    ))
    score += min(15, grounded * 3)
    if grounded < 3:
        reasons.append("thin image-grounded attributes")
    return {"score": min(100, score), "reasons": reasons}


def assess_production_contract(metadata, allow_evidence_blanks=False):
    """Check every AI-authored field expected by the Shopify listing pipeline."""
    metadata = metadata or {}
    required_fields = (
        "title", "seo_title", "meta_description", "short_description",
        "description", "product_specific_faq", "generic_faq", "alt_text",
        "suggested_tags", "google_product_category", "gender", "age_group",
        "condition", "custom_product", "custom_label_0", "custom_label_1",
        "custom_label_2", "custom_label_3", "custom_label_4",
        "category_attribute_picks",
    )
    required_metafields = (
        "color", "theme", "frame_style", "condition", "decoration_material",
        "artwork_frame_material", "art_movement", "art_style",
        "artwork_authenticity", "material", "orientation", "subject", "room",
        "mood", "palette", "audience", "occasion", "season", "composition",
        "display_suggestion",
    )
    optional_fields = {"custom_label_4"} if allow_evidence_blanks else set()
    missing = [
        field for field in required_fields
        if field not in optional_fields
        and (field not in metadata or metadata.get(field) in (None, "", [], {}))
    ]
    metafields = metadata.get("metafields") if isinstance(metadata.get("metafields"), dict) else {}
    # Some evidence-sensitive merchandising values may correctly be blank; the
    # full contract requires every key to be returned, not fabricated content.
    optional_metafields = {"audience", "season"} if allow_evidence_blanks else set()
    missing_metafields = [
        field for field in required_metafields
        if field not in optional_metafields and field not in metafields
    ]
    violations = []
    description = str(metadata.get("description") or "")
    description_words = re.findall(r"\b[\w'-]+\b", re.sub(r"<[^>]+>", " ", description))
    if len(re.findall(r"<p\b", description, flags=re.I)) != 3:
        violations.append("description must contain exactly 3 HTML paragraphs")
    if not 120 <= len(description_words) <= 190:
        violations.append("description must contain 120-190 words")
    short_words = re.findall(r"\b[\w'-]+\b", str(metadata.get("short_description") or ""))
    if not 50 <= len(short_words) <= 80:
        violations.append("short description must contain 50-80 words")
    if not 140 <= len(str(metadata.get("meta_description") or "")) <= 160:
        violations.append("meta description must contain 140-160 characters")
    if len(str(metadata.get("alt_text") or "")) > 125:
        violations.append("alt text exceeds 125 characters")
    tags = metadata.get("suggested_tags") or metadata.get("tags") or []
    if not isinstance(tags, list) or not 16 <= len(tags) <= 24:
        violations.append("suggested tags must contain 16-24 values")
    product_faq = metadata.get("product_specific_faq") or []
    generic_faq = metadata.get("generic_faq") or []
    if not isinstance(product_faq, list) or len(product_faq) < 3:
        violations.append("fewer than 3 product-specific FAQs")
    if not isinstance(generic_faq, list) or len(generic_faq) < 4:
        violations.append("fewer than 4 store-wide FAQs")
    deduction = min(100, len(missing) * 3 + len(missing_metafields) * 2 + len(violations) * 5)
    return {
        "score": 100 - deduction,
        "complete": not missing and not missing_metafields and not violations,
        "missing_fields": missing,
        "missing_metafields": missing_metafields,
        "violations": violations,
        "description_words": len(description_words),
        "short_description_words": len(short_words),
        "tag_count": len(tags) if isinstance(tags, list) else 0,
        "product_faq_count": len(product_faq) if isinstance(product_faq, list) else 0,
        "generic_faq_count": len(generic_faq) if isinstance(generic_faq, list) else 0,
    }


def _estimated_cost(model, usage):
    rates = MODEL_PRICES_PER_MILLION.get(model) or {"input": 0, "output": 0}
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    output_tokens += int(usage.get("thinking_tokens") or 0)
    return round((input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000, 8)


def run_gemini_benchmark(image_path):
    started = time.monotonic()
    with open(image_path, "rb") as image_file:
        image_bytes = image_file.read()
    mime = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
    response = get_client().models.generate_content(
        model="gemini-3.1-flash-lite",
        contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime), BENCHMARK_PROMPT],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=4096,
            thinking_config=types.ThinkingConfig(thinking_budget=512),
            temperature=0.7,
        ),
    )
    metadata = _extract_json(response.text)
    usage_raw = getattr(response, "usage_metadata", None)
    usage = {
        "model": "gemini-3.1-flash-lite",
        "prompt_tokens": int(getattr(usage_raw, "prompt_token_count", 0) or 0),
        "output_tokens": int(getattr(usage_raw, "candidates_token_count", 0) or 0),
        "thinking_tokens": int(getattr(usage_raw, "thoughts_token_count", 0) or 0),
        "total_tokens": int(getattr(usage_raw, "total_token_count", 0) or 0),
        "calls": 1,
    }
    metadata["_ai_usage"] = usage
    return {
        "provider": "gemini",
        "model": "gemini-3.1-flash-lite",
        "seconds": round(time.monotonic() - started, 2),
        "usage": usage,
        "estimated_cost_usd": _estimated_cost("gemini-3.1-flash-lite", usage),
        "qa": score_listing_metadata(metadata),
        "production_contract": assess_production_contract(metadata),
        "metadata": metadata,
    }


def run_gemini_production_benchmark(image_path):
    """Exercise the real production generator and normalisation pipeline."""
    started = time.monotonic()
    metadata = generate_product_metadata(
        image_path,
        custom_prompt=BENCHMARK_FACTS,
        available_collections=None,
        category_attribute_options=None,
        model_name="gemini-3.1-flash-lite",
    )
    if not metadata:
        raise RuntimeError("Production Gemini metadata generation returned no data")
    usage = metadata.get("_ai_usage") or {}
    return {
        "provider": "gemini-production",
        "model": usage.get("model") or "gemini-3.1-flash-lite",
        "seconds": round(time.monotonic() - started, 2),
        "usage": usage,
        "estimated_cost_usd": _estimated_cost("gemini-3.1-flash-lite", usage),
        "qa": score_listing_metadata(metadata),
        "production_contract": assess_production_contract(metadata, allow_evidence_blanks=True),
        "metadata": metadata,
    }


def run_qwen_benchmark(image_path):
    api_key = (os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is not configured")
    endpoint = os.environ.get(
        "QWEN_API_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"
    ).strip()
    model = os.environ.get("QWEN_VISION_MODEL", "qwen3-vl-flash").strip()
    with open(image_path, "rb") as image_file:
        image_bytes = image_file.read()
    mime = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
    started = time.monotonic()
    response = requests.post(
        endpoint,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"}},
                    {"type": "text", "text": BENCHMARK_PROMPT},
                ],
            }],
            "response_format": {"type": "json_object"},
            "temperature": 0.7,
            "max_tokens": 4096,
            "enable_thinking": False,
        },
        timeout=90,
    )
    response.raise_for_status()
    payload = response.json()
    metadata = _extract_json(payload["choices"][0]["message"]["content"])
    usage_raw = payload.get("usage") or {}
    usage = {
        "model": model,
        "prompt_tokens": int(usage_raw.get("prompt_tokens") or 0),
        "output_tokens": int(usage_raw.get("completion_tokens") or 0),
        "total_tokens": int(usage_raw.get("total_tokens") or 0),
        "calls": 1,
    }
    metadata["_ai_usage"] = usage
    return {
        "provider": "qwen",
        "model": model,
        "seconds": round(time.monotonic() - started, 2),
        "usage": usage,
        "estimated_cost_usd": _estimated_cost("qwen3-vl-flash", usage),
        "qa": score_listing_metadata(metadata),
        "production_contract": assess_production_contract(metadata),
        "metadata": metadata,
    }


def run_openrouter_benchmark(image_path, provider_name):
    api_key = (
        os.environ.get("OPEN_ROUTER_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
        or ""
    ).strip()
    if not api_key:
        raise RuntimeError("OPEN_ROUTER_API_KEY is not configured")
    model = OPENROUTER_BENCHMARK_MODELS[provider_name]
    endpoint = os.environ.get(
        "OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions"
    ).strip()
    with open(image_path, "rb") as image_file:
        image_bytes = image_file.read()
    mime = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
    started = time.monotonic()
    response = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.environ.get("APP_URL", "https://listing-cannon.onrender.com"),
            "X-Title": "Listing Cannon read-only model benchmark",
        },
        json={
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
                        },
                    },
                    {"type": "text", "text": BENCHMARK_PROMPT},
                ],
            }],
            "response_format": {"type": "json_object"},
            "temperature": 0.7,
            "max_tokens": 4096,
            "provider": {"sort": "price", "allow_fallbacks": True},
            "usage": {"include": True},
        },
        timeout=180,
    )
    if not response.ok:
        try:
            message = ((response.json().get("error") or {}).get("message") or response.text)
        except Exception:
            message = response.text
        raise RuntimeError(f"OpenRouter HTTP {response.status_code}: {str(message)[:500]}")
    payload = response.json()
    metadata = _extract_json(payload["choices"][0]["message"]["content"])
    usage_raw = payload.get("usage") or {}
    usage = {
        "model": model,
        "prompt_tokens": int(usage_raw.get("prompt_tokens") or 0),
        "output_tokens": int(usage_raw.get("completion_tokens") or 0),
        "total_tokens": int(usage_raw.get("total_tokens") or 0),
        "calls": 1,
    }
    metadata["_ai_usage"] = usage
    reported_cost = usage_raw.get("cost")
    estimated_cost = (
        round(float(reported_cost), 8)
        if reported_cost not in (None, "")
        else _estimated_cost(model, usage)
    )
    return {
        "provider": "openrouter",
        "benchmark_name": provider_name,
        "model": payload.get("model") or model,
        "served_by": payload.get("provider"),
        "seconds": round(time.monotonic() - started, 2),
        "usage": usage,
        "estimated_cost_usd": estimated_cost,
        "qa": score_listing_metadata(metadata),
        "production_contract": assess_production_contract(metadata),
        "metadata": metadata,
    }


def benchmark_provider(provider, image_path):
    if provider == "gemini":
        return run_gemini_benchmark(image_path)
    if provider == "gemini-production":
        return run_gemini_production_benchmark(image_path)
    if provider == "qwen":
        return run_qwen_benchmark(image_path)
    if provider in OPENROUTER_BENCHMARK_MODELS:
        return run_openrouter_benchmark(image_path, provider)
    raise ValueError(f"Unsupported benchmark provider: {provider}")
