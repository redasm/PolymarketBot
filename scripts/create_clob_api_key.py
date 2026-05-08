from dotenv import load_dotenv
import os

load_dotenv(".env")

from py_clob_client.client import ClobClient

client = ClobClient(
    "https://clob.polymarket.com",
    key=os.getenv("PRIVATE_KEY"),
    chain_id=137,
    signature_type=2,
    funder=os.getenv("POLYMARKET_FUNDER"),
)

creds = client.create_api_key()
print(creds)