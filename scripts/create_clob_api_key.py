"""Create or derive Polymarket CLOB L2 API credentials.

This is an L1 auth step: sign with PRIVATE_KEY to obtain reusable API creds.
For deposit-wallet trading, `signature_type=3` and `funder=<deposit wallet>`
are applied later when constructing the trading client, not while deriving
the API key itself.
"""

from dotenv import load_dotenv
import os

load_dotenv(".env")

try:
    from py_clob_client_v2 import ClobClient
except ImportError:
    from py_clob_client.client import ClobClient

client = ClobClient(
    os.getenv("CLOB_HOST", "https://clob.polymarket.com"),
    key=os.getenv("PRIVATE_KEY"),
    chain_id=int(os.getenv("CHAIN_ID", "137")),
)

if hasattr(client, "create_or_derive_api_key"):
    creds = client.create_or_derive_api_key()
elif hasattr(client, "create_or_derive_api_creds"):
    creds = client.create_or_derive_api_creds()
else:
    creds = client.create_api_key()

api_key = getattr(creds, "api_key", None) or creds.get("api_key") or creds.get("apiKey")
api_secret = getattr(creds, "api_secret", None) or creds.get("api_secret") or creds.get("secret")
api_passphrase = (
    getattr(creds, "api_passphrase", None)
    or creds.get("api_passphrase")
    or creds.get("passphrase")
)

print("CLOB_API_KEY=" + str(api_key or ""))
print("CLOB_SECRET=" + str(api_secret or ""))
print("CLOB_PASS_PHRASE=" + str(api_passphrase or ""))
