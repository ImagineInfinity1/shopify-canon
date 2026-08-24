import unittest

from ai_provider_benchmark import _estimated_cost, assess_production_contract


class AiProviderBenchmarkTests(unittest.TestCase):
    def _complete_metadata(self):
        paragraph = " ".join(["specific"] * 40)
        metafield_keys = (
            "color", "theme", "frame_style", "condition", "decoration_material",
            "artwork_frame_material", "art_movement", "art_style",
            "artwork_authenticity", "material", "orientation", "subject", "room",
            "mood", "palette", "audience", "occasion", "season", "composition",
            "display_suggestion",
        )
        return {
            "title": "Specific Botanical Wall Art | Red Geranium Paper Poster",
            "seo_title": "Red Geranium Botanical Wall Art | Paper Poster",
            "meta_description": "M" * 145,
            "short_description": " ".join(["specific"] * 55),
            "description": "".join(f"<p>{paragraph}</p>" for _ in range(3)),
            "product_specific_faq": [{"question": "Q?", "answer": "A"}] * 3,
            "generic_faq": [{"question": "Q?", "answer": "A"}] * 4,
            "alt_text": "Red geranium botanical paper poster on a cream background",
            "suggested_tags": [f"tag {index}" for index in range(16)],
            "google_product_category": "Home & Garden > Decor > Artwork",
            "gender": "Unisex",
            "age_group": "Adult",
            "condition": "New",
            "custom_product": True,
            "custom_label_0": "Botanical",
            "custom_label_1": "Wall art",
            "custom_label_2": "Red and cream",
            "custom_label_3": "Geranium",
            "custom_label_4": "Paper poster",
            "category_attribute_picks": {"Color": ["Red"]},
            "metafields": {key: "" for key in metafield_keys},
        }

    def test_complete_production_contract_covers_every_listing_field(self):
        result = assess_production_contract(self._complete_metadata())
        self.assertTrue(result["complete"])
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["description_words"], 120)
        self.assertEqual(result["tag_count"], 16)

    def test_contract_reports_missing_top_level_and_metafield_values(self):
        metadata = self._complete_metadata()
        del metadata["seo_title"]
        del metadata["metafields"]["palette"]
        result = assess_production_contract(metadata)
        self.assertFalse(result["complete"])
        self.assertIn("seo_title", result["missing_fields"])
        self.assertIn("palette", result["missing_metafields"])

    def test_production_contract_allows_evidence_sensitive_blanks(self):
        metadata = self._complete_metadata()
        metadata["custom_label_4"] = ""
        del metadata["metafields"]["audience"]
        del metadata["metafields"]["season"]
        result = assess_production_contract(metadata, allow_evidence_blanks=True)
        self.assertTrue(result["complete"])
        self.assertEqual(result["score"], 100)

    def test_gemini_cost_includes_thinking_tokens(self):
        cost = _estimated_cost("gemini-3.1-flash-lite", {
            "prompt_tokens": 1_000_000,
            "output_tokens": 500_000,
            "thinking_tokens": 500_000,
        })
        self.assertEqual(cost, 1.75)


if __name__ == "__main__":
    unittest.main()
