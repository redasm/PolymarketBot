from dotenv import load_dotenv
import os

load_dotenv(".env")

signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "2"))
funder = os.getenv("POLYMARKET_FUNDER") or os.getenv("POLYMARKET_DEPOSIT_WALLET")

if signature_type == 3:
    from py_clob_client_v2 import ClobClient, SignatureTypeV2

    resolved_signature_type = SignatureTypeV2.POLY_1271
else:
    from py_clob_client.client import ClobClient

    resolved_signature_type = signature_type

client = ClobClient(
    os.getenv("CLOB_HOST", "https://clob.polymarket.com"),
    key=os.getenv("PRIVATE_KEY"),
    chain_id=int(os.getenv("CHAIN_ID", "137")),
    signature_type=resolved_signature_type,
    funder=funder,
)

if hasattr(client, "create_or_derive_api_key"):
    creds = client.create_or_derive_api_key()
elif hasattr(client, "create_or_derive_api_creds"):
    creds = client.create_or_derive_api_creds()
else:
    creds = client.create_api_key()
print(creds)
