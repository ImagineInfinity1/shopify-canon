"""Shopify webhooks: keep the cached catalogue fresh without polling.

Before this, the app only learned about a change when someone pressed "Update
changed products". Between presses the cached catalogue could be hours or days
out of date, so the bulk editor could show a stale title or a price that had
already been changed in Shopify.

Shopify sends a small JSON POST the moment a product or collection changes.
Only two things matter from that POST: which shop it came from, and which
object changed. The app then re-reads that one object from the API, so what is
cached is always what Shopify actually holds - the webhook body itself is never
trusted as the new state.
"""
import base64
import hashlib
import hmac
import logging
import os

logger = logging.getLogger(__name__)

# Topics worth subscribing to. Product and collection changes are the only ones
# that can make the cached catalogue wrong.
WEBHOOK_TOPICS = [
    'PRODUCTS_CREATE',
    'PRODUCTS_UPDATE',
    'PRODUCTS_DELETE',
    'COLLECTIONS_CREATE',
    'COLLECTIONS_UPDATE',
    'COLLECTIONS_DELETE',
]

# Header topic ("products/update") to the GraphQL enum ("PRODUCTS_UPDATE").
def topic_to_enum(header_topic):
    return str(header_topic or '').strip().upper().replace('/', '_')


def _api_secret():
    return os.environ.get('SHOPIFY_API_SECRET', '').strip()


def verify_webhook(raw_body, hmac_header, secret=None):
    """Confirm a webhook really came from Shopify.

    Anyone can POST to a public URL, so an unverified body must never be acted
    on. Returns False when the secret is missing rather than assuming trust.
    """
    secret = secret if secret is not None else _api_secret()
    if not secret or not hmac_header or raw_body is None:
        return False
    digest = base64.b64encode(
        hmac.new(secret.encode('utf-8'), raw_body, hashlib.sha256).digest()
    ).decode('utf-8')
    return hmac.compare_digest(digest, str(hmac_header))


def callback_url_for(app_url, topic_enum=None):
    """The public URL Shopify posts to. One endpoint handles every topic."""
    return '%s/webhooks/shopify' % (app_url or '').rstrip('/')


_LIST_QUERY = """
query listWebhooks($first: Int!, $after: String) {
  webhookSubscriptions(first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      topic
      endpoint { __typename ... on WebhookHttpEndpoint { callbackUrl } }
    }
  }
}
"""

_CREATE_MUTATION = """
mutation createWebhook($topic: WebhookSubscriptionTopic!, $subscription: WebhookSubscriptionInput!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $subscription) {
    webhookSubscription { id topic }
    userErrors { field message }
  }
}
"""

_DELETE_MUTATION = """
mutation deleteWebhook($id: ID!) {
  webhookSubscriptionDelete(id: $id) {
    deletedWebhookSubscriptionId
    userErrors { field message }
  }
}
"""


def list_webhooks(shop_domain=None, access_token=None):
    """Every webhook subscription this app currently has on the store."""
    from shopify_graphql import execute_graphql_query

    subscriptions = []
    after = None
    while True:
        result = execute_graphql_query(
            _LIST_QUERY, {'first': 100, 'after': after},
            shop_domain=shop_domain, access_token=access_token,
        )
        connection = (((result or {}).get('data') or {}).get('webhookSubscriptions') or {})
        for node in connection.get('nodes') or []:
            endpoint = node.get('endpoint') or {}
            subscriptions.append({
                'id': node.get('id'),
                'topic': node.get('topic'),
                'callback_url': endpoint.get('callbackUrl') or '',
            })
        page_info = connection.get('pageInfo') or {}
        if not page_info.get('hasNextPage'):
            break
        after = page_info.get('endCursor')
    return subscriptions


def ensure_webhooks(callback_url, shop_domain=None, access_token=None, topics=None):
    """Subscribe to every topic that is not already pointing at this app.

    Existing subscriptions on a *different* URL for the same topic are removed
    first: leaving an old Render URL subscribed means Shopify keeps retrying a
    dead endpoint and eventually turns the subscription off.
    """
    from shopify_graphql import execute_graphql_query

    topics = list(topics or WEBHOOK_TOPICS)
    existing = list_webhooks(shop_domain=shop_domain, access_token=access_token)
    by_topic = {}
    for subscription in existing:
        by_topic.setdefault(subscription['topic'], []).append(subscription)

    created, kept, removed, failures = [], [], [], []
    for topic in topics:
        current = by_topic.get(topic) or []
        if any(item['callback_url'] == callback_url for item in current):
            kept.append(topic)
        else:
            result = execute_graphql_query(
                _CREATE_MUTATION,
                {'topic': topic, 'subscription': {'callbackUrl': callback_url, 'format': 'JSON'}},
                shop_domain=shop_domain, access_token=access_token,
            )
            payload = (((result or {}).get('data') or {}).get('webhookSubscriptionCreate') or {})
            errors = payload.get('userErrors') or []
            if errors or not payload.get('webhookSubscription'):
                message = '; '.join(str(e.get('message') or e) for e in errors) or 'unknown error'
                failures.append({'topic': topic, 'error': message})
                logger.warning('Could not subscribe to %s: %s', topic, message)
                continue
            created.append(topic)

        for stale in current:
            if stale['callback_url'] == callback_url:
                continue
            execute_graphql_query(
                _DELETE_MUTATION, {'id': stale['id']},
                shop_domain=shop_domain, access_token=access_token,
            )
            removed.append({'topic': topic, 'callback_url': stale['callback_url']})

    return {
        'success': not failures,
        'callback_url': callback_url,
        'created': created,
        'already_active': kept,
        'removed_stale': removed,
        'failures': failures,
    }
