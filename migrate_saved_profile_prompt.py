#!/usr/bin/env python3
"""One-time migration of saved Ready Images prompts to the Samila store profile.

The application-level prompt remains product-agnostic. This migration writes
store and production facts into the user-editable saved profile, without
changing any variants, prices, publishing settings, or other profile fields.
"""
import json
import os
import sys
from urllib.parse import urlparse

from gemini_utils import get_prompt_sections


PROFILE_MARKER = "PROFILE RULES VERSION: 7"

# Machine-read directive lines every saved profile must carry. When a profile
# is on an older rules version these are added to it in place; nothing else in
# the saved prompt is touched, so edits made in the app survive the upgrade.
REQUIRED_PROFILE_DIRECTIVES = [
    "ALWAYS INCLUDE COLLECTION: View All Posters",
]

PROFILE_ADDITIONS = {
    "intro": """

STORE PROFILE FACTS (Samila Home paper prints):
- PROFILE RULES VERSION: 7
- This saved profile creates Samila Home paper poster and wall-art listings.
- ALWAYS INCLUDE COLLECTION: View All Posters
- Beyond that catch-all, also choose at least one subject collection, one art-style collection and one colour collection. Judge colour on the dominant colour only. More than one from a category is fine when the fit is obvious.
- Use British English in customer-facing copy, including colour, colourful, centre and personalised. Keep platform-required taxonomy values unchanged.
- Prints are supplied unframed and the frame is not included.
- Each print is printed to order in the UK on 270 gsm premium satin paper using a 12-colour Mimaki UCJV300-160 printer and authentic Mimaki OEM inks.
- Each print is rolled and packaged by hand.
- Available sizes and variant values are supplied separately by the product creator. Copy those values exactly and never invent dimensions.
- Keep the visible subject, composition, typography, palette and style central to the copy. Persuasive wording is allowed, but every claim must be specific and supportable.
""".rstrip(),
    "product_title": """

STORE PROFILE TITLE GUIDANCE:
- Name the visible subject and product format naturally. Use one clear product-type phrase rather than stacking synonyms.
- Avoid pipe-separated keyword lists. Include only attributes supported by the image or configured product data.
""".rstrip(),
    "meta_description": """

STORE PROFILE META GUIDANCE:
- Describe the exact visible subject and distinguishing design details, then identify it naturally as an unframed paper print where space permits.
- Use British English and keep the complete sentence within the required character range.
""".rstrip(),
    "product_description": """

STORE PROFILE DESCRIPTION FACTS:
- In paragraph 3, state naturally that the print is supplied unframed, printed to order in the UK on 270 gsm premium satin paper, and available in the configured sizes.
- Include the required printer and ink facts naturally once. Do not repeat production facts merely to add words.
- Do not invent border treatment, dispatch time, colourfastness, archival life or dimensions.
- REQUIRED DESCRIPTION FACT: unframed || frame is not included || frame not included
- REQUIRED DESCRIPTION FACT: 270 gsm premium satin paper
- REQUIRED DESCRIPTION FACT: printed to order in the UK || produced to order in the UK
- REQUIRED DESCRIPTION FACT: 12-colour Mimaki UCJV300-160
- REQUIRED DESCRIPTION FACT: authentic Mimaki OEM inks || Mimaki OEM inks
- REQUIRED DESCRIPTION FACT: rolled and packaged by hand
- REQUIRED DESCRIPTION FACT: available in multiple sizes || available in the configured sizes || Size selector
""".rstrip(),
    "short_description": """

STORE PROFILE SHORT-DESCRIPTION GUIDANCE:
- Lead with the exact visible subject and design. Identify the item as an unframed paper print, but leave detailed production specifications for the full description and FAQ.
""".rstrip(),
    "product_specific_faq": """

STORE PROFILE PRODUCT-SPECIFIC FAQ GUIDANCE:
- REQUIRE PRODUCT-SPECIFIC FAQ: yes
- Ask 3-4 questions that help a shopper understand this exact design, selecting only relevant visible topics such as its subject, colours, style, composition, orientation, objects, or clearly legible wording.
- Keep these questions specific to the artwork but keep the rules universal across every subject. Do not repeat production, framing, paper, packaging, or size questions here.
""".rstrip(),
    "faq": """

STORE-WIDE FAQ QUESTIONS AND VERIFIED FACTS:
- REQUIRE STORE-WIDE FAQ: yes
- Questions to answer: Is the print supplied with a frame? What paper is used? Where is the print produced? How is it printed? How is it packaged? What sizes are available?
- Verified answers: frame not included; supplied unframed; 270 gsm premium satin paper; printed to order in the UK with a 12-colour Mimaki UCJV300-160 printer and authentic Mimaki OEM inks; rolled and packaged by hand; available choices are those shown in the Size selector.
- Answer only with the verified facts above. Do not add claims such as premium feel, exceptional reproduction, protection in transit, perfect condition, or similar embellishment.
- Do not invent dispatch, delivery, returns, border, care or durability claims.
""".rstrip(),
    "alt_text": """

STORE PROFILE ALT-TEXT GUIDANCE:
- Describe only the artwork image, not production specifications. Use British English and avoid promotional adjectives.
""".rstrip(),
}


def build_profile_prompt():
    sections = get_prompt_sections()
    saved = []
    prompt_parts = []
    for section_id, section in sections.items():
        if section.get("locked"):
            continue
        content = section.get("content", "")
        addition = PROFILE_ADDITIONS.get(section_id)
        if addition:
            content = content.rstrip() + addition
        saved.append({"id": section_id, "content": content})
        if content.strip():
            prompt_parts.append(content.strip())
    return "\n\n".join(prompt_parts), json.dumps(saved, ensure_ascii=False)


def update_profiles(conn, placeholder):
    prompt, sections = build_profile_prompt()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, profile_name, COALESCE(custom_prompt, '') FROM user_profiles "
        "WHERE COALESCE(profile_type, 'ready') = 'ready'"
    )
    rows = cur.fetchall()
    updated = []
    for profile_id, profile_name, current_prompt in rows:
        if PROFILE_MARKER in current_prompt:
            continue
        cur.execute(
            f"UPDATE user_profiles SET custom_prompt = {placeholder}, custom_prompt_sections = {placeholder} WHERE id = {placeholder}",
            (prompt, sections, profile_id),
        )
        updated.append(profile_name)
    conn.commit()
    return updated


def main():
    database_url = os.environ.get("DATABASE_URL", "sqlite:///instance/app.db")
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://"):]

    if database_url.startswith("postgresql://"):
        import psycopg2

        parsed = urlparse(database_url)
        conn = psycopg2.connect(
            host=parsed.hostname,
            port=parsed.port or 5432,
            dbname=(parsed.path or "/").lstrip("/").split("?")[0] or "app",
            user=parsed.username,
            password=parsed.password,
        )
        placeholder = "%s"
    else:
        import sqlite3

        db_path = database_url.replace("sqlite:///", "", 1)
        if not os.path.isfile(db_path):
            print(f"DB not found: {db_path}", file=sys.stderr)
            return 1
        conn = sqlite3.connect(db_path)
        placeholder = "?"

    try:
        updated = update_profiles(conn, placeholder)
    finally:
        conn.close()

    if updated:
        print("Updated Ready Images profile prompts:", ", ".join(updated))
    else:
        print("No Ready Images profile prompts required updating.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
