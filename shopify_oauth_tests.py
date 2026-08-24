import hashlib
import hmac
import os
import time
import unittest
from unittest.mock import patch

from werkzeug.datastructures import MultiDict

from shopify_oauth import _normalise_oauth_params, _verify_hmac


def signed_params(params, secret):
    encoded = []
    for key, value in params.items():
        if key.endswith("[]"):
            key = key[:-2]
            value = '["' + '", "'.join(value) + '"]'
        key = str(key).replace("%", "%25").replace("=", "%3D")
        value = str(value).replace("%", "%25")
        encoded.append(f"{key}={value}".replace("&", "%26"))
    message = "&".join(sorted(encoded))
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


class ShopifyOAuthTests(unittest.TestCase):
    def test_official_callback_shape(self):
        secret = "oauth-secret"
        params = {
            "code": "0907a61c0c8d55e99db179b68161bc00",
            "host": "c2hvcGlmeS5jb20vYWRtaW4=",
            "shop": "example.myshopify.com",
            "state": "nonce-value",
            "timestamp": str(int(time.time())),
        }
        params["hmac"] = signed_params(params, secret)
        with patch.dict(os.environ, {"SHOPIFY_API_SECRET": f"  {secret}\n"}):
            self.assertTrue(_verify_hmac(params))

    def test_repeated_array_values_are_not_flattened(self):
        secret = "oauth-secret"
        timestamp = str(int(time.time()))
        plain = {"ids[]": ["1", "2"], "shop": "example.myshopify.com", "timestamp": timestamp}
        signature = signed_params(plain, secret)
        params = MultiDict([
            ("ids[]", "1"),
            ("ids[]", "2"),
            ("shop", "example.myshopify.com"),
            ("timestamp", timestamp),
            ("hmac", signature),
        ])
        self.assertEqual(_normalise_oauth_params(params)["ids[]"], ["1", "2"])
        with patch.dict(os.environ, {"SHOPIFY_API_SECRET": secret}):
            self.assertTrue(_verify_hmac(params))

    def test_tampered_callback_is_rejected(self):
        secret = "oauth-secret"
        params = {
            "shop": "example.myshopify.com",
            "timestamp": str(int(time.time())),
        }
        params["hmac"] = signed_params(params, secret)
        params["shop"] = "attacker.myshopify.com"
        with patch.dict(os.environ, {"SHOPIFY_API_SECRET": secret}):
            self.assertFalse(_verify_hmac(params))


if __name__ == "__main__":
    unittest.main()
